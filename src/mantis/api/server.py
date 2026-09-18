"""``mantis serve``: the persistent Mantis service process (#21).

This is the production entry point: it owns application startup/config
validation, the FastAPI app from ``mantis.api.app``, and the long-lived
Prometheus registry/server (#66) for the lifetime of the process. There
is no second daemon for metrics, API, or agent execution — see
``docs/architecture.md``.

Graceful shutdown, precisely
-----------------------------

``uvicorn.Server`` installs SIGTERM/SIGINT handlers when run in the main
thread. On signal, its *own* sequence is: stop accepting new TCP
connections, wait up to ``timeout_graceful_shutdown`` for in-flight
requests/tasks to finish (cancelling them if that elapses), and only
*then* run the ASGI ``lifespan`` "shutdown" phase
(``mantis.api.app``'s ``app.state.shutting_down = True``).

That ordering is a real problem for #21's readiness contract on its
own: "readiness transitions to not-ready *before* new work is rejected"
should not depend on how long uvicorn's own connection/task draining
happens to take. :class:`_DrainingAwareServer` below fixes this by
overriding ``handle_exit`` (the method uvicorn's signal handler itself
calls) to flip the app's drain flag **synchronously, in the same
signal-handling moment** uvicorn decides to begin shutting down at all —
before its connection-draining sequence even starts, not after.

The second, deeper problem this module fixes is that
``InvocationService`` runs ``AgentRuntime.run()`` in a **daemon** thread
(see ``mantis.api.invocation._run_in_daemon_thread``), not the default
executor ``asyncio.to_thread`` would use. If uvicorn's
``timeout_graceful_shutdown`` elapses while a run is still executing,
uvicorn cancels the *awaiting* asyncio task — which stops the HTTP
response from ever completing, but cannot and does not stop the
underlying OS thread the blocking call is still running in (Python
cannot preempt arbitrary synchronous code; see
``mantis.reliability``'s identical honest limitation). A **non-daemon**
thread there would be joined by ``concurrent.futures.thread``'s own
``atexit`` hook, silently blocking process exit for as long as that
call keeps running — directly defeating "the process exits predictably
after the configured bounded grace period." A daemon thread cannot
block interpreter exit, so the process still exits on schedule; the
abandoned run itself is simply lost, exactly as honestly documented in
``docs/deployment.md``.
"""

from __future__ import annotations

import logging
import sys
from types import FrameType

import uvicorn
from fastapi import FastAPI

from mantis.api.app import create_app
from mantis.config import ApiServerConfig, ConfigurationError, get_metrics_enabled
from mantis.observability.logging import configure_logging, log_event
from mantis.observability.metrics import start_metrics_server

logger = logging.getLogger(__name__)


class _DrainingAwareServer(uvicorn.Server):
    """A ``uvicorn.Server`` that flips its app's drain flag the instant
    a shutdown signal is handled, rather than waiting for uvicorn's own
    (potentially much later) ASGI lifespan-shutdown phase. See this
    module's docstring for exactly why that ordering matters."""

    def __init__(self, config: uvicorn.Config, app: FastAPI) -> None:
        super().__init__(config)
        self.app = app
        """The real Mantis FastAPI app this server hosts — a public
        attribute (not just closed over) so tests can assert
        ``server.app.state.shutting_down`` deterministically, without
        racing a new HTTP connection against uvicorn's own
        connection-draining timing."""

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if not self.app.state.shutting_down:  # only log/flip once
            self.app.state.shutting_down = True
            log_event(logger, "mantis_api_shutdown_started", signal=sig)
        super().handle_exit(sig, frame)


def build_server(config: ApiServerConfig) -> uvicorn.Server:
    """Construct (but do not run) the real ``uvicorn.Server`` hosting
    the real Mantis app for ``config``. Split out from :func:`run_server`
    purely so tests can start/stop the real server deterministically
    (bind an ephemeral port, poll ``server.started``, call
    ``server.handle_exit(...)`` directly — the same method a real
    SIGTERM/SIGINT invokes) without needing real process signals.
    """
    app = create_app(server_config=config)
    uvicorn_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        # uvicorn's own type hint says `int`, but it only ever hands this
        # straight to asyncio.wait_for(timeout=...), which accepts a
        # float natively -- passed through as configured rather than
        # int()-truncated, so a fractional MANTIS_API_SHUTDOWN_GRACE_PERIOD_SECONDS
        # (e.g. 12.5) doesn't silently lose part of its configured grace
        # period.
        timeout_graceful_shutdown=config.shutdown_grace_period_seconds,
    )
    return _DrainingAwareServer(uvicorn_config, app)


def run_server(*, config: ApiServerConfig | None = None) -> None:
    """Build and run the real Mantis service. Blocks until the process
    receives SIGTERM/SIGINT and uvicorn's graceful shutdown completes.

    ``config`` is overridable only for tests that need to run the
    server against an ephemeral port; production always resolves
    ``ApiServerConfig.from_env()``.
    """
    resolved_config = config or ApiServerConfig.from_env()

    # Unlike mantis.cli's short-lived agent/eval invocations (where a
    # metrics server defaulting to on would create :9108 port
    # contention across concurrent one-shot processes -- see
    # docs/observability.md), mantis serve is exactly the persistent
    # process #66 exists to give metrics a real home in: one process,
    # one registry, held open for the service's lifetime. Still
    # explicitly overridable (MANTIS_METRICS_ENABLED=false) if an
    # operator doesn't want the port exposed at all.
    if get_metrics_enabled(default=True):
        start_metrics_server()
        log_event(logger, "mantis_api_metrics_server_started")

    server = build_server(resolved_config)

    log_event(
        logger,
        "mantis_api_serve_starting",
        host=resolved_config.host,
        port=resolved_config.port,
        auth_mode=resolved_config.auth_mode,
        max_concurrent_runs=resolved_config.max_concurrent_runs,
    )

    server.run()

    log_event(logger, "mantis_api_serve_stopped")


def main(argv: list[str] | None = None) -> int:
    """``mantis serve`` CLI entry point."""
    configure_logging()
    try:
        run_server()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
