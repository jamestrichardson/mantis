"""Shared local HTTP(S) test server fixture for tests/test_http.py and
tests/test_http_tools.py (#110).

A real ``http.server``-based server on an ephemeral port, with
per-test-configurable route handlers — this is the "deterministic
local HTTP server" #110's testing requirements ask for, not a mock of
``httpx``. Optionally wrapped in TLS (using tests/_tls_fixtures.py's
in-memory certificate generation) for the "TLS verify failure" case.

Not a test file itself (no ``test_`` functions) — imported by the real
test modules.
"""

from __future__ import annotations

import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable


class HTTPTestServer:
    """A real local HTTP (or, with ``tls_context``, HTTPS) server.

    ``routes`` maps an exact request path to a handler:
    ``handler(request_handler: BaseHTTPRequestHandler) -> None`` — the
    handler is responsible for calling ``send_response``/
    ``send_header``/``end_headers``/writing to ``wfile`` itself, giving
    each test full control over status/headers/body/streaming/timing.
    A path with no matching route gets a plain 404.
    """

    def __init__(self, routes: dict[str, Callable[[BaseHTTPRequestHandler], None]], *, tls_context: ssl.SSLContext | None = None) -> None:
        self.routes = routes
        outer_self = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch()

            def do_HEAD(self) -> None:  # noqa: N802
                self._dispatch()

            def _dispatch(self) -> None:
                route = outer_self.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                route(self)

            def log_message(self, fmt: str, *args: object) -> None:  # silence test output
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        if tls_context is not None:
            self._server.socket = tls_context.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "HTTPTestServer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def send_simple(handler: BaseHTTPRequestHandler, status: int, *, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
    handler.send_response(status)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    if "Content-Length" not in (headers or {}):
        handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD" and body:
        handler.wfile.write(body)


class SilentTCPServer:
    """Accepts a connection and never sends anything — for a
    connect-succeeds-but-read-times-out test, without any HTTP
    handling at all."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
                self._conns.append(conn)
            except socket.timeout:
                continue
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "SilentTCPServer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
