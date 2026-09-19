"""DNS resolution mechanics (#109): bounded, deadline-aware, multi-server
queries against a caller-selected, server-side-configured resolver
profile.

This module knows how to send one DNS query to one configured server,
classify the response/failure, and — when a profile names more than one
server — deterministically decide whether to try the next one. It has
no knowledge of agents, LLMs, tool schemas, resolver *aliases*, or
Mantis configuration — see ``mantis.tools.dns`` for the semantic,
LLM-facing layer that resolves an alias to a server list (via
``mantis.config.DNSConfig``) and shapes the result.

Uses ``dnspython`` (the standard, de facto Python DNS library) rather
than shelling out to ``dig``/``nslookup``/``host`` or hand-rolling a DNS
wire-format codec. Specifically ``dns.query.udp_with_fallback()`` — a
single low-level call that sends over UDP and transparently retries
over TCP if the response is truncated (the DNS protocol's own normal
fallback behavior), so this module never reimplements that. It never
uses ``dns.resolver.Resolver`` for the actual query: that class's own
multi-nameserver iteration bundles a mix of per-server outcomes into one
``NoNameservers`` exception, which is exactly the ordering/definitive-
answer control #109 needs *this* module to own explicitly (see
:func:`resolve_dns`'s docstring) — one query, one server, one raw
``dns.message.Message`` response with its ``rcode()`` inspected
directly is both simpler and more precisely controllable.

Reliability posture (#15): mirrors ``mantis.integrations.network``'s
current-state TCP probe exactly, for the same reason —
:func:`resolve_dns` is a current-state *observation*, not an idempotent
API read, so it is never wrapped in ``mantis.reliability.retry_call()``.
Trying several *distinct* configured servers for one profile is not a
retry loop, the same way trying several resolved TCP addresses isn't
(see ``check_tcp_connect``) — each server is a different destination,
tried at most once, in a bounded deterministic order.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Callable

import dns.exception
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype

from mantis.reliability import Deadline, IntegrationError, IntegrationErrorKind

SOURCE_SYSTEM = "dns"

SUPPORTED_RECORD_TYPES = ("A", "AAAA", "CNAME", "PTR")
"""#109's initial record-type allowlist, deliberately narrow. TXT, MX,
SRV, NS, SOA, ANY, AXFR, and anything not in this tuple are rejected as
invalid input before any query is attempted — see #109's non-goals."""

MAX_NAME_CHARS = 253
"""RFC 1035's own maximum encoded domain-name length. Applies to the
query name for A/AAAA/CNAME; PTR validates its input as an IP literal
instead (see :func:`validate_ptr_target`)."""

MAX_SERVERS_ATTEMPTED = 4
"""Hard cap on how many of a profile's configured servers are attempted
for one call, mirroring ``mantis.integrations.network.MAX_ADDRESSES_ATTEMPTED``
exactly — small and named deliberately, sized for interactive
troubleshooting, not exhaustively querying an operator's entire
resolver fleet. A profile is server-side configuration (never
model-inflated), so this bound is about predictable worst-case latency,
not an abuse defense."""

MAX_ANSWERS_RETURNED = 20
"""Cap on the total number of individual answer records returned
(across every RRset in the response, including any CNAME hops) — a
genuine defense: the *response* is external, resolver-controlled data,
which could otherwise hand the model an unbounded number of records."""

MAX_CNAME_CHAIN_DEPTH = 8
"""Cap on how many CNAME-typed records within one response's answer
section are collected before truncating — see #109's requirement to
bound CNAME chain depth explicitly rather than following/representing
an unbounded chain."""

DEFAULT_DNS_QUERY_TIMEOUT_SECONDS = 5.0
"""Ceiling on a single query attempt's timeout (covering the UDP
request and any TCP-fallback retry together — see
``dns.query.udp_with_fallback``'s own ``timeout``), used when no
:class:`~mantis.reliability.Deadline` is given, or further capped by
whatever remains of one when it is (see :func:`resolve_dns`) — mirrors
``mantis.integrations.network.DEFAULT_CONNECT_TIMEOUT_SECONDS``'s role
exactly."""

