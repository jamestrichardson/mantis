"""Tests for mantis.eval.runner: orchestration against a mocked model
client. No live model/network dependency — this is what AC "Tests cover
runner orchestration with a mocked model client" (#34) requires.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
import openai
import pytest

import mantis.runtime as runtime_module
from mantis.config import LiteLLMConfig, Secret
from mantis.eval.runner import run_comparison, run_scenario
from mantis.eval.scenarios import Scenario
from mantis.observability import metrics
from mantis.registry import Tool, ToolRegistry


def _backend_error() -> openai.APIConnectionError:
    """A real openai.OpenAIError, for tests that need to simulate a
    genuine backend/network failure — as opposed to a bug in the test's
    own fake harness, which must propagate rather than be swallowed."""
    return openai.APIConnectionError(request=httpx.Request("POST", "http://example.test"))


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

    def model_dump(self) -> dict[str, Any]:
        return {"content": self.content, "tool_calls": self.tool_calls}


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: Any = None
    model: str | None = None


class FakeCompletions:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)

    def create(self, **kwargs):
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


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
# Scoring integration
# ---------------------------------------------------------------------------


def test_run_scenario_attaches_evaluation_when_scenario_has_expectations(monkeypatch):
    from mantis.eval.expectations import RequiredAnswerPattern

    _patch_openai(monkeypatch, {"model-a": [_final("the answer mentions host03")]})
    scenario = _echo_scenario(
        expectations=[RequiredAnswerPattern("host03", name="cited_host03")]
    )

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.evaluation is not None
    assert result.evaluation["passed"] is True
    assert result.evaluation["score"] == 1
    assert result.evaluation["max_score"] == 1
    assert result.evaluation["checks"][0]["name"] == "cited_host03"


def test_run_scenario_evaluation_is_none_without_expectations(monkeypatch):
    _patch_openai(monkeypatch, {"model-a": [_final("hello")]})
    scenario = _echo_scenario()  # no expectations

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.evaluation is None


def test_run_scenario_evaluation_reflects_a_hard_failure(monkeypatch):
    from mantis.eval.expectations import ForbiddenAnswerPattern

    _patch_openai(monkeypatch, {"model-a": [_final("the firewall caused it")]})
    scenario = _echo_scenario(
        expectations=[
            ForbiddenAnswerPattern("firewall caused", name="no_firewall_blame", hard=True)
        ]
    )

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.evaluation["passed"] is False
    assert result.evaluation["score"] == 0
    assert result.evaluation["max_score"] == 1
    assert result.evaluation["hard_failures"] == ["no_firewall_blame"]


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


def test_run_scenario_accepts_an_explicit_routing_policy(monkeypatch):
    # #16 AC: the eval/qualification path must be able to exercise a
    # real primary+fallback routing policy, not only ever the
    # single-route default AgentRuntime otherwise constructs.
    from mantis.config import ModelRoutingPolicy

    _patch_openai(
        monkeypatch,
        {"model-a": [_backend_error()], "model-a-backup": [_final("recovered via fallback")]},
    )
    scenario = _echo_scenario()
    policy = ModelRoutingPolicy(primary_alias="model-a", fallback_aliases=("model-a-backup",), max_attempts=2)

    result = run_scenario(scenario, "model-a", base_model_config=_base_config(), routing_policy=policy)

    assert result.outcome == "ok"
    assert result.final_answer == "recovered via fallback"
    assert result.requested_primary_alias == "model-a"
    assert result.final_alias == "model-a-backup"


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


def test_run_scenario_records_error_outcome_for_a_backend_failure(monkeypatch):
    _patch_openai(monkeypatch, {"model-a": [_backend_error()]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.outcome == "error"
    assert result.final_answer is None
    assert "APIConnectionError" in result.error


def test_run_scenario_error_summary_is_bounded_and_never_the_raw_provider_body(monkeypatch):
    # A real example encountered during a live qualification run: an
    # nginx 504 Gateway Time-out HTML page as an openai.OpenAIError's
    # own message. error_summary must stay class-name(+status)-only;
    # error (kept for local/raw-file debugging) may still carry the
    # full detail.
    html_body = "<html><head><title>504 Gateway Time-out</title></head><body>nginx</body></html>"
    response = httpx.Response(504, request=httpx.Request("POST", "http://litellm.example.test"))
    exc = openai.InternalServerError(html_body, response=response, body=None)
    _patch_openai(monkeypatch, {"model-a": [exc]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.outcome == "error"
    assert result.error_summary == "InternalServerError (status=504)"
    assert html_body not in result.error_summary
    assert html_body in result.error  # full detail still available locally


def test_run_scenario_records_error_outcome_for_max_iterations_exceeded(monkeypatch):
    # A model that never stops calling tools (distinct arguments each
    # time, so the duplicate-call cache never short-circuits it) is
    # disqualifying model behavior, not a Mantis bug — must be recorded
    # as outcome="error", not raised. AgentRuntime's default budget is 8
    # iterations, so 9 distinct calls exceeds it.
    _patch_openai(
        monkeypatch,
        {"model-a": [_tool_call_response("echo", {"x": i}) for i in range(9)]},
    )
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.outcome == "error"
    assert "MaxIterationsExceededError" in result.error


def test_run_scenario_reraises_a_genuine_mantis_bug(monkeypatch):
    # A bug in Mantis's own code (here: the fake test harness standing in
    # for it) must never be recorded as outcome="error" and misattributed
    # to the model — it must propagate and blow up the run loudly.
    _patch_openai(monkeypatch, {"model-a": []})  # empty -> IndexError, not an OpenAIError
    scenario = _echo_scenario()

    with pytest.raises(IndexError):
        run_scenario(scenario, "model-a", base_model_config=_base_config())


def test_run_scenario_captures_raw_message_on_empty_answer_no_tool_calls(monkeypatch):
    # Reproduces the real qualification finding this was built for: a
    # model spends completion tokens but produces neither usable content
    # nor a tool call.
    response = FakeResponse(
        choices=[FakeChoice(message=FakeMessage(content="", tool_calls=None))],
        usage={"prompt_tokens": 800, "completion_tokens": 16, "total_tokens": 816},
    )
    _patch_openai(monkeypatch, {"model-a": [response]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.outcome == "ok"
    assert result.final_answer == ""
    assert result.raw_message is not None
    assert result.raw_message["content"] == ""


def test_run_scenario_raw_message_is_none_for_a_real_answer(monkeypatch):
    _patch_openai(monkeypatch, {"model-a": [_final("a real answer")]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.raw_message is None


def test_run_scenario_captures_backend_model_when_reported(monkeypatch):
    response = FakeResponse(
        choices=[FakeChoice(message=FakeMessage(content="hi", tool_calls=None))],
        model="ollama/qwen2.5:14b",
    )
    _patch_openai(monkeypatch, {"mantis-fast": [response]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "mantis-fast", base_model_config=_base_config())

    assert result.model == "mantis-fast"  # the requested alias
    assert result.backend_model == "ollama/qwen2.5:14b"  # the resolved backend identity


def test_run_scenario_backend_model_is_none_when_backend_omits_it(monkeypatch):
    _patch_openai(monkeypatch, {"model-a": [_final("hi")]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.backend_model is None


def test_run_scenario_records_requested_primary_and_final_alias_for_a_default_single_route_run(monkeypatch):
    # No explicit routing policy is passed anywhere in this eval path --
    # AgentRuntime.__post_init__ wraps model_alias in a default
    # one-route policy, and the eval result must still reflect it
    # (#16's "eval result records primary/final aliases" AC).
    _patch_openai(monkeypatch, {"model-a": [_final("hi")]})
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    assert result.requested_primary_alias == "model-a"
    assert result.final_alias == "model-a"
    assert len(result.route_attempts) == 1
    assert result.route_attempts[0]["outcome"] == "ok"
    assert result.route_attempts[0]["requested_alias"] == "model-a"


def test_run_scenario_route_attempts_do_not_inflate_tool_call_counts(monkeypatch):
    _patch_openai(
        monkeypatch,
        {"model-a": [_tool_call_response("echo", {"x": 1}), _final("done")]},
    )
    scenario = _echo_scenario()

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    # Two model-call attempts (two iterations), but exactly one real
    # tool execution -- the two counts must never be conflated.
    assert len(result.route_attempts) == 2
    assert len(result.tool_calls) == 1


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
            "bad-model": [_backend_error()],
        },
    )
    scenario = _echo_scenario()

    results = run_comparison(scenario, ["good-model", "bad-model"], base_model_config=_base_config())

    assert [r.model for r in results] == ["good-model", "bad-model"]
    assert results[0].outcome == "ok"
    assert results[1].outcome == "error"


def test_run_comparison_reraises_a_genuine_mantis_bug_and_stops(monkeypatch):
    # Unlike a model/backend failure, a Mantis bug must abort the whole
    # comparison rather than being recorded against whichever model was
    # running — continuing on to the next model would risk masking that
    # the harness itself is broken.
    _patch_openai(
        monkeypatch,
        {
            "good-model": [_final("all good")],
            "buggy-model": [],  # -> IndexError, a bug, not a backend failure
        },
    )
    scenario = _echo_scenario()

    with pytest.raises(IndexError):
        run_comparison(scenario, ["good-model", "buggy-model"], base_model_config=_base_config())


def test_run_comparison_supports_a_single_model(monkeypatch):
    _patch_openai(monkeypatch, {"only-model": [_final("hi")]})
    scenario = _echo_scenario()

    results = run_comparison(scenario, ["only-model"], base_model_config=_base_config())

    assert len(results) == 1
    assert results[0].model == "only-model"


# ---------------------------------------------------------------------------
# Observability: eval-specific events/metrics reuse the runtime's registry
# and run_id (#38 / #39)
# ---------------------------------------------------------------------------


def test_run_scenario_emits_eval_result_event_sharing_the_run_id(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="mantis.eval.runner")
    from mantis.eval.expectations import RequiredAnswerPattern

    _patch_openai(monkeypatch, {"model-a": [_final("mentions host03")]})
    scenario = _echo_scenario(expectations=[RequiredAnswerPattern("host03")])

    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    eval_result_events = [r for r in caplog.records if getattr(r, "event", None) == "mantis_eval_result"]
    assert len(eval_result_events) == 1
    event = eval_result_events[0]
    assert event.scenario == "echo-scenario"
    assert event.model_alias == "model-a"
    assert event.outcome == "pass"
    assert event.score == 1
    assert event.max_score == 1
    # Same run_id AgentRuntime generated for this run — proves eval events
    # are correlated with the underlying runtime/tool/model events, not a
    # parallel, disconnected identifier.
    run_id_events = [r for r in caplog.records if hasattr(r, "run_id") and r.run_id]
    assert len({r.run_id for r in run_id_events}) == 1
    assert result.evaluation["passed"] is True


def test_run_scenario_emits_eval_check_event_per_check(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="mantis.eval.runner")
    from mantis.eval.expectations import ForbiddenAnswerPattern, RequiredAnswerPattern

    _patch_openai(monkeypatch, {"model-a": [_final("mentions host03 only")]})
    scenario = _echo_scenario(
        expectations=[
            RequiredAnswerPattern("host03", name="cites_host"),
            ForbiddenAnswerPattern("firewall caused", name="no_blame"),
        ]
    )

    run_scenario(scenario, "model-a", base_model_config=_base_config())

    check_events = [r for r in caplog.records if getattr(r, "event", None) == "mantis_eval_check"]
    assert {r.check_name for r in check_events} == {"cites_host", "no_blame"}
    assert all(r.outcome == "pass" for r in check_events)


def test_run_scenario_records_eval_runs_total_and_score_ratio(monkeypatch):
    from mantis.eval.expectations import RequiredAnswerPattern

    labels = dict(scenario="echo-scenario", model_alias="model-a", environment=metrics.environment())
    before = metrics.EVAL_RUNS_TOTAL.labels(result="pass", **labels)._value.get()
    score_before = metrics.EVAL_SCORE_RATIO.labels(**labels)._sum.get()

    _patch_openai(monkeypatch, {"model-a": [_final("mentions host03")]})
    scenario = _echo_scenario(expectations=[RequiredAnswerPattern("host03")])
    run_scenario(scenario, "model-a", base_model_config=_base_config())

    after = metrics.EVAL_RUNS_TOTAL.labels(result="pass", **labels)._value.get()
    score_after = metrics.EVAL_SCORE_RATIO.labels(**labels)._sum.get()
    assert after == before + 1
    assert score_after == pytest.approx(score_before + 1.0)  # 1/1 checks passed


def test_run_scenario_records_hard_failures_metric(monkeypatch):
    from mantis.eval.expectations import ForbiddenAnswerPattern

    labels = dict(scenario="echo-scenario", model_alias="model-a", environment=metrics.environment())
    before = metrics.EVAL_HARD_FAILURES_TOTAL.labels(**labels)._value.get()

    _patch_openai(monkeypatch, {"model-a": [_final("the firewall caused it")]})
    scenario = _echo_scenario(
        expectations=[ForbiddenAnswerPattern("firewall caused", hard=True)]
    )
    run_scenario(scenario, "model-a", base_model_config=_base_config())

    after = metrics.EVAL_HARD_FAILURES_TOTAL.labels(**labels)._value.get()
    assert after == before + 1


def test_run_scenario_without_expectations_records_unscored_and_skips_score_ratio(monkeypatch):
    labels = dict(scenario="echo-scenario", model_alias="model-a", environment=metrics.environment())
    before = metrics.EVAL_RUNS_TOTAL.labels(result="unscored", **labels)._value.get()

    _patch_openai(monkeypatch, {"model-a": [_final("no expectations on this scenario")]})
    scenario = _echo_scenario()  # no expectations
    run_scenario(scenario, "model-a", base_model_config=_base_config())

    after = metrics.EVAL_RUNS_TOTAL.labels(result="unscored", **labels)._value.get()
    assert after == before + 1


def test_run_scenario_backend_failure_records_error_result(monkeypatch):
    labels = dict(scenario="echo-scenario", model_alias="model-a", environment=metrics.environment())
    before = metrics.EVAL_RUNS_TOTAL.labels(result="error", **labels)._value.get()

    _patch_openai(monkeypatch, {"model-a": [_backend_error()]})
    scenario = _echo_scenario()
    result = run_scenario(scenario, "model-a", base_model_config=_base_config())

    after = metrics.EVAL_RUNS_TOTAL.labels(result="error", **labels)._value.get()
    assert after == before + 1
    assert result.outcome == "error"
