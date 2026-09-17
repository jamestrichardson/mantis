"""Raw Prometheus HTTP API client.

This module knows how to authenticate to and talk to the Prometheus HTTP
API (``/api/v1/query`` and ``/api/v1/query_range`` only — see
``mantis.tools.prometheus`` for the semantic, LLM-facing layer built on
top of this client). It has no knowledge of agents, LLMs, tool schemas,
PromQL validation, or result shaping.

Every outbound request uses explicit connect/read timeouts and the
shared reliability contract (``mantis.reliability`` — see
``docs/reliability.md``): GET requests are retried through
:func:`~mantis.reliability.retry_call` for safe, bounded backoff on
classified-transient failures only, using the exact same
``AWXClient``-established pattern (see
``mantis.integrations.awx``) — no second retry helper, timeout config
family, or breaker abstraction was introduced for Prometheus.

Prometheus query-level errors (a PromQL parse/execution failure) are
kept deliberately separate from transport-level failures — see
:func:`_parse_envelope` and ``docs/prometheus.md``'s "Query errors vs
retrieval errors" section. A query error is *data* returned by this
client (:class:`PrometheusAPIResponse` with ``status="error"``), never
an exception; a transport/HTTP failure is always a raised
:class:`PrometheusError` (an :class:`~mantis.reliability.IntegrationError`
subclass), classified through the exact same
:func:`~mantis.reliability.classify_http_status` /
:func:`~mantis.reliability.classify_httpx_exception` used everywhere
else in Mantis.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from mantis.config import PrometheusConfig, ReliabilityConfig
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

SOURCE_SYSTEM = "prometheus"

# Prometheus reports a query-level failure (a PromQL parse or execution
# error) using these HTTP status codes, always with an error-shaped JSON
# body -- never as a transport/retry-worthy failure, regardless of the
# non-2xx status. Every other status code (401/403/429/502/503/504/...)
# is classified and retried exactly like any other Mantis integration;
# see docs/prometheus.md's "Query errors vs retrieval errors" section
# for why 503 is deliberately NOT included here even though Prometheus
# sometimes uses it for query timeouts -- #15's reliability contract
# treats 503 as a retryable transient transport signal uniformly across
# every integration, and this module does not special-case it.
_QUERY_ERROR_STATUS_CODES = frozenset({400, 422})


class PrometheusError(IntegrationError):
    """Raised for a transport/HTTP-level Prometheus API failure.

    Subclasses the shared :class:`~mantis.reliability.IntegrationError`
    so ``AgentRuntime`` can classify, retry-budget-account, and
    short-circuit on it generically — the runtime never needs to import
    this class. Never raised for a Prometheus query-level error (see
    :class:`PrometheusAPIResponse`).
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
class PrometheusAPIResponse:
    """A parsed Prometheus API response envelope.

    ``status`` is always ``"success"`` or ``"error"`` — a malformed or
    unrecognized envelope raises :class:`PrometheusError` instead of
    being represented here (see :func:`_parse_envelope`). On success,
    ``result_type`` is Prometheus's own ``data.resultType``
    (``"vector"``/``"matrix"``/``"scalar"``/``"string"``) and ``result``
    is the raw, unprocessed ``data.result`` value — normalization,
    bounding, and ordering are ``mantis.tools.prometheus``'s job, not
    this client's. On error, ``error_type``/``error`` are Prometheus's
    own ``errorType``/``error`` fields. ``warnings`` is always a plain
    list of strings (possibly empty), present on either outcome.
    """

    status: str
    result_type: str | None
    result: Any
    error_type: str | None
    error: str | None
    warnings: list[str] = field(default_factory=list)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Best-effort parse of a ``Retry-After`` header's simple
    integer-seconds form — same convention as ``mantis.integrations.awx``."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_envelope(response: httpx.Response, *, action: str) -> PrometheusAPIResponse:
    """Parse a Prometheus JSON response body into a
    :class:`PrometheusAPIResponse`. Called for both 2xx responses and
    the two query-error status codes (see `_QUERY_ERROR_STATUS_CODES`)
    — Prometheus's error envelope shape is identical either way.

    Raises :class:`PrometheusError` for a non-JSON body or an envelope
    missing/using an unrecognized ``status`` value — Mantis cannot make
    sense of either, so these are transport-adjacent failures, not query
    evidence.
    """
    try:
        payload = response.json()
    except ValueError as exc:
        raise PrometheusError(
            f"Prometheus returned a non-JSON response for {action} "
            f"(HTTP {response.status_code})",
            kind=IntegrationErrorKind.UNKNOWN,
            status_code=response.status_code,
        ) from exc

    if not isinstance(payload, dict):
        raise PrometheusError(
            f"Prometheus returned a non-object JSON response for {action} "
            f"(HTTP {response.status_code})",
            kind=IntegrationErrorKind.UNKNOWN,
            status_code=response.status_code,
        )

    status = payload.get("status")
    warnings = [str(w) for w in (payload.get("warnings") or [])]

    if status == "success":
        data = payload.get("data") or {}
        return PrometheusAPIResponse(
            status="success",
            result_type=data.get("resultType"),
            result=data.get("result"),
            error_type=None,
            error=None,
            warnings=warnings,
        )
    if status == "error":
        return PrometheusAPIResponse(
            status="error",
            result_type=None,
            result=None,
            error_type=payload.get("errorType"),
            error=payload.get("error"),
            warnings=warnings,
        )

    raise PrometheusError(
        f"Prometheus returned a malformed API envelope for {action} "
        f"(unexpected 'status' value: {status!r})",
        kind=IntegrationErrorKind.UNKNOWN,
        status_code=response.status_code,
    )


