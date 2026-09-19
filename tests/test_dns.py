"""Tests for mantis.integrations.dns: name/record-type validation, the
low-level query seam, and the bounded, deadline-aware multi-server
failover loop (#109).

All server responses are mocked via monkeypatching the module's own
`_send_query` seam (mirroring tests/test_network.py's `_resolve`/
`_connect` convention) -- no real DNS, no real public resolver, no
external network access. `test_send_query_*` is the deliberate
exception: it exercises the real `dns.query.udp_with_fallback()` call
against real local UDP/TCP sockets on ephemeral ports, to prove UDP
truncation genuinely triggers dnspython's own TCP fallback -- the one
thing mocking `_send_query` itself could never prove.
"""

from __future__ import annotations

import socket
import struct
import threading

import dns.rcode
import dns.rdatatype
import pytest

from mantis.integrations import dns as dns_integration
from mantis.integrations.dns import (
    DEFINITIVE_STATUSES,
    MAX_ANSWERS_RETURNED,
    MAX_CNAME_CHAIN_DEPTH,
    MAX_SERVERS_ATTEMPTED,
    PROTOCOL_FAILURE_STATUSES,
    DNSError,
    DNSNameValidationError,
    PTRTargetValidationError,
    RecordTypeValidationError,
    resolve_dns,
    validate_dns_name,
    validate_ptr_target,
    validate_record_type,
)
from mantis.reliability import Deadline, IntegrationErrorKind


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Fakes for _send_query's return shape -- only the attributes
# _classify_response/_extract_answers actually touch.
# ---------------------------------------------------------------------------


class _FakeRdata:
    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


class _FakeRRset:
    def __init__(self, record_type: str, ttl: int, values: list[str]) -> None:
        self.rdtype = dns.rdatatype.from_text(record_type)
        self.ttl = ttl
        self._values = [_FakeRdata(v) for v in values]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class _FakeMessage:
    def __init__(self, rcode: int, answer: list[_FakeRRset] | None = None) -> None:
        self._rcode = rcode
        self.answer = answer or []

    def rcode(self) -> int:
        return self._rcode


def _ok(record_type: str, values: list[str], ttl: int = 300) -> _FakeMessage:
    return _FakeMessage(dns.rcode.NOERROR, answer=[_FakeRRset(record_type, ttl, values)])


def _rcode_only(rcode: int) -> _FakeMessage:
    return _FakeMessage(rcode)


def _patch_send_query(monkeypatch, responses: dict) -> list[str]:
    """Patch ``_send_query`` so each configured server (a key in
    ``responses``) returns/raises whatever that entry says. An entry
    may be a ``_FakeMessage`` (returned as ``(message, False)``), an
    ``Exception`` instance (raised), or a zero-arg callable (called for
    its return/raise -- used when a test needs to advance a
    ``FakeClock`` mid-call). Returns the list of servers actually
    queried, in order, for tests that want to assert on ordering
    without relying on ``attempts`` alone."""
    called: list[str] = []

    def fake_send_query(qname, rdtype, server, *, timeout_seconds):
        called.append(server)
        outcome = responses[server]
        if callable(outcome) and not isinstance(outcome, (_FakeMessage, Exception)):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, False

    monkeypatch.setattr(dns_integration, "_send_query", fake_send_query)
    return called


# ---------------------------------------------------------------------------
# validate_record_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record_type", ["A", "AAAA", "CNAME", "PTR", "a", "aaaa", "cname", "ptr"])
def test_validate_record_type_accepts_supported_types(record_type):
    assert validate_record_type(record_type) == record_type.upper()


@pytest.mark.parametrize(
    "record_type", ["TXT", "MX", "SRV", "NS", "SOA", "ANY", "AXFR", "", "bogus", 123, None]
)
def test_validate_record_type_rejects_unsupported_types(record_type):
    with pytest.raises(RecordTypeValidationError):
        validate_record_type(record_type)