MAX_ATTEMPT_MESSAGE_CHARS = 200
"""Bound on each per-server attempt's diagnostic ``message`` — mirrors
``mantis.integrations.network.MAX_ATTEMPT_MESSAGE_CHARS``."""

DEFINITIVE_STATUSES = ("ok", "nxdomain", "no_data")
"""A response classified into one of these ends multi-server failover
immediately — the queried server gave a definitive, authoritative-for-
that-perspective answer about the name itself, so trying another server
in the *same* profile would risk exactly the "keep asking until you get
a preferred answer" anti-pattern #109 explicitly forbids. See
:func:`resolve_dns`'s docstring for the full per-outcome reasoning."""

PROTOCOL_FAILURE_STATUSES = ("servfail", "refused")
"""A response classified into one of these is a real DNS-protocol-level
answer (the server responded, just with a failure rcode) — informative
evidence in its own right, distinct from a transport failure where no
server ever responded at all. Permits trying the next configured
server (a *different* box might not share the same failure), but if
every attempted server ends this way, the highest-precedence one of
these is the final ``status`` — never raised as an error, since a real
response was received. See :data:`_PROTOCOL_FAILURE_PRECEDENCE`."""

_PROTOCOL_FAILURE_PRECEDENCE: tuple[str, ...] = ("refused", "servfail")
"""Most diagnostically specific first, mirroring
``mantis.integrations.network._STATUS_PRECEDENCE``'s philosophy:
REFUSED is a deliberate policy decision by the server; SERVFAIL is a
generic "something went wrong" report. Applied only when different
attempted servers in the same call disagree."""

_TRANSPORT_FAILURE_PRECEDENCE: tuple[str, ...] = (
    "connection_error",
    "malformed_response",
    "timeout",
)
"""Precedence used only when *every* attempted server failed at the
transport level (no server returned any DNS-protocol response at all)
— most diagnostically specific first, same philosophy as above. This
determines the :class:`DNSError` raised in that case; see
:func:`resolve_dns`."""

_TRANSPORT_FAILURE_KIND: dict[str, IntegrationErrorKind] = {
    "connection_error": IntegrationErrorKind.CONNECTION,
    "malformed_response": IntegrationErrorKind.SERVER_ERROR,
    "timeout": IntegrationErrorKind.TIMEOUT,
    "internal_error": IntegrationErrorKind.UNKNOWN,
}

_RECORD_TYPE_TO_RDATATYPE = {name: dns.rdatatype.from_text(name) for name in SUPPORTED_RECORD_TYPES}

_NAME_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,62})?"
_NAME_RE = re.compile(rf"^{_NAME_LABEL}(\.{_NAME_LABEL})*\.?$")
"""Same RFC-1123-ish allowlist shape as
``mantis.integrations.network._HOSTNAME_RE`` — dot-separated labels of
letters/digits/hyphens only, each <=63 chars, optional trailing dot.
Defined locally rather than imported: this module has no dependency on
``mantis.integrations.network``, matching every other Mantis
integration's convention of not cross-importing another integration's
private internals."""


class DNSNameValidationError(ValueError):
    """Raised by :func:`validate_dns_name` for a name that is not a
    plain, RFC-1123-ish domain name — a caller/model mistake, not a DNS
    observation. See ``mantis.tools.dns.dns_lookup`` for how this is
    safely surfaced (never by letting this exception propagate raw)."""


class PTRTargetValidationError(ValueError):
    """Raised by :func:`validate_ptr_target` for a ``PTR`` query's
    ``name`` that is not a valid IPv4/IPv6 literal."""


class RecordTypeValidationError(ValueError):
    """Raised by :func:`validate_record_type` for a record type outside
    :data:`SUPPORTED_RECORD_TYPES`."""


def validate_record_type(record_type: object) -> str:
    """Validate ``record_type`` against the #109 allowlist.

    Case-insensitive on input, normalized to uppercase on return. TXT,
    MX, SRV, NS, SOA, ANY, AXFR, and anything else outside
    :data:`SUPPORTED_RECORD_TYPES` are rejected — this alone is what
    keeps this tool from ever becoming a generic DNS query interface.
    """
    if not isinstance(record_type, str):
        raise RecordTypeValidationError(f"record_type must be a string, got {type(record_type).__name__}")
    normalized = record_type.strip().upper()
    if normalized not in SUPPORTED_RECORD_TYPES:
        raise RecordTypeValidationError(
            f"record_type must be one of {SUPPORTED_RECORD_TYPES}, got {record_type!r}"
        )
    return normalized


