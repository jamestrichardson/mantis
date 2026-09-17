"""Raw TCP connectivity mechanics: DNS resolution and socket connects.

This module knows how to resolve a host and attempt a bounded, timed TCP
connection to it. It has no knowledge of agents, LLMs, tool schemas, or
evidence/provenance shaping — see ``mantis.tools.network`` for the
semantic, LLM-facing layer built on top of this module.

Deliberately narrow (see ``docs/network-tcp-connectivity.md`` for the
full design rationale): this answers exactly one question — can Mantis
establish a TCP connection to this host/port right now — using nothing
but the standard library ``socket`` module. No ICMP/ping, no UDP, no
port scanning, no subprocesses, no shell commands.

Two functions are the only places this module touches the outside
world, deliberately isolated so tests can substitute them without
faking ``socket.socket``/``socket.getaddrinfo`` wholesale:

- :func:`_resolve` — one ``socket.getaddrinfo()`` call.
- :func:`_connect` — one blocking, timeout-bounded connect attempt.

Reliability posture (#15): a TCP probe is a current-state *observation*,
not an idempotent read that should be retried — see this module's
docstring on :func:`check_tcp_connect` for why ``retry_call()`` is
deliberately never used here, and ``docs/network-tcp-connectivity.md``'s
"Deadline semantics" section for the full reasoning.
"""

from __future__ import annotations

import errno as errno_module
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from ipaddress import ip_address
from typing import Any, Callable

from mantis.reliability import Deadline

MAX_ADDRESSES_ATTEMPTED = 4
"""Hard cap on how many resolved addresses are attempted for one check.
A hostname can resolve to many A/AAAA records; this keeps one tool call
bounded and interactive-troubleshooting-sized rather than sweeping every
address a resolver returns. Small and named deliberately — see
``docs/network-tcp-connectivity.md``."""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
"""Ceiling on a single connect attempt's socket timeout, used when no
:class:`~mantis.reliability.Deadline` is given, or further capped by
whatever remains of one when it is (see :func:`check_tcp_connect`) — a
single slow candidate must never be allowed to consume an entire tool
or run budget on its own."""

MAX_ATTEMPT_MESSAGE_CHARS = 200
"""Bound on each per-address attempt's diagnostic ``message`` — enough
to be useful, nowhere near large enough to leak a raw exception repr or
implementation internals into a tool result."""

_HOSTNAME_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,62})?"
_HOSTNAME_RE = re.compile(rf"^{_HOSTNAME_LABEL}(\.{_HOSTNAME_LABEL})*\.?$")
"""RFC 1123-ish hostname: dot-separated labels of letters/digits/hyphens
only, each <=63 chars, optional trailing dot. Deliberately an allowlist,
not a blocklist — this alone excludes whitespace, shell metacharacters,
URL schemes, path separators, and embedded credentials without needing
a separate denylist for each."""


class HostValidationError(ValueError):
    """Raised by :func:`validate_host` for a host string that is not a
    plain hostname or IP literal — a caller/model mistake, not a network
    observation. See ``mantis.tools.network.check_tcp_connectivity`` for
    how this is safely surfaced to the model (never by letting this
    exception propagate raw)."""


class PortValidationError(ValueError):
    """Raised by :func:`validate_port` for a port that is not an integer
    in ``[1, 65535]``."""


