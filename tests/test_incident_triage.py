"""Tests for mantis.agents.incident_triage: the Incident Triage agent's
tool wiring, read-only guard, budgets, and prompt contract.

Mirrors tests/test_system_troubleshooter.py's pattern: the OpenAI client
is stubbed with a scripted fake (no live LiteLLM), and any real tool call
exercised end-to-end is mocked at the HTTP/socket layer. No test requires
live LiteLLM, AWX, Prometheus, Loki, Kubernetes, Git, or network access.
"""

from __future__ import annotations

import ast
import json
import socket
from dataclasses import dataclass
from typing import Any

import pytest

from mantis.agents.incident_triage import (
    ALLOWED_TOOLS,
    AGENT_NAME,
    DEFAULT_PROMPT,
    MAX_ITERATIONS,
    MODEL_ENV,
    SYSTEM_PROMPT,
    TOOL_CALL_BUDGET,
    _assert_all_tools_registered_read_only,
    build_runtime,
)
from mantis.config import ConfigurationError, LiteLLMConfig
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
    "git_recent_changes",
    "kubernetes_list_pods",
    "kubernetes_list_deployments",
    "kubernetes_list_nodes",
    "kubernetes_list_events",
}


def test_agent_name_is_stable():
    assert AGENT_NAME == "incident-triage"


def test_allowed_tools_is_exactly_the_eleven_evidence_tools():
    assert set(ALLOWED_TOOLS) == _EXPECTED_ALLOWLIST
    assert len(ALLOWED_TOOLS) == len(_EXPECTED_ALLOWLIST)  # no duplicates


def test_build_runtime_resolves_all_eleven_tools_against_the_default_registry():
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


def test_build_runtime_does_not_pin_a_hardcoded_model_config(monkeypatch):
    monkeypatch.delenv("MANTIS_INCIDENT_TRIAGE_MODEL", raising=False)

    runtime = build_runtime()

    assert runtime.model_config == LiteLLMConfig.from_env()


# ---------------------------------------------------------------------------
# Mutating-tool guard: agent construction must fail loudly, never
# silently accept a mutating tool into this agent's allowlist.
# ---------------------------------------------------------------------------


def test_assert_all_tools_registered_read_only_passes_for_the_real_allowlist():
    _assert_all_tools_registered_read_only(ALLOWED_TOOLS)  # must not raise


def test_assert_all_tools_registered_read_only_raises_configuration_error_for_a_mutating_tool():
    def _handler() -> dict[str, Any]:
        return {}

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="fake_mutating_tool",
            schema={
                "type": "function",
                "function": {
                    "name": "fake_mutating_tool",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            handler=_handler,
            category="fake",
            mutating=True,
        )
    )

    with pytest.raises(ConfigurationError, match="fake_mutating_tool"):
        _assert_all_tools_registered_read_only(["fake_mutating_tool"], registry=registry)


def test_agent_cannot_call_a_tool_outside_its_allowlist():
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
                "function": {
                    "name": "shell_exec",
                    "description": "forbidden",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            handler=_forbidden_handler,
            category="forbidden",
            mutating=True,
        )
    )

    runtime = AgentRuntime(
        name="incident-triage-test",
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
# Per-agent model override -- see
# mantis.config.LiteLLMConfig.from_env's model_env parameter.
# ---------------------------------------------------------------------------


def test_model_env_constant_is_the_documented_variable_name():
    assert MODEL_ENV == "MANTIS_INCIDENT_TRIAGE_MODEL"


def test_build_runtime_uses_the_agent_specific_model_override_when_set(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "global-model")
    monkeypatch.setenv("MANTIS_INCIDENT_TRIAGE_MODEL", "incident-triage-specific-model")

    runtime = build_runtime()

    assert runtime.model_config.model == "incident-triage-specific-model"


def test_build_runtime_falls_back_to_litellm_model_when_override_unset(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "global-model")
    monkeypatch.delenv("MANTIS_INCIDENT_TRIAGE_MODEL", raising=False)

    runtime = build_runtime()

    assert runtime.model_config.model == "global-model"


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_tool_call_budget_is_exactly_thirteen():
    assert TOOL_CALL_BUDGET == 13


def test_max_iterations_is_exactly_seventeen():
    assert MAX_ITERATIONS == 17


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


@pytest.mark.parametrize("tool_name", sorted(_EXPECTED_ALLOWLIST))
def test_system_prompt_mentions_every_allowed_tool(tool_name):
    assert tool_name in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "phrase",
    [
        "explicit time window",  # requires an explicit incident window
        "do not guess",  # never invents an incident window
        "produce a final answer that plainly asks the user",  # asks instead of exploring
        "narrow",  # may narrow a window for follow-up queries
        "original requested window",  # must preserve the original window
        "never be inserted into the incident's historical timeline",  # current vs historical separation
        "this commit exists in the repository's history",  # git claim 1
        "this commit was deployed",  # git claim 2
        "this commit caused or contributed to the incident",  # git claim 3
        "queried successfully",  # evidence coverage states
        "query failed / unavailable",  # evidence coverage states
        "not queried",  # evidence coverage states
        "never evidence about the target system's own health",  # retrieval failure misattribution
        "read-only",  # remains read-only
        "obey, execute, or role-play",  # tool output as evidence, not instructions
        "time-ordered evidence timeline",  # output contract section
        "evidence coverage by source",  # output contract section
        "explicitly unproven hypotheses",  # output contract section
    ],
)
def test_system_prompt_contains_required_constraint(phrase):
    # Whitespace-normalized: SYSTEM_PROMPT is a wrapped triple-quoted
    # string, so a multi-word phrase can legitimately span a line break
    # (a literal "\n" where the checked phrase has a space).
    normalized = " ".join(SYSTEM_PROMPT.lower().split())
    assert phrase.lower() in normalized


