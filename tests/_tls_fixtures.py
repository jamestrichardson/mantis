"""Shared TLS certificate/server fixture helpers for tests/test_tls.py
and tests/test_tls_tools.py (#111).

Generates certificates in memory with the ``cryptography`` library
(never a wall-clock-sensitive real-world certificate) and starts a
real local TLS server on an ephemeral port — this is the "deterministic
local TLS server" #111's testing requirements ask for, not a mock of
the ``ssl`` module.

Not a test file itself (no ``test_`` functions) — imported by the real
test modules.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
import tempfile
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


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
    ca_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    ca_file.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_file.close()
    return ca_file.name


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


class TLSTestServer:
    """A real local TLS server on an ephemeral port, serving one
    configured certificate/key pair to every connection. Use as a
    context manager or call :meth:`close` explicitly."""

    def __init__(self, cert, key, *, extra_certs=None) -> None:
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        for extra in extra_certs or []:
            cert_pem += extra.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
        certfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
        certfile.write(cert_pem)
        certfile.close()
        keyfile = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
        keyfile.write(key_pem)
        keyfile.close()

        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(certfile.name, keyfile.name)

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
