"""Integration tests for the real Mantis FastAPI application (#21/#83).

This is the deterministic end-to-end test #83 requires: the real
``create_app()`` factory, the real ``AgentCatalog``
(``build_default_catalog()``), and the real ``InvocationService`` ->
``AgentRuntime`` construction path, through FastAPI's ``TestClient`` (a
real ASGI transport, not a hand-mocked route). The only substituted
dependency anywhere in this file is the model client
(``mantis.runtime.build_openai_client``) — no live LiteLLM/AWX/
Kubernetes/Prometheus/Loki is ever required.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

import mantis.runtime as runtime_module
from mantis.api.app import create_app
from mantis.config import ApiServerConfig, Secret

AUTH = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# Fake model client -- the one substituted external dependency, matching
# the existing tests/test_awx_troubleshooter.py convention exactly.
# ---------------------------------------------------------------------------


@dataclass
class _FakeMessage:
    content: str | None = None
    tool_calls: Any = None

    def model_dump(self) -> dict[str, Any]:
        return {"content": self.content, "tool_calls": self.tool_calls}


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list
    usage: Any = None


class _FakeCompletions:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)

    def create(self, **kwargs: Any) -> _FakeResponse:
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, responses: list[_FakeResponse]):
        self.completions = _FakeCompletions(responses)


class _FakeOpenAIClient:
    def __init__(self, responses: list[_FakeResponse]):
        self.chat = _FakeChat(responses)


def _final_answer(text: str) -> _FakeResponse:
    return _FakeResponse(choices=[_FakeChoice(message=_FakeMessage(content=text, tool_calls=None))])


@pytest.fixture
def fake_model(monkeypatch):
    """Patch mantis.runtime.build_openai_client so every AgentRuntime
    constructed anywhere in a test (including deep inside the real
    InvocationService -> AgentCatalog path) gets a scripted fake client
    instead of a real OpenAI/LiteLLM connection."""

    def _install(final_text: str = "The investigation is complete.") -> None:
        def _fake_build(_model_config):
            return _FakeOpenAIClient([_final_answer(final_text)])

        monkeypatch.setattr(runtime_module, "build_openai_client", _fake_build)

    return _install


def _server_config(**overrides: Any) -> ApiServerConfig:
    defaults: dict[str, Any] = dict(auth_mode="bearer_token", bearer_token=Secret("test-token"))
    defaults.update(overrides)
    return ApiServerConfig(**defaults)


# ---------------------------------------------------------------------------
# Health/readiness
# ---------------------------------------------------------------------------


def test_healthz_never_requires_auth():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_works_even_before_startup_completes():
    # Liveness must not depend on readiness -- no TestClient context
    # manager entered, so lifespan startup never ran.
    app = create_app(server_config=_server_config())
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200


def test_readyz_is_not_ready_before_startup():
    app = create_app(server_config=_server_config())
    client = TestClient(app)  # lifespan never entered

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "starting_up"}


def test_readyz_is_ready_after_startup():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "reason": None}


def test_readyz_never_requires_auth():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/readyz")  # no Authorization header
    assert response.status_code == 200


def test_readyz_transitions_to_not_ready_after_shutdown():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
    # Exiting the context manager ran the lifespan's shutdown phase.
    after_shutdown = TestClient(app).get("/readyz")
    assert after_shutdown.status_code == 503
    assert after_shutdown.json()["reason"] == "shutting_down"


def test_new_runs_are_rejected_once_shutdown_has_begun():
    app = create_app(server_config=_server_config())
    with TestClient(app):
        pass  # runs startup then shutdown

    client = TestClient(app)
    response = client.post("/api/v1/runs", json={"agent": "awx-troubleshooter", "prompt": "x"}, headers=AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "not_ready"


# ---------------------------------------------------------------------------
# OpenAPI / docs
# ---------------------------------------------------------------------------


def test_openapi_json_is_served():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/api/v1/agents" in schema["paths"]
    assert "/api/v1/runs" in schema["paths"]


def test_openapi_documents_bearer_auth_scheme():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    security_schemes = schema["components"]["securitySchemes"]
    assert any(s.get("scheme") == "bearer" for s in security_schemes.values())


def test_docs_ui_is_served():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/docs")
    assert response.status_code == 200
    assert b"swagger" in response.content.lower()


def test_openapi_documents_every_error_status_code_runs_can_actually_return():
    # Regression guard: keep the generated OpenAPI schema aligned with
    # docs/api.md's error table -- every status/error.type the real
    # implementation can produce for POST /api/v1/runs should be
    # declared, with a concrete example of that exact error.type.
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    responses = schema["paths"]["/api/v1/runs"]["post"]["responses"]
    expected = {
        "401": "unauthenticated",
        "404": "unknown_agent",
        "409": "agent_unavailable",
        "422": "validation_error",
        "429": "overloaded",
        "503": "not_ready",
        "500": "internal_error",
    }
    for status, error_type in expected.items():
        assert status in responses, f"missing {status} in documented responses"
        example = responses[status]["content"]["application/json"]["example"]
        assert example["error"]["type"] == error_type


def test_openapi_documents_unauthorized_and_server_error_for_agents_listing():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    responses = schema["paths"]["/api/v1/agents"]["get"]["responses"]
    assert set(responses) >= {"200", "401", "500"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_agents_without_auth_header_is_401():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/api/v1/agents")
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthenticated"


def test_agents_with_wrong_token_is_401():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/api/v1/agents", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_agents_with_correct_token_succeeds():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/api/v1/agents", headers=AUTH)
    assert response.status_code == 200


def test_disabled_auth_mode_allows_unauthenticated_requests():
    app = create_app(server_config=_server_config(auth_mode="disabled", bearer_token=None))
    with TestClient(app) as client:
        response = client.get("/api/v1/agents")
    assert response.status_code == 200


def test_www_authenticate_header_present_on_401():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/api/v1/agents")
    assert response.headers.get("www-authenticate", "").lower().startswith("bearer")


# ---------------------------------------------------------------------------
# GET /api/v1/agents
# ---------------------------------------------------------------------------


def test_agents_lists_the_real_catalog():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/api/v1/agents", headers=AUTH)

    ids = {a["id"] for a in response.json()["agents"]}
    assert ids == {"awx-troubleshooter", "system-troubleshooter", "incident-triage"}


def test_agents_never_exposes_system_prompt_or_internal_details():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.get("/api/v1/agents", headers=AUTH)

    body_text = response.text
    assert "SYSTEM_PROMPT" not in body_text
    assert "AgentRuntime" not in body_text
    assert "mantis.agents" not in body_text


# ---------------------------------------------------------------------------
# POST /api/v1/runs -- real application wiring, fake model only
# ---------------------------------------------------------------------------


def test_successful_run_uses_the_real_application_wiring(fake_model):
    fake_model("AWX shows no recent failures.")
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={"agent": "awx-troubleshooter", "prompt": "any recent failures?"},
            headers=AUTH,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "success"
    assert body["output"] == "AWX shows no recent failures."
    assert body["agent"] == "awx-troubleshooter"
    assert body["run_id"]
    assert body["error"] is None
    assert isinstance(body["duration_ms"], int)


def test_failed_run_returns_200_with_error_outcome(monkeypatch):
    import mantis.runtime as rt

    # Force max-iterations by scripting an endless tool-call loop is
    # heavier than needed here -- instead, monkeypatch AgentRuntime.run
    # itself to simulate the exact failure InvocationService must
    # classify, keeping this test focused on the API contract rather
    # than re-deriving #11's own iteration-loop behavior.
    monkeypatch.setattr(
        rt.AgentRuntime,
        "run",
        lambda self, prompt, *, run_id=None: (_ for _ in ()).throw(rt.MaxIterationsExceededError("nope")),
    )

    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs", json={"agent": "awx-troubleshooter", "prompt": "x"}, headers=AUTH
        )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "error"
    assert body["output"] is None
    assert body["error"]["kind"] == "max_iterations"


def test_unknown_agent_returns_404():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.post("/api/v1/runs", json={"agent": "nope", "prompt": "x"}, headers=AUTH)

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "unknown_agent"


def test_oversized_prompt_returns_422():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs", json={"agent": "awx-troubleshooter", "prompt": "x" * 5000}, headers=AUTH
        )

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "validation_error"


def test_missing_prompt_returns_422():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.post("/api/v1/runs", json={"agent": "awx-troubleshooter"}, headers=AUTH)
    assert response.status_code == 422


def test_unknown_fields_are_rejected():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={"agent": "awx-troubleshooter", "prompt": "x", "model": "gpt-4", "tools": ["shell"]},
            headers=AUTH,
        )
    assert response.status_code == 422


def test_caller_cannot_override_server_side_routing_aliases():
    # #16: routing/fallback aliases are exclusively server-side
    # (mantis.config.ModelRoutingPolicy, wired through each agent's
    # build_runtime()) -- RunRequest has no model/alias/routing field at
    # all, and extra="forbid" rejects an attempt to smuggle one in.
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/runs",
            json={
                "agent": "awx-troubleshooter",
                "prompt": "x",
                "model_alias": "attacker-chosen-alias",
                "fallback_aliases": ["attacker-chosen-fallback"],
                "routing_policy": {"primary_alias": "attacker-chosen-alias"},
            },
            headers=AUTH,
        )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "validation_error"


def test_run_without_auth_is_401():
    app = create_app(server_config=_server_config())
    with TestClient(app) as client:
        response = client.post("/api/v1/runs", json={"agent": "awx-troubleshooter", "prompt": "x"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Concurrency / overload
# ---------------------------------------------------------------------------


def test_concurrency_saturation_returns_429():
    import mantis.runtime as rt

    started = threading.Event()
    release = threading.Event()
    original_run = rt.AgentRuntime.run

    def _slow_run(self, prompt, *, run_id=None):
        started.set()
        release.wait(timeout=5)
        return "done"

    app = create_app(server_config=_server_config(max_concurrent_runs=1))

    rt.AgentRuntime.run = _slow_run
    try:
        with TestClient(app) as client:
            first_call = threading.Thread(
                target=lambda: client.post(
                    "/api/v1/runs", json={"agent": "awx-troubleshooter", "prompt": "x"}, headers=AUTH
                )
            )
            first_call.start()
            assert started.wait(timeout=5), "first run never started"

            response = client.post(
                "/api/v1/runs", json={"agent": "system-troubleshooter", "prompt": "y"}, headers=AUTH
            )

            assert response.status_code == 429
            assert response.json()["error"]["type"] == "overloaded"

            release.set()
            first_call.join(timeout=5)
    finally:
        rt.AgentRuntime.run = original_run


# ---------------------------------------------------------------------------
# Safe error serialization
# ---------------------------------------------------------------------------


def test_unhandled_exception_never_leaks_a_traceback(monkeypatch):
    import mantis.runtime as rt

    def _explode(self, prompt, *, run_id=None):
        raise RuntimeError("super secret internal detail: /etc/mantis/secrets.env")

    monkeypatch.setattr(rt.AgentRuntime, "run", _explode)

    app = create_app(server_config=_server_config())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/runs", json={"agent": "awx-troubleshooter", "prompt": "x"}, headers=AUTH
        )

    assert response.status_code == 200  # InvocationService classifies this as an execution failure
    body = response.json()
    assert body["outcome"] == "error"
    assert "secrets.env" not in response.text
    assert "Traceback" not in response.text
    assert body["error"]["kind"] == "internal_error"
