"""Shared TLS certificate/server fixture helpers for tests/test_tls.py,
tests/test_tls_tools.py (#111), and tests/test_http.py's TLS-wrapped
HTTP scenarios (#110).

Generates certificates in memory with the ``cryptography`` library
(never a wall-clock-sensitive real-world certificate) and starts a
real local TLS server on an ephemeral port — this is the "deterministic
local TLS server" #111's testing requirements ask for, not a mock of
the ``ssl`` module.

Not a test file itself (no ``test_`` functions) — imported by the real
test modules.

Every temporary PEM file created here (CA files for
``ssl.SSLContext.load_verify_locations``, cert/key files for
``ssl.SSLContext.load_cert_chain`` — both stdlib APIs that require a
filesystem path, not in-memory bytes) is tracked and removed by
:func:`cleanup_temp_files`, called once at test-session end by
``tests/conftest.py``'s autouse fixture — never left behind across
runs (Copilot review: "Clean up temporary CA PEM files" /
"Delete temporary certificate and key files").
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import socket
import ssl
import tempfile
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_TEMP_FILE_PATHS: list[str] = []


def _write_pem_tempfile(data: bytes) -> str:
    """Write ``data`` to a new ``delete=False`` temporary file (the
    stdlib ``ssl`` APIs above need a real path) and track it for
    cleanup — see :func:`cleanup_temp_files`."""
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    handle.write(data)
    handle.close()
    _TEMP_FILE_PATHS.append(handle.name)
    return handle.name


def cleanup_temp_files() -> None:
    """Remove every temporary PEM file created by this module so far.
    Called once, at test-session end, by ``tests/conftest.py`` — never
    called mid-session, since a file's path may still be in active use
    by a test (e.g. as a configured ``ca_file``) for the rest of the
    run."""
    for path in _TEMP_FILE_PATHS:
        try:
            os.unlink(path)
        except OSError:
            pass
    _TEMP_FILE_PATHS.clear()


def make_ca() -> tuple["x509.Certificate", "rsa.RSAPrivateKey"]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mantis Test CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def write_ca_file(ca_cert: "x509.Certificate") -> str:
    return _write_pem_tempfile(ca_cert.public_bytes(serialization.Encoding.PEM))


def make_leaf(
    cn: str,
    *,
    san_dns: list[str] | None = None,
    san_ip: list[str] | None = None,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
    signer: tuple["x509.Certificate", "rsa.RSAPrivateKey"] | None = None,
):
    """Build a leaf certificate/key pair. Self-signed unless ``signer``
    (a ``(ca_cert, ca_key)`` pair from :func:`make_ca`) is given."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    nb = not_before or (now - datetime.timedelta(days=1))
    na = not_after or (now + datetime.timedelta(days=30))
    if signer is None:
        issuer = subject
        signing_key = key
    else:
        ca_cert, ca_key = signer
        issuer = ca_cert.subject
        signing_key = ca_key
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(nb)
        .not_valid_after(na)
    )
    san_entries = []
    if san_dns:
        san_entries.extend(x509.DNSName(d) for d in san_dns)
    if san_ip:
        san_entries.extend(x509.IPAddress(ipaddress.ip_address(ip)) for ip in san_ip)
    if san_entries:
        builder = builder.add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
    cert = builder.sign(signing_key, hashes.SHA256())
    return cert, key


def write_cert_key_files(cert, key, *, extra_certs=None) -> tuple[str, str]:
    """Write a cert/key pair to tracked temporary PEM files, for the
    ``ssl.SSLContext.load_cert_chain()`` stdlib API, which requires
    file paths rather than in-memory bytes. Returns
    ``(certfile_path, keyfile_path)``. ``extra_certs`` are appended
    into the same cert-chain file (e.g. to build a server certificate
    plus intermediate)."""
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    for extra in extra_certs or []:
        cert_pem += extra.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    return _write_pem_tempfile(cert_pem), _write_pem_tempfile(key_pem)


class TLSTestServer:
    """A real local TLS server on an ephemeral port, serving one
    configured certificate/key pair to every connection. Use as a
    context manager or call :meth:`close` explicitly."""

    def __init__(self, cert, key, *, extra_certs=None) -> None:
        certfile_path, keyfile_path = write_cert_key_files(cert, key, extra_certs=extra_certs)

        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(certfile_path, keyfile_path)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            def handle(conn=conn) -> None:
                try:
                    tls_conn = self._ctx.wrap_socket(conn, server_side=True)
                    tls_conn.recv(1)
                    tls_conn.close()
                except Exception:
                    pass

            threading.Thread(target=handle, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "TLSTestServer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class NonTLSTCPServer:
    """A plain TCP server that accepts connections but never speaks
    TLS at all — for the "TLS handshake failure before certificate is
    presented" test case."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            def handle(conn=conn) -> None:
                try:
                    conn.sendall(b"not tls at all, just plain garbage bytes")
                    conn.close()
                except Exception:
                    pass

            threading.Thread(target=handle, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "NonTLSTCPServer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SilentTCPServer:
    """Accepts a connection and never sends anything (and never speaks
    TLS) — for a handshake-timeout test."""

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
