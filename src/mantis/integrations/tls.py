"""Bounded, read-only TLS certificate inspection mechanics (#111): two
explicitly bounded handshakes against a server-side-configured direct
TLS endpoint — one to obtain certificate metadata regardless of
whether it would verify, one to determine chain trust independently.

This module knows how to resolve/connect/handshake and parse a
certificate; it has no knowledge of agents, LLMs, tool schemas, or
target *aliases* — see ``mantis.tools.tls`` for the semantic, LLM-facing
layer that resolves an alias (via ``mantis.config.TLSProfilesConfig``)
into the concrete host/port/server_name this module receives.

Critical design requirement (#111): **inspect != verify**. A verified
TLS handshake failing must never mean "no certificate information
available" — that would defeat the entire purpose of this tool. See
:func:`inspect_tls` for exactly how the two handshakes stay independent.

Certificate parsing uses the ``cryptography`` library
(``cryptography.x509``), not any private/undocumented stdlib helper:
``ssl.SSLSocket.getpeercert()`` (the dict form) returns an **empty
dict** whenever the handshake didn't verify — per its own documented
behavior — which is exactly the trap #111 exists to avoid.
``getpeercert(binary_form=True)`` returns the raw DER bytes
unconditionally (verified or not, as long as a certificate was
presented at all), and ``cryptography.x509.load_der_x509_certificate``
parses that DER into structured metadata. See ``docs/tls-certificate-inspection.md``
for the full design and justification.

Reliability posture (#15): mirrors every other current-state Mantis
probe — never wrapped in ``mantis.reliability.retry_call()``. Address
selection reuses ``mantis.integrations.network``'s bounded, deterministic
multi-address philosophy (resolve, dedupe, cap, try in order, stop on
success) without importing it directly (no integration-to-integration
dependency; the same small allowlist logic is reimplemented locally,
matching every other integration's convention of not cross-importing
another integration's private internals).
"""

from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtensionOID

from mantis.reliability import Deadline, IntegrationError, IntegrationErrorKind

SOURCE_SYSTEM = "tls"

MAX_ADDRESSES_ATTEMPTED = 4
"""Hard cap on how many resolved addresses are attempted for one call —
mirrors ``mantis.integrations.network.MAX_ADDRESSES_ATTEMPTED`` exactly.
Stops at the first address where a certificate is successfully
obtained; never tried to compare across addresses."""

DEFAULT_TLS_TIMEOUT_SECONDS = 5.0
"""Ceiling on a single address's connect *and* handshake together
(Python's blocking socket API shares one timeout across both), used
when no :class:`~mantis.reliability.Deadline` further caps it, or
further capped by whatever remains of one when it is."""

MAX_SAN_ENTRIES = 25
"""Cap on the number of DNS/IP SAN entries returned, each counted
separately — a certificate is external, presenter-controlled data and
could otherwise hand the model an unbounded number of SAN entries."""

MAX_SAN_STRING_CHARS = 253
"""Cap on each individual SAN entry's string length (RFC 1035's own
domain-name length limit; generous for an IP-literal SAN)."""

MAX_SUBJECT_CHARS = 500
MAX_ISSUER_CHARS = 500
"""Caps on the subject/issuer distinguished-name strings — bounded,
never a raw dump of arbitrary certificate extensions."""

_ALLOWED_ADDRESS_FAMILIES = (socket.AF_INET, socket.AF_INET6)


class TLSError(IntegrationError):
    """Raised for a TLS retrieval/handshake failure: DNS failure, TCP
    connect failure, or a TLS handshake failure *before* any
    certificate was presented. Never raised once a certificate has
    actually been obtained — an untrusted/expired/hostname-mismatched
    certificate is verification *evidence*, returned as a normal
    result (see :func:`inspect_tls` and #111's "inspect != verify"
    requirement), not an error."""

    def __init__(self, message: str, *, kind: IntegrationErrorKind) -> None:
        super().__init__(message, kind=kind, source_system=SOURCE_SYSTEM)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_ip_literal(value: str) -> bool:
    try:
        ip_address(value)
        return True
    except ValueError:
        return False


