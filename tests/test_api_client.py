"""Tests for mantis.api_client.MantisApiClient: the CLI's only path to
the Mantis API (#83). All HTTP interactions are mocked with respx — no
live Mantis API required, and no local execution ever happens here.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mantis.api_client import (
    ApiAuthError,
    ApiRequestError,
    ApiServerError,
    ApiTimeoutError,
    ApiUnavailableError,
    MantisApiClient,
)
from mantis.config import ApiClientConfig, Secret


@pytest.fixture
def client() -> MantisApiClient:
    return MantisApiClient(ApiClientConfig(base_url="https://mantis.example.test", token=Secret("tok")))


@respx.mock
def test_list_agents_sends_bearer_token(client):
    route = respx.get("https://mantis.example.test/api/v1/agents").mock(
        return_value=httpx.Response(200, json={"agents": []})
    )

    agents = client.list_agents()

    assert agents == []
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_list_agents_parses_agent_fields(client):
    respx.get("https://mantis.example.test/api/v1/agents").mock(
        return_value=httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "id": "system-troubleshooter",
                        "display_name": "System Troubleshooter",
                        "description": "desc",
                        "read_only": True,
                        "available": True,
                        "unavailable_reason": None,
                    }
                ]
            },
        )
    )

    agents = client.list_agents()

    assert len(agents) == 1
    assert agents[0].id == "system-troubleshooter"
    assert agents[0].available is True


@respx.mock
def test_create_run_sends_agent_and_prompt(client):
    route = respx.post("https://mantis.example.test/api/v1/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "run_id": "abc",
                "agent": "system-troubleshooter",
                "outcome": "success",
                "output": "the answer",
                "error": None,
                "started_at": "t0",
                "finished_at": "t1",
                "duration_ms": 10,
            },
        )
    )

    result = client.create_run("system-troubleshooter", "investigate")

    assert result.run_id == "abc"
    assert result.outcome == "success"
    assert result.output == "the answer"
    assert result.error_kind is None
    sent_body = route.calls.last.request.content
    assert b'"agent":"system-troubleshooter"' in sent_body or b'"agent": "system-troubleshooter"' in sent_body


@respx.mock
def test_create_run_parses_error_outcome(client):
    respx.post("https://mantis.example.test/api/v1/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "run_id": "abc",
                "agent": "system-troubleshooter",
                "outcome": "error",
                "output": None,
                "error": {"kind": "max_iterations", "message": "could not finish"},
                "started_at": "t0",
                "finished_at": "t1",
                "duration_ms": 10,
            },
        )
    )

    result = client.create_run("system-troubleshooter", "investigate")

    assert result.outcome == "error"
    assert result.error_kind == "max_iterations"
    assert result.error_message == "could not finish"


@respx.mock
def test_unauthenticated_response_raises_api_auth_error(client):
    respx.get("https://mantis.example.test/api/v1/agents").mock(return_value=httpx.Response(401))

    with pytest.raises(ApiAuthError):
        client.list_agents()


@respx.mock
def test_unknown_agent_raises_api_request_error_with_parsed_type(client):
    respx.post("https://mantis.example.test/api/v1/runs").mock(
        return_value=httpx.Response(
            404, json={"error": {"type": "unknown_agent", "message": "Unknown agent: 'nope'", "run_id": None}}
        )
    )

    with pytest.raises(ApiRequestError) as exc_info:
        client.create_run("nope", "prompt")

    assert exc_info.value.status_code == 404
    assert exc_info.value.error_type == "unknown_agent"
    assert "nope" in str(exc_info.value)


@respx.mock
def test_overload_raises_api_request_error(client):
    respx.post("https://mantis.example.test/api/v1/runs").mock(
        return_value=httpx.Response(
            429, json={"error": {"type": "overloaded", "message": "try again", "run_id": "r1"}}
        )
    )

    with pytest.raises(ApiRequestError) as exc_info:
        client.create_run("agent", "prompt")

    assert exc_info.value.status_code == 429
    assert exc_info.value.error_type == "overloaded"
    # The server deliberately assigns a run ID before rejecting an
    # overloaded request (see InvocationService); this client must not
    # discard it -- it's what makes a rejected attempt correlatable in
    # server-side logs.
    assert exc_info.value.run_id == "r1"


@respx.mock
def test_unknown_agent_error_has_no_run_id(client):
    # A rejection that happens before a run ID is ever assigned (e.g.
    # unknown agent) must not fabricate one.
    respx.post("https://mantis.example.test/api/v1/runs").mock(
        return_value=httpx.Response(
            404, json={"error": {"type": "unknown_agent", "message": "Unknown agent: 'nope'", "run_id": None}}
        )
    )

    with pytest.raises(ApiRequestError) as exc_info:
        client.create_run("nope", "prompt")

    assert exc_info.value.run_id is None


@respx.mock
def test_non_dict_json_error_body_does_not_escape_the_typed_error(client):
    # A misbehaving proxy/gateway could return a JSON body that isn't
    # the expected {"error": {...}} object at all -- this must still
    # raise ApiRequestError with safe defaults, never an unhandled
    # AttributeError/TypeError escaping this client's typed hierarchy.
    respx.post("https://mantis.example.test/api/v1/runs").mock(return_value=httpx.Response(400, json=["oops"]))

    with pytest.raises(ApiRequestError) as exc_info:
        client.create_run("agent", "prompt")

    assert exc_info.value.status_code == 400
    assert exc_info.value.error_type == "request_error"
    assert exc_info.value.run_id is None


@respx.mock
def test_error_field_present_but_not_an_object_falls_back_safely(client):
    respx.post("https://mantis.example.test/api/v1/runs").mock(
        return_value=httpx.Response(422, json={"error": "just a string, not an object"})
    )

    with pytest.raises(ApiRequestError) as exc_info:
        client.create_run("agent", "prompt")

    assert exc_info.value.error_type == "request_error"
    assert exc_info.value.run_id is None


@respx.mock
def test_non_json_error_body_falls_back_safely(client):
    respx.post("https://mantis.example.test/api/v1/runs").mock(
        return_value=httpx.Response(400, content=b"not json at all")
    )

    with pytest.raises(ApiRequestError) as exc_info:
        client.create_run("agent", "prompt")

    assert exc_info.value.error_type == "request_error"
    assert "rejected" in str(exc_info.value).lower()


@respx.mock
def test_server_error_raises_api_server_error(client):
    respx.get("https://mantis.example.test/api/v1/agents").mock(return_value=httpx.Response(500))

    with pytest.raises(ApiServerError):
        client.list_agents()


@respx.mock
def test_malformed_success_body_raises_api_server_error(client):
    respx.get("https://mantis.example.test/api/v1/agents").mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(ApiServerError):
        client.list_agents()


def test_connection_failure_raises_api_unavailable_error(client):
    with respx.mock:
        respx.get("https://mantis.example.test/api/v1/agents").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(ApiUnavailableError):
            client.list_agents()


def test_timeout_raises_api_timeout_error(client):
    with respx.mock:
        respx.get("https://mantis.example.test/api/v1/agents").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        with pytest.raises(ApiTimeoutError):
            client.list_agents()


@respx.mock
def test_no_token_configured_sends_no_authorization_header():
    route = respx.get("https://mantis.example.test/api/v1/agents").mock(
        return_value=httpx.Response(200, json={"agents": []})
    )
    anon_client = MantisApiClient(ApiClientConfig(base_url="https://mantis.example.test", token=None))

    anon_client.list_agents()

    assert "Authorization" not in route.calls.last.request.headers