# ---------------------------------------------------------------------------
# validate_dns_name (A/AAAA/CNAME)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["example.com", "service.example.com", "a", "host-03.internal", "example.com."]
)
def test_validate_dns_name_accepts_valid_names(name):
    assert validate_dns_name(name) == name


@pytest.mark.parametrize(
    "name,reason",
    [
        ("", "empty"),
        ("a" * 254, "too long"),
        ("host 03", "whitespace"),
        ("host\n03", "control"),
        ("user:pass@host03", "credentials"),
        ("host`whoami`", "shell metacharacters"),
        (123, "not a string"),
        (None, "not a string"),
    ],
)
def test_validate_dns_name_rejects_malformed_names(name, reason):
    with pytest.raises(DNSNameValidationError):
        validate_dns_name(name)


# ---------------------------------------------------------------------------
# validate_ptr_target (PTR)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["172.30.210.25", "::1", "2001:db8::1"])
def test_validate_ptr_target_accepts_ip_literals(target):
    assert validate_ptr_target(target) == target


@pytest.mark.parametrize("target", ["example.com", "not-an-ip", "", 123, None])
def test_validate_ptr_target_rejects_non_ip_values(target):
    with pytest.raises(PTRTargetValidationError):
        validate_ptr_target(target)


# ---------------------------------------------------------------------------
# Happy path: A / AAAA / CNAME / PTR, multiple answers, TTL preservation
# ---------------------------------------------------------------------------


def test_a_record_lookup(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _ok("A", ["172.30.210.25"], ttl=300)})

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",))

    assert result.status == "ok"
    assert result.responding_resolver == "10.0.0.1"
    assert [a.value for a in result.answers] == ["172.30.210.25"]
    assert result.answers[0].ttl == 300
    assert result.answers[0].record_type == "A"


def test_aaaa_record_lookup(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _ok("AAAA", ["2001:db8::1"], ttl=120)})

    result = resolve_dns("service.example.com", "AAAA", ("10.0.0.1",))

    assert result.status == "ok"
    assert result.answers[0].value == "2001:db8::1"
    assert result.answers[0].record_type == "AAAA"


def test_cname_record_lookup(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _ok("CNAME", ["lb.example.com."], ttl=300)})

    result = resolve_dns("service.example.com", "CNAME", ("10.0.0.1",))

    assert result.status == "ok"
    # Trailing dot stripped for a readable, un-surprising value.
    assert result.answers[0].value == "lb.example.com"
    assert result.answers[0].record_type == "CNAME"


def test_ptr_record_lookup(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _ok("PTR", ["host25.example.com."], ttl=300)})

    result = resolve_dns("25.210.30.172.in-addr.arpa", "PTR", ("10.0.0.1",))

    assert result.status == "ok"
    assert result.answers[0].value == "host25.example.com"


def test_multiple_answers_are_all_returned_in_order(monkeypatch):
    _patch_send_query(
        monkeypatch, {"10.0.0.1": _ok("A", ["172.30.205.10", "172.30.205.11", "172.30.205.12"])}
    )

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",))

    assert [a.value for a in result.answers] == ["172.30.205.10", "172.30.205.11", "172.30.205.12"]
    assert result.truncated is False


def test_ttl_is_preserved_per_answer(monkeypatch):
    fake_msg = _FakeMessage(
        dns.rcode.NOERROR,
        answer=[
            _FakeRRset("A", 60, ["172.30.205.10"]),
            _FakeRRset("A", 300, ["172.30.205.11"]),
        ],
    )
    _patch_send_query(monkeypatch, {"10.0.0.1": fake_msg})

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",))

    assert [a.ttl for a in result.answers] == [60, 300]


# ---------------------------------------------------------------------------
# Semantic status correctness: ok/nxdomain/no_data/servfail/refused stay
# distinguishable, and never conflated with a transport failure.
# ---------------------------------------------------------------------------


