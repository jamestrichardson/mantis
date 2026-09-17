"""Raw Loki HTTP API client.

This module knows how to authenticate to and talk to the Loki HTTP API
(``/loki/api/v1/query_range`` only — see ``mantis.tools.loki`` for the
semantic, LLM-facing layer built on top of this client). It has no
knowledge of agents, LLMs, tool schemas, LogQL validation, or result
shaping.

Every outbound request uses explicit connect/read timeouts and the
shared reliability contract (``mantis.reliability`` — see
``docs/reliability.md``): GET requests are retried through
:func:`~mantis.reliability.retry_call` for safe, bounded backoff on
classified-transient failures only, using the exact same
``AWXClient``/``PrometheusClient``-established pattern (see
``mantis.integrations.awx``, ``mantis.integrations.prometheus``) — no
second retry helper, timeout config family, or breaker abstraction was
introduced for Loki.

Loki query-level errors (a LogQL parse or execution failure) are kept
deliberately separate from transport-level failures — see
:func:`_parse_envelope` and ``docs/loki.md``'s "Query errors vs
retrieval errors" section. A query error is *data* returned by this
client (:class:`LokiAPIResponse` with ``status="error"``), never an
exception; a transport/HTTP failure is always a raised :class:`LokiError`
(an :class:`~mantis.reliability.IntegrationError` subclass), classified
through the exact same :func:`~mantis.reliability.classify_http_status` /
:func:`~mantis.reliability.classify_httpx_exception` used everywhere
else in Mantis.

This client only ever issues the one read this issue scopes: a bounded
LogQL range query. It never accepts a model-supplied URL, path, HTTP
method, header, tenant value, or auth material — those are all fixed by
this module and :class:`~mantis.config.LokiConfig` (see
``mantis.tools.loki`` for what *is* model-facing: LogQL text, a bounded
time window, and an optional read direction).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from mantis.config import LokiConfig, ReliabilityConfig
from mantis.observability.logging import log_event
from mantis.reliability import (
    Deadline,
    IntegrationError,
    IntegrationErrorKind,
    RetryPolicy,
    classify_http_status,
    classify_httpx_exception,
    retry_call,
)

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "loki"

# Loki reports a query-level failure (a LogQL parse or execution error)
# using these HTTP status codes, with an error-shaped JSON body -- never
# as a transport/retry-worthy failure, regardless of the non-2xx status.
# Every other status code (401/403/429/502/503/504/...) is classified
# and retried exactly like any other Mantis integration -- see
# mantis.integrations.prometheus's identical convention and
# docs/loki.md's "Query errors vs retrieval errors" section for why this
# module does not special-case 503 even though a query-side timeout
# could plausibly use it.
_QUERY_ERROR_STATUS_CODES = frozenset({400, 422})


class LokiError(IntegrationError):
    """Raised for a transport/HTTP-level Loki API failure.

    Subclasses the shared :class:`~mantis.reliability.IntegrationError`
    so ``AgentRuntime`` can classify, retry-budget-account, and
    short-circuit on it generically — the runtime never needs to import
    this class. Never raised for a Loki query-level error (see
    :class:`LokiAPIResponse`).
    """

    def __init__(
        self,
        message: str,
        *,
        kind: IntegrationErrorKind = IntegrationErrorKind.UNKNOWN,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message,
            kind=kind,
            source_system=SOURCE_SYSTEM,
            status_code=status_code,
            retry_after=retry_after,
        )


@dataclass(frozen=True)
class LokiAPIResponse:
    """A parsed Loki API response envelope.

    ``status`` is always ``"success"`` or ``"error"`` — a malformed or
    unrecognized envelope raises :class:`LokiError` instead of being
    represented here (see :func:`_parse_envelope`). On success,
    ``result_type`` is Loki's own ``data.resultType`` (this client/tool
    only supports ``"streams"`` — see ``mantis.tools.loki``) and
    ``result`` is the raw, unprocessed ``data.result`` value —
    normalization, bounding, and ordering are ``mantis.tools.loki``'s
    job, not this client's. On error, ``error`` is Loki's own reported
    error message (Loki's query-error envelope, unlike Prometheus's,
    does not include a separate machine-readable error-type field).
    ``warnings`` is always a plain list of strings (possibly empty),
    present on either outcome, mirroring Prometheus's convention for
    the Loki versions that report LogQL warnings. ``warnings_malformed``
    is ``True`` when the envelope *had* a ``"warnings"`` field but it
    wasn't a list (e.g. a bare string or an object) -- distinct from the
    field being absent entirely, which is a normal, common case for a
    Loki version/deployment that doesn't report warnings at all. A
    malformed (but present) ``"warnings"`` value means some response
    content was discarded rather than parsed, so
    ``mantis.tools.loki._shape_result`` folds this into
    ``meta.truncated`` rather than silently treating the query as fully
    complete.
    """

    status: str
    result_type: str | None
    result: Any
    error: str | None
    warnings: list[str] = field(default_factory=list)
    warnings_malformed: bool = False


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Best-effort parse of a ``Retry-After`` header's simple
    integer-seconds form — same convention as ``mantis.integrations.awx``
    and ``mantis.integrations.prometheus``."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_envelope(response: httpx.Response, *, action: str) -> LokiAPIResponse:
    """Parse a Loki JSON response body into a :class:`LokiAPIResponse`.
    Called for both 2xx responses and the two query-error status codes
    (see `_QUERY_ERROR_STATUS_CODES`).

    Raises :class:`LokiError` for a non-JSON body or an envelope
    missing/using an unrecognized ``status`` value — Mantis cannot make
    sense of either, so these are transport-adjacent failures, not query
    evidence. Mirrors ``mantis.integrations.prometheus._parse_envelope``
    exactly, adjusted for Loki's field names.
    """
    try:
        payload = response.json()
    except ValueError as exc:
        raise LokiError(
            f"Loki returned a non-JSON response for {action} (HTTP {response.status_code})",
            kind=IntegrationErrorKind.UNKNOWN,
            status_code=response.status_code,
        ) from exc

    if not isinstance(payload, dict):
        raise LokiError(
            f"Loki returned a non-object JSON response for {action} (HTTP {response.status_code})",
            kind=IntegrationErrorKind.UNKNOWN,
            status_code=response.status_code,
        )

    status = payload.get("status")
    # A malformed "warnings" field (e.g. a bare string instead of a
    # list) is never iterated -- iterating a string yields one list
    # entry per character, which would build a potentially enormous
    # intermediate list from arbitrary response data before
    # mantis.tools.loki's warning-count/length bounds ever get a chance
    # to apply. It's still recorded as "malformed" (as opposed to simply
    # "absent", the normal case for a Loki version that doesn't report
    # warnings at all) so mantis.tools.loki can fold that into
    # meta.truncated -- discarding response content silently, without
    # ever reflecting it in completeness, would violate #10's truncation
    # contract just as much as dropping a log line would.
    raw_warnings = payload.get("warnings")
    warnings_malformed = raw_warnings is not None and not isinstance(raw_warnings, list)
    warnings = [str(w) for w in raw_warnings] if isinstance(raw_warnings, list) else []

    if status == "success":
        data = payload.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise LokiError(
                f"Loki returned a malformed API envelope for {action} "
                f"('data' was a {type(data).__name__}, not an object)",
                kind=IntegrationErrorKind.UNKNOWN,
                status_code=response.status_code,
            )
        return LokiAPIResponse(
            status="success",
            result_type=data.get("resultType"),
            result=data.get("result"),
            error=None,
            warnings=warnings,
            warnings_malformed=warnings_malformed,
        )
    if status == "error":
        error = payload.get("error")
        if error is None:
            # Some Loki versions use "message" instead of "error" for
            # the query-error envelope -- accept either rather than
            # silently reporting an empty message for one of them.
            error = payload.get("message")
        return LokiAPIResponse(
            status="error",
            result_type=None,
            result=None,
            error=error,
            warnings=warnings,
            warnings_malformed=warnings_malformed,
        )

    raise LokiError(
        f"Loki returned a malformed API envelope for {action} (unexpected 'status' value: {status!r})",
        kind=IntegrationErrorKind.UNKNOWN,
        status_code=response.status_code,
    )


@dataclass
class LokiClient:
    """Thin HTTP client for the Loki HTTP API.

    Only ``/loki/api/v1/query_range`` is used (see
    ``mantis.tools.loki`` for the semantic tool built on this) — no
    admin/config endpoints, no ingestion/push endpoints, no tail/follow
    streaming (see #10's non-goals).

    Every request uses explicit connect/read timeouts
    (``reliability.http_connect_timeout_seconds`` /
    ``.http_read_timeout_seconds`` — never httpx's implicit default),
    further capped at whatever remains of a caller-supplied
    :class:`~mantis.reliability.Deadline` when one is given, and goes
    through :func:`~mantis.reliability.retry_call` with
    ``reliability``'s retry policy — safe here because this client only
    ever performs a read. See ``docs/reliability.md``.
    """

    config: LokiConfig
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig.from_env)
    sleep: Callable[[float], None] = time.sleep
    """Injectable backoff-sleep hook, matching ``AWXClient``'s/
    ``PrometheusClient``'s existing test-injection convention — tests
    pass a recording no-op so retry tests never really wait. Defaults to
    real ``time.sleep`` for production use."""

    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self.reliability.retry_max_attempts,
            backoff_base_seconds=self.reliability.retry_backoff_base_seconds,
            backoff_cap_seconds=self.reliability.retry_backoff_cap_seconds,
        )

    def _client(self, *, deadline: Deadline | None = None) -> httpx.Client:
        connect = self.reliability.http_connect_timeout_seconds
        read = self.reliability.http_read_timeout_seconds
        if deadline is not None:
            remaining = deadline.remaining()
            connect = min(connect, remaining)
            read = min(read, remaining)

        headers: dict[str, str] = {"Accept": "application/json"}
        if self.config.tenant_id is not None:
            # Loki's multi-tenancy header. Always a fixed, deployment-
            # configured value -- never model-supplied. See
            # mantis.config.LokiConfig and docs/loki.md.
            headers["X-Scope-OrgID"] = self.config.tenant_id
        auth: tuple[str, str] | None = None
        if self.config.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.config.bearer_token.get_secret_value()}"
        elif self.config.basic_auth_username is not None and self.config.basic_auth_password is not None:
            auth = (self.config.basic_auth_username, self.config.basic_auth_password.get_secret_value())

        return httpx.Client(
            base_url=self.config.url,
            headers=headers,
            auth=auth,
            verify=self.config.verify_ssl,
            timeout=httpx.Timeout(connect=connect, read=read, write=read, pool=connect),
        )

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any],
        action: str,
        deadline: Deadline | None,
    ) -> LokiAPIResponse:
        """Shared GET-with-retry path for ``query_range``.

        ``action`` is a short human-readable description used only in
        the bounded diagnostic message — never authoritative, never
        containing a credential (headers, never the URL or params, carry
        the credential, and are never included in any message this
        client builds).
        """

        def attempt() -> LokiAPIResponse:
            try:
                with self._client(deadline=deadline) as client:
                    response = client.get(path, params=params)
                    if response.status_code in _QUERY_ERROR_STATUS_CODES:
                        # A Loki query-level error (bad LogQL, execution
                        # failure) -- data, not a transport failure.
                        # Parsed and returned directly, never raised,
                        # never retried, regardless of the non-2xx HTTP
                        # status.
                        return _parse_envelope(response, action=action)
                    response.raise_for_status()
                    return _parse_envelope(response, action=action)
            except httpx.HTTPStatusError as exc:
                kind = classify_http_status(exc.response.status_code)
                raise LokiError(
                    f"Failed to {action}: HTTP {exc.response.status_code}",
                    kind=kind,
                    status_code=exc.response.status_code,
                    retry_after=(
                        _parse_retry_after(exc.response)
                        if kind == IntegrationErrorKind.RATE_LIMIT
                        else None
                    ),
                ) from exc
            except httpx.HTTPError as exc:
                kind = classify_httpx_exception(exc)
                raise LokiError(f"Failed to {action}: {exc}", kind=kind) from exc

        def on_attempt(attempt_number: int, error: IntegrationError | None) -> None:
            if error is None:
                return
            will_retry = error.retryable and attempt_number < self.reliability.retry_max_attempts
            log_event(
                logger,
                "mantis_integration_retry",
                level=logging.INFO if will_retry else logging.WARNING,
                source_system=SOURCE_SYSTEM,
                action=action,
                attempt=attempt_number,
                max_attempts=self.reliability.retry_max_attempts,
                error_kind=error.kind.value,
                will_retry=will_retry,
            )

        return retry_call(
            attempt,
            policy=self._retry_policy(),
            deadline=deadline,
            source_system=SOURCE_SYSTEM,
            sleep=self.sleep,
            on_attempt=on_attempt,
        )

    def query_range(
        self,
        logql: str,
        *,
        start_ns: str,
        end_ns: str,
        direction: str,
        limit: int,
        deadline: Deadline | None = None,
    ) -> LokiAPIResponse:
        """Execute a bounded LogQL range query
        (``GET /loki/api/v1/query_range``).

        Args:
            logql: Already-validated LogQL query text (see
                ``mantis.tools.loki.validate_logql`` — this client does
                not validate).
            start_ns: Range start as a Unix nanosecond-timestamp string
                (Loki's own native precision — see
                ``mantis.tools.loki`` for why the tool layer formats it
                this way instead of a lossy float).
            end_ns: Range end as a Unix nanosecond-timestamp string.
            direction: ``"forward"`` or ``"backward"`` — already
                validated by the tool layer.
            limit: Maximum number of log lines Loki itself should
                return across all streams. Mantis enforces its own,
                separate output bounds regardless (defense in depth —
                see ``mantis.tools.loki``'s named caps) but also asks
                Loki not to do more work than Mantis will use.
            deadline: Remaining tool-call time budget, if any — threaded
                into the retry policy so a retry never knowingly starts
                (or sleeps for a backoff) past it. See
                ``docs/reliability.md``.
        """
        params: dict[str, Any] = {
            "query": logql,
            "start": start_ns,
            "end": end_ns,
            "direction": direction,
            "limit": str(limit),
        }
        return self._get(
            "/loki/api/v1/query_range",
            params=params,
            action="execute LogQL range query",
            deadline=deadline,
        )
