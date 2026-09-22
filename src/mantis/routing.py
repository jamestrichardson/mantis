"""Deterministic, server-side model-call routing/fallback (#16).

This is model-*call* routing, not whole-agent replay: a "logical model
call" is one model step in ``AgentRuntime``'s existing conversation loop
(one iteration). When that single step's HTTP call to LiteLLM fails for
an *explicitly eligible* reason, ``AgentRuntime`` may retry the exact
same conversation/tool-schema state against the next configured
fallback alias, still within the same iteration — never restarting the
investigation, never replaying an already-executed tool call, and never
resetting the run's iteration count, tool-call budget, deadline, or
run-local breaker state. See ``mantis.config.ModelRoutingPolicy`` for the
policy shape and ``docs/model-routing.md`` for the full design.

Two small, purely data/classification pieces live here (mirroring
``mantis.reliability``'s split of "taxonomy + classification" from
"policy" — that lives in ``mantis.config``):

- :class:`ModelCallFailureKind` / :func:`classify_model_call_exception` —
  a small, stable vocabulary for *why* one model-call attempt failed,
  classified from the real ``openai`` SDK exception type only — never
  by parsing its message text, and never surfacing the raw
  provider/LiteLLM error body as classification input.
- :class:`ModelCallAttempt` — one bounded, safe-to-log/safe-to-persist
  record of a single attempt (which alias, which attempt number,
  outcome, classified failure kind, latency, tokens) — the building
  block for #16's attempt-history observability/eval-integration
  requirements. Never carries a raw exception message or provider error
  body; see :func:`safe_model_call_detail`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ModelCallFailureKind(str, Enum):
    """The stable Mantis model-call failure taxonomy (#16). Deliberately
    small — enough distinction to drive correct fallback-eligibility
    decisions and give an operator a useful, low-cardinality label,
    never a general HTTP-status taxonomy or a copy of the raw
    provider/OpenAI exception hierarchy."""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMIT = "rate_limit"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    BAD_REQUEST = "bad_request"
    SERVER_ERROR = "server_error"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


DEFAULT_ELIGIBLE_FAILURE_KINDS: frozenset[ModelCallFailureKind] = frozenset(
    {
        ModelCallFailureKind.TIMEOUT,
        ModelCallFailureKind.CONNECTION,
        ModelCallFailureKind.RATE_LIMIT,
        ModelCallFailureKind.SERVER_ERROR,
    }
)
"""By default, fallback is only permitted for failures that plausibly
differ by route/model availability -- a different configured alias
might genuinely not be rate-limited, timing out, or down even when the
primary is. Every other kind (below) must NOT silently fall back by
default:

- ``authentication``/``authorization`` — a credential/permission
  problem, which very likely applies identically to every route behind
  the same LiteLLM gateway; retrying a different model alias doesn't
  fix a bad API key.
- ``bad_request`` — the *request itself* was malformed (by Mantis, by
  the model's own tool-call arguments, or by an unsupported tool
  schema) — a different model alias would receive the exact same
  malformed request and likely fail the same way, or fail
  misleadingly differently.
- ``invalid_response`` — the backend returned something Mantis's
  runtime cannot structurally interpret; this could be a Mantis-side
  bug in reading the response just as easily as a route-specific
  quirk, so it fails closed by default rather than being assumed safe
  to retry elsewhere.
- ``unknown`` — an unclassified failure fails closed by design (see
  this module's own docstring and the issue's "Unknown failures fail
  closed unless explicitly and safely classified" requirement) —
  never silently treated as eligible just because it wasn't
  recognized.

A caller may pass a different ``eligible_failure_kinds`` set to
``mantis.config.ModelRoutingPolicy`` to change this per agent, but the
default here is deliberately conservative.
"""


def classify_model_call_exception(exc: Exception) -> ModelCallFailureKind:
    """Classify an exception raised by the OpenAI-SDK client's
    ``chat.completions.create()`` call into :class:`ModelCallFailureKind`,
    purely from the exception's *type* (and, where the SDK exposes it,
    its HTTP status code) — never by parsing ``str(exc)``, which may
    contain an arbitrary, potentially large provider/LiteLLM/upstream
    error body (a real example encountered during #13 qualification: an
    nginx 504 Gateway Time-out HTML page as the exception's message).
    """
    # Local import: keeps `import mantis.routing` cheap for any caller
    # that only wants the taxonomy/dataclasses, and avoids importing the
    # `openai` package at module scope purely for isinstance checks.
    import openai

    # openai.APITimeoutError subclasses APIConnectionError -- must be
    # checked first, or every timeout would be misclassified as a
    # generic connection failure.
    if isinstance(exc, openai.APITimeoutError):
        return ModelCallFailureKind.TIMEOUT
    if isinstance(exc, openai.APIConnectionError):
        return ModelCallFailureKind.CONNECTION
    if isinstance(exc, openai.RateLimitError):
        return ModelCallFailureKind.RATE_LIMIT
    if isinstance(exc, openai.AuthenticationError):
        return ModelCallFailureKind.AUTHENTICATION
    if isinstance(exc, openai.PermissionDeniedError):
        return ModelCallFailureKind.AUTHORIZATION
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError, openai.NotFoundError)):
        return ModelCallFailureKind.BAD_REQUEST
    if isinstance(exc, openai.InternalServerError):
        return ModelCallFailureKind.SERVER_ERROR
    if isinstance(exc, openai.APIResponseValidationError):
        return ModelCallFailureKind.INVALID_RESPONSE
    return ModelCallFailureKind.UNKNOWN


def safe_model_call_detail(exc: Exception) -> str:
    """A bounded, safe-to-log/safe-to-persist description of a model-call
    failure — the exception's class name, plus its HTTP status code when
    the SDK exposes one (every ``openai.APIStatusError`` subclass does).
    Deliberately never includes ``str(exc)`` — the provider/LiteLLM error
    body it carries is exactly the "raw provider error body" #16
    prohibits from becoming API-safe error metadata or a log/metric
    value (it can be arbitrarily large or contain upstream infrastructure
    detail, e.g. an nginx error page)."""
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        return f"{type(exc).__name__} (status={status_code})"
    return type(exc).__name__


@dataclass(frozen=True)
class ModelCallAttempt:
    """One bounded, safe-to-log/safe-to-persist record of a single
    model-call attempt within one logical model call (one
    ``AgentRuntime`` iteration) — the building block for #16's
    attempt-history observability and eval-integration requirements.

    Never carries a raw provider/OpenAI/LiteLLM exception message, a
    credential, a prompt, or a tool result — only the small, structured
    fields below. ``requested_alias`` is always one of the agent's
    server-side-configured ``ModelRoutingPolicy`` routes, never anything
    caller-supplied.
    """

    iteration: int
    attempt_number: int
    """1-indexed within this iteration's routing sequence (1 = the
    primary alias's attempt)."""
    requested_alias: str
    routing_reason: str
    """``"primary"`` for attempt 1, ``"fallback"`` for every subsequent
    attempt in the same iteration -- deliberately not more specific
    (e.g. not "fallback_after_timeout") to keep this a genuinely
    low-cardinality field, matching the failure kind captured
    separately in ``failure_kind``."""
    outcome: str  # "ok" | "error"
    failure_kind: ModelCallFailureKind | None = None
    """``None`` when ``outcome == "ok"``."""
    detail: str | None = None
    """Bounded, safe detail (see :func:`safe_model_call_detail`) when
    ``outcome == "error"``; ``None`` on success."""
    latency_seconds: float = 0.0
    total_tokens: int | None = None
    backend_model: str | None = None
    """The backend-resolved model identity (``response.model``) when
    this attempt succeeded and the backend reported one; ``None``
    otherwise -- never guessed."""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "iteration": self.iteration,
            "attempt_number": self.attempt_number,
            "requested_alias": self.requested_alias,
            "routing_reason": self.routing_reason,
            "outcome": self.outcome,
            "failure_kind": self.failure_kind.value if self.failure_kind is not None else None,
            "detail": self.detail,
            "latency_seconds": self.latency_seconds,
            "total_tokens": self.total_tokens,
            "backend_model": self.backend_model,
        }
        return d
