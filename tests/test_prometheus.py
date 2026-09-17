"""Tests for the Prometheus integration client
(mantis.integrations.prometheus): HTTP mechanics, #15 reliability reuse,
API-envelope parsing, and config/auth.

All HTTP interactions are mocked with respx; no live Prometheus instance
required. No test sleeps in real time (retries use an injected no-op
sleep).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mantis.config import PrometheusConfig, Secret
from mantis.integrations.prometheus import PrometheusClient, PrometheusError
from mantis.reliability import Deadline, IntegrationErrorKind


@pytest.fixture
def prom_client() -> PrometheusClient:
    return PrometheusClient(
        config=PrometheusConfig(url="https://prom.example.test"),
        sleep=lambda *_: None,
    )


def _success(result_type: str, result) -> dict:
    return {"status": "success", "data": {"resultType": result_type, "result": result}}


# ---------------------------------------------------------------------------
# Success: instant vector, scalar, string, range matrix, empty results,
# warnings alongside valid data
# ---------------------------------------------------------------------------


@respx.mock
def test_instant_vector_success(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json=_success(
                "vector",
                [{"metric": {"__name__": "up", "instance": "host:9100"}, "value": [1700000000.0, "1"]}],
            ),
        )
    )

    response = prom_client.query("up")

    assert response.status == "success"
    assert response.result_type == "vector"
    assert response.result[0]["metric"]["instance"] == "host:9100"


@respx.mock
def test_instant_scalar_success(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_success("scalar", [1700000000.0, "42"]))
    )

    response = prom_client.query("1+1")

    assert response.result_type == "scalar"
    assert response.result == [1700000000.0, "42"]


@respx.mock
def test_instant_string_success(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_success("string", [1700000000.0, "hello"]))
    )

    response = prom_client.query('"hello"')

    assert response.result_type == "string"


@respx.mock
def test_range_matrix_success(prom_client):
    respx.get("https://prom.example.test/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json=_success(
                "matrix",
                [
                    {
                        "metric": {"__name__": "up", "instance": "host:9100"},
                        "values": [[1700000000.0, "1"], [1700000060.0, "0"]],
                    }
                ],
            ),
        )
    )

    response = prom_client.query_range("up", start="1700000000", end="1700000060", step="60")

    assert response.result_type == "matrix"
    assert len(response.result[0]["values"]) == 2


@respx.mock
def test_empty_vector_is_a_successful_response(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_success("vector", []))
    )

    response = prom_client.query("up{instance=\"nonexistent\"}")

    assert response.status == "success"
    assert response.result == []


@respx.mock
def test_empty_matrix_is_a_successful_response(prom_client):
    respx.get("https://prom.example.test/api/v1/query_range").mock(
        return_value=httpx.Response(200, json=_success("matrix", []))
    )

    response = prom_client.query_range("up{instance=\"nonexistent\"}", start="0", end="60", step="15")

    assert response.status == "success"
    assert response.result == []


@respx.mock
def test_warnings_preserved_alongside_valid_data(prom_client):
    payload = _success("vector", [{"metric": {"__name__": "up"}, "value": [1700000000.0, "1"]}])
    payload["warnings"] = ["query used more than the configured limit"]
    respx.get("https://prom.example.test/api/v1/query").mock(return_value=httpx.Response(200, json=payload))

    response = prom_client.query("up")

    assert response.warnings == ["query used more than the configured limit"]
    assert response.status == "success"


# ---------------------------------------------------------------------------
# API semantics: status=error, malformed envelope
# ---------------------------------------------------------------------------


@respx.mock
def test_query_error_via_400_is_data_not_an_exception(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(
            400, json={"status": "error", "errorType": "bad_data", "error": "parse error at char 3"}
        )
    )

    response = prom_client.query("up{")

    assert response.status == "error"
    assert response.error_type == "bad_data"
    assert response.error == "parse error at char 3"


@respx.mock
def test_query_error_via_422_is_data_not_an_exception(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(
            422, json={"status": "error", "errorType": "execution", "error": "vector cannot contain metrics with the same labelset"}
        )
    )

    response = prom_client.query("up + up")

    assert response.status == "error"
    assert response.error_type == "execution"


@respx.mock
def test_query_error_via_200_is_also_supported(prom_client):
    # Some Prometheus-compatible backends/proxies may report a query
    # error with a 200 status -- the envelope shape is what matters.
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "error", "errorType": "bad_data", "error": "bad query"})
    )

    response = prom_client.query("up")

    assert response.status == "error"


@respx.mock
def test_400_query_error_is_never_retried(prom_client):
    route = respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "bad"})
    )

    prom_client.query("up{")

    assert route.call_count == 1


@respx.mock
def test_malformed_success_envelope_raises_prometheus_error(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    with pytest.raises(PrometheusError) as excinfo:
        prom_client.query("up")

    assert excinfo.value.kind == IntegrationErrorKind.UNKNOWN


@respx.mock
def test_success_envelope_with_non_object_data_raises_prometheus_error_not_attribute_error(prom_client):
    # Regression test (PR #76 review): "data" being a list (or any
    # non-object) previously reached data.get(...) directly and raised
    # an unclassified AttributeError instead of a clean PrometheusError.
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": []})
    )

    with pytest.raises(PrometheusError) as excinfo:
        prom_client.query("up")

    assert excinfo.value.kind == IntegrationErrorKind.UNKNOWN


@respx.mock
def test_success_envelope_with_missing_data_defaults_to_empty(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )

    response = prom_client.query("up")

    assert response.status == "success"
    assert response.result is None
    assert response.result_type is None


@respx.mock
def test_non_json_response_raises_prometheus_error(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, text="not json")
    )

    with pytest.raises(PrometheusError):
        prom_client.query("up")


@respx.mock
def test_unexpected_result_type_is_passed_through_without_crashing(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_success("something_new", []))
    )

    response = prom_client.query("up")

    assert response.result_type == "something_new"


# ---------------------------------------------------------------------------
# Reliability (#15): explicit timeouts, retry/no-retry classification
# ---------------------------------------------------------------------------


def test_prometheus_client_uses_explicit_connect_and_read_timeouts(prom_client):
    with prom_client._client() as client:
        timeout = client.timeout
    assert timeout.connect == prom_client.reliability.http_connect_timeout_seconds
    assert timeout.read == prom_client.reliability.http_read_timeout_seconds


def test_prometheus_client_caps_effective_timeout_at_remaining_deadline(prom_client):
    deadline = Deadline.after(2.0)
    with prom_client._client(deadline=deadline) as client:
        timeout = client.timeout
    assert timeout.connect <= 2.0
    assert timeout.read <= 2.0


@pytest.mark.parametrize("status,attr", [(401, "AUTHENTICATION"), (403, "AUTHORIZATION")])
@respx.mock
def test_401_403_are_not_retried(prom_client, status, attr):
    route = respx.get("https://prom.example.test/api/v1/query").mock(return_value=httpx.Response(status))

    with pytest.raises(PrometheusError) as excinfo:
        prom_client.query("up")

    assert excinfo.value.kind == getattr(IntegrationErrorKind, attr)
    assert route.call_count == 1


@respx.mock
def test_429_is_retried(prom_client):
    route = respx.get("https://prom.example.test/api/v1/query")
    route.side_effect = [httpx.Response(429), httpx.Response(200, json=_success("vector", []))]

    response = prom_client.query("up")

    assert response.status == "success"
    assert route.call_count == 2


@pytest.mark.parametrize("status", [502, 503, 504])
@respx.mock
def test_5xx_statuses_are_retried(prom_client, status):
    route = respx.get("https://prom.example.test/api/v1/query")
    route.side_effect = [httpx.Response(status), httpx.Response(200, json=_success("vector", []))]

    response = prom_client.query("up")

    assert response.status == "success"
    assert route.call_count == 2


@respx.mock
def test_connect_timeout_classifies_as_timeout(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(PrometheusError) as excinfo:
        prom_client.query("up")

    assert excinfo.value.kind == IntegrationErrorKind.TIMEOUT


@respx.mock
def test_connection_error_classifies_as_connection(prom_client):
    respx.get("https://prom.example.test/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(PrometheusError) as excinfo:
        prom_client.query("up")

    assert excinfo.value.kind == IntegrationErrorKind.CONNECTION


@respx.mock
def test_deadline_already_expired_means_zero_requests(prom_client):
    route = respx.get("https://prom.example.test/api/v1/query").mock(return_value=httpx.Response(200))
    deadline = Deadline.after(0.0)

    with pytest.raises(Exception):
        prom_client.query("up", deadline=deadline)

    assert route.call_count == 0


@respx.mock
def test_retry_exhaustion_raises_after_named_max_attempts(prom_client):
    route = respx.get("https://prom.example.test/api/v1/query").mock(return_value=httpx.Response(503))

    with pytest.raises(PrometheusError):
        prom_client.query("up")

    assert route.call_count == prom_client.reliability.retry_max_attempts


# ---------------------------------------------------------------------------
# Config / auth
# ---------------------------------------------------------------------------


@respx.mock
def test_unauthenticated_request_sends_no_authorization_header():
    route = respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_success("vector", []))
    )
    client = PrometheusClient(config=PrometheusConfig(url="https://prom.example.test"), sleep=lambda *_: None)

    client.query("up")

    assert "authorization" not in {k.lower() for k in route.calls.last.request.headers.keys()}


@respx.mock
def test_bearer_token_sent_as_authorization_header():
    route = respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_success("vector", []))
    )
    client = PrometheusClient(
        config=PrometheusConfig(url="https://prom.example.test", bearer_token=Secret("s3cr3t-token")),
        sleep=lambda *_: None,
    )

    client.query("up")

    assert route.calls.last.request.headers["Authorization"] == "Bearer s3cr3t-token"


@respx.mock
def test_basic_auth_sent_when_no_bearer_token():
    route = respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_success("vector", []))
    )
    client = PrometheusClient(
        config=PrometheusConfig(
            url="https://prom.example.test",
            basic_auth_username="mantis",
            basic_auth_password=Secret("hunter2"),
        ),
        sleep=lambda *_: None,
    )

    client.query("up")

    import base64

    expected = "Basic " + base64.b64encode(b"mantis:hunter2").decode()
    assert route.calls.last.request.headers["Authorization"] == expected


@respx.mock
def test_bearer_token_takes_priority_over_basic_auth():
    route = respx.get("https://prom.example.test/api/v1/query").mock(
        return_value=httpx.Response(200, json=_success("vector", []))
    )
    client = PrometheusClient(
        config=PrometheusConfig(
            url="https://prom.example.test",
            bearer_token=Secret("bearer-wins"),
            basic_auth_username="mantis",
            basic_auth_password=Secret("hunter2"),
        ),
        sleep=lambda *_: None,
    )

    client.query("up")

    assert route.calls.last.request.headers["Authorization"] == "Bearer bearer-wins"


def test_verify_ssl_defaults_to_true():
    config = PrometheusConfig(url="https://prom.example.test")
    assert config.verify_ssl is True


def test_verify_ssl_can_be_explicitly_disabled():
    config = PrometheusConfig(url="https://prom.example.test", verify_ssl=False)
    assert config.verify_ssl is False


@respx.mock
def test_credentials_do_not_leak_into_a_raised_error_message():
    respx.get("https://prom.example.test/api/v1/query").mock(return_value=httpx.Response(500))
    client = PrometheusClient(
        config=PrometheusConfig(url="https://prom.example.test", bearer_token=Secret("super-secret-token")),
        sleep=lambda *_: None,
    )

    with pytest.raises(PrometheusError) as excinfo:
        client.query("up")

    assert "super-secret-token" not in str(excinfo.value)


@respx.mock
def test_credentials_do_not_leak_into_structured_retry_logs(caplog):
    import logging

    respx.get("https://prom.example.test/api/v1/query").mock(return_value=httpx.Response(503))
    client = PrometheusClient(
        config=PrometheusConfig(url="https://prom.example.test", bearer_token=Secret("super-secret-token")),
        sleep=lambda *_: None,
    )
    caplog.set_level(logging.INFO, logger="mantis.integrations.prometheus")

    with pytest.raises(PrometheusError):
        client.query("up")

    logged_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "super-secret-token" not in logged_text


def test_config_from_env_reads_mantis_prometheus_url(monkeypatch):
    monkeypatch.setenv("MANTIS_PROMETHEUS_URL", "https://prom.internal.example/")
    monkeypatch.delenv("MANTIS_PROMETHEUS_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MANTIS_PROMETHEUS_BASIC_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("MANTIS_PROMETHEUS_BASIC_AUTH_PASSWORD", raising=False)

    config = PrometheusConfig.from_env()

    assert config.url == "https://prom.internal.example"  # trailing slash stripped
    assert config.bearer_token is None


def test_config_from_env_reads_bearer_token(monkeypatch):
    monkeypatch.setenv("MANTIS_PROMETHEUS_URL", "https://prom.internal.example")
    monkeypatch.setenv("MANTIS_PROMETHEUS_BEARER_TOKEN", "env-token")

    config = PrometheusConfig.from_env()

    assert config.bearer_token.get_secret_value() == "env-token"


def test_config_repr_never_exposes_bearer_token_or_basic_auth_password():
    config = PrometheusConfig(
        url="https://prom.example.test",
        bearer_token=Secret("super-secret-token"),
        basic_auth_password=Secret("hunter2"),
    )

    assert "super-secret-token" not in repr(config)
    assert "hunter2" not in repr(config)