def validate_host(host: Any) -> str:
    """Validate ``host`` is a plain hostname or IP literal.

    Rejects (deliberately, per ``docs/network-tcp-connectivity.md``):
    non-string input, empty/oversized strings, whitespace/control
    characters, URLs/schemes (``http://...``), path-like values
    (containing ``/`` or ``\\``), embedded credentials (``user@host``),
    and anything that isn't a valid IP literal or RFC-1123-ish hostname.
    Never constructs a command string from ``host`` — this is pure
    string validation, nothing is ever shelled out.

    IP literals (IPv4 or IPv6) are accepted via :mod:`ipaddress` and
    bypass the hostname character rules entirely, since colons in an
    IPv6 literal are not valid hostname characters.
    """
    if not isinstance(host, str):
        raise HostValidationError(f"host must be a string, got {type(host).__name__}")
    if not host or len(host) > 253:
        raise HostValidationError("host must be a non-empty string of at most 253 characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in host):
        raise HostValidationError("host must not contain whitespace or control characters")

    try:
        ip_address(host)
        return host
    except ValueError:
        pass

    if "@" in host:
        raise HostValidationError("host must not contain embedded credentials")
    if "://" in host:
        raise HostValidationError("host must not contain a URL scheme")
    if "/" in host or "\\" in host:
        raise HostValidationError("host must not contain a path separator")
    if not _HOSTNAME_RE.match(host):
        raise HostValidationError("host must be a valid hostname or IP literal")
    return host


def validate_port(port: Any) -> int:
    """Validate ``port`` is a plain integer in ``[1, 65535]``.

    Rejects ``bool`` explicitly even though ``bool`` is an ``int``
    subclass in Python — a model sending JSON ``true``/``false`` is not
    a sane port value.
    """
    if isinstance(port, bool) or not isinstance(port, int):
        raise PortValidationError(f"port must be an integer, got {type(port).__name__}")
    if not (1 <= port <= 65535):
        raise PortValidationError(f"port must be between 1 and 65535, got {port}")
    return port


class ConnectStatus(str, Enum):
    """The current-state TCP observation vocabulary. Deliberately small
    and mapped deterministically from OS/socket outcomes — see
    :func:`_classify_os_error` and ``docs/network-tcp-connectivity.md``
    for exactly what each value does and does not prove.
    """

    CONNECTED = "connected"
    DNS_FAILURE = "dns_failure"
    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    NETWORK_UNREACHABLE = "network_unreachable"
    HOST_UNREACHABLE = "host_unreachable"
    CONNECTION_ERROR = "connection_error"
    BUDGET_EXCEEDED = "budget_exceeded"


# Overall-status precedence when multiple attempted addresses fail with
# different classifications — most diagnostically specific first, so the
# summary status is deterministic and never depends on which address
# happened to be attempted last. See docs/network-tcp-connectivity.md's
# "Failure precedence" section.
_STATUS_PRECEDENCE: tuple[ConnectStatus, ...] = (
    ConnectStatus.HOST_UNREACHABLE,
    ConnectStatus.NETWORK_UNREACHABLE,
    ConnectStatus.CONNECTION_REFUSED,
    ConnectStatus.TIMEOUT,
    ConnectStatus.CONNECTION_ERROR,
)


def _classify_os_error(exc: OSError) -> ConnectStatus:
    code = exc.errno
    if code == errno_module.ECONNREFUSED:
        return ConnectStatus.CONNECTION_REFUSED
    if code == errno_module.ENETUNREACH:
        return ConnectStatus.NETWORK_UNREACHABLE
    if code == errno_module.EHOSTUNREACH:
        return ConnectStatus.HOST_UNREACHABLE
    if code == errno_module.ETIMEDOUT:
        return ConnectStatus.TIMEOUT
    return ConnectStatus.CONNECTION_ERROR


def _bounded_message(text: str, *, max_chars: int = MAX_ATTEMPT_MESSAGE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


@dataclass(frozen=True)
class AddressAttempt:
    """One bounded, structured observation of a single connect attempt
    against one resolved address. Never carries a raw exception repr or
    other opaque implementation internals — ``message`` is bounded plain
    text, ``errno`` is the raw OS error number only when one was
    reported (safe, small, useful for cross-referencing OS docs)."""

    address: str
    address_family: str  # "ipv4" | "ipv6"
    status: str
    errno: int | None
    latency_ms: float
    message: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "address_family": self.address_family,
            "status": self.status,
            "errno": self.errno,
            "latency_ms": self.latency_ms,
            "message": self.message,
        }


@dataclass(frozen=True)
class TCPConnectResult:
    """The full outcome of one :func:`check_tcp_connect` call."""

    host: str
    port: int
    status: str
    connected: bool
    resolved_address: str | None
    address_family: str | None
    latency_ms: float | None
    attempts: list[AddressAttempt] = field(default_factory=list)
    truncated: bool = False
    """True when more resolved candidates existed than were actually
    attempted — either :data:`MAX_ADDRESSES_ATTEMPTED` was reached, or
    the deadline was exhausted before every candidate could be tried.
    Distinct from ``connected``/``status``: this describes the
    *completeness* of the attempt list, not the outcome."""
    observed_at: str = ""


def _resolve(host: str, port: int) -> list[tuple[int, int, int, str, tuple]]:
    """The one call that actually performs DNS resolution — isolated so
    tests can substitute it without a real resolver. See
    ``check_tcp_connect``'s docstring for the honest limitation this
    implies: plain ``socket.getaddrinfo()`` is synchronous and exposes
    no portable per-call timeout, so this can block past the caller's
    deadline. Mantis checks the deadline before and after calling this,
    not during — see docs/network-tcp-connectivity.md.
    """
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _connect(family: int, sockaddr: tuple, *, timeout_seconds: float) -> None:
    """The one call that actually opens a socket and connects — isolated
    so tests can substitute it without faking ``socket.socket``. Raises
    on any failure (``socket.timeout``/``OSError``); returns normally on
    success. Always closes the socket itself."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout_seconds)
        sock.connect(sockaddr)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _resolve_candidates(host: str, port: int) -> list[tuple[int, tuple]]:
    """Resolve ``host``/``port`` into a deduplicated, bounded, ordered
    list of ``(family, sockaddr)`` candidates. IPv4 and IPv6 results are
    both preserved, in whatever order the resolver returned them —
    Mantis never assumes IPv4-only. Deduplicates identical
    ``(family, sockaddr)`` pairs (a resolver can legitimately return the
    same address more than once) before the caller applies
    :data:`MAX_ADDRESSES_ATTEMPTED`.
    """
    infos = _resolve(host, port)
    seen: set[tuple[int, tuple]] = set()
    candidates: list[tuple[int, tuple]] = []
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        key = (family, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((family, sockaddr))
    return candidates


def _attempt_connect(
    family: int, sockaddr: tuple, *, timeout_seconds: float, clock: Callable[[], float]
) -> AddressAttempt:
    address = sockaddr[0]
    address_family = "ipv6" if family == socket.AF_INET6 else "ipv4"
    start = clock()
    status: ConnectStatus
    errno_value: int | None = None
    message: str | None = None
    try:
        _connect(family, sockaddr, timeout_seconds=timeout_seconds)
        status = ConnectStatus.CONNECTED
    except socket.timeout:
        status = ConnectStatus.TIMEOUT
        message = "connect timed out"
    except OSError as exc:
        status = _classify_os_error(exc)
        errno_value = exc.errno
        message = _bounded_message(str(exc))
    latency_ms = (clock() - start) * 1000.0
    return AddressAttempt(
        address=address,
        address_family=address_family,
        status=status.value,
        errno=errno_value,
        latency_ms=round(latency_ms, 3),
        message=message,
    )


def _classify_overall(attempts: list[AddressAttempt]) -> str:
    """Deterministic overall status from every failed attempt's
    classification, ranked by :data:`_STATUS_PRECEDENCE` — never simply
    "whichever address was attempted last". See
    ``docs/network-tcp-connectivity.md``'s "Failure precedence" section.
    """
    observed = {attempt.status for attempt in attempts}
    for status in _STATUS_PRECEDENCE:
        if status.value in observed:
            return status.value
    return ConnectStatus.CONNECTION_ERROR.value  # defensive; unreachable in practice


def check_tcp_connect(
    host: str,
    port: int,
    *,
    deadline: Deadline | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TCPConnectResult:
    """Attempt a bounded, deadline-aware TCP connection to ``host``:``port``.

    Callers must validate ``host``/``port`` first (see
    :func:`validate_host`/:func:`validate_port`) — this function trusts
    its arguments and does no shell/subprocess work of any kind.

    Deliberately never wraps connect attempts in
    :func:`mantis.reliability.retry_call` — a TCP probe is a
    current-state *observation*, not an idempotent API read that should
    be retried on transient failure the way an AWX HTTP GET is (see
    ``mantis.reliability``'s docstring and #15's contract). Attempting
    several *distinct* resolved addresses for the same host is not a
    retry loop — each is a different destination, tried once, in a
    bounded deterministic order; the same address is never attempted
    twice.

    Deadline handling: ``deadline`` is checked before resolution starts,
    and again before every individual connect attempt — never mid
    ``socket.getaddrinfo()`` or mid single ``connect()`` call, since
    neither can be portably preempted once started (see this module's
    docstring and ``docs/network-tcp-connectivity.md``'s honest
    limitation on synchronous DNS resolution). Each attempt's own socket
    timeout is capped at ``min(DEFAULT_CONNECT_TIMEOUT_SECONDS,
    deadline.remaining())`` so a single slow candidate can never itself
    exceed the caller's remaining budget.
    """
    observed_at = datetime.now(timezone.utc).isoformat()

    if deadline is not None and deadline.expired():
        return TCPConnectResult(
            host=host,
            port=port,
            status=ConnectStatus.BUDGET_EXCEEDED.value,
            connected=False,
            resolved_address=None,
            address_family=None,
            latency_ms=None,
            attempts=[],
            truncated=False,
            observed_at=observed_at,
        )

    try:
        candidates = _resolve_candidates(host, port)
    except socket.gaierror:
        return TCPConnectResult(
            host=host,
            port=port,
            status=ConnectStatus.DNS_FAILURE.value,
            connected=False,
            resolved_address=None,
            address_family=None,
            latency_ms=None,
            attempts=[],
            truncated=False,
            observed_at=observed_at,
        )

    if not candidates:
        # A resolver returning zero usable (AF_INET/AF_INET6) records is
        # functionally the same as a resolution failure for our purposes.
        return TCPConnectResult(
            host=host,
            port=port,
            status=ConnectStatus.DNS_FAILURE.value,
            connected=False,
            resolved_address=None,
            address_family=None,
            latency_ms=None,
            attempts=[],
            truncated=False,
            observed_at=observed_at,
        )

    truncated = len(candidates) > MAX_ADDRESSES_ATTEMPTED
    candidates = candidates[:MAX_ADDRESSES_ATTEMPTED]

    attempts: list[AddressAttempt] = []
    for family, sockaddr in candidates:
        if deadline is not None and deadline.expired():
            truncated = True
            break

        timeout_seconds = DEFAULT_CONNECT_TIMEOUT_SECONDS
        if deadline is not None:
            timeout_seconds = min(timeout_seconds, deadline.remaining())
            if timeout_seconds <= 0:
                truncated = True
                break

        attempt = _attempt_connect(family, sockaddr, timeout_seconds=timeout_seconds, clock=clock)
        attempts.append(attempt)

        if attempt.status == ConnectStatus.CONNECTED.value:
            return TCPConnectResult(
                host=host,
                port=port,
                status=ConnectStatus.CONNECTED.value,
                connected=True,
                resolved_address=attempt.address,
                address_family=attempt.address_family,
                latency_ms=attempt.latency_ms,
                attempts=attempts,
                truncated=truncated,
                observed_at=observed_at,
            )

    if not attempts:
        # Every candidate was skipped -- only possible when the deadline
        # was exhausted before even the first attempt could start.
        status = ConnectStatus.BUDGET_EXCEEDED.value
    else:
        status = _classify_overall(attempts)

    return TCPConnectResult(
        host=host,
        port=port,
        status=status,
        connected=False,
        resolved_address=None,
        address_family=None,
        latency_ms=None,
        attempts=attempts,
        truncated=truncated,
        observed_at=observed_at,
    )
