"""Tests for mantis.integrations.tls: certificate parsing, hostname
matching, and the two-handshake inspect-vs-verify mechanism (#111).

Uses real local TLS servers on ephemeral ports with in-memory generated
certificates (tests/_tls_fixtures.py) — never a real public
certificate (no wall-clock dependency), never live Internet access.
"""

from __future__ import annotations

import datetime
import socket

import pytest

from mantis.integrations.tls import (
    MAX_SAN_ENTRIES,
    CertificateParseError,
    TLSError,
    _parse_certificate,
    inspect_tls,
)
from mantis.reliability import Deadline, IntegrationErrorKind
from tests._tls_fixtures import (
    NonTLSTCPServer,
    SilentTCPServer,
    TLSTestServer,
    make_ca,
    make_leaf,
    write_ca_file,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Valid, trusted certificate
# ---------------------------------------------------------------------------


def test_valid_trusted_certificate_reports_status_valid():
    ca_cert, ca_key = make_ca()
    ca_file = write_ca_file(ca_cert)
    cert, key = make_leaf("trusted.example.com", san_dns=["trusted.example.com"], signer=(ca_cert, ca_key))

    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "trusted.example.com", ca_file=ca_file)

    assert result.verification.chain_trusted is True
    assert result.verification.hostname_matches is True
    assert result.verification.time_valid is True
    assert result.verification.status == "valid"
    assert result.certificate.san_dns == ["trusted.example.com"]
    assert result.connected_address == "127.0.0.1"
    assert result.tls_version.startswith("TLSv1")


def test_certificate_metadata_fields_are_populated():
    cert, key = make_leaf("meta.example.com", san_dns=["meta.example.com"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "meta.example.com")

    c = result.certificate
    assert c.subject == "CN=meta.example.com"
    assert c.issuer == "CN=meta.example.com"
    assert c.serial_number.isdigit()
    assert len(c.sha256_fingerprint) == 64  # hex-encoded SHA-256
    assert c.not_before
    assert c.not_after


# ---------------------------------------------------------------------------
# Critical requirement: inspect != verify -- these must all still
# return full certificate metadata, never "no certificate available."
# ---------------------------------------------------------------------------


def test_self_signed_certificate_still_returns_metadata():
    cert, key = make_leaf("selfsigned.example.com", san_dns=["selfsigned.example.com"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "selfsigned.example.com")

    assert result.certificate.subject == "CN=selfsigned.example.com"
    assert result.verification.chain_trusted is False
    assert result.verification.hostname_matches is True
    assert result.verification.time_valid is True
    assert result.verification.status == "untrusted"


def test_untrusted_issuer_certificate_still_returns_metadata():
    # A different, unrelated CA than the one the verification handshake
    # is told to trust -- same "chain_trusted=False, metadata still
    # present" shape as self-signed.
    unrelated_ca_cert, unrelated_ca_key = make_ca()
    other_ca_cert, _other_ca_key = make_ca()
    other_ca_file = write_ca_file(other_ca_cert)
    cert, key = make_leaf(
        "untrusted-issuer.example.com",
        san_dns=["untrusted-issuer.example.com"],
        signer=(unrelated_ca_cert, unrelated_ca_key),
    )
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "untrusted-issuer.example.com", ca_file=other_ca_file)

    assert result.certificate.issuer == "CN=Mantis Test CA"
    assert result.verification.chain_trusted is False
    assert result.verification.status == "untrusted"


def test_expired_certificate_still_returns_metadata():
    ca_cert, ca_key = make_ca()
    ca_file = write_ca_file(ca_cert)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert, key = make_leaf(
        "expired.example.com",
        san_dns=["expired.example.com"],
        signer=(ca_cert, ca_key),
        not_before=now - datetime.timedelta(days=60),
        not_after=now - datetime.timedelta(days=30),
    )
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "expired.example.com", ca_file=ca_file)

    assert result.certificate.subject == "CN=expired.example.com"
    assert result.verification.time_valid is False
    assert result.verification.status == "expired_or_not_yet_valid"


