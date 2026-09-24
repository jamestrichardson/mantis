"""End-to-end routing evidence for #16, through a real local HTTP
boundary and the real ``openai`` SDK -- see ``tests/_llm_fixtures.py``.

Nothing here monkeypatches ``chat.completions.create()``. Every test
talks to a real ``MockLLMGateway`` (a genuine local ``http.server``
instance on an ephemeral port) through the real, unmodified
``mantis.runtime.build_openai_client`` construction path. This is the
deterministic local HTTP-boundary evidence Issue #16's revised
acceptance criteria ask for, replacing any dependency on a production/
self-hosted LiteLLM deployment.

Three layers, per the issue's "at least one test must exercise the
real qualification/eval plumbing" requirement:

- ``test_e2e_...`` (direct ``AgentRuntime``): the most detailed,
  assertion-heavy proof of every routing mechanic, with the client's
  own retry behavior disabled (``.with_options(max_retries=0)``, a
  real, public ``openai`` SDK method -- not a modification of
  ``create()`` itself) so each Mantis-level attempt maps to exactly
  one real HTTP request.
- ``test_e2e_run_scenario_...`` (``mantis.eval.runner.run_scenario``):
  the real eval harness, unmodified. The real ``openai`` SDK's own
  default retry behavior (``max_retries=2``) is left in place here on
  purpose and accounted for explicitly in the assertions -- a 5xx is
  itself retried by the SDK before Mantis's own routing/fallback ever
  observes the exception, which is real, observable behavior worth
  keeping visible rather than hidden.
- ``test_e2e_qualify_models_...`` / ``test_e2e_cli_qualify_...``: the
  real qualification path and the real ``mantis eval qualify`` CLI
  command handler, demonstrating the same scenario primary-only vs.
  primary+fallback (#16's Evaluation AC).
"""

from __future__ import annotations

import json

from mantis.config import LiteLLMConfig, ModelRoutingPolicy, Secret
from mantis.eval.qualification import qualify_models
from mantis.eval.runner import run_scenario
from mantis.eval.scenarios import Scenario, default_scenarios
from mantis.registry import Tool, ToolRegistry
from mantis.routing import ModelCallFailureKind
from mantis.runtime import AgentRuntime, ModelRoutingExhaustedError

from _llm_fixtures import MockLLMGateway, final_message_response, tool_call_response

_MANTIS_METADATA_KEYS = {"mantis_agent", "mantis_run_id", "mantis_iteration"}


