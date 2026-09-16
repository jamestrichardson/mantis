"""Tests for mantis.runtime.AgentRuntime.

The OpenAI client is stubbed out entirely (no network calls) so these
tests exercise only the runtime's dispatch/loop logic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from mantis.config import LiteLLMConfig, Secret
from mantis.registry import Tool, ToolRegistry
from mantis.runtime import AgentRuntime, MaxIterationsExceededError


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
    reasoning_content: str | None = None
    """Simulates a provider-specific extra field (e.g. some backends put
    chain-of-thought here instead of/alongside content) — exercises
    AgentRuntime's model_dump()-based diagnostic capture."""

    def model_dump(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": self.tool_calls,
            "reasoning_content": self.reasoning_content,
        }


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: Any = None


@dataclass
class FakeUsage:
    """Mimics the OpenAI SDK's pydantic-based usage object closely enough
    to exercise AgentRuntime's model_dump()-based extraction."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def model_dump(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class FakeCompletions:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeCompletions ran out of scripted responses")
        return self._responses.pop(0)


class FakeChat:
    def __init__(self, responses: list[FakeResponse]):
        self.completions = FakeCompletions(responses)


class FakeOpenAIClient:
    def __init__(self, responses: list[FakeResponse]):
        self.chat = FakeChat(responses)


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> FakeToolCall:
    return FakeToolCall(id=call_id, function=FakeFunctionCall(name=name, arguments=json.dumps(arguments)))


def _final_message_response(text: str) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=text, tool_calls=None))])


def _tool_call_response(*calls: FakeToolCall) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=None, tool_calls=list(calls)))])


def _build_runtime(
    responses: list[FakeResponse],
    *,
    tools: list[str] | None = None,
    registry: ToolRegistry | None = None,
    max_iterations: int = 8,
    tool_call_budget: int | None = None,
    temperature: float | None = None,
) -> AgentRuntime:
    runtime = AgentRuntime(
        name="test-agent",
        system_prompt="You are a test agent.",
        tools=tools or [],
        model_config=LiteLLMConfig(url="http://localhost:4000", api_key=Secret("k"), model="m"),
        registry=registry or ToolRegistry(),
        max_iterations=max_iterations,
        tool_call_budget=tool_call_budget,
        temperature=temperature,
    )
    runtime._client = FakeOpenAIClient(responses)
    return runtime


def _echo_tool(name: str = "echo", handler=None) -> Tool:
    schema = {
        "type": "function",
        "function": {"name": name, "description": "echoes input", "parameters": {}},
    }
    return Tool(name=name, schema=schema, handler=handler or (lambda **kw: {"echo": kw}))


def test_run_returns_final_answer_with_no_tool_calls():
    runtime = _build_runtime([_final_message_response("hello there")])

    result = runtime.run("hi")

    assert result == "hello there"


def test_run_dispatches_tool_call_and_returns_final_answer():
    registry = ToolRegistry()
    registry.register(_echo_tool(handler=lambda x: {"got": x}))

    responses = [
        _tool_call_response(_tool_call("call_1", "echo", {"x": 1})),
        _final_message_response("done"),
    ]
    runtime = _build_runtime(responses, tools=["echo"], registry=registry)

    result = runtime.run("do the thing")

    assert result == "done"
    assert runtime.call_log[-1].outcome == "ok"
    assert runtime.call_log[-1].result == {"got": 1}
    # The tool result must be fed back to the model as a tool message.
    second_call_messages = runtime._client.chat.completions.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"]) == {"got": 1}


def test_run_handles_unknown_tool_cleanly():
    responses = [
        _tool_call_response(_tool_call("call_1", "not_registered", {})),
        _final_message_response("recovered"),
    ]
    runtime = _build_runtime(responses, tools=[])

    result = runtime.run("do the thing")

    assert result == "recovered"
    assert runtime.call_log[-1].outcome == "unknown_tool"


def test_run_handles_malformed_arguments_cleanly():
    registry = ToolRegistry()
    registry.register(_echo_tool())

    bad_call = FakeToolCall(id="call_1", function=FakeFunctionCall(name="echo", arguments="{not json"))
    responses = [
        _tool_call_response(bad_call),
        _final_message_response("recovered"),
    ]
    runtime = _build_runtime(responses, tools=["echo"], registry=registry)

    result = runtime.run("do the thing")

    assert result == "recovered"
    assert runtime.call_log[-1].outcome == "bad_arguments"


def test_run_handles_integration_exception_cleanly():
    def boom(**kwargs):
        raise RuntimeError("integration exploded")

    registry = ToolRegistry()
    registry.register(_echo_tool(handler=boom))

    responses = [
        _tool_call_response(_tool_call("call_1", "echo", {})),
        _final_message_response("recovered"),
    ]
    runtime = _build_runtime(responses, tools=["echo"], registry=registry)

    result = runtime.run("do the thing")

    assert result == "recovered"
    assert runtime.call_log[-1].outcome == "error"
    assert "integration exploded" in runtime.call_log[-1].detail
    assert runtime.call_log[-1].result is None


def test_run_detects_exact_duplicate_tool_calls():
    registry = ToolRegistry()
    call_count = {"n": 0}

    def counting_handler(**kwargs):
        call_count["n"] += 1
        return {"n": call_count["n"]}

    registry.register(_echo_tool(handler=counting_handler))

    responses = [
        _tool_call_response(_tool_call("call_1", "echo", {"x": 1})),
        _tool_call_response(_tool_call("call_2", "echo", {"x": 1})),
        _final_message_response("done"),
    ]
    runtime = _build_runtime(responses, tools=["echo"], registry=registry)

    result = runtime.run("do the thing")

    assert result == "done"
    assert call_count["n"] == 1  # handler only actually executed once
    assert runtime.call_log[-1].outcome == "duplicate"
    assert runtime.call_log[-1].result == {"n": 1}  # the cached result


def test_duplicate_tool_call_replays_cached_result_not_an_error():
    # Regression test: a duplicate call must never be answered with an
    # error that withholds data — a model that re-asks for the same thing
    # (common with smaller/local models) must still get the real result,
    # or it has nothing to work with and can end up parroting an error
    # back as its "final answer" instead of ever producing a real one.
    registry = ToolRegistry()
    registry.register(_echo_tool(handler=lambda **kw: {"jobs": ["job-1", "job-2"]}))

    responses = [
        _tool_call_response(_tool_call("call_1", "echo", {"limit": 5})),
        _tool_call_response(_tool_call("call_2", "echo", {"limit": 5})),
        _final_message_response("done"),
    ]
    runtime = _build_runtime(responses, tools=["echo"], registry=registry)

    runtime.run("do the thing")

    third_call_messages = runtime._client.chat.completions.calls[2]["messages"]
    tool_messages = [m for m in third_call_messages if m["role"] == "tool"]
    duplicate_reply = json.loads(tool_messages[-1]["content"])

    assert "error" not in duplicate_reply
    assert duplicate_reply["result"] == {"jobs": ["job-1", "job-2"]}


def test_run_raises_when_max_iterations_exceeded():
    registry = ToolRegistry()
    registry.register(_echo_tool())

    # Always returns a tool call, never a final answer.
    responses = [
        _tool_call_response(_tool_call(f"call_{i}", "echo", {"x": i})) for i in range(3)
    ]
    runtime = _build_runtime(responses, tools=["echo"], registry=registry, max_iterations=3)

    with pytest.raises(MaxIterationsExceededError):
        runtime.run("do the thing")


def test_tool_choice_is_never_sent():
    runtime = _build_runtime([_final_message_response("hi")])

    runtime.run("hello")

    call_kwargs = runtime._client.chat.completions.calls[0]
    assert "tool_choice" not in call_kwargs


def test_no_tools_key_sent_when_agent_has_no_tools():
    runtime = _build_runtime([_final_message_response("hi")], tools=[])

    runtime.run("hello")

    call_kwargs = runtime._client.chat.completions.calls[0]
    assert "tools" not in call_kwargs


def test_tool_call_budget_withholds_tools_after_it_is_reached():
    # Regression test for the real-world slowdown this was added to fix:
    # once a single-tool agent has a successful result, tool schemas must
    # stop being offered so the final answer isn't generated under
    # tool-call grammar constraints (and so the model literally cannot
    # loop on repeat calls).
    registry = ToolRegistry()
    registry.register(_echo_tool(handler=lambda **kw: {"ok": True}))

    responses = [
        _tool_call_response(_tool_call("call_1", "echo", {"x": 1})),
        _final_message_response("done"),
    ]
    runtime = _build_runtime(
        responses, tools=["echo"], registry=registry, tool_call_budget=1
    )

    result = runtime.run("do the thing")

    assert result == "done"
    first_call_kwargs = runtime._client.chat.completions.calls[0]
    second_call_kwargs = runtime._client.chat.completions.calls[1]
    assert "tools" in first_call_kwargs
    assert "tools" not in second_call_kwargs


def test_tool_call_budget_none_keeps_offering_tools_indefinitely():
    registry = ToolRegistry()
    registry.register(_echo_tool(handler=lambda **kw: {"ok": True}))

    responses = [
        _tool_call_response(_tool_call("call_1", "echo", {"x": 1})),
        _tool_call_response(_tool_call("call_2", "echo", {"x": 2})),
        _final_message_response("done"),
    ]
    runtime = _build_runtime(
        responses, tools=["echo"], registry=registry, tool_call_budget=None
    )

    runtime.run("do the thing")

    for call_kwargs in runtime._client.chat.completions.calls:
        assert "tools" in call_kwargs


def test_temperature_is_passed_through_when_set():
    runtime = _build_runtime([_final_message_response("hi")], temperature=0.1)

    runtime.run("hello")

    call_kwargs = runtime._client.chat.completions.calls[0]
    assert call_kwargs["temperature"] == 0.1


def test_temperature_omitted_when_unset():
    runtime = _build_runtime([_final_message_response("hi")])

    runtime.run("hello")

    call_kwargs = runtime._client.chat.completions.calls[0]
    assert "temperature" not in call_kwargs


def test_usage_log_captures_usage_when_backend_returns_it():
    response = FakeResponse(
        choices=[FakeChoice(message=FakeMessage(content="hi", tool_calls=None))],
        usage=FakeUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    runtime = _build_runtime([response])

    runtime.run("hello")

    assert runtime.usage_log == [
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    ]


def test_usage_log_entry_is_none_when_backend_omits_usage():
    runtime = _build_runtime([_final_message_response("hi")])

    runtime.run("hello")

    assert runtime.usage_log == [None]


def test_usage_log_has_one_entry_per_iteration():
    registry = ToolRegistry()
    registry.register(_echo_tool())

    responses = [
        FakeResponse(
            choices=[
                FakeChoice(
                    message=FakeMessage(
                        content=None,
                        tool_calls=[_tool_call("call_1", "echo", {"x": 1})],
                    )
                )
            ],
            usage=FakeUsage(prompt_tokens=20, completion_tokens=2, total_tokens=22),
        ),
        FakeResponse(
            choices=[FakeChoice(message=FakeMessage(content="done", tool_calls=None))],
            usage=FakeUsage(prompt_tokens=30, completion_tokens=8, total_tokens=38),
        ),
    ]
    runtime = _build_runtime(responses, tools=["echo"], registry=registry)

    runtime.run("do the thing")

    assert [entry["total_tokens"] for entry in runtime.usage_log] == [22, 38]


def test_diagnostic_raw_message_captured_when_answer_is_empty_and_no_tool_calls():
    # Regression test for a real qualification finding: a model that spends
    # completion tokens but produces neither usable content nor a tool
    # call (e.g. output landed in a provider-specific field like
    # reasoning_content) must be diagnosable from the run itself.
    response = FakeResponse(
        choices=[
            FakeChoice(
                message=FakeMessage(
                    content="", tool_calls=None, reasoning_content="(unparsed attempt)"
                )
            )
        ],
        usage=FakeUsage(prompt_tokens=800, completion_tokens=16, total_tokens=816),
    )
    runtime = _build_runtime([response])

    result = runtime.run("hello")

    assert result == ""
    assert runtime.diagnostic_raw_message == {
        "content": "",
        "tool_calls": None,
        "reasoning_content": "(unparsed attempt)",
    }


def test_diagnostic_raw_message_not_captured_for_a_real_answer():
    runtime = _build_runtime([_final_message_response("a real answer")])

    runtime.run("hello")

    assert runtime.diagnostic_raw_message is None


def test_diagnostic_raw_message_captured_when_answer_is_whitespace_only():
    # Whitespace-only content is still "no usable answer" for diagnostic
    # purposes, same as truly empty content.
    response = FakeResponse(
        choices=[FakeChoice(message=FakeMessage(content="   \n", tool_calls=None))]
    )
    runtime = _build_runtime([response])

    result = runtime.run("hello")

    assert result == "   \n"
    assert runtime.diagnostic_raw_message is not None
