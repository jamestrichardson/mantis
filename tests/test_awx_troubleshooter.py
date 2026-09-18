"""Tests for mantis.agents.awx_troubleshooter: the real, shipped AWX
Troubleshooter agent's tool wiring.

PR #74 review: awx_get_job_failure (#28) is registered in the shared
tool registry but must also actually be reachable by the one agent that
already exists to consume AWX evidence, not just sit unused in the
registry. These tests prove the real production wiring — ALLOWED_TOOLS,
SYSTEM_PROMPT, and AgentRuntime's tool resolution — actually works end
to end, with a scripted model response (no live LiteLLM).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import respx

from mantis.agents.awx_troubleshooter import ALLOWED_TOOLS, MODEL_ENV, SYSTEM_PROMPT, build_runtime
from mantis.config import LiteLLMConfig


@dataclass
class FakeFunctionCall:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunctionCall


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None

    def model_dump(self) -> dict[str, Any]:
        return {"content": self.content, "tool_calls": self.tool_calls}


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: Any = None


class FakeCompletions:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)

    def create(self, **kwargs):
        return self._responses.pop(0)


class FakeChat:
    def __init__(self, responses: list[FakeResponse]):
        self.completions = FakeCompletions(responses)


class FakeOpenAIClient:
    def __init__(self, responses: list[FakeResponse]):
        self.chat = FakeChat(responses)


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> FakeToolCall:
    return FakeToolCall(id=call_id, function=FakeFunctionCall(name=name, arguments=json.dumps(arguments)))


def _tool_call_response(*calls: FakeToolCall) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=None, tool_calls=list(calls)))])


def _final_message_response(text: str) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=text, tool_calls=None))])


def test_allowed_tools_includes_both_awx_evidence_tools():
    assert "awx_recent_failed_jobs" in ALLOWED_TOOLS
    assert "awx_get_job_failure" in ALLOWED_TOOLS


def test_build_runtime_resolves_both_tools_against_the_default_registry():
    # The real ToolNotFoundError risk this test guards against: if
    # ALLOWED_TOOLS ever names a tool that isn't registered in
    # default_registry, AgentRuntime.__post_init__ raises immediately.
    runtime = build_runtime()

    resolved_names = {tool.name for tool in runtime._resolved_tools.values()}
    assert resolved_names == {"awx_recent_failed_jobs", "awx_get_job_failure"}


def test_system_prompt_explains_when_to_use_the_job_failure_tool():
    assert "awx_get_job_failure" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Per-agent model override (#16 precursor) -- see
# mantis.config.LiteLLMConfig.from_env's model_env parameter.
# ---------------------------------------------------------------------------


def test_model_env_constant_is_the_documented_variable_name():
    assert MODEL_ENV == "MANTIS_AWX_TROUBLESHOOTER_MODEL"


def test_build_runtime_uses_the_agent_specific_model_override_when_set(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "global-model")
    monkeypatch.setenv("MANTIS_AWX_TROUBLESHOOTER_MODEL", "awx-specific-model")

    runtime = build_runtime()

    assert runtime.model_config.model == "awx-specific-model"


def test_build_runtime_falls_back_to_litellm_model_when_override_unset(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "global-model")
    monkeypatch.delenv("MANTIS_AWX_TROUBLESHOOTER_MODEL", raising=False)

    runtime = build_runtime()

    assert runtime.model_config.model == "global-model"
    assert runtime.model_config == LiteLLMConfig.from_env()


@respx.mock
def test_model_can_successfully_call_awx_get_job_failure_through_the_real_wiring():
    # End-to-end wiring regression test: a model choosing to call the
    # new tool (as SYSTEM_PROMPT now instructs it to, for a
    # specific-job-id request) must resolve, execute, and return a
    # well-formed result through the real production AgentRuntime +
    # default_registry + ALLOWED_TOOLS wiring -- only the AWX HTTP layer
    # is mocked.
    respx.get("https://awx.example.test/api/v2/jobs/4231/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 4231,
                "name": "deploy-webservers",
                "status": "failed",
                "started": "2026-09-10T08:00:00Z",
                "finished": "2026-09-10T08:02:11Z",
                "job_explanation": "",
            },
        )
    )
    respx.get("https://awx.example.test/api/v2/jobs/4231/job_events/").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "next": None,
                "results": [
                    {
                        "id": 1,
                        "counter": 1,
                        "event": "runner_on_unreachable",
                        "host_name": "host03",
                        "created": "2026-09-10T08:01:00Z",
                        "failed": True,
                        "stdout": "ssh: connect to host host03 port 22: No route to host",
                    }
                ],
            },
        )
    )
    respx.get("https://awx.example.test/api/v2/jobs/4231/stdout/", params={"format": "txt"}).mock(
        return_value=httpx.Response(200, text="PLAY RECAP\nhost03: unreachable=1")
    )

    runtime = build_runtime()
    runtime._client = FakeOpenAIClient(
        [
            _tool_call_response(_tool_call("call_1", "awx_get_job_failure", {"job_id": 4231})),
            _final_message_response(
                "Job 4231 failed: host03 was unreachable (No route to host)."
            ),
        ]
    )

    answer = runtime.run("Investigate AWX job 4231 in detail.")

    assert answer == "Job 4231 failed: host03 was unreachable (No route to host)."
    assert runtime.call_log[-1].outcome == "ok"
    tool_result = runtime.call_log[-1].result
    assert tool_result["structured_failures"][0]["host"] == "host03"