def _resolve_candidates(host: str, port: int) -> list[tuple[int, tuple]]:
    """Resolve ``host``/``port`` into a deduplicated, ordered list of
    ``(family, sockaddr)`` candidates — mirrors
    ``mantis.integrations.network._resolve_candidates`` exactly (not
    imported, per this module's docstring)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: set[tuple[int, tuple]] = set()
    candidates: list[tuple[int, tuple]] = []
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        if family not in _ALLOWED_ADDRESS_FAMILIES:
            continue
        key = (family, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((family, sockaddr))
    return candidates


def _dns_name_matches(pattern: str, hostname: str) -> bool:
    """RFC 6125-style leftmost-single-label wildcard match only (e.g.
    ``*.example.com`` matches ``foo.example.com`` but not
    ``foo.bar.example.com`` or ``example.com`` itself) — a small,
    explicit, self-contained matcher, not a dependency on
    ``ssl.match_hostname`` (deprecated) or any other private/
    undocumented helper."""
    pattern = pattern.lower().rstrip(".")
    hostname = hostname.lower().rstrip(".")
    if pattern == hostname:
        return True
    if not pattern.startswith("*."):
        return False
    suffix = pattern[1:]  # ".example.com"
    if not hostname.endswith(suffix):
        return False
    remaining = hostname[: -len(suffix)]
    return bool(remaining) and "." not in remaining


@dataclass(frozen=True)
class CertificateInfo:
    """Bounded, structured certificate metadata — never full PEM/DER,
    never a dump of arbitrary extensions beyond what's listed here."""

    subject: str
    issuer: str
    serial_number: str
    sha256_fingerprint: str
    not_before: str
    not_after: str
    san_dns: list[str] = field(default_factory=list)
    san_ip: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "serial_number": self.serial_number,
            "sha256_fingerprint": self.sha256_fingerprint,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "san_dns": self.san_dns,
            "san_ip": self.san_ip,
        }


