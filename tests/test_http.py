"""Tests for mantis.integrations.http: path/method validation and the
bounded, streaming HTTP request mechanics (#110).

Uses a real local HTTP(S) server on an ephemeral port
(tests/_http_fixtures.py) for every network-observing test — never a
mock of ``httpx`` itself, and never live Internet access.
"""

from __future__ import annotations

import socket
import time

import pytest

from mantis.integrations.http import (
    MAX_BODY_BYTES_READ,
    MAX_HEADER_VALUE_CHARS,
    HTTPMethodValidationError,
    HTTPPathValidationError,
    HTTPProbeError,
    probe_http,
    validate_http_method,
    validate_http_path,
)
from mantis.reliability import Deadline, IntegrationErrorKind
from tests._http_fixtures import HTTPTestServer, SilentTCPServer, send_simple
from tests._tls_fixtures import make_leaf


def _probe(host, port, path="/", method="GET", **kwargs):
    kwargs.setdefault("connect_timeout_seconds", 3.0)
    kwargs.setdefault("read_timeout_seconds", 3.0)
    return probe_http(
        scheme=kwargs.pop("scheme", "http"),
        host=host,
        port=port,
        base_path=kwargs.pop("base_path", ""),
        verify_ssl=kwargs.pop("verify_ssl", True),
        path=path,
        method=method,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# validate_http_method
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "get", "head"])
def test_validate_http_method_accepts_supported_methods(method):
    assert validate_http_method(method) == method.upper()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "CONNECT", "OPTIONS", "TRACE", "", 123, None])
def test_validate_http_method_rejects_unsupported_methods(method):
    with pytest.raises(HTTPMethodValidationError):
        validate_http_method(method)


# ---------------------------------------------------------------------------
# validate_http_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/api/health", "/a/b/c", "/x" * 100])
def test_validate_http_path_accepts_valid_paths(path):
    assert validate_http_path(path) == path


@pytest.mark.parametrize(
    "path,reason",
    [
        ("", "empty"),
        ("no-leading-slash", "no leading slash"),
        ("https://evil.example/", "absolute URL"),
        ("http://evil.example/", "absolute URL"),
        ("//evil.example/", "protocol-relative host escape"),
        ("/\r\nHost: evil.example", "CRLF injection"),
        ("/\nHost: evil.example", "LF injection"),
        ("/path with space", "whitespace"),
        ("/path\x00null", "control character"),
        ("/user@host/path", "embedded credentials"),
        ("/path?query=1", "query string"),
        ("/path#fragment", "fragment"),
        ("/path\\backslash", "backslash"),
        ("/" + "x" * 600, "too long"),
        (123, "not a string"),
        (None, "not a string"),
    ],
)
def test_validate_http_path_rejects_invalid_paths(path, reason):
    with pytest.raises(HTTPPathValidationError):
        validate_http_path(path)


# ---------------------------------------------------------------------------
# Basic status codes -- every one is normal, successful evidence
# ---------------------------------------------------------------------------


def test_get_200():
    with HTTPTestServer({"/ok": lambda h: send_simple(h, 200, headers={"Content-Type": "text/plain"}, body=b"hello")}) as server:
        result = _probe("127.0.0.1", server.port, "/ok")

    assert result.status_code == 200
    assert result.body_excerpt == "hello"
    assert result.truncated is False


def test_head_200_never_reads_a_body():
    with HTTPTestServer({"/ok": lambda h: send_simple(h, 200, headers={"Content-Length": "5"})}) as server:
        result = _probe("127.0.0.1", server.port, "/ok", method="HEAD")

    assert result.status_code == 200
    assert result.body_excerpt is None
    assert result.body_bytes_observed == 0


def test_204_no_content():
    with HTTPTestServer({"/empty": lambda h: send_simple(h, 204)}) as server:
        result = _probe("127.0.0.1", server.port, "/empty")

    assert result.status_code == 204
    assert result.body_excerpt == ""


@pytest.mark.parametrize("status", [301, 302])
def test_redirect_with_location(status):
    with HTTPTestServer(
        {"/redirect": lambda h: send_simple(h, status, headers={"Location": "https://elsewhere.example/target"})}
    ) as server:
        result = _probe("127.0.0.1", server.port, "/redirect")

    assert result.status_code == status
    assert result.redirect_location == "https://elsewhere.example/target"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_client_error_statuses_are_normal_results(status):
    with HTTPTestServer({"/x": lambda h: send_simple(h, status)}) as server:
        result = _probe("127.0.0.1", server.port, "/x")
    assert result.status_code == status