def test_nxdomain_is_distinct_from_no_data(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _rcode_only(dns.rcode.NXDOMAIN)})
    result = resolve_dns("nope.example.com", "A", ("10.0.0.1",))
    assert result.status == "nxdomain"
    assert result.answers == []


def test_noerror_with_empty_answer_is_no_data_not_nxdomain(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _rcode_only(dns.rcode.NOERROR)})
    result = resolve_dns("example.com", "AAAA", ("10.0.0.1",))
    assert result.status == "no_data"
    assert result.status != "nxdomain"


def test_servfail_is_a_normal_status_not_raised(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _rcode_only(dns.rcode.SERVFAIL)})
    result = resolve_dns("example.com", "A", ("10.0.0.1",))
    assert result.status == "servfail"
    assert result.status != "not_found"


def test_refused_is_a_normal_status_not_raised(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _rcode_only(dns.rcode.REFUSED)})
    result = resolve_dns("example.com", "A", ("10.0.0.1",))
    assert result.status == "refused"
    assert result.status != "not_found"


def test_timeout_is_never_reported_as_nxdomain(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": dns_integration.dns.exception.Timeout()})
    with pytest.raises(DNSError) as exc_info:
        resolve_dns("example.com", "A", ("10.0.0.1",))
    assert exc_info.value.kind == IntegrationErrorKind.TIMEOUT
    # The whole point: a resolver timing out is a retrieval failure,
    # never evidence the name doesn't exist.
    assert "nxdomain" not in str(exc_info.value).lower()


def test_all_five_semantic_statuses_are_mutually_distinguishable():
    assert len(set(DEFINITIVE_STATUSES) | set(PROTOCOL_FAILURE_STATUSES)) == 5
    assert set(DEFINITIVE_STATUSES) == {"ok", "nxdomain", "no_data"}
    assert set(PROTOCOL_FAILURE_STATUSES) == {"servfail", "refused"}


# ---------------------------------------------------------------------------
# Transport/retrieval failures: raised via the existing #15 error model,
# never encoded as a semantic status value.
# ---------------------------------------------------------------------------


def test_connection_error_raises_dns_error_with_connection_kind(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": ConnectionRefusedError("refused")})
    with pytest.raises(DNSError) as exc_info:
        resolve_dns("example.com", "A", ("10.0.0.1",))
    assert exc_info.value.kind == IntegrationErrorKind.CONNECTION


def test_malformed_response_raises_dns_error(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": dns_integration.dns.exception.FormError("bad wire format")})
    with pytest.raises(DNSError) as exc_info:
        resolve_dns("example.com", "A", ("10.0.0.1",))
    assert exc_info.value.kind == IntegrationErrorKind.SERVER_ERROR


def test_unexpected_internal_exception_raises_dns_error_not_a_raw_traceback(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": RuntimeError("something truly unexpected")})
    with pytest.raises(DNSError) as exc_info:
        resolve_dns("example.com", "A", ("10.0.0.1",))
    assert exc_info.value.kind == IntegrationErrorKind.UNKNOWN


def test_dns_error_message_is_bounded(monkeypatch):
    huge = "z" * 10_000
    _patch_send_query(monkeypatch, {"10.0.0.1": ConnectionRefusedError(huge)})
    with pytest.raises(DNSError) as exc_info:
        resolve_dns("example.com", "A", ("10.0.0.1",))
    assert len(str(exc_info.value)) < 1000


# ---------------------------------------------------------------------------
# Answer/CNAME-chain bounding, truncation correctness
# ---------------------------------------------------------------------------


def test_answer_count_is_bounded_and_truncation_is_flagged(monkeypatch):
    many_values = [f"172.30.205.{i}" for i in range(MAX_ANSWERS_RETURNED + 5)]
    _patch_send_query(monkeypatch, {"10.0.0.1": _ok("A", many_values)})

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",))

    assert len(result.answers) == MAX_ANSWERS_RETURNED
    assert result.truncated is True


def test_answer_count_under_the_cap_is_not_flagged_truncated(monkeypatch):
    _patch_send_query(monkeypatch, {"10.0.0.1": _ok("A", ["172.30.205.10"])})

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",))

    assert result.truncated is False


def test_cname_chain_depth_is_bounded(monkeypatch):
    # A pathological chain of CNAME hops with no final address record --
    # must stop at MAX_CNAME_CHAIN_DEPTH and flag truncation, never
    # silently represent an unbounded chain.
    rrsets = [
        _FakeRRset("CNAME", 300, [f"hop{i}.example.com."]) for i in range(MAX_CNAME_CHAIN_DEPTH + 3)
    ]
    fake_msg = _FakeMessage(dns.rcode.NOERROR, answer=rrsets)
    _patch_send_query(monkeypatch, {"10.0.0.1": fake_msg})

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",))

    assert len(result.answers) == MAX_CNAME_CHAIN_DEPTH
    assert all(a.record_type == "CNAME" for a in result.answers)
    assert result.truncated is True


def test_cname_chain_followed_by_final_answer_within_bounds(monkeypatch):
    rrsets = [
        _FakeRRset("CNAME", 300, ["lb.example.com."]),
        _FakeRRset("A", 60, ["172.30.210.25"]),
    ]
    fake_msg = _FakeMessage(dns.rcode.NOERROR, answer=rrsets)
    _patch_send_query(monkeypatch, {"10.0.0.1": fake_msg})

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",))

    assert [a.record_type for a in result.answers] == ["CNAME", "A"]
    assert result.answers[-1].value == "172.30.210.25"
    assert result.truncated is False


# ---------------------------------------------------------------------------
# Deterministic multi-server failover (#109's central requirement)
# ---------------------------------------------------------------------------


def test_servers_are_tried_in_configured_order(monkeypatch):
    called = _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": _rcode_only(dns.rcode.SERVFAIL),
            "10.0.0.2": _ok("A", ["172.30.210.25"]),
        },
    )

    resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"))

    assert called == ["10.0.0.1", "10.0.0.2"]


def test_successful_answer_stops_failover_the_second_server_is_never_queried(monkeypatch):
    called = _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": _ok("A", ["172.30.210.25"]),
            "10.0.0.2": _ok("A", ["172.30.210.99"]),
        },
    )

    result = resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"))

    assert called == ["10.0.0.1"]
    assert result.responding_resolver == "10.0.0.1"
    assert result.answers[0].value == "172.30.210.25"


