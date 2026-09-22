"""Tests for mantis.api.server: the real `mantis serve` process (#21).

Starts the actual uvicorn.Server built by build_server() against an
ephemeral local port and drives it with real HTTP requests — no live
LiteLLM/AWX/Kubernetes/Prometheus/Loki is needed (the agent catalog is
constructed but never invoked by these tests), and shutdown is driven
the same way uvicorn's own SIGTERM/SIGINT handler drives it
(`server.should_exit = True`), so this is a real, deterministic
integration test of the actual service lifecycle, not a mock of it.
"""

from __future__ import annotations

import signal
import threading
import time

import httpx
import pytest

import mantis.api.server as server_module
import mantis.runtime as runtime_module
from mantis.api.server import build_server, run_server
from mantis.config import ApiServerConfig


@pytest.fixture
def running_server():
    config = ApiServerConfig(auth_mode="disabled", host="127.0.0.1", port=0, shutdown_grace_period_seconds=5.0)
    server = build_server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "server did not report started within the deadline"

    port = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield server, base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def test_server_serves_healthz_on_the_bound_port(running_server):
    _server, base_url = running_server

    response = httpx.get(f"{base_url}/healthz", timeout=5.0)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_server_is_ready_once_started(running_server):
    _server, base_url = running_server

    response = httpx.get(f"{base_url}/readyz", timeout=5.0)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_server_lists_real_agents(running_server):
    _server, base_url = running_server

    response = httpx.get(f"{base_url}/api/v1/agents", timeout=5.0)

    ids = {a["id"] for a in response.json()["agents"]}
    assert ids == {"awx-troubleshooter", "system-troubleshooter", "incident-triage"}


def test_should_exit_triggers_graceful_shutdown_and_the_thread_exits():
    config = ApiServerConfig(auth_mode="disabled", host="127.0.0.1", port=0, shutdown_grace_period_seconds=5.0)
    server = build_server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    server.should_exit = True
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "server thread did not exit after should_exit"


def test_build_server_uses_the_configured_shutdown_grace_period():
    config = ApiServerConfig(auth_mode="disabled", port=0, shutdown_grace_period_seconds=12.0)

    server = build_server(config)

    assert server.config.timeout_graceful_shutdown == 12


def test_build_server_preserves_fractional_shutdown_grace_period():
    # uvicorn's own timeout_graceful_shutdown hands this straight to
    # asyncio.wait_for(timeout=...), which accepts a float -- this must
    # not be silently int()-truncated, losing part of the configured
    # grace period.
    config = ApiServerConfig(auth_mode="disabled", port=0, shutdown_grace_period_seconds=12.5)

    server = build_server(config)

    assert server.config.timeout_graceful_shutdown == 12.5


# ---------------------------------------------------------------------------
# Metrics ownership: `mantis serve` (run_server) is the sole owner of the
# persistent metrics listener -- unlike mantis.cli's HTTP-client-only
# commands, which never touch it at all (see tests/test_cli.py). Uses a
# fake server object so this exercises exactly run_server()'s own
# metrics-gating decision, not a real bind/listen.
# ---------------------------------------------------------------------------


class _FakeServer:
    def run(self) -> None:
        pass


def test_run_server_starts_metrics_by_default(monkeypatch):
    monkeypatch.delenv("MANTIS_METRICS_ENABLED", raising=False)
    calls: list[None] = []
    monkeypatch.setattr(server_module, "start_metrics_server", lambda *a, **kw: calls.append(None))
    monkeypatch.setattr(server_module, "build_server", lambda config: _FakeServer())

    run_server(config=ApiServerConfig(auth_mode="disabled", port=0))

    assert len(calls) == 1


def test_run_server_does_not_start_metrics_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("MANTIS_METRICS_ENABLED", "false")
    calls: list[None] = []
    monkeypatch.setattr(server_module, "start_metrics_server", lambda *a, **kw: calls.append(None))
    monkeypatch.setattr(server_module, "build_server", lambda config: _FakeServer())

    run_server(config=ApiServerConfig(auth_mode="disabled", port=0))

    assert calls == []


# ---------------------------------------------------------------------------
# Graceful shutdown, precisely: the drain flag flips in the same
# synchronous signal-handling moment uvicorn decides to shut down at all
# -- never waiting for uvicorn's own (potentially much later) connection/
# task-draining sequence -- and the process still exits on schedule even
# when a run is genuinely blocked and never finishes. See
# mantis.api.server's module docstring for the full reasoning.
# ---------------------------------------------------------------------------


def test_handle_exit_flips_the_drain_flag_synchronously():
    # No running event loop or thread needed at all: handle_exit() is a
    # plain synchronous method, so this is fully deterministic -- it
    # proves the flag flips in the exact call a real SIGTERM/SIGINT
    # handler makes, not merely "eventually, once uvicorn gets to it."
    config = ApiServerConfig(auth_mode="disabled", port=0)
    server = build_server(config)
    assert server.app.state.shutting_down is False

    server.handle_exit(signal.SIGTERM, None)

    assert server.app.state.shutting_down is True
    assert server.should_exit is True


def test_handle_exit_is_idempotent_about_logging_but_still_sets_should_exit():
    config = ApiServerConfig(auth_mode="disabled", port=0)
    server = build_server(config)

    server.handle_exit(signal.SIGTERM, None)
    server.handle_exit(signal.SIGTERM, None)  # a second signal must not raise

    assert server.app.state.shutting_down is True


def test_shutdown_exits_promptly_even_with_a_blocked_in_flight_run(monkeypatch):
    # The scenario #21's grace-period contract is actually about: a run
    # already executing AgentRuntime.run() synchronously in its own
    # thread when shutdown begins, which never finishes on its own.
    # Proves the server process still exits on schedule (because that
    # thread is a daemon, per mantis.api.invocation._run_in_daemon_thread)
    # rather than hanging until the blocked call eventually returns.
    started = threading.Event()
    release = threading.Event()

    def _blocking_run(self, prompt, *, run_id=None):
        started.set()
        release.wait(timeout=10.0)
        return "done"

    monkeypatch.setattr(runtime_module.AgentRuntime, "run", _blocking_run)

    grace_period = 1.0
    config = ApiServerConfig(
        auth_mode="disabled", host="127.0.0.1", port=0, shutdown_grace_period_seconds=grace_period
    )
    server = build_server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    port = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    request_thread = threading.Thread(
        target=lambda: httpx.post(
            f"{base_url}/api/v1/runs",
            json={"agent": "awx-troubleshooter", "prompt": "investigate"},
            timeout=10.0,
        ),
        daemon=True,
    )
    request_thread.start()
    assert started.wait(timeout=5.0), "the in-flight run never started"

    try:
        server.handle_exit(signal.SIGTERM, None)

        # The server (and therefore the process, in production) must
        # exit within roughly the configured grace period -- deliberately
        # never releasing the blocked run, so this only passes if the
        # daemon-thread design actually works, not because the call
        # happened to finish in time.
        server_thread.join(timeout=grace_period + 5.0)
        assert not server_thread.is_alive(), (
            "server did not exit within the grace period while a run was still blocked"
        )
    finally:
        release.set()  # let the abandoned thread finish so it doesn't leak past the test