def test_429_is_a_normal_result():
    with HTTPTestServer({"/x": lambda h: send_simple(h, 429, headers={"Retry-After": "30"})}) as server:
        result = _probe("127.0.0.1", server.port, "/x")
    assert result.status_code == 429
    assert result.headers["retry-after"] == "30"


@pytest.mark.parametrize("status", [500, 503])
def test_server_error_statuses_are_normal_results_not_raised(status):
    with HTTPTestServer({"/x": lambda h: send_simple(h, status)}) as server:
        result = _probe("127.0.0.1", server.port, "/x")
    assert result.status_code == status


# ---------------------------------------------------------------------------
# Bounded, streaming body reads -- not read-then-truncate
# ---------------------------------------------------------------------------


def test_large_streamed_body_is_bounded_and_truncated():
    huge = b"z" * (MAX_BODY_BYTES_READ * 3)

    def handler(h):
        h.send_response(200)
        h.send_header("Content-Length", str(len(huge)))
        h.end_headers()
        h.wfile.write(huge)

    with HTTPTestServer({"/big": handler}) as server:
        result = _probe("127.0.0.1", server.port, "/big")

    assert result.body_bytes_observed == MAX_BODY_BYTES_READ
    assert result.truncated is True


def test_slow_chunked_body_stops_early_proving_genuine_streaming():
    # A "read everything, then truncate" implementation would take as
    # long as the whole slow transfer; a genuinely bounded streaming
    # read stops as soon as MAX_BODY_BYTES_READ is reached, regardless
    # of how much more the server would still send.
    chunk = b"y" * 8192
    chunk_count = 40  # far more than needed to exceed the 64 KiB cap

    def handler(h):
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        for _ in range(chunk_count):
            h.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
            h.wfile.write(chunk)
            h.wfile.write(b"\r\n")
            h.wfile.flush()
            time.sleep(0.05)
        h.wfile.write(b"0\r\n\r\n")

    with HTTPTestServer({"/slow": handler}) as server:
        started = time.monotonic()
        result = _probe("127.0.0.1", server.port, "/slow")
        elapsed = time.monotonic() - started

    assert result.truncated is True
    assert result.body_bytes_observed <= MAX_BODY_BYTES_READ
    # 40 chunks at 0.05s apart would take ~2s if fully drained; a
    # genuinely bounded streaming read stops after roughly 8 chunks.
    assert elapsed < 1.5, f"did not stop early -- took {elapsed:.2f}s (looks like a full buffered read)"


# ---------------------------------------------------------------------------
# Header bounding: allowlist, value length, oversized/many headers
# ---------------------------------------------------------------------------


def test_only_allowlisted_headers_are_returned():
    def handler(h):
        send_simple(
            h,
            200,
            headers={
                "Content-Type": "text/plain",
                "Set-Cookie": "session=abc123; HttpOnly",
                "X-Custom-Nonsense": "whatever",
                "Authorization": "Bearer should-never-appear",
            },
        )

    with HTTPTestServer({"/x": handler}) as server:
        result = _probe("127.0.0.1", server.port, "/x")

    assert "set-cookie" not in result.headers
    assert "authorization" not in result.headers
    assert "x-custom-nonsense" not in result.headers
    assert result.headers["content-type"] == "text/plain"


def test_oversized_header_value_is_bounded_and_flagged():
    huge_value = "z" * (MAX_HEADER_VALUE_CHARS * 3)

    def handler(h):
        send_simple(h, 200, headers={"Server": huge_value})

    with HTTPTestServer({"/x": handler}) as server:
        result = _probe("127.0.0.1", server.port, "/x")

    assert len(result.headers["server"]) == MAX_HEADER_VALUE_CHARS
    assert result.truncated is True


def test_many_non_allowlisted_headers_have_no_effect():
    headers = {f"X-Extra-{i}": f"value-{i}" for i in range(200)}
    headers["Content-Type"] = "text/plain"

    def handler(h):
        send_simple(h, 200, headers=headers)

    with HTTPTestServer({"/x": handler}) as server:
        result = _probe("127.0.0.1", server.port, "/x")

    # Only allowlisted names ever appear, regardless of how many
    # non-allowlisted headers the server sent (200 "X-Extra-*" here,
    # plus the server's own automatic Server/Date/Content-Length --
    # all of which happen to already be in the allowlist).
    assert set(result.headers) <= {"content-type", "content-length", "server", "date", "location", "retry-after"}
    assert result.headers["content-type"] == "text/plain"
    assert not any(name.startswith("x-extra-") for name in result.headers)


# ---------------------------------------------------------------------------
# Binary/non-UTF-8 body
# ---------------------------------------------------------------------------