@pytest.mark.parametrize("definitive_rcode,expected_status", [
    (dns.rcode.NXDOMAIN, "nxdomain"),
    (dns.rcode.NOERROR, "no_data"),
])
def test_definitive_outcome_stops_failover_never_asks_a_different_server(monkeypatch, definitive_rcode, expected_status):
    # This is the exact anti-pattern #109 forbids: never keep asking
    # other servers in the same profile "to compare" once one has given
    # a definitive answer about the name itself.
    called = _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": _rcode_only(definitive_rcode),
            "10.0.0.2": _ok("A", ["172.30.210.25"]),
        },
    )

    result = resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"))

    assert called == ["10.0.0.1"]
    assert result.status == expected_status


@pytest.mark.parametrize(
    "first_outcome_factory",
    [
        lambda: _rcode_only(dns.rcode.SERVFAIL),
        lambda: _rcode_only(dns.rcode.REFUSED),
        lambda: dns_integration.dns.exception.Timeout(),
        lambda: ConnectionRefusedError("refused"),
        lambda: dns_integration.dns.exception.FormError("bad wire"),
    ],
)
def test_non_definitive_failures_permit_trying_the_next_server(monkeypatch, first_outcome_factory):
    called = _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": first_outcome_factory(),
            "10.0.0.2": _ok("A", ["172.30.210.25"]),
        },
    )

    result = resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"))

    assert called == ["10.0.0.1", "10.0.0.2"]
    assert result.status == "ok"
    assert result.responding_resolver == "10.0.0.2"