def test_not_yet_valid_certificate_still_returns_metadata():
    ca_cert, ca_key = make_ca()
    ca_file = write_ca_file(ca_cert)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert, key = make_leaf(
        "future.example.com",
        san_dns=["future.example.com"],
        signer=(ca_cert, ca_key),
        not_before=now + datetime.timedelta(days=30),
        not_after=now + datetime.timedelta(days=60),
    )
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "future.example.com", ca_file=ca_file)

    assert result.certificate.subject == "CN=future.example.com"
    assert result.verification.time_valid is False
    assert result.verification.status == "expired_or_not_yet_valid"


def test_hostname_mismatch_still_returns_metadata():
    ca_cert, ca_key = make_ca()
    ca_file = write_ca_file(ca_cert)
    cert, key = make_leaf("correct.example.com", san_dns=["correct.example.com"], signer=(ca_cert, ca_key))
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "wrong.example.com", ca_file=ca_file)

    assert result.certificate.subject == "CN=correct.example.com"
    assert result.verification.chain_trusted is True
    assert result.verification.hostname_matches is False
    assert result.verification.status == "hostname_mismatch"


# ---------------------------------------------------------------------------
# Verification dimensions stay independent -- never collapsed into one
# boolean.
# ---------------------------------------------------------------------------


def test_verification_dimensions_are_independent_not_collapsed():
    ca_cert, ca_key = make_ca()
    ca_file = write_ca_file(ca_cert)
    cert, key = make_leaf("wrongcert.example.com", san_dns=["wrongcert.example.com"], signer=(ca_cert, ca_key))
    with TLSTestServer(cert, key) as server:
        # chain_trusted=True (signed by the trusted CA), but the SNI we
        # ask about doesn't match this cert's SAN -- must be
        # hostname_matches=False independent of chain trust.
        result = inspect_tls("127.0.0.1", server.port, "totally-different.example.com", ca_file=ca_file)

    assert result.verification.chain_trusted is True
    assert result.verification.hostname_matches is False
    assert result.verification.time_valid is True
    # All three dimensions individually visible -- not one boolean.
    assert isinstance(result.verification.chain_trusted, bool)
    assert isinstance(result.verification.hostname_matches, bool)
    assert isinstance(result.verification.time_valid, bool)


# ---------------------------------------------------------------------------
# SAN handling: DNS SAN, IP SAN, correct/wrong SNI
# ---------------------------------------------------------------------------


