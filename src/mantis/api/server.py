"""``mantis serve``: the persistent Mantis service process (#21).

This is the production entry point: it owns application startup/config
validation, the FastAPI app from ``mantis.api.app``, and the long-lived
Prometheus registry/server (#66) for the lifetime of the process. There
is no second daemon for metrics, API, or agent execution — see
``docs/architecture.md``.

Graceful shutdown is uvicorn's own: ``uvicorn.Server`` installs SIGTERM/
SIGINT handlers when run in the main thread, stops accepting new
connections immediately on signal, waits up to
``shutdown_grace_period_seconds`` for in-flight requests to finish, then
exits — see ``docs/deployment.md``'s "Graceful shutdown" section for the
exact sequence and its honest limitations.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from mantis.api.app import create_app
from mantis.config import ApiServerConfig, ConfigurationError, get_metrics_enabled
from mantis.observability.logging import configure_logging, log_event
from mantis.observability.metrics import start_metrics_server

logger = logging.getLogger(__name__)


def build_server(config: ApiServerConfig) -> uvicorn.Server:
    """Construct (but do not run) the real ``uvicorn.Server`` hosting
    the real Mantis app for ``config``. Split out from :func:`run_server`
    purely so tests can start/stop the real server deterministically
    (bind an ephemeral port, poll ``server.started``, flip
    ``server.should_exit`` — the same mechanism uvicorn's own SIGTERM/
    SIGINT handler uses) without needing real process signals.
    """
    app = create_app(server_config=config)
    uvicorn_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        timeout_graceful_shutdown=int(config.shutdown_grace_period_seconds),
    )
    return uvicorn.Server(uvicorn_config)


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
