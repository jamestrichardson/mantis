"""The shared reliability contract for Mantis integrations and agent runs.

Mantis is expanding from one integration (AWX) into several (Prometheus,
Loki, network, Git, Kubernetes). Without a shared contract, each new
client would invent its own timeout behavior, retry semantics, and error
vocabulary, and a single degraded service could consume an entire agent
run through repeated slow failures. This module is that shared contract
— see ``docs/reliability.md`` for the full model, defaults, and worked
examples.

Five distinct pieces, all small and generic (no AWX-specific knowledge
here — that lives in ``mantis.integrations.awx``):

- :class:`IntegrationErrorKind` / :class:`IntegrationError` — one
  classification vocabulary every integration maps its failures into.
  ``AgentRuntime`` catches :class:`IntegrationError` generically (it
  never imports an integration-specific exception type), which is what
  lets the runtime stay integration-agnostic while still classifying,
  retrying, and short-circuiting.
- :class:`RetryPolicy` / :func:`retry_call` — a small, bounded retry
  helper for safe idempotent reads only. Never used for mutating
  operations.
- :class:`Deadline` — a monotonic wall-clock budget, used for both the
  per-tool-call and per-run budgets in ``AgentRuntime``, and threaded
  into :func:`retry_call` so a retry loop never starts an attempt (or a
  backoff sleep) that would run past its caller's remaining budget.
- :class:`RunLocalBreaker` — a lightweight, in-memory, per-``run()``
  failure guard. Explicitly **not** a persistent/global circuit
  breaker: no Redis, no database, no cross-run or cross-process state.
  A fresh instance is created at the start of every
  ``AgentRuntime.run()``.

Four separate mechanisms are easy to conflate; keep them named and
distinct in code and docs (see docs/reliability.md's "Retry budget vs.
tool-call budget" section for the full explanation):

- ``AgentRuntime.tool_call_budget`` — how many *successful,
  model-requested* tool calls an agent may make in a run.
- retry/attempt budget (:class:`RetryPolicy`, this module) — how many
  *transport attempts* one logical tool call's integration read may
  consume.
- duplicate-call replay (``AgentRuntime``) — reusing an already-fetched
  result for an exact-duplicate model request; never triggers another
  integration attempt.
- max model-loop iterations (``AgentRuntime.max_iterations``) — how many
  model round-trips a run may take before giving up.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

from mantis.contracts import ToolErrorKind

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_DIAGNOSTIC_MESSAGE_CHARS = 500
"""Bounded diagnostic detail retained on an :class:`IntegrationError` —
generous enough for a useful message, nowhere near large enough to
smuggle a full response body. This is diagnostic context, never
authoritative evidence — see ``docs/reliability.md``."""


# ---------------------------------------------------------------------------
# 1. Shared failure taxonomy
# ---------------------------------------------------------------------------


class IntegrationErrorKind(str, Enum):
    """Shared vocabulary every integration classifies its failures into.

    Retryability derives from this classification (see
    :data:`RETRYABLE_KINDS`), never from parsing exception message text.
    Deliberately small — this is not a general HTTP-status taxonomy,
    just enough distinction to drive correct retry/short-circuit
    behavior and give an operator a useful, low-cardinality label.
    """

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    SERVER_ERROR = "server_error"
    UNKNOWN = "unknown"


RETRYABLE_KINDS = frozenset(
    {
        IntegrationErrorKind.TIMEOUT,
        IntegrationErrorKind.CONNECTION,
        IntegrationErrorKind.RATE_LIMIT,
        IntegrationErrorKind.SERVER_ERROR,
    }
)
"""Only these kinds are safe to retry automatically, and only these
kinds count toward opening a :class:`RunLocalBreaker` — a wrong
answer/missing resource (``not_found``) or a malformed request
(``bad_request``) never indicates the integration itself is
unavailable, so neither retries nor trips the short circuit."""


class IntegrationError(RuntimeError):
    """Base class for every integration-specific error.

    ``AgentRuntime`` catches this type generically — never an
    integration-specific subclass like ``mantis.integrations.awx.AWXError``
    — which is what keeps the runtime integration-agnostic (see this
    module's docstring). An integration defines its own subclasses (so
    existing ``except AWXStdoutError`` call sites keep working unchanged)
    but every one of them ultimately carries the same classification
    fields.

    ``message`` is bounded to :data:`MAX_DIAGNOSTIC_MESSAGE_CHARS` and
    must never contain a credential — integration code is responsible
    for not embedding one in the first place; this class does not scan
    for or redact secrets itself (that's ``mantis.security``'s job, for
    the different concern of *tool-result* content, not this exception's
    own diagnostic text).
    """

    def __init__(
        self,
        message: str,
        *,
        kind: IntegrationErrorKind,
        source_system: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        bounded_message = message[:MAX_DIAGNOSTIC_MESSAGE_CHARS]
        super().__init__(bounded_message)
        self.kind = kind
        self.source_system = source_system
        self.status_code = status_code
        self.retry_after = retry_after
        """Seconds a server asked callers to wait before retrying (HTTP
        ``Retry-After``, when present and parseable), or ``None``."""

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    def to_tool_error_kind(self) -> ToolErrorKind:
        """Map onto the existing, stable tool-result contract
        (``mantis.contracts.ToolErrorKind``) — the richer classification
        here is for retry/short-circuit decisions; the tool-facing
        result keeps using the contract #23 already established (see
        ``docs/tools.md``)."""
        return _TOOL_ERROR_KIND_MAP.get(self.kind, ToolErrorKind.UNKNOWN)


_TOOL_ERROR_KIND_MAP: dict[IntegrationErrorKind, ToolErrorKind] = {
    IntegrationErrorKind.TIMEOUT: ToolErrorKind.TIMEOUT,
    IntegrationErrorKind.CONNECTION: ToolErrorKind.RETRIEVAL_ERROR,
    IntegrationErrorKind.AUTHENTICATION: ToolErrorKind.AUTH_ERROR,
    IntegrationErrorKind.AUTHORIZATION: ToolErrorKind.AUTH_ERROR,
    IntegrationErrorKind.RATE_LIMIT: ToolErrorKind.RATE_LIMITED,
    IntegrationErrorKind.NOT_FOUND: ToolErrorKind.NOT_FOUND,
    IntegrationErrorKind.BAD_REQUEST: ToolErrorKind.UPSTREAM_ERROR,
    IntegrationErrorKind.SERVER_ERROR: ToolErrorKind.UPSTREAM_ERROR,
    IntegrationErrorKind.UNKNOWN: ToolErrorKind.UNKNOWN,
}


def classify_http_status(status_code: int) -> IntegrationErrorKind:
    """Map an HTTP status code onto :class:`IntegrationErrorKind`.

    Generic — reusable by every current and future HTTP-based
    integration (AWX today; Prometheus/Loki next), not AWX-specific.
    """
    if status_code == 401:
        return IntegrationErrorKind.AUTHENTICATION
    if status_code == 403:
        return IntegrationErrorKind.AUTHORIZATION
    if status_code == 404:
        return IntegrationErrorKind.NOT_FOUND
    if status_code == 429:
        return IntegrationErrorKind.RATE_LIMIT
    if status_code == 400 or status_code == 422:
        return IntegrationErrorKind.BAD_REQUEST
    if 500 <= status_code < 600:
        return IntegrationErrorKind.SERVER_ERROR
    if 400 <= status_code < 500:
        return IntegrationErrorKind.BAD_REQUEST
    return IntegrationErrorKind.UNKNOWN


def classify_httpx_exception(exc: Exception) -> IntegrationErrorKind:
    """Map an ``httpx`` transport-level exception (raised before any
    response was received) onto :class:`IntegrationErrorKind`. For a
    response that *was* received, classify the status code instead (see
    :func:`classify_http_status`) — this function is only for
    connect/read/transport failures with no status code at all.
    """
    import httpx

    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return IntegrationErrorKind.TIMEOUT
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.TransportError)):
        return IntegrationErrorKind.CONNECTION
    return IntegrationErrorKind.UNKNOWN


# ---------------------------------------------------------------------------
# Deadlines: a monotonic wall-clock budget, shared by tool-call/run
# budgets and threaded into the retry helper.
# ---------------------------------------------------------------------------


@dataclass
class Deadline:
    """A monotonic wall-clock budget.

    Deliberately simple: this bounds when Mantis *starts* new work (the
    next retry attempt, the next model/tool iteration) — it cannot
    forcibly interrupt a single blocking call already in progress
    (Python cannot preempt arbitrary synchronous code). The actual
    worst-case duration of one blocking call is bounded separately, by
    that call's own HTTP connect/read timeouts. See
    ``docs/reliability.md``'s "What this does and does not guarantee"
    section — this nuance is documented deliberately, not glossed over.
    """

    expires_at: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def after(cls, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> "Deadline":
        return cls(expires_at=clock() + seconds, clock=clock)

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock())

    def expired(self) -> bool:
        return self.remaining() <= 0


class DeadlineExceededError(RuntimeError):
    """Raised when work would start after its :class:`Deadline` has
    already passed. Distinct from :class:`IntegrationError` — this is a
    budget/scheduling failure, not a classified integration failure,
    though callers commonly convert one into a tool-facing result using
    the same shape."""

    def __init__(self, message: str, *, scope: str) -> None:
        super().__init__(message)
        self.scope = scope
        """Which budget was exhausted — e.g. ``"tool"`` or ``"run"``."""


# ---------------------------------------------------------------------------
# 2. Safe retry policy, for idempotent reads only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """A small, bounded retry policy for safe idempotent reads.

    Never apply this to a mutating operation — retry safety depends
    entirely on the operation being idempotent, which this policy has
    no way to verify on its own; that's the caller's responsibility (in
    practice: only ever call :func:`retry_call` from a tool/integration
    whose ``Tool.mutating`` is ``False``).
    """

    max_attempts: int = 3
    """Named retry/attempt budget: the maximum number of transport
    attempts one logical integration read may consume — separate from
    ``AgentRuntime.tool_call_budget`` (model-requested tool calls) and
    duplicate-call replay (see this module's docstring)."""
    backoff_base_seconds: float = 0.5
    backoff_cap_seconds: float = 8.0
    respect_retry_after: bool = True

    def backoff_seconds(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Delay before ``attempt`` (1-indexed: the delay before the
        *second* attempt is ``backoff_seconds(1)``). Exponential with
        full jitter, capped at ``backoff_cap_seconds`` — jitter avoids
        every failing caller retrying in lockstep, capping avoids an
        unbounded wait even at a high attempt count.
        """
        if self.respect_retry_after and retry_after is not None:
            return max(0.0, min(retry_after, self.backoff_cap_seconds))
        exponential = self.backoff_base_seconds * (2 ** (attempt - 1))
        capped = min(exponential, self.backoff_cap_seconds)
        return random.uniform(0.0, capped)


DEFAULT_RETRY_POLICY = RetryPolicy()


def retry_call(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    deadline: Deadline | None = None,
    source_system: str,
    sleep: Callable[[float], None] = time.sleep,
    on_attempt: Callable[[int, IntegrationError | None], None] | None = None,
) -> T:
    """Call ``fn()`` (a zero-argument thunk wrapping one integration
    read), retrying on a classified transient :class:`IntegrationError`
    up to ``policy.max_attempts`` times total.

    Only retries when the caught exception is an :class:`IntegrationError`
    with ``.retryable`` true — anything else (including a non-retryable
    ``IntegrationError``, e.g. authentication/authorization/not_found/
    bad_request) propagates immediately on the first attempt, no retry
    consumed.

    ``deadline``, if given, is checked before every attempt *and* before
    every backoff sleep: if starting another attempt (or waiting out the
    backoff before one) would begin after the deadline, retrying stops
    and the last exception is raised — the retry budget must never
    knowingly start work, or sleep, past the caller's remaining time
    budget.

    ``sleep`` defaults to ``time.sleep`` but accepts an injectable
    replacement so tests never really wait — pass a recording no-op
    stub. ``on_attempt(attempt_number, error_or_none)`` is an optional
    hook for structured logging/metrics at each attempt (this function
    itself does not log — the caller has the source_system/tool context
    to log a properly labeled event; see
    ``mantis.integrations.awx``'s usage for the pattern).
    """
    last_error: IntegrationError | None = None

    for attempt in range(1, policy.max_attempts + 1):
        if deadline is not None and deadline.expired():
            if last_error is None:
                # The deadline was already gone before a single attempt
                # could be made — never began work past the budget, but
                # there's no IntegrationError to re-raise, so this is a
                # budget failure in its own right, not a retry exhaustion.
                raise DeadlineExceededError(
                    f"[{source_system}] remaining budget exhausted before any attempt could start",
                    scope="tool",
                )
            break

        try:
            result = fn()
        except IntegrationError as exc:
            last_error = exc
            if on_attempt is not None:
                on_attempt(attempt, exc)

            if not exc.retryable:
                raise

            if attempt >= policy.max_attempts:
                break

            backoff = policy.backoff_seconds(attempt, retry_after=exc.retry_after)
            if deadline is not None and backoff > deadline.remaining():
                logger.info(
                    "[%s] stopping retries: remaining budget (%.2fs) cannot accommodate "
                    "the next backoff (%.2fs)",
                    source_system,
                    deadline.remaining(),
                    backoff,
                )
                break

            if backoff > 0:
                sleep(backoff)
            continue
        else:
            if on_attempt is not None:
                on_attempt(attempt, None)
            return result

    assert last_error is not None  # unreachable with max_attempts >= 1 and fn always raising/returning
    raise last_error


# ---------------------------------------------------------------------------
# 3. Run-local short circuit — NOT a persistent/global circuit breaker
# ---------------------------------------------------------------------------

DEFAULT_SHORT_CIRCUIT_THRESHOLD = 3
"""Consecutive classified-transient failures against the same
``source_system`` within one run before :class:`RunLocalBreaker` opens
for that system. Small and named deliberately — see
``docs/reliability.md``."""


@dataclass
class RunLocalBreaker:
    """A lightweight, in-memory, per-run failure guard.

    Explicitly not a persistent/global circuit breaker: no cross-run or
    cross-process state, no background health probes, nothing but a
    plain dict living for the lifetime of one ``AgentRuntime.run()``
    call. A fresh instance is created at the start of every ``run()`` —
    see ``AgentRuntime._run_loop``.

    Only :data:`RETRYABLE_KINDS` failures count toward opening the
    guard — a ``not_found`` (the user asked for something that doesn't
    exist) or a ``bad_request`` (a malformed query) never indicates the
    integration itself is unavailable, so neither ever trips it,
    regardless of how many times it happens in a run.
    """

    threshold: int = DEFAULT_SHORT_CIRCUIT_THRESHOLD
    _failure_counts: dict[str, int] = field(default_factory=dict)
    _open: set[str] = field(default_factory=set)

    def is_open(self, source_system: str) -> bool:
        return source_system in self._open

    def record_success(self, source_system: str) -> None:
        self._failure_counts.pop(source_system, None)
        self._open.discard(source_system)

    def record_failure(self, source_system: str, kind: IntegrationErrorKind) -> None:
        if kind not in RETRYABLE_KINDS:
            return
        count = self._failure_counts.get(source_system, 0) + 1
        self._failure_counts[source_system] = count
        if count >= self.threshold:
            self._open.add(source_system)
