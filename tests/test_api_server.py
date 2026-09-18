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

import threading
import time

import httpx
import pytest

from mantis.api.server import build_server
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
    assert ids == {"awx-troubleshooter", "system-troubleshooter"}


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
