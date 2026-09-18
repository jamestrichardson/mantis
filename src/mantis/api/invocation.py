"""The shared server-side invocation path every API route (and, later,
history recording (#84) and portal/MCP callers) goes through (#83).

::

    HTTP route -> InvocationService -> AgentCatalog -> real AgentRuntime

:class:`InvocationService` does not reimplement any part of
:class:`~mantis.runtime.AgentRuntime` — it only resolves which agent to
run, assigns a stable run ID *before* execution, enforces a bounded
process-local concurrency limit, invokes the real agent, and produces a
stable, safe outcome/error shape. See ``docs/api.md``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypeVar

from openai import OpenAIError

from mantis.api.catalog import (
    UNAVAILABLE_REASON_MISCONFIGURED,
    AgentCatalog,
    AgentUnavailableError,
)
from mantis.config import ConfigurationError
from mantis.observability.logging import log_event, new_run_id
from mantis.runtime import MaxIterationsExceededError, RunDeadlineExceededError
from mantis.security import redact_text

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000
"""Hard cap on a request prompt's length — enforced by
``mantis.api.schemas.RunRequest`` before a request ever reaches
:class:`InvocationService`; repeated here only as the canonical
constant other modules (tests, schemas) import from."""

T = TypeVar("T")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_in_daemon_thread(fn: Callable[..., T], *args: object, **kwargs: object) -> "asyncio.Future[T]":
    """Run a blocking call in a **daemon** thread and return a Future
    that resolves when it completes — deliberately not
    ``asyncio.to_thread``/``loop.run_in_executor(None, ...)``, both of
    which submit to the process's default ``ThreadPoolExecutor``, whose
    worker threads are non-daemon. A non-daemon thread running
    ``AgentRuntime.run()`` past the point uvicorn cancels the awaiting
    task (see :meth:`InvocationService.invoke` and #21's graceful
    shutdown contract) would otherwise be joined by
    ``concurrent.futures.thread``'s own ``atexit`` hook, silently
    blocking process exit for as long as that call keeps running —
    directly defeating "the process exits predictably after the
    configured bounded grace period."

    Awaiting the returned future is fully cancellable in the normal
    asyncio sense (a cancellation just stops *waiting*, exactly like
    ``mantis.reliability``'s existing honest limitation that Mantis
    cannot forcibly interrupt an already-running blocking call) — the
    thread itself is not, and is not claimed to be, stoppable; making it
    a daemon thread only guarantees it can never block interpreter exit.
    """
    loop = asyncio.get_running_loop()
    future: "asyncio.Future[T]" = loop.create_future()

    def _worker() -> None:
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 -- forwarded to the future, not swallowed
            if not loop.is_closed():
                loop.call_soon_threadsafe(_set_exception, exc)
        else:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_set_result, result)

    def _set_result(result: T) -> None:
        if not future.done():
            future.set_result(result)

    def _set_exception(exc: BaseException) -> None:
        if not future.done():
            future.set_exception(exc)

    threading.Thread(target=_worker, daemon=True, name="mantis-agent-run").start()
    return future


class InvocationError(Exception):
    """Base class for a rejection that happens *before* an agent is
    actually invoked (concurrency saturation today; catalog errors are
    ``mantis.api.catalog.AgentCatalogError`` subclasses, kept separate
    since they're raised by the catalog, not this service). Carries a
    stable ``kind`` and the HTTP status the API layer maps it to."""

    def __init__(self, message: str, *, kind: str, http_status: int, run_id: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.run_id = run_id


class ConcurrencyLimitExceededError(InvocationError):
    """Raised when the server-local concurrency limit is already
    saturated. ``run_id`` is still set (a run ID is assigned before
    concurrency is enforced — see :meth:`InvocationService.invoke` — so
    even a rejected attempt can be correlated in logs)."""

    def __init__(self, *, run_id: str) -> None:
        super().__init__(
            "Mantis is at its configured concurrent-run limit; try again shortly.",
            kind="overloaded",
            http_status=429,
            run_id=run_id,
        )


@dataclass(frozen=True)
class RunError:
    """A safe, bounded description of why a run did not succeed — never
    a raw stack trace, provider response body, or credential-bearing
    text. See :meth:`InvocationService._classify_failure`."""

    kind: str
    message: str


@dataclass(frozen=True)
class RunResult:
    """The outcome of one :meth:`InvocationService.invoke` call — the
    server-side shape ``mantis.api.schemas`` renders into the HTTP
    response, and the natural hook point for #84 to persist."""

    run_id: str
    agent: str
    outcome: str  # "success" | "error"
    output: str | None
    error: RunError | None
    started_at: str
    finished_at: str
    duration_ms: int


class _ConcurrencyLimiter:
    """A simple, process-local, non-blocking concurrency gate.

    Deliberately not a queue: :meth:`try_acquire` either grants a slot
    immediately or reports saturation immediately — there is no bounded
    or unbounded wait. See #83's "no unbounded queue" requirement and
    ``docs/api.md``'s concurrency/overload section.
    """

    def __init__(self, max_concurrent: int) -> None:
        self._max_concurrent = max_concurrent
        self._current = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._current >= self._max_concurrent:
                return False
            self._current += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._current -= 1


class InvocationService:
    """The one shared server-side path for actually running an agent."""

    def __init__(self, catalog: AgentCatalog, *, max_concurrent_runs: int) -> None:
        self.catalog = catalog
        self._limiter = _ConcurrencyLimiter(max_concurrent_runs)

    async def invoke(self, agent_id: str, prompt: str) -> RunResult:
        """Resolve, assign a run ID to, bound the concurrency of, and
        execute one real agent run.

        Raises :class:`~mantis.api.catalog.UnknownAgentError` /
        :class:`~mantis.api.catalog.AgentUnavailableError` /
        :class:`ConcurrencyLimitExceededError` for a rejection that
        happens before the agent is actually invoked — the API layer
        maps each to its own HTTP status (see ``mantis.api.app``). A
        failure *during* agent execution (max-iterations, a model
        provider error, an unexpected exception, ...) is never raised —
        it is returned as a :class:`RunResult` with
        ``outcome="error"`` instead, since the invocation attempt itself
        completed.
        """
        entry = self.catalog.get(agent_id)  # raises UnknownAgentError
        run_id = new_run_id()

        try:
            runtime = entry.build_runtime()
        except ConfigurationError:
            raise AgentUnavailableError(agent_id, reason=UNAVAILABLE_REASON_MISCONFIGURED) from None

        if not await self._limiter.try_acquire():
            # docs/api.md promises this run_id is correlatable in
            # server-side logs even for a rejected attempt -- without
            # this event, it never actually appeared in any log line
            # (mantis_api_run_started, the next one logged, only fires
            # *after* this check), silently breaking that promise.
            log_event(
                logger,
                "mantis_api_run_rejected",
                level=logging.WARNING,
                run_id=run_id,
                agent=agent_id,
                reason="overloaded",
            )
            raise ConcurrencyLimitExceededError(run_id=run_id)

        started_at = _utc_now_iso()
        start_perf = time.perf_counter()
        log_event(logger, "mantis_api_run_started", run_id=run_id, agent=agent_id)
        try:
            output = await _run_in_daemon_thread(runtime.run, prompt, run_id=run_id)
            outcome = "success"
            error: RunError | None = None
        except Exception as exc:  # noqa: BLE001 -- classified below; this is the invocation
            # boundary's own last-resort net, matching AgentRuntime._dispatch_tool_call's
            # equally deliberate broad catch for the same reason: a caller must always get
            # a stable RunResult, never an unhandled exception propagating into the route.
            output = None
            outcome = "error"
            error = self._classify_failure(exc, run_id=run_id, agent_id=agent_id)
        finally:
            await self._limiter.release()

        finished_at = _utc_now_iso()
        duration_ms = int((time.perf_counter() - start_perf) * 1000)
        log_event(
            logger,
            "mantis_api_run_completed" if outcome == "success" else "mantis_api_run_failed",
            level=logging.INFO if outcome == "success" else logging.WARNING,
            run_id=run_id,
            agent=agent_id,
            outcome=outcome,
            duration_ms=duration_ms,
        )
        return RunResult(
            run_id=run_id,
            agent=agent_id,
            outcome=outcome,
            output=output,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _classify_failure(exc: Exception, *, run_id: str, agent_id: str) -> RunError:
        """Map an exception raised out of ``AgentRuntime.run()`` onto a
        safe, bounded :class:`RunError`. The real exception (redacted,
        never raw) is always logged server-side for diagnosis; only the
        fixed, generic message below ever reaches the client — never a
        stack trace, provider response body, file path, or credential."""
        if isinstance(exc, MaxIterationsExceededError):
            kind, message = (
                "max_iterations",
                "The agent could not produce a final answer within its iteration limit.",
            )
        elif isinstance(exc, RunDeadlineExceededError):
            kind, message = "run_timeout", "The agent run exceeded its configured time budget."
        elif isinstance(exc, ConfigurationError):
            kind, message = "server_configuration_error", "The server is misconfigured for this agent."
        elif isinstance(exc, OpenAIError):
            kind, message = "model_provider_error", "The model provider could not complete this request."
        else:
            kind, message = "internal_error", "An unexpected error occurred while executing this agent."

        log_event(
            logger,
            "mantis_api_run_error_detail",
            level=logging.ERROR,
            run_id=run_id,
            agent=agent_id,
            error_kind=type(exc).__name__,
            detail=redact_text(str(exc))[:1000],
        )
        return RunError(kind=kind, message=message)
