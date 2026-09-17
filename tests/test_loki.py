"""Tests for the Loki integration client (mantis.integrations.loki):
HTTP mechanics, #15 reliability reuse, API-envelope parsing, and
config/auth.

All HTTP interactions are mocked with respx; no live Loki instance
required. No test sleeps in real time (retries use an injected no-op
sleep).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mantis.config import LokiConfig, Secret
from mantis.integrations.loki import LokiClient, LokiError
from mantis.reliability import Deadline, IntegrationErrorKind


@pytest.fixture
def loki_client() -> LokiClient:
    return LokiClient(config=LokiConfig(url="https://loki.example.test"), sleep=lambda *_: None)


def _success(result_type: str, result) -> dict:
    return {"status": "success", "data": {"resultType": result_type, "result": result}}


def _stream(labels: dict, values: list) -> dict:
    return {"stream": labels, "values": values}


# ---------------------------------------------------------------------------
# Success: streams, empty results, warnings alongside valid data
# ---------------------------------------------------------------------------


@respx.mock
def test_streams_success(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json=_success(
                "streams",
                [_stream({"job": "sshd"}, [["1700000000000000000", "hello"]])],
            ),
        )
    )

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="1699999000000000000", end_ns="1700001000000000000",
        direction="backward", limit=100,
    )

    assert response.status == "success"
    assert response.result_type == "streams"
    assert response.result[0]["stream"]["job"] == "sshd"


@respx.mock
def test_empty_streams_is_a_successful_response(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )

    response = loki_client.query_range(
        '{job="nonexistent"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.status == "success"
    assert response.result == []


@respx.mock
def test_warnings_preserved_alongside_valid_data(loki_client):
    payload = _success("streams", [_stream({"job": "sshd"}, [["1700000000000000000", "hi"]])])
    payload["warnings"] = ["maximum of series (500) reached"]
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=payload)
    )

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.warnings == ["maximum of series (500) reached"]
    assert response.status == "success"


@respx.mock
def test_malformed_warnings_field_is_ignored_not_iterated_char_by_char(loki_client):
    # Regression test (PR #80 review): "warnings" being a bare string
    # (instead of a list) must not be iterated -- that would silently
    # explode into one list entry per character, building a potentially
    # enormous intermediate list straight from an unbounded response
    # field before any semantic-layer bound applies. The malformed shape
    # is still recorded via warnings_malformed (see
    # test_malformed_warnings_marks_meta_truncated_at_the_tool_layer in
    # tests/test_loki_tools.py) rather than disappearing without a trace.
    payload = _success("streams", [])
    payload["warnings"] = "x" * 100_000
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=payload)
    )

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.warnings == []
    assert response.warnings_malformed is True


@respx.mock
def test_malformed_warnings_field_on_error_response_is_also_ignored(loki_client):
    payload = {"status": "error", "error": "bad query", "warnings": {"not": "a list"}}
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(400, json=payload)
    )

    response = loki_client.query_range(
        "{job=", start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.warnings == []
    assert response.warnings_malformed is True


@respx.mock
def test_absent_warnings_field_is_not_malformed(loki_client):
    # A missing "warnings" key entirely is the normal case for a Loki
    # version/deployment that doesn't report warnings at all -- distinct
    # from a present-but-wrong-shaped value.
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.warnings == []
    assert response.warnings_malformed is False


# ---------------------------------------------------------------------------
# Request shape: query params
# ---------------------------------------------------------------------------


@respx.mock
def test_query_range_sends_expected_params(loki_client):
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )

    loki_client.query_range(
        '{job="sshd"}', start_ns="111", end_ns="222", direction="forward", limit=42
    )

    params = dict(route.calls.last.request.url.params)
    assert params["query"] == '{job="sshd"}'
    assert params["start"] == "111"
    assert params["end"] == "222"
    assert params["direction"] == "forward"
    assert params["limit"] == "42"


# ---------------------------------------------------------------------------
# API semantics: status=error, malformed envelope
# ---------------------------------------------------------------------------


@respx.mock
def test_query_error_via_400_is_data_not_an_exception(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(400, json={"status": "error", "error": "parse error at line 1, col 1"})
    )

    response = loki_client.query_range(
        "{job=", start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.status == "error"
    assert response.error == "parse error at line 1, col 1"


@respx.mock
def test_query_error_via_422_is_data_not_an_exception(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(422, json={"status": "error", "error": "max query length exceeded"})
    )

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.status == "error"
    assert response.error == "max query length exceeded"


@respx.mock
def test_query_error_using_message_field_is_also_supported(loki_client):
    # Some Loki versions use "message" instead of "error" for the
    # query-error envelope.
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(400, json={"status": "error", "message": "bad request"})
    )

    response = loki_client.query_range(
        "{job=", start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.status == "error"
    assert response.error == "bad request"


@respx.mock
def test_400_query_error_is_never_retried(loki_client):
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(400, json={"status": "error", "error": "bad"})
    )

    loki_client.query_range("{job=", start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    assert route.call_count == 1


@respx.mock
def test_malformed_success_envelope_raises_loki_error(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    with pytest.raises(LokiError) as excinfo:
        loki_client.query_range(
            '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
        )

    assert excinfo.value.kind == IntegrationErrorKind.UNKNOWN


@respx.mock
def test_success_envelope_with_non_object_data_raises_loki_error_not_attribute_error(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": []})
    )

    with pytest.raises(LokiError) as excinfo:
        loki_client.query_range(
            '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
        )

    assert excinfo.value.kind == IntegrationErrorKind.UNKNOWN


@respx.mock
def test_success_envelope_with_missing_data_defaults_to_empty(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.status == "success"
    assert response.result is None
    assert response.result_type is None


@respx.mock
def test_non_json_response_raises_loki_error(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, text="not json")
    )

    with pytest.raises(LokiError):
        loki_client.query_range(
            '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
        )


@respx.mock
def test_unexpected_result_type_is_passed_through_without_crashing(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("matrix", []))
    )

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.result_type == "matrix"


# ---------------------------------------------------------------------------
# Reliability (#15): explicit timeouts, retry/no-retry classification
# ---------------------------------------------------------------------------


def test_loki_client_uses_explicit_connect_and_read_timeouts(loki_client):
    with loki_client._client() as client:
        timeout = client.timeout
    assert timeout.connect == loki_client.reliability.http_connect_timeout_seconds
    assert timeout.read == loki_client.reliability.http_read_timeout_seconds


def test_loki_client_caps_effective_timeout_at_remaining_deadline(loki_client):
    deadline = Deadline.after(2.0)
    with loki_client._client(deadline=deadline) as client:
        timeout = client.timeout
    assert timeout.connect <= 2.0
    assert timeout.read <= 2.0


@pytest.mark.parametrize("status,attr", [(401, "AUTHENTICATION"), (403, "AUTHORIZATION")])
@respx.mock
def test_401_403_are_not_retried(loki_client, status, attr):
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(status)
    )

    with pytest.raises(LokiError) as excinfo:
        loki_client.query_range(
            '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
        )

    assert excinfo.value.kind == getattr(IntegrationErrorKind, attr)
    assert route.call_count == 1


@respx.mock
def test_429_is_retried(loki_client):
    route = respx.get("https://loki.example.test/loki/api/v1/query_range")
    route.side_effect = [httpx.Response(429), httpx.Response(200, json=_success("streams", []))]

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.status == "success"
    assert route.call_count == 2


@pytest.mark.parametrize("status", [502, 503, 504])
@respx.mock
def test_5xx_statuses_are_retried(loki_client, status):
    route = respx.get("https://loki.example.test/loki/api/v1/query_range")
    route.side_effect = [httpx.Response(status), httpx.Response(200, json=_success("streams", []))]

    response = loki_client.query_range(
        '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
    )

    assert response.status == "success"
    assert route.call_count == 2


@respx.mock
def test_connect_timeout_classifies_as_timeout(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )

    with pytest.raises(LokiError) as excinfo:
        loki_client.query_range(
            '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
        )

    assert excinfo.value.kind == IntegrationErrorKind.TIMEOUT


@respx.mock
def test_connection_error_classifies_as_connection(loki_client):
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(LokiError) as excinfo:
        loki_client.query_range(
            '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
        )

    assert excinfo.value.kind == IntegrationErrorKind.CONNECTION


@respx.mock
def test_deadline_already_expired_means_zero_requests(loki_client):
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200)
    )
    deadline = Deadline.after(0.0)

    with pytest.raises(Exception):
        loki_client.query_range(
            '{job="sshd"}',
            start_ns="0",
            end_ns="60000000000",
            direction="backward",
            limit=100,
            deadline=deadline,
        )

    assert route.call_count == 0


@respx.mock
def test_retry_exhaustion_raises_after_named_max_attempts(loki_client):
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(LokiError):
        loki_client.query_range(
            '{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100
        )

    assert route.call_count == loki_client.reliability.retry_max_attempts


# ---------------------------------------------------------------------------
# Config / auth / tenant
# ---------------------------------------------------------------------------


@respx.mock
def test_unauthenticated_request_sends_no_authorization_header():
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )
    client = LokiClient(config=LokiConfig(url="https://loki.example.test"), sleep=lambda *_: None)

    client.query_range('{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    assert "authorization" not in {k.lower() for k in route.calls.last.request.headers.keys()}


@respx.mock
def test_bearer_token_sent_as_authorization_header():
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )
    client = LokiClient(
        config=LokiConfig(url="https://loki.example.test", bearer_token=Secret("s3cr3t-token")),
        sleep=lambda *_: None,
    )

    client.query_range('{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    assert route.calls.last.request.headers["Authorization"] == "Bearer s3cr3t-token"


@respx.mock
def test_basic_auth_sent_when_no_bearer_token():
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )
    client = LokiClient(
        config=LokiConfig(
            url="https://loki.example.test", basic_auth_username="mantis", basic_auth_password=Secret("hunter2")
        ),
        sleep=lambda *_: None,
    )

    client.query_range('{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    import base64

    expected = "Basic " + base64.b64encode(b"mantis:hunter2").decode()
    assert route.calls.last.request.headers["Authorization"] == expected


@respx.mock
def test_bearer_token_takes_priority_over_basic_auth():
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )
    client = LokiClient(
        config=LokiConfig(
            url="https://loki.example.test",
            bearer_token=Secret("bearer-wins"),
            basic_auth_username="mantis",
            basic_auth_password=Secret("hunter2"),
        ),
        sleep=lambda *_: None,
    )

    client.query_range('{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    assert route.calls.last.request.headers["Authorization"] == "Bearer bearer-wins"


@respx.mock
def test_tenant_id_sent_as_scope_orgid_header():
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )
    client = LokiClient(
        config=LokiConfig(url="https://loki.example.test", tenant_id="team-ops"), sleep=lambda *_: None
    )

    client.query_range('{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    assert route.calls.last.request.headers["X-Scope-OrgID"] == "team-ops"


@respx.mock
def test_no_tenant_header_when_not_configured():
    route = respx.get("https://loki.example.test/loki/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("streams", []))
    )
    client = LokiClient(config=LokiConfig(url="https://loki.example.test"), sleep=lambda *_: None)

    client.query_range('{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    assert "x-scope-orgid" not in {k.lower() for k in route.calls.last.request.headers.keys()}


def test_verify_ssl_defaults_to_true():
    config = LokiConfig(url="https://loki.example.test")
    assert config.verify_ssl is True


def test_verify_ssl_can_be_explicitly_disabled():
    config = LokiConfig(url="https://loki.example.test", verify_ssl=False)
    assert config.verify_ssl is False


@respx.mock
def test_credentials_do_not_leak_into_a_raised_error_message():
    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(return_value=httpx.Response(500))
    client = LokiClient(
        config=LokiConfig(url="https://loki.example.test", bearer_token=Secret("super-secret-token")),
        sleep=lambda *_: None,
    )

    with pytest.raises(LokiError) as excinfo:
        client.query_range('{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    assert "super-secret-token" not in str(excinfo.value)


@respx.mock
def test_credentials_do_not_leak_into_structured_retry_logs(caplog):
    import logging

    respx.get("https://loki.example.test/loki/api/v1/query_range").mock(return_value=httpx.Response(503))
    client = LokiClient(
        config=LokiConfig(url="https://loki.example.test", bearer_token=Secret("super-secret-token")),
        sleep=lambda *_: None,
    )
    caplog.set_level(logging.INFO, logger="mantis.integrations.loki")

    with pytest.raises(LokiError):
        client.query_range('{job="sshd"}', start_ns="0", end_ns="60000000000", direction="backward", limit=100)

    logged_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "super-secret-token" not in logged_text


def test_config_from_env_reads_mantis_loki_url(monkeypatch):
    monkeypatch.setenv("MANTIS_LOKI_URL", "https://loki.internal.example/")
    monkeypatch.delenv("MANTIS_LOKI_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MANTIS_LOKI_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("MANTIS_LOKI_BASIC_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("MANTIS_LOKI_TENANT_ID", raising=False)

    config = LokiConfig.from_env()

    assert config.url == "https://loki.internal.example"  # trailing slash stripped
    assert config.bearer_token is None
    assert config.tenant_id is None


def test_config_from_env_reads_bearer_token(monkeypatch):
    monkeypatch.setenv("MANTIS_LOKI_URL", "https://loki.internal.example")
    monkeypatch.setenv("MANTIS_LOKI_BEARER_TOKEN", "env-token")

    config = LokiConfig.from_env()

    assert config.bearer_token.get_secret_value() == "env-token"


def test_config_from_env_reads_tenant_id(monkeypatch):
    monkeypatch.setenv("MANTIS_LOKI_URL", "https://loki.internal.example")
    monkeypatch.setenv("MANTIS_LOKI_TENANT_ID", "team-ops")

    config = LokiConfig.from_env()

    assert config.tenant_id == "team-ops"


def test_config_repr_never_exposes_bearer_token_or_basic_auth_password():
    config = LokiConfig(
        url="https://loki.example.test",
        bearer_token=Secret("super-secret-token"),
        basic_auth_password=Secret("hunter2"),
    )

    assert "super-secret-token" not in repr(config)
    assert "hunter2" not in repr(config)