def validate_dns_name(name: object) -> str:
    """Validate ``name`` is a plain domain name, for the ``A``/``AAAA``/
    ``CNAME`` record types.

    Rejects (deliberately, mirroring
    ``mantis.integrations.network.validate_host``'s allowlist approach):
    non-string input, empty/oversized strings (:data:`MAX_NAME_CHARS`),
    whitespace/control characters, and anything else that isn't a
    dot-separated sequence of RFC-1123-ish labels — including embedded
    credentials, shell metacharacters, and path-like values. An
    IP-literal-shaped label sequence (e.g. ``"172.30.210.25"``) is
    syntactically a valid domain name and is not specially rejected
    here; querying one is harmless (almost always just an ``nxdomain``)
    and this function does not need to guess intent. See
    :func:`validate_ptr_target` for the actual IP-literal case, used
    only for ``PTR``. Never constructs a command string; nothing here
    is ever shelled out.
    """
    if not isinstance(name, str):
        raise DNSNameValidationError(f"name must be a string, got {type(name).__name__}")
    if not name or len(name) > MAX_NAME_CHARS:
        raise DNSNameValidationError(f"name must be a non-empty string of at most {MAX_NAME_CHARS} characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise DNSNameValidationError("name must not contain whitespace or control characters")
    if not _NAME_RE.match(name):
        raise DNSNameValidationError("name must be a valid domain name")
    return name


def validate_ptr_target(name: object) -> str:
    """Validate ``name`` is a plain IPv4/IPv6 literal, for the ``PTR``
    record type (a reverse lookup target, not a domain name)."""
    if not isinstance(name, str):
        raise PTRTargetValidationError(f"name must be a string, got {type(name).__name__}")
    try:
        ip_address(name)
    except ValueError as exc:
        raise PTRTargetValidationError(
            f"name must be a valid IPv4/IPv6 address literal for a PTR lookup, got {name!r}"
        ) from exc
    return name


class DNSError(IntegrationError):
    """Raised for a DNS transport/retrieval failure — timeout, a
    connection/transport-level error, a malformed/unusable response, or
    any other unexpected failure sending/receiving a query. Never raised
    for a real DNS-protocol-level outcome (``ok``/``nxdomain``/
    ``no_data``/``servfail``/``refused``, all returned as a normal
    result's ``status`` instead — see :func:`resolve_dns`): a DNS
    resolver *timing out* is a retrieval failure, never evidence that
    the target name does not exist, and must never be confused with
    ``nxdomain``.
    """

    def __init__(self, message: str, *, kind: IntegrationErrorKind) -> None:
        super().__init__(message, kind=kind, source_system=SOURCE_SYSTEM)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_message(text: str, *, max_chars: int = MAX_ATTEMPT_MESSAGE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


@dataclass(frozen=True)
class DNSAnswer:
    """One answer record — a direct pass-through of one resolver-reported
    value (never Mantis-computed interpretation)."""

    value: str
    ttl: int
    record_type: str

    def to_dict(self) -> dict:
        return {"value": self.value, "ttl": self.ttl, "type": self.record_type}


@dataclass(frozen=True)
class DNSServerAttempt:
    """One bounded, structured observation of a single query attempt
    against one configured server. Never carries a raw exception repr
    — ``message`` is bounded plain text, mirroring
    ``mantis.integrations.network.AddressAttempt``."""

    server: str
    outcome: str
    used_tcp: bool
    latency_ms: float
    message: str | None

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "outcome": self.outcome,
            "used_tcp": self.used_tcp,
            "latency_ms": self.latency_ms,
            "message": self.message,
        }


@dataclass(frozen=True)
class DNSLookupResult:
    """The full outcome of one :func:`resolve_dns` call."""

    name: str
    record_type: str
    status: str
    responding_resolver: str | None
    answers: list[DNSAnswer] = field(default_factory=list)
    attempts: list[DNSServerAttempt] = field(default_factory=list)
    truncated: bool = False
    observed_at: str = ""


def _send_query(
    qname: "dns.name.Name", rdtype: int, server: str, *, timeout_seconds: float, port: int = 53
) -> tuple["dns.message.Message", bool]:
    """The one call that actually sends a DNS query and waits for a
    response — isolated so tests can substitute it without a real
    resolver/socket, exactly mirroring
    ``mantis.integrations.network``'s ``_resolve``/``_connect`` seams.

    Uses ``dns.query.udp_with_fallback()`` — sends over UDP, and
    transparently retries over TCP if the response comes back truncated
    (the ``TC`` flag) — dnspython's own normal protocol fallback,
    reused here rather than reimplemented. Raises on any failure
    (``dns.exception.Timeout``, an ``OSError``/``ConnectionError`` from
    the socket layer, or another ``dns.exception.DNSException`` for a
    malformed/unusable response); returns ``(response, used_tcp)`` on
    success, whatever the response's ``rcode()`` is — classifying that
    rcode is :func:`_attempt_server`'s job, not this function's.

    ``port`` defaults to the standard DNS port (53) — every configured
    resolver profile server is queried on it. It exists as a parameter
    only so a test can point this real call at a local fake server on
    an ephemeral port (see ``tests/test_dns.py``'s UDP-truncation/TCP-
    fallback test); production code never passes anything but the
    default.
    """
    query = dns.message.make_query(qname, rdtype)
    return dns.query.udp_with_fallback(query, server, timeout=timeout_seconds, port=port)


def _classify_response(response: "dns.message.Message") -> str:
    """Map a received DNS response's rcode onto #109's semantic status
    vocabulary. ``NOERROR`` with an empty answer section is
    ``no_data`` — deliberately distinct from ``nxdomain`` (the name
    exists but has no records of the requested type, vs. the name does
    not exist at all)."""
    rcode = response.rcode()
    if rcode == dns.rcode.NOERROR:
        return "ok" if response.answer else "no_data"
    if rcode == dns.rcode.NXDOMAIN:
        return "nxdomain"
    if rcode == dns.rcode.SERVFAIL:
        return "servfail"
    if rcode == dns.rcode.REFUSED:
        return "refused"
    # Any other rcode (FORMERR, NOTIMP, YXDOMAIN, ...) is not one of
    # #109's supported semantic outcomes -- treated as a malformed/
    # unusable response rather than silently forced into one of the
    # five above, which would misrepresent what the server actually said.
    return "malformed_response"


def _extract_answers(response: "dns.message.Message") -> tuple[list[DNSAnswer], bool]:
    """Flatten every RRset in ``response.answer`` (in order -- a CNAME
    chain's hops followed by the final record, exactly as the resolver
    returned them) into a bounded list of :class:`DNSAnswer`, stopping
    at :data:`MAX_CNAME_CHAIN_DEPTH` CNAME-typed records or
    :data:`MAX_ANSWERS_RETURNED` total records, whichever comes first.
    Returns ``(answers, truncated)``.
    """
    answers: list[DNSAnswer] = []
    cname_count = 0
    truncated = False
    for rrset in response.answer:
        record_type = dns.rdatatype.to_text(rrset.rdtype)
        for rdata in rrset:
            if len(answers) >= MAX_ANSWERS_RETURNED:
                truncated = True
                break
            if record_type == "CNAME" and cname_count >= MAX_CNAME_CHAIN_DEPTH:
                truncated = True
                break
            value = str(rdata).rstrip(".") if record_type in ("CNAME", "PTR") else str(rdata)
            answers.append(DNSAnswer(value=value, ttl=rrset.ttl, record_type=record_type))
            if record_type == "CNAME":
                cname_count += 1
        else:
            continue
        break
    return answers, truncated


def _attempt_server(
    server: str, qname: "dns.name.Name", rdtype: int, *, timeout_seconds: float, clock: Callable[[], float]
) -> tuple[DNSServerAttempt, list[DNSAnswer], bool]:
    """Query one server and classify the outcome. Returns the bounded
    attempt evidence plus (only meaningful when ``outcome`` ends up
    ``"ok"``) the extracted, bounded answer list and its truncation
    flag."""
    start = clock()
    used_tcp = False
    message: str | None = None
    answers: list[DNSAnswer] = []
    answers_truncated = False
    try:
        response, used_tcp = _send_query(qname, rdtype, server, timeout_seconds=timeout_seconds)
        outcome = _classify_response(response)
        if outcome == "ok":
            answers, answers_truncated = _extract_answers(response)
    except dns.exception.Timeout:
        outcome = "timeout"
        message = "query timed out"
    except (OSError, ConnectionError) as exc:
        outcome = "connection_error"
        message = _bounded_message(f"{type(exc).__name__}: {exc}")
    except dns.exception.DNSException as exc:
        outcome = "malformed_response"
        message = _bounded_message(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 -- last-resort classification, never re-raised raw
        outcome = "internal_error"
        message = _bounded_message(f"{type(exc).__name__}: {exc}")
    latency_ms = (clock() - start) * 1000.0
    attempt = DNSServerAttempt(
        server=server,
        outcome=outcome,
        used_tcp=used_tcp,
        latency_ms=round(latency_ms, 3),
        message=message,
    )
    return attempt, answers, answers_truncated


def _select_final_status(attempts: list[DNSServerAttempt]) -> str:
    """Choose the deterministic overall status once every attempted
    server has failed non-definitively -- see
    :data:`_PROTOCOL_FAILURE_PRECEDENCE`/:data:`_TRANSPORT_FAILURE_PRECEDENCE`.
    A real protocol-level response (``servfail``/``refused``) always
    outranks a pure transport failure (``timeout``/``connection_error``/
    ``malformed_response``/``internal_error``): a server that actually
    answered, even with a failure rcode, tells you more than one that
    never responded at all."""
    observed = {attempt.outcome for attempt in attempts}
    for status in _PROTOCOL_FAILURE_PRECEDENCE:
        if status in observed:
            return status
    for status in _TRANSPORT_FAILURE_PRECEDENCE:
        if status in observed:
            return status
    return "internal_error"  # defensive; unreachable in practice


def resolve_dns(
    name: str,
    record_type: str,
    servers: tuple[str, ...],
    *,
    deadline: Deadline | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DNSLookupResult:
    """Query ``name``/``record_type`` against ``servers``, in order,
    applying #109's deterministic multi-server failover rule.

    Callers must validate ``name``/``record_type`` first (see
    :func:`validate_dns_name`/:func:`validate_ptr_target`/
    :func:`validate_record_type`) and resolve a resolver *alias* to
    this concrete ``servers`` tuple themselves (see
    ``mantis.tools.dns``) — this function trusts its arguments, knows
    nothing about aliases/config, and never shells out or does any DNS
    packet decoding of its own.

    **Ordering**: servers are tried strictly in the order given (the
    order an operator configured them in), up to
    :data:`MAX_SERVERS_ATTEMPTED`. The same server is never queried
    twice.

    **Which failures permit trying the next server / when a response is
    definitive**: each attempt is classified by :func:`_classify_response`
    or by the exception it raised. Outcomes ``ok``/``nxdomain``/
    ``no_data`` (:data:`DEFINITIVE_STATUSES`) are definitive for this
    profile/perspective — the server gave a real, authoritative-for-it
    answer about the name itself (whether the name resolves, doesn't
    exist, or exists with no records of this type). **Failover stops
    immediately** on any of these; the remaining configured servers are
    never queried, exactly the same way ``check_tcp_connect`` stops on
    the first successful connect. Trying another server after a
    definitive answer would risk exactly the "keep asking until you get
    a preferred answer" anti-pattern #109 forbids — one call queries
    one profile, never fans out to compare servers.

    Outcomes ``servfail``/``refused`` (:data:`PROTOCOL_FAILURE_STATUSES`)
    and the transport failures ``timeout``/``connection_error``/
    ``malformed_response``/``internal_error`` all permit trying the
    *next* configured server — none of them says anything about
    whether the name exists; they only say this particular server
    couldn't or wouldn't answer *right now*, and a different server
    configured for the same perspective might.

    **Whether a successful answer stops failover**: yes, immediately
    (see above) — this also means ``ok`` never triggers comparing
    servers "just to see."

    **Which server actually answered**: ``DNSLookupResult.responding_resolver``
    is the specific server that produced the final result — set for
    every definitive outcome and for a protocol-failure outcome
    (a real response was received from *that* server), left ``None``
    only when every attempted server failed at the transport level (no
    server ever produced a DNS-protocol response at all).

    **If every attempted server ends in a protocol failure** (``servfail``/
    ``refused``, possibly mixed with transport failures), the highest-
    precedence protocol failure across all attempts is the final
    ``status`` — still a normal, successful return, since a real DNS
    response was received; see :func:`_select_final_status`.

    **If no attempted server ever produced a DNS-protocol response at
    all** (only ``timeout``/``connection_error``/``malformed_response``/
    ``internal_error`` observed), this raises :class:`DNSError` instead
    of returning a result — per #109's requirement that transport/
    retrieval failures stay separate from the semantic status
    vocabulary through the existing #15 error model, exactly like every
    other Mantis integration's transport failures (see
    ``mantis.integrations.prometheus``/``.loki``/``.kubernetes``).

    **Deadline handling**: mirrors ``check_tcp_connect`` exactly. If
    ``deadline`` is already expired before the first attempt, or
    expires between attempts (stopping remaining configured servers
    from being tried), the result is ``status="budget_exceeded"`` — a
    scheduling fact, not a DNS observation, and never a
    precedence-derived status from an incomplete set of observations
    (a later, untried server might have answered definitively). Each
    attempt's own query timeout is
    ``min(DEFAULT_DNS_QUERY_TIMEOUT_SECONDS, deadline.remaining())``.
    """
    observed_at = _utc_now_iso()

    if deadline is not None and deadline.expired():
        return DNSLookupResult(
            name=name,
            record_type=record_type,
            status="budget_exceeded",
            responding_resolver=None,
            observed_at=observed_at,
        )

    qname = dns.name.from_text(name)
    rdtype = _RECORD_TYPE_TO_RDATATYPE[record_type]

    truncated = len(servers) > MAX_SERVERS_ATTEMPTED
    attempted_servers = servers[:MAX_SERVERS_ATTEMPTED]

    attempts: list[DNSServerAttempt] = []
    deadline_stopped = False
    for server in attempted_servers:
        if deadline is not None and deadline.expired():
            truncated = True
            deadline_stopped = True
            break

        timeout_seconds = DEFAULT_DNS_QUERY_TIMEOUT_SECONDS
        if deadline is not None:
            timeout_seconds = min(timeout_seconds, deadline.remaining())
            if timeout_seconds <= 0:
                truncated = True
                deadline_stopped = True
                break

        attempt, answers, answers_truncated = _attempt_server(
            server, qname, rdtype, timeout_seconds=timeout_seconds, clock=clock
        )
        attempts.append(attempt)

        if attempt.outcome in DEFINITIVE_STATUSES:
            return DNSLookupResult(
                name=name,
                record_type=record_type,
                status=attempt.outcome,
                responding_resolver=server,
                answers=answers,
                attempts=attempts,
                truncated=truncated or answers_truncated,
                observed_at=observed_at,
            )
        # servfail/refused/timeout/connection_error/malformed_response/
        # internal_error: none of these is evidence about the name
        # itself -- try the next configured server (see docstring).

    if not attempts or deadline_stopped:
        # The deadline -- not the servers -- is why the bounded set
        # wasn't fully evaluated. A later, untried server might have
        # answered definitively; reporting a precedence-derived status
        # from an incomplete set would overstate what's actually known.
        return DNSLookupResult(
            name=name,
            record_type=record_type,
            status="budget_exceeded",
            responding_resolver=None,
            attempts=attempts,
            truncated=truncated,
            observed_at=observed_at,
        )

    final_status = _select_final_status(attempts)
    if final_status in PROTOCOL_FAILURE_STATUSES:
        responding_server = next(
            (a.server for a in reversed(attempts) if a.outcome == final_status), None
        )
        return DNSLookupResult(
            name=name,
            record_type=record_type,
            status=final_status,
            responding_resolver=responding_server,
            attempts=attempts,
            truncated=truncated,
            observed_at=observed_at,
        )

    kind = _TRANSPORT_FAILURE_KIND[final_status]
    last_message = next(
        (a.message for a in reversed(attempts) if a.outcome == final_status), final_status
    )
    raise DNSError(
        f"Every configured server failed to answer ({final_status}): {last_message}",
        kind=kind,
    )