def _bounded_str(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def _extract_sans(cert: "x509.Certificate") -> tuple[list[str], list[str], bool]:
    truncated = False
    san_dns: list[str] = []
    san_ip: list[str] = []
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        return san_dns, san_ip, truncated

    dns_values = ext.value.get_values_for_type(x509.DNSName)
    ip_values = ext.value.get_values_for_type(x509.IPAddress)

    if len(dns_values) > MAX_SAN_ENTRIES:
        truncated = True
    if len(ip_values) > MAX_SAN_ENTRIES:
        truncated = True

    for name in dns_values[:MAX_SAN_ENTRIES]:
        bounded, was_truncated = _bounded_str(name, MAX_SAN_STRING_CHARS)
        san_dns.append(bounded)
        truncated = truncated or was_truncated
    for addr in ip_values[:MAX_SAN_ENTRIES]:
        bounded, was_truncated = _bounded_str(str(addr), MAX_SAN_STRING_CHARS)
        san_ip.append(bounded)
        truncated = truncated or was_truncated

    return san_dns, san_ip, truncated


class CertificateParseError(ValueError):
    """Raised by :func:`_parse_certificate` when the DER bytes a peer
    presented cannot be parsed as an X.509 certificate at all — a
    malformed/corrupt certificate, not a transport failure (a
    handshake did complete and a certificate *was* presented)."""


def _parse_certificate(der: bytes) -> tuple[CertificateInfo, bool, "x509.Certificate"]:
    """Parse raw DER bytes into bounded :class:`CertificateInfo`.
    Returns ``(info, truncated, cert)`` — the raw parsed
    ``cryptography.x509.Certificate`` is also returned so
    :func:`inspect_tls` can compute hostname-match/time-validity
    directly from it without re-parsing."""
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError as exc:
        raise CertificateParseError(f"could not parse presented certificate: {exc}") from exc

    subject, subject_truncated = _bounded_str(cert.subject.rfc4514_string(), MAX_SUBJECT_CHARS)
    issuer, issuer_truncated = _bounded_str(cert.issuer.rfc4514_string(), MAX_ISSUER_CHARS)
    san_dns, san_ip, san_truncated = _extract_sans(cert)

    info = CertificateInfo(
        subject=subject,
        issuer=issuer,
        serial_number=str(cert.serial_number),
        sha256_fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
        not_before=cert.not_valid_before_utc.isoformat(),
        not_after=cert.not_valid_after_utc.isoformat(),
        san_dns=san_dns,
        san_ip=san_ip,
    )
    truncated = subject_truncated or issuer_truncated or san_truncated
    return info, truncated, cert


def _hostname_matches(cert: "x509.Certificate", server_name: str) -> bool:
    if _is_ip_literal(server_name):
        target = ip_address(server_name)
        try:
            ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        except x509.ExtensionNotFound:
            return False
        return target in ext.value.get_values_for_type(x509.IPAddress)

    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        return False
    return any(_dns_name_matches(pattern, server_name) for pattern in ext.value.get_values_for_type(x509.DNSName))


def _time_valid(cert: "x509.Certificate", *, now: datetime) -> bool:
    return cert.not_valid_before_utc <= now <= cert.not_valid_after_utc


@dataclass(frozen=True)
class VerificationInfo:
    """Verification evidence, kept as **independent dimensions** — see
    #111's explicit requirement never to collapse chain trust,
    hostname match, and time validity into one boolean.

    ``chain_trusted`` is ``None`` only when the second (verification)
    handshake could not be attempted at all because the caller's
    remaining deadline was exhausted after the first (inspection)
    handshake already succeeded — an honest "not determined", never
    coerced to ``True`` or ``False``.
    """

    chain_trusted: bool | None
    hostname_matches: bool
    time_valid: bool
    status: str

    def to_dict(self) -> dict:
        return {
            "chain_trusted": self.chain_trusted,
            "hostname_matches": self.hostname_matches,
            "time_valid": self.time_valid,
            "status": self.status,
        }


def _derive_status(*, chain_trusted: bool | None, hostname_matches: bool, time_valid: bool) -> str:
    """A normalized, clearly-**derived** overall label — never a
    substitute for the three independent dimensions above, which
    remain visible in the result regardless of this value. Time
    validity is checked first (an expired/not-yet-valid certificate is
    the most fundamental problem, independent of who signed it or what
    name it covers); ``None`` (verification not attempted) takes
    precedence over a stale computed value from a skipped handshake.
    """
    if not time_valid:
        return "expired_or_not_yet_valid"
    if chain_trusted is None:
        return "unknown"
    if not chain_trusted:
        return "untrusted"
    if not hostname_matches:
        return "hostname_mismatch"
    return "valid"


@dataclass(frozen=True)
class TLSInspectionResult:
    """The full outcome of one :func:`inspect_tls` call."""

    host: str
    port: int
    server_name: str
    connected_address: str
    tls_version: str
    cipher: str
    certificate: CertificateInfo
    verification: VerificationInfo
    truncated: bool = False
    observed_at: str = ""


def _inspect_one_address(
    family: int, sockaddr: tuple, *, server_name: str, timeout_seconds: float
) -> tuple[bytes, str, str]:
    """Phase 1: connect and perform the **unverified** inspection
    handshake against one candidate address. Returns
    ``(der_bytes, tls_version, cipher)``. Raises ``OSError``/``socket.timeout``
    on connect failure, or ``ssl.SSLError`` on a handshake failure
    before any certificate is obtained — the caller classifies these.

    ``verify_mode=CERT_NONE``/``check_hostname=False`` is what makes
    this handshake succeed and yield a certificate *regardless of
    whether it would validate* — see this module's docstring for why
    ``getpeercert(binary_form=True)`` (not the dict form) is what makes
    that certificate actually retrievable here.
    """
    raw_sock = socket.socket(family, socket.SOCK_STREAM)
    raw_sock.settimeout(timeout_seconds)
    try:
        raw_sock.connect(sockaddr)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls_sock = ctx.wrap_socket(raw_sock, server_hostname=server_name)
        try:
            der = tls_sock.getpeercert(binary_form=True)
            if der is None:
                raise ssl.SSLError("no certificate presented by peer")
            return der, tls_sock.version() or "", (tls_sock.cipher() or ("", "", 0))[0]
        finally:
            tls_sock.close()
    except Exception:
        try:
            raw_sock.close()
        except OSError:
            pass
        raise


def _verify_one_address(
    family: int, sockaddr: tuple, *, server_name: str, timeout_seconds: float, ca_file: str | None
) -> bool:
    """Phase 2: a *separate* handshake against the same address,
    performing full chain validation (system/default trust store, or
    ``ca_file`` if configured) but with ``check_hostname=False`` so
    this handshake's success/failure reflects **chain trust alone**,
    never conflated with a hostname-match failure (see this module's
    docstring and :func:`inspect_tls`). Returns ``True``/``False``;
    never raises for an untrusted chain (that's the expected, useful
    "not trusted" outcome, not a retrieval failure) — only a genuine
    connect-level failure propagates, which the caller treats as
    "verification not determined" rather than failing the whole call
    (the certificate was already obtained in phase 1).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    if ca_file:
        ctx.load_verify_locations(cafile=ca_file)
    else:
        ctx.load_default_certs()

    raw_sock = socket.socket(family, socket.SOCK_STREAM)
    raw_sock.settimeout(timeout_seconds)
    try:
        raw_sock.connect(sockaddr)
        try:
            tls_sock = ctx.wrap_socket(raw_sock, server_hostname=server_name)
        except ssl.SSLCertVerificationError:
            return False
        tls_sock.close()
        return True
    finally:
        try:
            raw_sock.close()
        except OSError:
            pass


def inspect_tls(
    host: str,
    port: int,
    server_name: str,
    *,
    ca_file: str | None = None,
    deadline: Deadline | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TLSInspectionResult:
    """Inspect the TLS certificate presented at ``host``:``port``,
    using ``server_name`` as SNI, independent of whether it would
    verify.

    Callers must resolve a target *alias* to these concrete values
    themselves (see ``mantis.tools.tls``) — this function trusts its
    arguments, knows nothing about aliases/config, and never shells
    out to ``openssl``.

    **Two explicitly bounded network operations occur**, both against
    the *same* successfully-connected address, both drawing from the
    same overall ``deadline`` budget:

    1. **Inspection handshake** (``verify_mode=CERT_NONE``): obtains
       the certificate's raw DER bytes regardless of trust — this is
       what makes the tool still useful when verification would fail
       (self-signed, untrusted issuer, expired, not-yet-valid, hostname
       mismatch — see #111's "inspect != verify" requirement). Address
       selection mirrors ``check_tcp_connect``: resolve, dedupe, cap at
       :data:`MAX_ADDRESSES_ATTEMPTED`, try in order, stop at the first
       address where a certificate is actually obtained.
    2. **Verification handshake** (``verify_mode=CERT_REQUIRED``,
       ``check_hostname=False``): against the same address that
       answered phase 1, determines chain trust *alone* — never
       conflated with hostname matching, which is computed independently
       (below) from the already-obtained certificate, not from this
       handshake's own pass/fail.

    ``hostname_matches`` and ``time_valid`` are both computed directly
    from the parsed certificate (SAN entries vs. ``server_name``; not-
    before/not-after vs. the current time) — no network operation
    needed for either, and neither depends on whether phase 2 ever
    runs. This is what keeps all three verification dimensions
    genuinely independent rather than derived from one opaque
    handshake's single pass/fail outcome (see
    ``VerificationInfo``).

    If the deadline is exhausted after phase 1 succeeds but before
    phase 2 can start, phase 2 is skipped and ``chain_trusted`` is
    ``None`` (not determined) — the certificate metadata already
    obtained is still returned, never discarded.

    Raises :class:`TLSError` only for a failure *before* any
    certificate was obtained: DNS failure (no candidates resolved),
    every candidate's TCP connect failing, or every candidate's
    inspection handshake failing (a non-TLS endpoint, a protocol
    mismatch, ...). Never raised once phase 1 has succeeded — an
    untrusted/expired/mismatched certificate is verification
    *evidence*, not an error.
    """
    observed_at = _utc_now_iso()

    if deadline is not None and deadline.expired():
        raise TLSError("remaining budget exhausted before the handshake could start", kind=IntegrationErrorKind.TIMEOUT)

    try:
        candidates = _resolve_candidates(host, port)
    except socket.gaierror as exc:
        raise TLSError(f"DNS resolution failed for {host!r}: {exc}", kind=IntegrationErrorKind.CONNECTION) from exc

    if not candidates:
        raise TLSError(f"DNS resolution returned no usable address for {host!r}", kind=IntegrationErrorKind.CONNECTION)

    truncated = len(candidates) > MAX_ADDRESSES_ATTEMPTED
    candidates = candidates[:MAX_ADDRESSES_ATTEMPTED]

    last_error: Exception | None = None
    last_kind = IntegrationErrorKind.CONNECTION
    der: bytes | None = None
    tls_version = ""
    cipher = ""
    connected_address = ""
    family_used = 0
    sockaddr_used: tuple = ()

    for family, sockaddr in candidates:
        if deadline is not None and deadline.expired():
            raise TLSError("remaining budget exhausted before every candidate could be tried", kind=IntegrationErrorKind.TIMEOUT)
        timeout_seconds = DEFAULT_TLS_TIMEOUT_SECONDS
        if deadline is not None:
            timeout_seconds = min(timeout_seconds, deadline.remaining())

        try:
            der, tls_version, cipher = _inspect_one_address(
                family, sockaddr, server_name=server_name, timeout_seconds=timeout_seconds
            )
            connected_address = sockaddr[0]
            family_used = family
            sockaddr_used = sockaddr
            break
        except socket.timeout as exc:
            last_error, last_kind = exc, IntegrationErrorKind.TIMEOUT
        except OSError as exc:
            last_error, last_kind = exc, IntegrationErrorKind.CONNECTION
        except ssl.SSLError as exc:
            last_error, last_kind = exc, IntegrationErrorKind.CONNECTION

    if der is None:
        assert last_error is not None
        raise TLSError(f"TLS inspection failed against every attempted address: {last_error}", kind=last_kind)

    try:
        certificate, cert_truncated, cert = _parse_certificate(der)
    except CertificateParseError as exc:
        # A handshake completed and a certificate *was* presented, but
        # its bytes don't parse as a well-formed X.509 certificate --
        # a malformed-response-shaped failure, not "no certificate
        # available" (mirrors mantis.integrations.dns's malformed_response
        # classification).
        raise TLSError(str(exc), kind=IntegrationErrorKind.SERVER_ERROR) from exc

    now = datetime.now(timezone.utc)
    time_ok = _time_valid(cert, now=now)
    hostname_ok = _hostname_matches(cert, server_name)

    chain_trusted: bool | None = None
    if deadline is None or not deadline.expired():
        verify_timeout = DEFAULT_TLS_TIMEOUT_SECONDS
        if deadline is not None:
            verify_timeout = min(verify_timeout, deadline.remaining())
        try:
            chain_trusted = _verify_one_address(
                family_used, sockaddr_used, server_name=server_name, timeout_seconds=verify_timeout, ca_file=ca_file
            )
        except (OSError, socket.timeout, ssl.SSLError):
            # The certificate is already in hand from phase 1; a
            # failure specifically in the *verification* handshake
            # (e.g. the address became unreachable between phases)
            # must not discard that -- verification is simply not
            # determined, not coerced to a guess.
            chain_trusted = None

    status = _derive_status(chain_trusted=chain_trusted, hostname_matches=hostname_ok, time_valid=time_ok)
    verification = VerificationInfo(
        chain_trusted=chain_trusted, hostname_matches=hostname_ok, time_valid=time_ok, status=status
    )

    return TLSInspectionResult(
        host=host,
        port=port,
        server_name=server_name,
        connected_address=connected_address,
        tls_version=tls_version,
        cipher=cipher,
        certificate=certificate,
        verification=verification,
        truncated=truncated or cert_truncated,
        observed_at=observed_at,
    )
