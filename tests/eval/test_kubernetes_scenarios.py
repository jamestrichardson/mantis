"""Deterministic good/bad-answer scoring tests for the #18
``kubernetes-pod-restart-correlated-with-scrape-gap`` golden scenario
(mantis.eval.fixtures.kubernetes), which combines #18's pod-restart
evidence with #9's time-series Prometheus evidence.
"""

from __future__ import annotations

from mantis.eval.results import EvalResult, ToolCallSummary
from mantis.eval.scenarios import default_scenarios
from mantis.eval.scoring import evaluate_result

_SCENARIO_NAME = "kubernetes-pod-restart-correlated-with-scrape-gap"


def _score(final_answer: str, *, num_pod_calls: int = 1):
    import mantis.eval  # noqa: F401

    scenario = default_scenarios.get(_SCENARIO_NAME)
    registry = scenario.build_registry()
    pod_tool = registry.get("kubernetes_list_pods")
    prom_tool = registry.get("prometheus_query_range")

    pod_args = {"namespace": "payments"}
    pod_result = pod_tool.handler(**pod_args)
    prom_args = {
        "query": "up{instance='payment-api:8080'}",
        "start": "2026-09-17T03:10:00Z",
        "end": "2026-09-17T03:20:00Z",
        "step": "60s",
    }
    prom_result = prom_tool.handler(**prom_args)

    tool_calls = []
    for i in range(num_pod_calls):
        tool_calls.append(
            ToolCallSummary(
                1 + i, "kubernetes_list_pods", pod_args, "ok" if i == 0 else "duplicate", "", pod_result
            )
        )
    tool_calls.append(
        ToolCallSummary(1 + num_pod_calls, "prometheus_query_range", prom_args, "ok", "", prom_result)
    )

    result = EvalResult(
        scenario=scenario.name,
        scenario_version=scenario.version,
        model="test",
        started_at="t0",
        finished_at="t1",
        elapsed_seconds=1.0,
        outcome="ok",
        final_answer=final_answer,
        tool_calls=tool_calls,
        iterations=len(tool_calls) + 1,
    )
    return evaluate_result(scenario.expectations, result)


def test_scenario_is_registered_with_the_real_tool_wiring():
    import mantis.eval  # noqa: F401

    scenario = default_scenarios.get(_SCENARIO_NAME)
    assert "kubernetes_list_pods" in scenario.agent_tools
    assert "prometheus_query_range" in scenario.agent_tools
    assert scenario.tool_call_budget == 2
    assert scenario.expectations


def test_pod_tool_result_shows_the_restart_evidence():
    import mantis.eval  # noqa: F401

    scenario = default_scenarios.get(_SCENARIO_NAME)
    registry = scenario.build_registry()
    pod_tool = registry.get("kubernetes_list_pods")

    result = pod_tool.handler(namespace="payments")

    container = result["pods"][0]["containers"][0]
    assert container["restart_count"] == 3
    assert container["last_termination"]["reason"] == "Error"
    assert container["last_termination"]["finished_at"] == "2026-09-17T03:14:00+00:00"


def test_good_answer_notes_temporal_correlation_without_causal_claim():
    evaluation = _score(
        "The pod payment-api-7f9c8d-abcde in namespace payments has restarted 3 times; its "
        "container last terminated (reason=Error) at 03:14:00 UTC. A Prometheus range query "
        "for payment-api:8080 shows the scrape (up metric) drop to 0 around the same time "
        "window and recover a few minutes later. The restart and the scrape gap happened "
        "around the same time, but it's unclear whether one is related to the other -- they "
        "might share a common trigger, or might be unrelated."
    )
    assert evaluation.passed
    assert evaluation.score == evaluation.max_score


def test_bad_answer_fails_when_it_claims_the_restart_caused_the_gap():
    evaluation = _score(
        "The pod restarted at 03:14:00 UTC, which caused the Prometheus scrape gap for "
        "payment-api:8080."
    )
    assert not evaluation.passed
    failed_names = {c.name for c in evaluation.checks if not c.passed}
    assert "does_not_claim_restart_caused_the_gap" in failed_names or "unsupported_causal_claim" in failed_names


def test_bad_answer_fails_when_it_claims_the_scrape_gap_caused_the_restart():
    evaluation = _score(
        "The Prometheus scrape gap for payment-api:8080 is why the pod's container "
        "restarted at 03:14:00 UTC."
    )
    assert not evaluation.passed


def test_bad_answer_fails_when_a_required_source_is_missing():
    import mantis.eval  # noqa: F401

    scenario = default_scenarios.get(_SCENARIO_NAME)
    registry = scenario.build_registry()
    pod_tool = registry.get("kubernetes_list_pods")
    pod_result = pod_tool.handler(namespace="payments")

    result = EvalResult(
        scenario=scenario.name,
        scenario_version=scenario.version,
        model="test",
        started_at="t0",
        finished_at="t1",
        elapsed_seconds=1.0,
        outcome="ok",
        final_answer="The pod restarted 3 times; the last termination was at 03:14:00 UTC.",
        tool_calls=[
            ToolCallSummary(1, "kubernetes_list_pods", {"namespace": "payments"}, "ok", "", pod_result)
        ],
        iterations=2,
    )
    evaluation = evaluate_result(scenario.expectations, result)
    assert not evaluation.passed


def test_good_answer_never_mentions_unexpected_pod_names():
    evaluation = _score(
        "Pod payment-api-different-name-xyz restarted around the same time as a Prometheus "
        "scrape gap for payment-api:8080; the evidence does not establish causation either way."
    )
    assert not evaluation.passed
