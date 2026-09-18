"""Tests for mantis.agents.system_troubleshooter: the System
Troubleshooter agent's tool wiring, budgets, and prompt contract (#11).

Mirrors tests/test_awx_troubleshooter.py's pattern: the OpenAI client is
stubbed with a scripted fake (no live LiteLLM), and AWX/Prometheus/Loki
HTTP calls are mocked with respx where a real end-to-end tool call is
exercised. No test requires live LiteLLM, AWX, Prometheus, Loki, or
network access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from mantis.agents.system_troubleshooter import (
    ALLOWED_TOOLS,
    DEFAULT_PROMPT,
    MAX_ITERATIONS,
    SYSTEM_PROMPT,
    TOOL_CALL_BUDGET,
    build_runtime,
)
from mantis.registry import Tool, ToolRegistry
from mantis.runtime import AgentRuntime


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


def _tool_call_response(*calls: FakeToolCall) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=None, tool_calls=list(calls)))])


def _final_message_response(text: str) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=text, tool_calls=None))])


# ---------------------------------------------------------------------------
# Exact allowlist / wiring
# ---------------------------------------------------------------------------

_EXPECTED_ALLOWLIST = {
    "awx_recent_failed_jobs",
    "awx_get_job_failure",
    "check_tcp_connectivity",
    "prometheus_query",
    "prometheus_query_range",
    "loki_query",
}


def test_allowed_tools_is_exactly_the_six_evidence_tools():
    assert set(ALLOWED_TOOLS) == _EXPECTED_ALLOWLIST
    assert len(ALLOWED_TOOLS) == len(_EXPECTED_ALLOWLIST)  # no duplicates


def test_build_runtime_resolves_all_six_tools_against_the_default_registry():
    # The real ToolNotFoundError risk this guards against: if
    # ALLOWED_TOOLS ever names a tool that isn't registered in
    # default_registry, AgentRuntime.__post_init__ raises immediately.
    runtime = build_runtime()

    resolved_names = {tool.name for tool in runtime._resolved_tools.values()}
    assert resolved_names == _EXPECTED_ALLOWLIST


def test_all_resolved_tools_are_read_only():
    runtime = build_runtime()

    assert all(tool.mutating is False for tool in runtime._resolved_tools.values())


def test_all_resolved_tools_are_registered_as_containing_untrusted_text():
    # Every evidence source is external, Mantis-uncontrolled data -- #14
    # must apply to all of it, not just some.
    runtime = build_runtime()

    assert all(tool.contains_untrusted_text is True for tool in runtime._resolved_tools.values())


def test_build_runtime_uses_the_shared_default_registry():
    from mantis.registry import default_registry

    runtime = build_runtime()

    assert runtime.registry is default_registry


def test_build_runtime_does_not_pin_a_specific_model_config():
    # Model selection must be configuration-driven (a configured LiteLLM
    # alias via LITELLM_MODEL), never a provider-specific model ID
    # hardcoded in the agent -- see mantis.config.LiteLLMConfig.from_env
    # and this agent's build_runtime docstring.
    runtime = build_runtime()

    from mantis.config import LiteLLMConfig

    assert runtime.model_config == LiteLLMConfig.from_env()


def test_agent_cannot_call_a_tool_outside_its_allowlist():
    # Every tool currently registered in the real default_registry
    # happens to also be in ALLOWED_TOOLS (there is no other tool to
    # test exclusion against there), so this proves the allowlist
    # mechanism itself using a deliberately separate registry containing
    # an extra, disallowed tool -- the runtime must reject a call to it
    # as "unknown_tool" rather than executing it, purely because it was
    # never named in `tools=`.
    executed: list[str] = []

    def _forbidden_handler() -> dict[str, Any]:
        executed.append("called")
        return {"result": "should never run"}

    registry = ToolRegistry()
    for name in ALLOWED_TOOLS:
        from mantis.registry import default_registry

        registry.register(default_registry.get(name))
    registry.register(
        Tool(
            name="shell_exec",
            schema={
                "type": "function",
                "function": {"name": "shell_exec", "description": "forbidden", "parameters": {"type": "object", "properties": {}}},
            },
            handler=_forbidden_handler,
            category="forbidden",
            mutating=True,
        )
    )

    runtime = AgentRuntime(
        name="system-troubleshooter-test",
        system_prompt=SYSTEM_PROMPT,
        tools=ALLOWED_TOOLS,  # deliberately excludes "shell_exec"
        registry=registry,
        tool_call_budget=TOOL_CALL_BUDGET,
        max_iterations=MAX_ITERATIONS,
    )
    runtime._client = FakeOpenAIClient(
        [
            _tool_call_response(_tool_call("call_1", "shell_exec", {})),
            _final_message_response("done"),
        ]
    )

    runtime.run("do something forbidden")

    assert executed == []
    assert runtime.call_log[0].outcome == "unknown_tool"


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_tool_call_budget_is_exactly_eight():
    assert TOOL_CALL_BUDGET == 8


def test_max_iterations_is_exactly_twelve():
    assert MAX_ITERATIONS == 12


def test_max_iterations_exceeds_tool_call_budget_with_headroom_for_a_final_answer():
    # If this were ever violated, the agent could exhaust its tool-call
    # budget with no iteration left to actually produce a final answer.
    assert MAX_ITERATIONS > TOOL_CALL_BUDGET


def test_build_runtime_wires_the_documented_budgets():
    runtime = build_runtime()

    assert runtime.tool_call_budget == TOOL_CALL_BUDGET
    assert runtime.max_iterations == MAX_ITERATIONS


def test_tool_schemas_are_withheld_once_the_budget_is_exhausted():
    # End-to-end proof the configured budget actually takes effect: after
    # TOOL_CALL_BUDGET successful tool calls, no further tools are
    # offered, so the next model call must produce a final answer.
    #
    # Uses awx_recent_failed_jobs (mocked via respx -- no live network)
    # rather than check_tcp_connectivity, which would otherwise attempt a
    # real DNS/socket connection. Distinct `limit` per call: the runtime
    # replays an *exact* duplicate call (same tool, same arguments) from
    # cache without incrementing successful_tool_calls, so identical
    # arguments here would silently prevent the budget from ever being
    # reached.
    import httpx
    import respx

    responses = [
        _tool_call_response(_tool_call(f"call_{i}", "awx_recent_failed_jobs", {"limit": i + 1}))
        for i in range(TOOL_CALL_BUDGET)
    ]
    responses.append(_final_message_response("Investigation complete."))

    runtime = build_runtime()
    fake_client = FakeOpenAIClient(responses)
    runtime._client = fake_client

    with respx.mock:
        respx.get("https://awx.example.test/api/v2/jobs/").mock(
            return_value=httpx.Response(200, json={"count": 0, "next": None, "results": []})
        )
        answer = runtime.run(DEFAULT_PROMPT)

    assert answer == "Investigation complete."
    # The final call (the (TOOL_CALL_BUDGET + 1)-th) must not have been
    # offered tool schemas.
    assert "tools" not in fake_client.chat.completions.calls[-1]
    assert len(fake_client.chat.completions.calls) == TOOL_CALL_BUDGET + 1


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "awx_recent_failed_jobs",
        "awx_get_job_failure",
        "check_tcp_connectivity",
        "prometheus_query",
        "prometheus_query_range",
        "loki_query",
    ],
)
def test_system_prompt_mentions_every_allowed_tool(tool_name):
    assert tool_name in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "phrase",
    [
        "historical",  # distinguishes historical AWX evidence
        "current-state",  # distinguishes current TCP observations
        "time-series",  # distinguishes Prometheus observations
        "log lines",  # distinguishes Loki log evidence
        "not evidence about the target system",  # retrieval failure != target-system evidence
        "hypothesis",  # facts vs hypotheses separation
        "confidence",  # confidence tied to evidence strength
        "unproven",  # explicitly unproven hypotheses
        "truncated",  # acknowledge truncated/missing evidence
        "never repeat an identical call",  # discourages redundant calls
        "stop calling tools",  # stop when evidence is sufficient
        "read-only",  # remains read-only
        "obey, execute, or role-play",  # tool output as evidence, not instructions
    ],
)
def test_system_prompt_contains_required_constraint(phrase):
    # Whitespace-normalized: SYSTEM_PROMPT is a wrapped triple-quoted
    # string, so a multi-word phrase can legitimately span a line break
    # (a literal "\n" where the checked phrase has a space).
    normalized = " ".join(SYSTEM_PROMPT.lower().split())
    assert phrase.lower() in normalized


def test_default_prompt_is_the_ferros_c01_worked_example():
    assert "ferros-c01" in DEFAULT_PROMPT


# ---------------------------------------------------------------------------
# End-to-end: real production wiring with a scripted model
# ---------------------------------------------------------------------------


def test_agent_can_chain_multiple_real_tools_in_one_investigation():
    # End-to-end wiring regression test: a model choosing to call several
    # distinct real tools in sequence must resolve, execute, and feed
    # results back through the real production AgentRuntime +
    # default_registry + ALLOWED_TOOLS wiring, with the shared registry
    # providing every tool -- no agent-specific tool code.
    import respx
    import httpx

    with respx.mock:
        respx.get("https://awx.example.test/api/v2/jobs/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "count": 0,
                    "next": None,
                    "results": [],
                },
            )
        )

        runtime = build_runtime()
        runtime._client = FakeOpenAIClient(
            [
                _tool_call_response(_tool_call("call_1", "awx_recent_failed_jobs", {"limit": 5})),
                _final_message_response(
                    "No recent failed AWX jobs were found for ferros-c01. Evidence is currently "
                    "limited to this one source; recommend also checking current TCP connectivity "
                    "and recent monitoring/log data before drawing any conclusion."
                ),
            ]
        )

        answer = runtime.run("Why is ferros-c01 unreachable?")

    assert "No recent failed AWX jobs" in answer
    assert runtime.call_log[0].outcome == "ok"
    assert runtime.call_log[0].tool_name == "awx_recent_failed_jobs"