def _lookup_evidence_tool(call_counter: list[int]) -> Tool:
    """A minimal read-only tool whose handler counts invocations --
    the mechanism every "executed exactly once" assertion below relies
    on."""

    def handler(**kwargs: object) -> dict[str, object]:
        call_counter[0] += 1
        return {"evidence": "the target host is unreachable"}

    return Tool(
        name="lookup_evidence",
        schema={
            "type": "function",
            "function": {
                "name": "lookup_evidence",
                "description": "Look up evidence for the incident under investigation.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        handler=handler,
        category="test",
        contains_untrusted_text=False,
    )


def _assert_no_user_derived_metadata(body: dict) -> None:
    """#16's Metadata AC: request metadata never carries a prompt, tool
    result, credential, target hostname, or other user-derived string --
    checked here by asserting the *entire* metadata object is exactly
    the three documented keys with the expected value types, not a
    substring search (a substring check could pass by accident; an
    exact key-set check cannot)."""
    metadata = body["metadata"]
    assert set(metadata) == _MANTIS_METADATA_KEYS
    assert isinstance(metadata["mantis_agent"], str)
    assert isinstance(metadata["mantis_run_id"], str)
    assert isinstance(metadata["mantis_iteration"], int)


# ---------------------------------------------------------------------------
# 1. Direct AgentRuntime: the detailed mechanics, one real HTTP request
#    per Mantis-level attempt.
# ---------------------------------------------------------------------------


def test_e2e_primary_fails_fallback_succeeds_over_real_http():
    call_counter = [0]
    registry = ToolRegistry()
    registry.register(_lookup_evidence_tool(call_counter))

    with MockLLMGateway(
        {
            # Iteration 1: primary succeeds, requesting the tool.
            # Iteration 2: primary fails eligibly (503 -> SERVER_ERROR).
            "mock-primary": [
                tool_call_response("lookup_evidence", {}, call_id="call_1"),
                503,
            ],
            # Iteration 2's fallback succeeds with the final answer.
            "mock-fallback": [final_message_response("The host is unreachable; escalate to network on-call.")],
        }
    ) as gw:
        policy = ModelRoutingPolicy(primary_alias="mock-primary", fallback_aliases=("mock-fallback",), max_attempts=2)
        runtime = AgentRuntime(
            name="e2e-agent",
            system_prompt="You are a test agent.",
            tools=["lookup_evidence"],
            registry=registry,
            model_config=LiteLLMConfig(url=gw.url, api_key=Secret("k"), model="mock-primary"),
            routing_policy=policy,
            tool_call_budget=5,
            max_iterations=5,
        )
        # Real openai.OpenAI client, real retry logic disabled via the
        # SDK's own public .with_options() -- never a modification of
        # create() itself -- so one Mantis-level attempt is exactly one
        # real HTTP request to the mock gateway.
        runtime._client = runtime._client.with_options(max_retries=0)

        answer = runtime.run("Investigate why host-01 is unreachable.", run_id="run-e2e-1")

    # --- The final answer and tool execution ---
    assert answer == "The host is unreachable; escalate to network on-call."
    assert call_counter[0] == 1, "the tool must execute exactly once"
    assert len(runtime.call_log) == 1
    assert runtime.call_log[0].tool_name == "lookup_evidence"
    assert runtime.call_log[0].outcome == "ok"

    # --- The model-call attempt history: primary tool call (ok),
    # primary failure (error), fallback success (ok) -- three attempts,
    # not four; fallback attempted exactly once. ---
    assert len(runtime.model_call_log) == 3
    first, second, third = runtime.model_call_log
    assert (first.iteration, first.requested_alias, first.outcome) == (1, "mock-primary", "ok")
    assert (second.iteration, second.requested_alias, second.outcome) == (2, "mock-primary", "error")
    assert second.failure_kind == ModelCallFailureKind.SERVER_ERROR
    assert (third.iteration, third.requested_alias, third.outcome) == (2, "mock-fallback", "ok")
    fallback_attempts = [a for a in runtime.model_call_log if a.routing_reason == "fallback"]
    assert len(fallback_attempts) == 1

    # --- requested_primary_alias / final_alias / failed_route_attempts,
    # computed the same way mantis.eval.runner.run_scenario does. ---
    assert runtime.routing_policy.primary_alias == "mock-primary"
    final_alias = next(
        (a.requested_alias for a in reversed(runtime.model_call_log) if a.outcome == "ok" and a.iteration == 2),
        None,
    )
    assert final_alias == "mock-fallback"
    failed_route_attempts = tuple(
        f"{a.requested_alias}:{a.failure_kind.value}" for a in runtime.model_call_log if a.outcome != "ok"
    )
    assert failed_route_attempts == ("mock-primary:server_error",)

    # --- The real, serialized HTTP requests: exactly 3 reached the
    # gateway (no SDK-level retries, since max_retries=0), and the
    # primary/fallback pair for iteration 2 are identical except model. ---
    assert len(gw.received_requests) == 3
    iter1_primary, iter2_primary, iter2_fallback = gw.received_requests
    assert iter1_primary["model"] == "mock-primary"
    assert iter2_primary["model"] == "mock-primary"
    assert iter2_fallback["model"] == "mock-fallback"

    # The fallback must see the SAME conversation state as the primary's
    # failed attempt -- including the tool result from iteration 1 --
    # never a replay of the tool call itself (already proven by
    # call_counter above) and never a reset/truncated conversation.
    assert iter2_primary["messages"] == iter2_fallback["messages"]
    assert json.dumps(iter2_primary.get("tools")) == json.dumps(iter2_fallback.get("tools"))
    assert iter2_primary["metadata"] == iter2_fallback["metadata"]
    # The only relevant difference between the two requests is model.
    diff_keys = {
        k
        for k in set(iter2_primary) | set(iter2_fallback)
        if iter2_primary.get(k) != iter2_fallback.get(k)
    }
    assert diff_keys == {"model"}

    # The tool's real result (from iteration 1) is present in what the
    # fallback actually received -- proof it wasn't dropped or replayed,
    # just carried forward in the existing conversation.
    tool_result_messages = [m for m in iter2_fallback["messages"] if m.get("role") == "tool"]
    assert len(tool_result_messages) == 1
    assert "unreachable" in tool_result_messages[0]["content"]

    # --- #16's Metadata AC, over the real HTTP request. ---
    for body in gw.received_requests:
        _assert_no_user_derived_metadata(body)
    assert iter1_primary["metadata"]["mantis_agent"] == "e2e-agent"
    assert iter1_primary["metadata"]["mantis_run_id"] == "run-e2e-1"
    assert iter1_primary["metadata"]["mantis_iteration"] == 1
    assert iter2_primary["metadata"]["mantis_iteration"] == 2
    assert iter2_fallback["metadata"]["mantis_iteration"] == 2

    # --- Never a prompt/tool-result/credential/hostname leaking into
    # metadata specifically (the broader message content legitimately
    # contains "host-01"/evidence text -- that's fine, it's the request
    # body as a whole; metadata itself must not). ---
    for body in gw.received_requests:
        serialized_metadata = json.dumps(body["metadata"])
        assert "host-01" not in serialized_metadata
        assert "unreachable" not in serialized_metadata
        assert "k" != serialized_metadata  # the API key itself
        assert "Bearer" not in serialized_metadata


def test_e2e_routing_exhausted_when_both_routes_fail_over_real_http():
    # The other real-HTTP-boundary branch: every route failing must
    # still raise ModelRoutingExhaustedError with the real attempt
    # history attached, never hang or silently succeed.
    with MockLLMGateway({"mock-primary": [503], "mock-fallback": [503]}) as gw:
        policy = ModelRoutingPolicy(primary_alias="mock-primary", fallback_aliases=("mock-fallback",), max_attempts=2)
        runtime = AgentRuntime(
            name="e2e-agent",
            system_prompt="You are a test agent.",
            tools=[],
            registry=ToolRegistry(),
            model_config=LiteLLMConfig(url=gw.url, api_key=Secret("k"), model="mock-primary"),
            routing_policy=policy,
        )
        runtime._client = runtime._client.with_options(max_retries=0)

        try:
            runtime.run("hello")
            raised = False
        except ModelRoutingExhaustedError as exc:
            raised = True
            assert len(exc.attempts) == 2
            assert [a.requested_alias for a in exc.attempts] == ["mock-primary", "mock-fallback"]

        assert raised
        assert len(gw.received_requests) == 2


# ---------------------------------------------------------------------------
# 2. mantis.eval.runner.run_scenario: the real eval harness.
# ---------------------------------------------------------------------------


def _routed_scenario(call_counter: list[int]) -> Scenario:
    registry = ToolRegistry()
    registry.register(_lookup_evidence_tool(call_counter))
    return Scenario(
        name="e2e-routed-scenario",
        version="1.0",
        description="#16 E2E: prior tool evidence, then a routed fallback on a later iteration.",
        prompt="Investigate why host-01 is unreachable.",
        system_prompt="You are a test agent.",
        agent_tools=["lookup_evidence"],
        build_registry=lambda: registry,
        max_iterations=5,
        tool_call_budget=5,
    )


def test_e2e_run_scenario_records_fallback_through_real_eval_plumbing():
    # Exercises the real mantis.eval.runner.run_scenario -- the actual
    # #13/#16 eval plumbing, not a hand-built AgentRuntime. Left with
    # the real openai SDK's own default retry behavior (max_retries=2):
    # iteration 2's 503 is retried by the SDK itself twice before
    # Mantis's routing/fallback ever sees the resulting exception, so
    # the primary alias receives 4 real HTTP requests total below (1
    # success in iteration 1, then 1 failure retried twice in iteration
    # 2) -- real, observed behavior, accounted for explicitly rather
    # than hidden.
    call_counter = [0]
    with MockLLMGateway(
        {
            "mock-primary": [
                tool_call_response("lookup_evidence", {}, call_id="call_1"),  # iteration 1: succeeds
                503,
                503,
                503,  # iteration 2: fails, retried twice by the SDK itself
            ],
            "mock-fallback": [final_message_response("Escalate to network on-call.")],
        }
    ) as gw:
        policy = ModelRoutingPolicy(primary_alias="mock-primary", fallback_aliases=("mock-fallback",), max_attempts=2)
        result = run_scenario(
            _routed_scenario(call_counter),
            "mock-primary",
            base_model_config=LiteLLMConfig(url=gw.url, api_key=Secret("k"), model="mock-primary"),
            routing_policy=policy,
        )

    assert result.outcome == "ok"
    assert result.final_answer == "Escalate to network on-call."
    assert result.requested_primary_alias == "mock-primary"
    assert result.final_alias == "mock-fallback"
    failed = [a for a in result.route_attempts if a["outcome"] != "ok"]
    assert len(failed) == 1
    assert failed[0]["requested_alias"] == "mock-primary"
    assert failed[0]["failure_kind"] == "server_error"

    # Tool-call accounting is untouched by routing: exactly one real
    # tool execution, one entry in tool_calls, never inflated by the
    # SDK's 3 internal HTTP attempts or Mantis's own 2 route attempts.
    assert call_counter[0] == 1
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].tool_name == "lookup_evidence"

    primary_requests = [r for r in gw.received_requests if r["model"] == "mock-primary"]
    fallback_requests = [r for r in gw.received_requests if r["model"] == "mock-fallback"]
    assert len(primary_requests) == 4  # 1 success (iteration 1) + 1 failure retried twice by the SDK (iteration 2)
    assert len(fallback_requests) == 1
    for body in gw.received_requests:
        _assert_no_user_derived_metadata(body)


# ---------------------------------------------------------------------------
# 3. mantis.eval.qualification.qualify_models: primary-only vs.
#    primary+fallback, the same scenario (#16's Evaluation AC).
# ---------------------------------------------------------------------------


def test_e2e_qualify_models_primary_only_records_a_plain_backend_failure(monkeypatch):
    call_counter = [0]
    monkeypatch.setitem(default_scenarios._scenarios, "e2e-routed-scenario", _routed_scenario(call_counter))
    with MockLLMGateway({"mock-primary": [503, 503, 503]}) as gw:
        run = qualify_models(
            ["mock-primary"],
            scenario_names=("e2e-routed-scenario",),
            suite_id="e2e-routed-suite",
            suite_version="v1",
            base_model_config=LiteLLMConfig(url=gw.url, api_key=Secret("k"), model="unused"),
        )

    record = run.records[0]
    assert record.outcome == "error"
    assert record.requested_alias == "mock-primary"
    # No routing_policies given -> the single-route default -> no
    # fallback attempted at all, a plain backend/model failure exactly
    # as it behaved before #16.
    assert len([r for r in gw.received_requests if r["model"] == "mock-fallback"]) == 0


def test_e2e_qualify_models_primary_plus_fallback_recovers_the_same_scenario(monkeypatch):
    call_counter = [0]
    monkeypatch.setitem(default_scenarios._scenarios, "e2e-routed-scenario", _routed_scenario(call_counter))
    with MockLLMGateway(
        {
            "mock-primary": [
                tool_call_response("lookup_evidence", {}, call_id="call_1"),  # iteration 1: succeeds
                503,
                503,
                503,  # iteration 2: fails, retried twice by the SDK itself
            ],
            "mock-fallback": [final_message_response("Escalate to network on-call.")],
        }
    ) as gw:
        policy = ModelRoutingPolicy(primary_alias="mock-primary", fallback_aliases=("mock-fallback",), max_attempts=2)
        run = qualify_models(
            ["mock-primary"],
            scenario_names=("e2e-routed-scenario",),
            suite_id="e2e-routed-suite",
            suite_version="v1",
            base_model_config=LiteLLMConfig(url=gw.url, api_key=Secret("k"), model="unused"),
            routing_policies={"mock-primary": policy},
        )

    record = run.records[0]
    # The SAME scenario, primary+fallback this time: bounded qualification
    # record shows the requested primary, the failed primary route
    # attempt, and the successful final (fallback) alias.
    assert record.outcome == "ok"
    assert record.requested_alias == "mock-primary"
    assert record.final_alias == "mock-fallback"
    assert record.failed_route_attempts == ("mock-primary:server_error",)
    assert record.tool_call_count == 1
    assert call_counter[0] == 1


# ---------------------------------------------------------------------------
# 4. `mantis eval qualify --fallback`: the real CLI command handler.
# ---------------------------------------------------------------------------


def test_e2e_cli_qualify_fallback_flag_demonstrates_routed_recovery(monkeypatch, tmp_path):
    import mantis.eval.cli as eval_cli

    call_counter = [0]
    monkeypatch.setitem(default_scenarios._scenarios, "e2e-routed-scenario", _routed_scenario(call_counter))
    monkeypatch.setitem(eval_cli._QUALIFY_SUITES, "e2e", (("e2e-routed-scenario",), "e2e-routed-suite", "v1"))
    monkeypatch.chdir(tmp_path)

    with MockLLMGateway(
        {
            "mock-primary": [
                tool_call_response("lookup_evidence", {}, call_id="call_1"),  # iteration 1: succeeds
                503,
                503,
                503,  # iteration 2: fails, retried twice by the SDK itself
            ],
            "mock-fallback": [final_message_response("Escalate to network on-call.")],
        }
    ) as gw:
        monkeypatch.setenv("LITELLM_URL", gw.url)
        monkeypatch.setenv("LITELLM_API_KEY", "k")

        exit_code = eval_cli.main(
            [
                "qualify",
                "--models",
                "mock-primary",
                "--fallback",
                "mock-fallback",
                "--suite",
                "e2e",
                "--out",
                str(tmp_path / "routed.jsonl"),
            ]
        )

    assert exit_code == 0
    written = [json.loads(line) for line in (tmp_path / "routed.jsonl").read_text().splitlines()]
    assert len(written) == 1
    record = written[0]
    assert record["requested_alias"] == "mock-primary"
    assert record["final_alias"] == "mock-fallback"
    assert record["failed_route_attempts"] == ["mock-primary:server_error"]
    assert record["outcome"] == "ok"
    assert record["tool_call_count"] == 1
    assert call_counter[0] == 1