def test_responding_resolver_names_the_server_that_actually_answered(monkeypatch):
    _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": _rcode_only(dns.rcode.SERVFAIL),
            "10.0.0.2": _rcode_only(dns.rcode.SERVFAIL),
            "10.0.0.3": _ok("A", ["172.30.210.25"]),
        },
    )

    result = resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2", "10.0.0.3"))

    assert result.responding_resolver == "10.0.0.3"


def test_exhausting_all_servers_with_only_protocol_failures_returns_highest_precedence(monkeypatch):
    _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": _rcode_only(dns.rcode.SERVFAIL),
            "10.0.0.2": _rcode_only(dns.rcode.REFUSED),
        },
    )

    result = resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"))

    # refused outranks servfail -- a deliberate, most-specific-first
    # precedence, mirroring mantis.integrations.network's philosophy.
    assert result.status == "refused"
    assert result.responding_resolver == "10.0.0.2"


def test_exhausting_all_servers_with_only_protocol_failures_precedence_is_order_independent(monkeypatch):
    _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": _rcode_only(dns.rcode.REFUSED),
            "10.0.0.2": _rcode_only(dns.rcode.SERVFAIL),
        },
    )

    result = resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"))

    assert result.status == "refused"


def test_exhausting_all_servers_with_only_transport_failures_raises_by_precedence(monkeypatch):
    _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": dns_integration.dns.exception.Timeout(),
            "10.0.0.2": ConnectionRefusedError("refused"),
        },
    )

    with pytest.raises(DNSError) as exc_info:
        resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"))

    # connection_error outranks timeout -- a concrete OS-observed fact
    # beats a generic "no response" outcome.
    assert exc_info.value.kind == IntegrationErrorKind.CONNECTION


def test_a_protocol_failure_outranks_a_transport_failure_when_both_are_observed(monkeypatch):
    _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": dns_integration.dns.exception.Timeout(),
            "10.0.0.2": _rcode_only(dns.rcode.SERVFAIL),
        },
    )

    result = resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"))

    # A real response (even a failure rcode) is more informative than
    # no response at all -- never raised in this case.
    assert result.status == "servfail"


def test_do_not_query_all_servers_once_a_definitive_answer_exists(monkeypatch):
    # Explicit regression guard for #109's "do not query all servers
    # simply to compare them" rule, with three configured servers.
    called = _patch_send_query(
        monkeypatch,
        {
            "10.0.0.1": _ok("A", ["172.30.210.25"]),
            "10.0.0.2": _ok("A", ["203.0.113.44"]),
            "10.0.0.3": _ok("A", ["198.51.100.1"]),
        },
    )

    resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2", "10.0.0.3"))

    assert called == ["10.0.0.1"]


def test_servers_beyond_the_cap_are_never_attempted(monkeypatch):
    servers = tuple(f"10.0.0.{i}" for i in range(1, MAX_SERVERS_ATTEMPTED + 3))
    responses = {s: _rcode_only(dns.rcode.SERVFAIL) for s in servers}
    called = _patch_send_query(monkeypatch, responses)

    result = resolve_dns("service.example.com", "A", servers)

    assert len(called) == MAX_SERVERS_ATTEMPTED
    assert result.truncated is True


# ---------------------------------------------------------------------------
# Deadline semantics (#15) -- mirrors tests/test_network.py's pattern
# exactly.
# ---------------------------------------------------------------------------


def test_deadline_already_expired_reports_budget_exceeded_with_no_attempts(monkeypatch):
    called = _patch_send_query(monkeypatch, {"10.0.0.1": _ok("A", ["172.30.210.25"])})
    clock = FakeClock()
    deadline = Deadline.after(-1.0, clock=clock)

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",), deadline=deadline, clock=clock)

    assert result.status == "budget_exceeded"
    assert result.attempts == []
    assert called == []