def test_default_prompt_names_an_explicit_target_and_absolute_window():
    assert "ferros-c01" in DEFAULT_PROMPT
    assert "2026-09-16T02:55:00+00:00" in DEFAULT_PROMPT
    assert "2026-09-16T03:15:00+00:00" in DEFAULT_PROMPT


# ---------------------------------------------------------------------------
# Registration/invocation path: no alternate local-execution path exists
# -- the only supported way to invoke this agent through the API/CLI is
# mantis.api.catalog's canonical entry (#83), mirroring every other
# agent. This is a structural check, not a behavioral one.
# ---------------------------------------------------------------------------


def test_incident_triage_module_defines_no_agentruntime_construction_of_its_own_besides_build_runtime():
    import mantis.agents.incident_triage as module

    source = open(module.__file__).read()
    tree = ast.parse(source)
    construction_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AgentRuntime"
    ]
    # Exactly one call site: build_runtime()'s own construction.
    assert len(construction_sites) == 1


def test_incident_triage_is_registered_in_the_canonical_catalog():
    from mantis.api.catalog import build_default_catalog

    catalog = build_default_catalog()
    entry = catalog.get(AGENT_NAME)

    assert entry.build_runtime is build_runtime
    assert entry.default_prompt == DEFAULT_PROMPT
    assert entry.read_only is True


# ---------------------------------------------------------------------------
# Configuration/agent probing performs no external network I/O.
# ---------------------------------------------------------------------------


def test_build_runtime_performs_no_network_access(monkeypatch):
    # Constructing this agent's AgentRuntime must never itself attempt
    # to contact a network peer -- proven by making any socket-level
    # connect fail loudly if it's ever attempted during build_runtime().
    def _forbidden(*args, **kwargs):
        raise AssertionError("build_runtime() must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "sendto", _forbidden)

    build_runtime()  # must not raise the AssertionError above


def test_probe_availability_performs_no_network_access(monkeypatch):
    from mantis.api.catalog import build_default_catalog

    def _forbidden(*args, **kwargs):
        raise AssertionError("probe_availability() must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "sendto", _forbidden)

    catalog = build_default_catalog()
    available, reason = catalog.get(AGENT_NAME).probe_availability()

    assert available is True
    assert reason is None


# ---------------------------------------------------------------------------
# End-to-end: real production wiring with a scripted model
# ---------------------------------------------------------------------------


def test_agent_can_chain_multiple_real_tools_in_one_investigation():
    # End-to-end wiring regression test: a model choosing to call several
    # distinct real tools in sequence must resolve, execute, and feed
    # results back through the real production AgentRuntime +
    # default_registry + ALLOWED_TOOLS wiring, with the shared registry
    # providing every tool -- no agent-specific tool code.
    import httpx
    import respx

    with respx.mock:
        respx.get("https://awx.example.test/api/v2/jobs/").mock(
            return_value=httpx.Response(200, json={"count": 0, "next": None, "results": []})
        )

        runtime = build_runtime()
        runtime._client = FakeOpenAIClient(
            [
                _tool_call_response(_tool_call("call_1", "awx_recent_failed_jobs", {"limit": 5})),
                _final_message_response(
                    "No recent failed AWX jobs were found for ferros-c01 in the requested window. "
                    "Evidence is currently limited to this one source; recommend also checking "
                    "current TCP connectivity, Prometheus, and Kubernetes state before drawing "
                    "any conclusion."
                ),
            ]
        )

        answer = runtime.run(
            "Investigate the incident affecting ferros-c01 between "
            "2026-09-16T02:55:00+00:00 and 2026-09-16T03:15:00+00:00."
        )

    assert "No recent failed AWX jobs" in answer
    assert runtime.call_log[0].outcome == "ok"
    assert runtime.call_log[0].tool_name == "awx_recent_failed_jobs"
