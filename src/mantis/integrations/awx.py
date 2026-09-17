"""Raw AWX API client.

This module knows how to authenticate to and talk to the AWX HTTP API. It
has no knowledge of agents, LLMs, or tool schemas — see
``mantis.tools.awx`` for the semantic, LLM-facing layer built on top of
this client.

Every outbound request uses explicit connect/read timeouts and the
shared reliability contract (``mantis.reliability`` — see
``docs/reliability.md`` for the full model, defaults, and rationale):
GET requests are retried through :func:`~mantis.reliability.retry_call`
for safe, bounded backoff on classified-transient failures only; a
failed request never relies on httpx's implicit default timeout
behavior.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple

import httpx

from mantis.config import AWXConfig, ReliabilityConfig
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

SOURCE_SYSTEM = "awx"


class JobListPage(NamedTuple):
    """One page of AWX job-list results, plus the total match count.

    ``total_count`` lets a caller detect truncation (more matching jobs
    exist in AWX than were returned in ``jobs``) without a second request.
    """

    jobs: list[dict[str, Any]]
    total_count: int

# AWX returns a short informational payload instead of full stdout when the
# output is too large to display inline, and expects callers to re-request
# with format=txt_download instead. We detect that case by looking for this
# phrase (AWX's own wording) in the truncated response.
_STDOUT_TOO_LARGE_MARKER = "download"


class AWXError(IntegrationError):
    """Raised when the AWX API returns an unexpected error response.

    Subclasses the shared :class:`~mantis.reliability.IntegrationError`
    (``source_system`` is always ``"awx"``) so ``AgentRuntime`` can
    classify, retry-budget-account, and short-circuit on it generically
    — the runtime never needs to import this class. ``kind`` defaults to
    ``UNKNOWN`` only for the (expected to be rare) case of a raise site
    that genuinely can't classify further; every raise site in this
    module supplies a real classification.
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