@dataclass
class PrometheusClient:
    """Thin HTTP client for the Prometheus HTTP API.

    Only ``/api/v1/query`` and ``/api/v1/query_range`` are used — see
    ``mantis.tools.prometheus`` for the semantic tools built on these.

    Every request uses explicit connect/read timeouts
    (``reliability.http_connect_timeout_seconds`` /
    ``.http_read_timeout_seconds`` — never httpx's implicit default),
    further capped at whatever remains of a caller-supplied
    :class:`~mantis.reliability.Deadline` when one is given, and goes
    through :func:`~mantis.reliability.retry_call` with
    ``reliability``'s retry policy — safe here because every method this
    client exposes is a read. See ``docs/reliability.md``.
    """

    config: PrometheusConfig
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig.from_env)
    sleep: Callable[[float], None] = time.sleep
    """Injectable backoff-sleep hook, matching ``AWXClient``'s existing
    test-injection convention — tests pass a recording no-op so retry
    tests never really wait. Defaults to real ``time.sleep`` for
    production use."""

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
    ) -> PrometheusAPIResponse:
        """Shared GET-with-retry path for both ``query`` and
        ``query_range``.

        ``action`` is a short human-readable description (e.g. "execute
        instant PromQL query") used only in the bounded diagnostic
        message — never authoritative, never containing a credential
        (headers, never the URL or params, carry the credential, and
        are never included in any message this client builds).
        """

        def attempt() -> PrometheusAPIResponse:
            try:
                with self._client(deadline=deadline) as client:
                    response = client.get(path, params=params)
                    if response.status_code in _QUERY_ERROR_STATUS_CODES:
                        # A Prometheus query-level error (bad PromQL,
                        # execution failure) -- data, not a transport
                        # failure. Parsed and returned directly, never
                        # raised, never retried, regardless of the
                        # non-2xx HTTP status.
                        return _parse_envelope(response, action=action)
                    response.raise_for_status()
                    return _parse_envelope(response, action=action)
            except httpx.HTTPStatusError as exc:
                kind = classify_http_status(exc.response.status_code)
                raise PrometheusError(
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
                raise PrometheusError(f"Failed to {action}: {exc}", kind=kind) from exc

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

    def query(
        self,
        promql: str,
        *,
        time_param: str | None = None,
        deadline: Deadline | None = None,
    ) -> PrometheusAPIResponse:
        """Execute an instant PromQL query (``GET /api/v1/query``).

        Args:
            promql: Already-validated PromQL query text (see
                ``mantis.tools.prometheus.validate_promql`` — this
                client does not validate).
            time_param: Evaluation instant as a Unix timestamp string,
                or ``None`` to let Prometheus evaluate at its own
                current time (the ``time`` query parameter is omitted
                entirely in that case).
            deadline: Remaining tool-call time budget, if any — threaded
                into the retry policy so a retry never knowingly starts
                (or sleeps for a backoff) past it. See
                ``docs/reliability.md``.
        """
        params: dict[str, Any] = {"query": promql}
        if time_param is not None:
            params["time"] = time_param
        return self._get(
            "/api/v1/query",
            params=params,
            action="execute instant PromQL query",
            deadline=deadline,
        )

    def query_range(
        self,
        promql: str,
        *,
        start: str,
        end: str,
        step: str,
        deadline: Deadline | None = None,
    ) -> PrometheusAPIResponse:
        """Execute a range PromQL query (``GET /api/v1/query_range``).

        Args:
            promql: Already-validated PromQL query text.
            start: Range start as a Unix timestamp string.
            end: Range end as a Unix timestamp string.
            step: Query resolution step, in seconds, as a string.
            deadline: Remaining tool-call time budget, if any.
        """
        params: dict[str, Any] = {"query": promql, "start": start, "end": end, "step": step}
        return self._get(
            "/api/v1/query_range",
            params=params,
            action="execute range PromQL query",
            deadline=deadline,
        )
