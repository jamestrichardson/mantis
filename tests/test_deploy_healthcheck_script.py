"""Tests for deploy/standalone/healthcheck.sh (#97): the lightweight
bash-only /readyz probe baked into the runtime image's Docker
healthcheck, replacing a `python -c 'import urllib.request; ...'` probe
whose interpreter-startup/import overhead alone could exceed the
healthcheck's own configured timeout on a slow host.

Runs the real script against a real raw TCP server on localhost (no
mocking) — this exercises the actual `/dev/tcp` connect + HTTP/1.0
request + status-line parse, not a description of it.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "standalone" / "healthcheck.sh"
COMPOSE_FILE = Path(__file__).resolve().parents[1] / "deploy" / "standalone" / "compose.yaml"


def _serve_once(response: bytes) -> tuple[socket.socket, int, threading.Thread]:
    """Bind an ephemeral localhost port, accept exactly one connection
    in a background thread, and reply with the given raw HTTP
    response bytes."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _accept_and_respond() -> None:
        conn, _ = server.accept()
        try:
            conn.recv(4096)
            conn.sendall(response)
        finally:
            conn.close()

    thread = threading.Thread(target=_accept_and_respond, daemon=True)
    thread.start()
    return server, port, thread


def _serve_and_hold(hold_seconds: float) -> tuple[socket.socket, int, threading.Thread]:
    """Accept exactly one connection and then hold it open without ever
    writing a response -- the realistic hang case this probe's own
    internal timeout exists for (accepted, but the server is slow/
    blocked before responding)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _accept_and_hold() -> None:
        conn, _ = server.accept()
        try:
            conn.recv(4096)
            time.sleep(hold_seconds)
        finally:
            conn.close()

    thread = threading.Thread(target=_accept_and_hold, daemon=True)
    thread.start()
    return server, port, thread


def _run(url: str, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {"MANTIS_HEALTH_URL": url, "PATH": "/usr/bin:/bin"}
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


@pytest.fixture(autouse=True)
def _require_bash():
    if shutil.which("bash") is None:
        pytest.skip("bash not available on this host")


def test_probe_succeeds_on_a_real_200_response():
    server, port, thread = _serve_once(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}")
    try:
        result = _run(f"http://127.0.0.1:{port}/readyz")
    finally:
        thread.join(timeout=2)
        server.close()

    assert result.returncode == 0, result.stderr


def test_probe_succeeds_on_any_2xx_status_not_just_200():
    # /readyz only ever returns 200 or 503 in practice, but the probe's
    # own contract is "treat any 2xx as success", not "== 200" -- prove
    # it against a distinct 2xx code so a future, unrelated 2xx doesn't
    # silently start failing this healthcheck.
    server, port, thread = _serve_once(b"HTTP/1.0 204 No Content\r\nContent-Length: 0\r\n\r\n")
    try:
        result = _run(f"http://127.0.0.1:{port}/readyz")
    finally:
        thread.join(timeout=2)
        server.close()

    assert result.returncode == 0, result.stderr


def test_probe_fails_on_a_503_not_ready_response():
    server, port, thread = _serve_once(
        b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
    )
    try:
        result = _run(f"http://127.0.0.1:{port}/readyz")
    finally:
        thread.join(timeout=2)
        server.close()

    assert result.returncode != 0
    assert "503" in result.stderr


def test_probe_fails_when_connection_is_refused():
    # An unused ephemeral port with nothing listening -- the same
    # failure mode as the service not having started yet.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    unused_port = probe.getsockname()[1]
    probe.close()

    result = _run(f"http://127.0.0.1:{unused_port}/readyz")

    assert result.returncode != 0


def test_probe_fails_on_a_malformed_response():
    server, port, thread = _serve_once(b"not even http\r\n\r\n")
    try:
        result = _run(f"http://127.0.0.1:{port}/readyz")
    finally:
        thread.join(timeout=2)
        server.close()

    assert result.returncode != 0
    assert "malformed" in result.stderr.lower()


def test_probe_fails_on_timeout_and_stays_bounded_by_its_own_budget():
    # The connection is accepted (so this isn't a refused-connection
    # case) but the server never responds -- the probe's own internal
    # timeout, not Docker's outer healthcheck timeout, must be what
    # bounds this. Configures a tight 1s probe timeout against a server
    # that holds the connection open for far longer, and asserts the
    # probe actually returns in roughly that 1s window, not by hanging
    # until pytest's own subprocess timeout kills it.
    server, port, thread = _serve_and_hold(hold_seconds=10.0)
    try:
        started = time.monotonic()
        result = _run(
            f"http://127.0.0.1:{port}/readyz",
            extra_env={"MANTIS_HEALTHCHECK_PROBE_TIMEOUT_SECONDS": "1"},
        )
        elapsed = time.monotonic() - started
    finally:
        thread.join(timeout=1)
        server.close()

    assert result.returncode != 0
    assert "no response" in result.stderr.lower()
    assert elapsed < 5, f"probe did not respect its own ~1s timeout budget (took {elapsed:.1f}s)"


def test_probe_rejects_an_unsupported_url_scheme():
    result = _run("https://127.0.0.1:8080/readyz")

    assert result.returncode != 0
    assert "unsupported" in result.stderr.lower()


def test_probe_defaults_to_the_documented_url_when_unset():
    # No MANTIS_HEALTH_URL at all -- must fall back to the documented
    # default (127.0.0.1:8080/readyz), not silently no-op or crash on a
    # missing/empty variable.
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    # Nothing listens on 8080 in the test environment, so this must
    # fail via a refused connection, not via a scheme/parsing error.
    assert result.returncode != 0
    assert "unsupported" not in result.stderr.lower()


# ---------------------------------------------------------------------------
# compose.yaml wiring: the Docker healthcheck must actually invoke the
# baked-in script above, not a re-inlined slow probe, and must carry
# deliberate timeout/start_period headroom (#97).
# ---------------------------------------------------------------------------


def test_compose_healthcheck_invokes_the_baked_in_script():
    content = COMPOSE_FILE.read_text()
    assert '["CMD", "/usr/local/bin/mantis-healthcheck"]' in content
    # Checks for the actual removed invocation, not just a mention of
    # the module name (this file's own comments legitimately reference
    # it as historical context).
    assert "urllib.request.urlopen" not in content


def test_compose_healthcheck_has_headroom_for_a_slow_host():
    content = COMPOSE_FILE.read_text()
    assert "timeout: 5s" in content
    assert "start_period: 5s" in content