class AWXStdoutError(IntegrationError):
    """Raised when job stdout specifically cannot be retrieved.

    This is intentionally a distinct exception type from :class:`AWXError`
    so callers (tools) can represent a failure to *fetch* stdout separately
    from a failure *of the underlying AWX job itself* — both are
    :class:`~mantis.reliability.IntegrationError` subclasses underneath,
    so the runtime's generic handling applies to either.
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


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Best-effort parse of a ``Retry-After`` header's simple
    integer-seconds form. AWX/most APIs use this form rather than the
    HTTP-date form; if it's not a plain integer, we just don't have a
    server-suggested wait and fall back to the policy's own backoff."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass
class AWXClient:
    """Thin HTTP client for the AWX v2 API.

    All methods return parsed JSON (``dict``/``list``) or raw text, and
    raise :class:`AWXError` / :class:`AWXStdoutError` on failure. This
    client performs no business logic beyond talking to the API.

    Every request uses explicit connect/read timeouts
    (``reliability.http_connect_timeout_seconds`` /
    ``.http_read_timeout_seconds`` — never httpx's implicit default) and
    goes through :func:`~mantis.reliability.retry_call` with
    ``reliability``'s retry policy — safe here because every method this
    client exposes is a read. See ``docs/reliability.md``.
    """

    config: AWXConfig
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig.from_env)
    sleep: Callable[[float], None] = time.sleep
    """Injectable backoff-sleep hook, matching this client's existing
    test-injection convention (see ``_client``/eval's fixture-client
    override) — tests pass a recording no-op so retry tests never
    really wait. Defaults to real ``time.sleep`` for production use."""

    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self.reliability.retry_max_attempts,
            backoff_base_seconds=self.reliability.retry_backoff_base_seconds,
            backoff_cap_seconds=self.reliability.retry_backoff_cap_seconds,
        )

    def _client(self, accept: str) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.url,
            headers={
                "Authorization": f"Bearer {self.config.token.get_secret_value()}",
                "Accept": accept,
            },
            verify=self.config.verify_ssl,
            timeout=httpx.Timeout(
                connect=self.reliability.http_connect_timeout_seconds,
                read=self.reliability.http_read_timeout_seconds,
                write=self.reliability.http_read_timeout_seconds,
                pool=self.reliability.http_connect_timeout_seconds,
            ),
        )

    def _get(
        self,
        path: str,
        *,
        accept: str,
        params: dict[str, Any] | None,
        error_cls: type,
        action: str,
        deadline: Deadline | None,
    ) -> httpx.Response:
        """Shared GET-with-retry path for every read this client makes.

        ``action`` is a short human-readable description (e.g. "list AWX
        jobs") used only in the bounded diagnostic message — never
        authoritative, never containing a credential.
        """

        def attempt() -> httpx.Response:
            try:
                with self._client(accept=accept) as client:
                    response = client.get(path, params=params)
                    response.raise_for_status()
                    return response
            except httpx.HTTPStatusError as exc:
                kind = classify_http_status(exc.response.status_code)
                raise error_cls(
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
                raise error_cls(f"Failed to {action}: {exc}", kind=kind) from exc

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

    def list_jobs(
        self,
        *,
        status: str | None = None,
        order_by: str | None = None,
        page_size: int = 10,
        deadline: Deadline | None = None,
    ) -> JobListPage:
        """Return one page of job records from ``/api/v2/jobs/``.

        Args:
            status: Filter by AWX job status (e.g. "failed").
            order_by: AWX ``order_by`` value (e.g. "-finished").
            page_size: Maximum number of jobs to request from AWX.
            deadline: Remaining tool-call time budget, if any — threaded
                into the retry policy so a retry never knowingly starts
                (or sleeps for a backoff) past it. See
                ``docs/reliability.md``.

        Returns:
            A :class:`JobListPage` with the returned jobs and AWX's
            reported total match count (``count`` in the API response),
            so a caller can tell whether more matching jobs exist than
            were returned without a second request.
        """
        params: dict[str, Any] = {"page_size": page_size}
        if status is not None:
            params["status"] = status
        if order_by is not None:
            params["order_by"] = order_by

        response = self._get(
            "/api/v2/jobs/",
            accept="application/json",
            params=params,
            error_cls=AWXError,
            action="list AWX jobs",
            deadline=deadline,
        )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AWXError(
                f"AWX returned non-JSON response listing jobs: {exc}",
                kind=IntegrationErrorKind.UNKNOWN,
            ) from exc

        jobs = payload.get("results", [])
        return JobListPage(jobs=jobs, total_count=payload.get("count", len(jobs)))

    def get_job(self, job_id: int, *, deadline: Deadline | None = None) -> dict[str, Any]:
        """Return the full job detail record for ``job_id``."""
        response = self._get(
            f"/api/v2/jobs/{job_id}/",
            accept="application/json",
            params=None,
            error_cls=AWXError,
            action=f"fetch AWX job {job_id}",
            deadline=deadline,
        )

        try:
            return response.json()
        except ValueError as exc:
            raise AWXError(
                f"AWX returned non-JSON response for job {job_id}: {exc}",
                kind=IntegrationErrorKind.UNKNOWN,
            ) from exc

    def get_job_stdout(self, job_id: int, *, deadline: Deadline | None = None) -> str:
        """Return the plain-text stdout for ``job_id``.

        AWX may respond to a normal ``format=txt`` request with a short
        message saying the output is too large to display and that the
        download endpoint should be used instead. This method detects that
        case and transparently retries with ``format=txt_download`` — a
        distinct concern from, and unrelated to, the transport-level
        retry policy applied to each individual request.

        Raises :class:`AWXStdoutError` (never :class:`AWXError`) on
        failure, so callers can distinguish "we couldn't retrieve stdout"
        from "the AWX job itself failed."
        """
        text = self._fetch_stdout(job_id, fmt="txt", deadline=deadline)

        if self._looks_truncated(text):
            logger.info(
                "AWX stdout for job %s appears too large for inline display; "
                "retrying with txt_download",
                job_id,
            )
            text = self._fetch_stdout(job_id, fmt="txt_download", deadline=deadline)

        return text

    def _fetch_stdout(self, job_id: int, *, fmt: str, deadline: Deadline | None) -> str:
        response = self._get(
            f"/api/v2/jobs/{job_id}/stdout/",
            accept="text/plain",
            params={"format": fmt},
            error_cls=AWXStdoutError,
            action=f"retrieve stdout for AWX job {job_id} (format={fmt})",
            deadline=deadline,
        )
        return response.text

    @staticmethod
    def _looks_truncated(text: str) -> bool:
        """Heuristic: does this response look like AWX's "too large" notice?

        AWX's inline stdout endpoint returns a short body pointing at the
        download endpoint when the real output exceeds its display limit.
        A genuine (even large) stdout dump is expected to be much longer
        than this notice, so we key off both length and the presence of
        the marker text rather than length alone.
        """
        if len(text) > 4096:
            return False
        return _STDOUT_TOO_LARGE_MARKER in text.lower()
