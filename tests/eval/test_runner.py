"""Tests for mantis.eval.runner: orchestration against a mocked model
client. No live model/network dependency — this is what AC "Tests cover
runner orchestration with a mocked model client" (#34) requires.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import mantis.runtime as runtime_module
from mantis.config import LiteLLMConfig, Secret
from mantis.eval.runner import run_comparison, run_scenario
from mantis.eval.scenarios import Scenario
from mantis.registry import Tool, ToolRegistry


# ---------------------------------------------------------------------------
# A minimal fake OpenAI client, scripted per model alias.
# ---------------------------------------------------------------------------


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


def _final(text: str) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=text, tool_calls=None))])


def _tool_call_response(name: str, arguments: dict[str, Any]) -> FakeResponse:
    call = FakeToolCall(id="call_1", function=FakeFunctionCall(name=name, arguments=json.dumps(arguments)))
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=None, tool_calls=[call]))])


def _patch_openai(monkeypatch, responses_by_model: dict[str, list[FakeResponse]]):
    """Make AgentRuntime's OpenAI(...) construction return a fake client
    scripted per model — keyed off the model alias in kwargs["model"] at
    call time via a small router client.
    """

    class RouterClient:
        def __init__(self, *_args, **_kwargs):
            self._real_clients: dict[str, FakeOpenAIClient] = {
                model: FakeOpenAIClient(resp) for model, resp in responses_by_model.items()
            }
            self.chat = self

        @property
        def completions(self):
            return self

        def create(self, *, model, **kwargs):
            return self._real_clients[model].chat.completions.create(model=model, **kwargs)

    monkeypatch.setattr(runtime_module, "OpenAI", RouterClient)


def _echo_scenario(**overrides) -> Scenario:
    def build_registry() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="echo",
                schema={
                    "type": "function",
                    "function": {"name": "echo", "description": "echo", "parameters": {}},
                },
                handler=lambda **kw: {"echoed": kw},
            )
        )
        return registry

    defaults = dict(
        name="echo-scenario",
        version="1.0",
        description="test scenario",
        prompt="say hi",
        system_prompt="you are a test agent",
        agent_tools=["echo"],
        build_registry=build_registry,
    )
    defaults.update(overrides)
    return Scenario(**defaults)


def _base_config() -> LiteLLMConfig:
    return LiteLLMConfig(url="http://localhost:4000", api_key=Secret("k"), model="unused")


# ---------------------------------------------------------------------------
# run_scenario
# ---------------------------------------------------------------------------


def test_run_scenario_against_one_model(monkeypatch):
    _patch_openai(monkeypatch, {"model-a": [_final("hello")]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.outcome == "ok"
    assert result.model == "model-a"
    assert result.scenario == "echo-scenario"
    assert result.scenario_version == "1.0"
    assert result.final_answer == "hello"
    assert result.elapsed_seconds >= 0
    assert result.tool_calls == []


def test_run_scenario_captures_tool_call_trace(monkeypatch):
    _patch_openai(
        monkeypatch,
        {"model-a": [_tool_call_response("echo", {"x": 1}), _final("done")]},
    )
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.outcome == "ok"
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.tool_name == "echo"
    assert call.arguments == {"x": 1}
    assert call.outcome == "ok"
    assert call.result == {"echoed": {"x": 1}}


def test_run_scenario_counts_duplicate_and_malformed_calls(monkeypatch):
    bad_call = FakeToolCall(id="c1", function=FakeFunctionCall(name="echo", arguments="{not json"))
    bad_response = FakeResponse(choices=[FakeChoice(message=FakeMessage(content=None, tool_calls=[bad_call]))])
    dup_call = _tool_call_response("echo", {"x": 1})

    _patch_openai(
        monkeypatch,
        {
            "model-a": [
                bad_response,
                _tool_call_response("echo", {"x": 1}),
                dup_call,
                _final("done"),
            ]
        },
    )
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.malformed_call_count == 1
    assert result.duplicate_call_count == 1


def test_run_scenario_records_error_outcome_without_raising(monkeypatch):
    # No scripted responses at all -> FakeCompletions.create() will raise
    # IndexError (pop from empty list) on the very first call, simulating
    # an arbitrary model/runtime failure.
    _patch_openai(monkeypatch, {"model-a": []})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.outcome == "error"
    assert result.final_answer is None
    assert result.error is not None


def test_run_scenario_captures_usage_and_total_tokens(monkeypatch):
    response = FakeResponse(
        choices=[FakeChoice(message=FakeMessage(content="hi", tool_calls=None))],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    _patch_openai(monkeypatch, {"model-a": [response]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.usage == [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}]
    assert result.total_tokens == 15


# ---------------------------------------------------------------------------
# run_comparison
# ---------------------------------------------------------------------------


def test_run_comparison_runs_every_model_even_if_one_fails(monkeypatch):
    _patch_openai(
        monkeypatch,
        {
            "good-model": [_final("all good")],
            "bad-model": [],  # will error immediately
        },
    )
    scenario = _echo_scenario()

    results = run_comparison(scenario, ["good-model", "bad-model"], base_model_config=_base_config())

    assert [r.model for r in results] == ["good-model", "bad-model"]
    assert results[0].outcome == "ok"
    assert results[1].outcome == "error"


def test_run_comparison_supports_a_single_model(monkeypatch):
    _patch_openai(monkeypatch, {"only-model": [_final("hi")]})
    scenario = _echo_scenario()

    results = run_comparison(scenario, ["only-model"], base_model_config=_base_config())

    assert len(results) == 1
    assert results[0].model == "only-model"