def test_dns_san_is_reported():
    cert, key = make_leaf("multi.example.com", san_dns=["multi.example.com", "alt.example.com"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "alt.example.com")

    assert set(result.certificate.san_dns) == {"multi.example.com", "alt.example.com"}
    assert result.verification.hostname_matches is True


def test_ip_san_is_reported_and_matched():
    cert, key = make_leaf("iptarget", san_ip=["203.0.113.5"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "203.0.113.5")

    assert result.certificate.san_ip == ["203.0.113.5"]
    assert result.verification.hostname_matches is True


def test_ip_san_present_but_different_ip_requested_is_a_mismatch():
    cert, key = make_leaf("iptarget", san_ip=["203.0.113.5"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "203.0.113.99")

    assert result.verification.hostname_matches is False


def test_wildcard_san_matches_one_label():
    cert, key = make_leaf("wildcard.example.com", san_dns=["*.example.com"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "foo.example.com")

    assert result.verification.hostname_matches is True


def test_wildcard_san_does_not_match_two_labels_deep():
    cert, key = make_leaf("wildcard.example.com", san_dns=["*.example.com"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "foo.bar.example.com")

    assert result.verification.hostname_matches is False


def test_correct_sni_selects_matching_certificate():
    cert, key = make_leaf("sni-test.example.com", san_dns=["sni-test.example.com"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "sni-test.example.com")

    assert result.server_name == "sni-test.example.com"
    assert result.verification.hostname_matches is True


# ---------------------------------------------------------------------------
# Retrieval/handshake failures -- distinct from verification evidence,
# always raised via the #15 error model.
# ---------------------------------------------------------------------------


def test_connection_refused_raises_tls_error():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    unused_port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(TLSError) as exc_info:
        inspect_tls("127.0.0.1", unused_port, "nobody.example.com")

    assert exc_info.value.kind == IntegrationErrorKind.CONNECTION


def test_dns_failure_raises_tls_error():
    with pytest.raises(TLSError) as exc_info:
        inspect_tls("this-name-does-not-resolve.invalid", 443, "this-name-does-not-resolve.invalid")

    assert exc_info.value.kind == IntegrationErrorKind.CONNECTION


def test_tls_failure_before_certificate_presentation_raises_tls_error():
    with NonTLSTCPServer() as server:
        with pytest.raises(TLSError) as exc_info:
            inspect_tls("127.0.0.1", server.port, "not-tls.example.com")

    assert exc_info.value.kind == IntegrationErrorKind.CONNECTION
    assert "nxdomain" not in str(exc_info.value).lower()


def test_handshake_timeout_raises_tls_error_with_timeout_kind():
    with SilentTCPServer() as server:
        deadline = Deadline.after(0.3)
        with pytest.raises(TLSError) as exc_info:
            inspect_tls("127.0.0.1", server.port, "silent.example.com", deadline=deadline)

    assert exc_info.value.kind == IntegrationErrorKind.TIMEOUT


def test_deadline_already_expired_raises_timeout_before_any_attempt():
    deadline = Deadline.after(-1.0)
    with pytest.raises(TLSError) as exc_info:
        inspect_tls("127.0.0.1", 443, "example.com", deadline=deadline)

    assert exc_info.value.kind == IntegrationErrorKind.TIMEOUT


# ---------------------------------------------------------------------------
# Verification handshake skipped gracefully when budget runs out after
# phase 1 already succeeded -- the certificate is never discarded.
# ---------------------------------------------------------------------------


def test_budget_exhausted_before_verification_keeps_certificate_metadata(monkeypatch):
    cert, key = make_leaf("budget.example.com", san_dns=["budget.example.com"])
    with TLSTestServer(cert, key) as server:
        clock = FakeClock()
        deadline = Deadline.after(1.0, clock=clock)

        import mantis.integrations.tls as tls_integration

        real_inspect_one = tls_integration._inspect_one_address

        def fake_inspect_one(*args, **kwargs):
            result = real_inspect_one(*args, **kwargs)
            clock.advance(10.0)  # consume the whole remaining budget
            return result

        monkeypatch.setattr(tls_integration, "_inspect_one_address", fake_inspect_one)

        result = inspect_tls("127.0.0.1", server.port, "budget.example.com", deadline=deadline, clock=clock)

    assert result.certificate.subject == "CN=budget.example.com"
    assert result.verification.chain_trusted is None
    assert result.verification.status == "unknown"


# ---------------------------------------------------------------------------
# Malformed certificate (unit-tested directly against the parser, since
# a real OpenSSL handshake cannot be coaxed into completing with a
# genuinely malformed certificate -- "where practical" per #111).
# ---------------------------------------------------------------------------


def test_malformed_certificate_bytes_raise_certificate_parse_error():
    with pytest.raises(CertificateParseError):
        _parse_certificate(b"this is not a valid DER-encoded certificate at all")


# ---------------------------------------------------------------------------
# SAN/result truncation -- bounded, and meta.truncated only true when
# something was actually omitted.
# ---------------------------------------------------------------------------


def test_san_truncation_is_bounded_and_flagged():
    many_names = [f"host{i}.example.com" for i in range(MAX_SAN_ENTRIES + 10)]
    cert, key = make_leaf("many-sans.example.com", san_dns=many_names)
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, many_names[0])

    assert len(result.certificate.san_dns) == MAX_SAN_ENTRIES
    assert result.truncated is True


def test_san_count_under_the_cap_is_not_flagged_truncated():
    cert, key = make_leaf("few-sans.example.com", san_dns=["few-sans.example.com"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "few-sans.example.com")

    assert result.truncated is False


# ---------------------------------------------------------------------------
# Deterministic multi-address handling
# ---------------------------------------------------------------------------


def test_connected_address_is_reported():
    cert, key = make_leaf("addr.example.com", san_dns=["addr.example.com"])
    with TLSTestServer(cert, key) as server:
        result = inspect_tls("127.0.0.1", server.port, "addr.example.com")

    assert result.connected_address == "127.0.0.1"