def test_binary_non_utf8_body_does_not_crash():
    binary_body = bytes(range(256)) * 4

    def handler(h):
        h.send_response(200)
        h.send_header("Content-Length", str(len(binary_body)))
        h.end_headers()
        h.wfile.write(binary_body)

    with HTTPTestServer({"/bin": handler}) as server:
        result = _probe("127.0.0.1", server.port, "/bin")

    assert result.status_code == 200
    assert isinstance(result.body_excerpt, str)  # decoded with replacement, never raised


# ---------------------------------------------------------------------------
# Transport failures: timeout, connect failure, TLS verify failure --
# all raised via the #15 error model, never a fake HTTP status.
# ---------------------------------------------------------------------------


def test_read_timeout_raises_http_probe_error():
    with SilentTCPServer() as server:
        with pytest.raises(HTTPProbeError) as exc_info:
            _probe("127.0.0.1", server.port, "/x", connect_timeout_seconds=0.5, read_timeout_seconds=0.5)

    assert exc_info.value.kind == IntegrationErrorKind.TIMEOUT


def test_connect_failure_raises_http_probe_error():
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_sock.bind(("127.0.0.1", 0))
    unused_port = probe_sock.getsockname()[1]
    probe_sock.close()

    with pytest.raises(HTTPProbeError) as exc_info:
        _probe("127.0.0.1", unused_port, "/x", connect_timeout_seconds=1.0, read_timeout_seconds=1.0)

    assert exc_info.value.kind == IntegrationErrorKind.CONNECTION


def test_tls_verify_failure_raises_http_probe_error():
    cert, key = make_leaf("selfsigned.example.com", san_dns=["selfsigned.example.com"])
    tls_ctx = __import__("ssl").SSLContext(__import__("ssl").PROTOCOL_TLS_SERVER)
    import tempfile

    from cryptography.hazmat.primitives import serialization

    certfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    certfile.write(cert.public_bytes(serialization.Encoding.PEM))
    certfile.close()
    keyfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    keyfile.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    keyfile.close()
    tls_ctx.load_cert_chain(certfile.name, keyfile.name)

    with HTTPTestServer({"/x": lambda h: send_simple(h, 200)}, tls_context=tls_ctx) as server:
        with pytest.raises(HTTPProbeError) as exc_info:
            _probe(
                "127.0.0.1",
                server.port,
                "/x",
                scheme="https",
                verify_ssl=True,
                connect_timeout_seconds=3.0,
                read_timeout_seconds=3.0,
            )

    assert exc_info.value.kind == IntegrationErrorKind.CONNECTION


def test_verify_ssl_false_allows_self_signed_target():
    cert, key = make_leaf("selfsigned.example.com", san_dns=["selfsigned.example.com"])
    import ssl
    import tempfile

    from cryptography.hazmat.primitives import serialization

    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    certfile.write(cert.public_bytes(serialization.Encoding.PEM))
    certfile.close()
    keyfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    keyfile.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    keyfile.close()
    tls_ctx.load_cert_chain(certfile.name, keyfile.name)

    with HTTPTestServer({"/x": lambda h: send_simple(h, 200, body=b"ok")}, tls_context=tls_ctx) as server:
        result = _probe(
            "127.0.0.1",
            server.port,
            "/x",
            scheme="https",
            verify_ssl=False,
            connect_timeout_seconds=3.0,
            read_timeout_seconds=3.0,
        )

    assert result.status_code == 200


# ---------------------------------------------------------------------------
# Redirects are never automatically followed
# ---------------------------------------------------------------------------


def test_redirect_is_never_automatically_followed():
    called = {"target_hit": False}

    def redirect_handler(h):
        send_simple(h, 302, headers={"Location": "/target"})

    def target_handler(h):
        called["target_hit"] = True
        send_simple(h, 200, body=b"should never be reached")

    with HTTPTestServer({"/start": redirect_handler, "/target": target_handler}) as server:
        result = _probe("127.0.0.1", server.port, "/start")

    assert result.status_code == 302
    assert called["target_hit"] is False


# ---------------------------------------------------------------------------
# Deadline semantics (#15)
# ---------------------------------------------------------------------------


def test_deadline_already_expired_raises_before_any_request():
    with HTTPTestServer({"/x": lambda h: send_simple(h, 200)}) as server:
        deadline = Deadline.after(-1.0)
        with pytest.raises(HTTPProbeError) as exc_info:
            _probe("127.0.0.1", server.port, "/x", deadline=deadline)

    assert exc_info.value.kind == IntegrationErrorKind.TIMEOUT