def test_deadline_exhausted_after_a_protocol_failure_reports_budget_exceeded_not_the_failure(monkeypatch):
    clock = FakeClock()
    deadline = Deadline.after(1.0, clock=clock)

    def first_server():
        clock.advance(10.0)  # consumes the whole remaining deadline
        return _rcode_only(dns.rcode.SERVFAIL)

    _patch_send_query(monkeypatch, {"10.0.0.1": first_server, "10.0.0.2": _ok("A", ["172.30.210.25"])})

    result = resolve_dns("service.example.com", "A", ("10.0.0.1", "10.0.0.2"), deadline=deadline, clock=clock)

    assert result.status == "budget_exceeded"
    assert len(result.attempts) == 1
    assert result.attempts[0].outcome == "servfail"
    assert result.truncated is True


def test_deadline_with_room_to_spare_uses_the_normal_precedence(monkeypatch):
    clock = FakeClock()
    deadline = Deadline.after(300.0, clock=clock)
    _patch_send_query(monkeypatch, {"10.0.0.1": _rcode_only(dns.rcode.SERVFAIL)})

    result = resolve_dns("service.example.com", "A", ("10.0.0.1",), deadline=deadline, clock=clock)

    assert result.status == "servfail"


# ---------------------------------------------------------------------------
# Real UDP/TCP fallback: the one test exercising the actual
# dns.query.udp_with_fallback() call against real local sockets.
# ---------------------------------------------------------------------------


def _fake_udp_server(port: int, response_bytes_builder) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))

    def serve() -> None:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except OSError:
                return
            sock.sendto(response_bytes_builder(data), addr)

    threading.Thread(target=serve, daemon=True).start()
    return sock


def _fake_tcp_server(port: int, response_bytes_builder) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)

    def serve() -> None:
        try:
            conn, _addr = sock.accept()
        except OSError:
            return
        try:
            length_bytes = conn.recv(2)
            length = struct.unpack("!H", length_bytes)[0]
            data = conn.recv(length)
            response = response_bytes_builder(data)
            conn.sendall(struct.pack("!H", len(response)) + response)
        finally:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock


def test_send_query_falls_back_to_tcp_when_udp_response_is_truncated():
    import dns.flags
    import dns.message
    import dns.name
    import dns.rrset

    # dns.query.udp_with_fallback() uses the *same* port number for both
    # the UDP request and the TCP fallback (standard DNS convention),
    # so both fake servers below must bind that one port number --
    # briefly bind-and-close a UDP socket to discover a free one.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    def truncated_udp_response(wire: bytes) -> bytes:
        req = dns.message.from_wire(wire)
        resp = dns.message.make_response(req)
        resp.flags |= dns.flags.TC
        return resp.to_wire()

    final_rrset = dns.rrset.from_text("service.example.com.", 300, "IN", "A", "172.30.210.25")

    def tcp_response(wire: bytes) -> bytes:
        req = dns.message.from_wire(wire)
        resp = dns.message.make_response(req)
        resp.answer.append(final_rrset)
        return resp.to_wire()

    udp = _fake_udp_server(port, truncated_udp_response)
    tcp = _fake_tcp_server(port, tcp_response)
    try:
        qname = dns.name.from_text("service.example.com")
        response, used_tcp = dns_integration._send_query(
            qname, dns.rdatatype.A, "127.0.0.1", timeout_seconds=3, port=port
        )
    finally:
        udp.close()
        tcp.close()

    assert used_tcp is True
    assert len(response.answer) == 1
    assert str(response.answer[0][0]) == "172.30.210.25"


def test_send_query_times_out_against_an_unresponsive_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        import dns.exception
        import dns.name

        qname = dns.name.from_text("service.example.com")
        with pytest.raises(dns.exception.Timeout):
            dns_integration._send_query(qname, dns.rdatatype.A, "127.0.0.1", timeout_seconds=1, port=port)
    finally:
        sock.close()
