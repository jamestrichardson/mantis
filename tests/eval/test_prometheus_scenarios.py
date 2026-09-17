"""Deterministic good/bad-answer scoring tests for the #9 multi-signal
golden scenarios (mantis.eval.fixtures.prometheus), which combine #28's
historical AWX evidence, #9's time-series Prometheus evidence, and #8's
current-state TCP evidence.
"""

from __future__ import annotations

from mantis.eval.results import EvalResult, ToolCallSummary
from mantis.eval.scenarios import default_scenarios
from mantis.eval.scoring import evaluate_result


def _score(scenario_name: str):
    import mantis.eval  # noqa: F401

    scenario = default_scenarios.get(scenario_name)
    registry = scenario.build_registry()

    job_args = {"job_id": 7301}
    job_result = registry.get("awx_get_job_failure").handler(**job_args)

    net_args = {"host": "ferros-c01", "port": 22}
    net_result = registry.get("check_tcp_connectivity").handler(**net_args)

    prom_args = {"query": 'up{instance="ferros-c01:9100"}', "start": 1700000000, "end": 1700000360, "step": 60}
    prom_result = registry.get("prometheus_query_range").handler(**prom_args)

    def build(final_answer: str) -> EvalResult:
        tool_calls = [
            ToolCallSummary(1, "awx_get_job_failure", job_args, "ok", "", job_result),
            ToolCallSummary(2, "check_tcp_connectivity", net_args, "ok", "", net_result),
            ToolCallSummary(3, "prometheus_query_range", prom_args, "ok", "", prom_result),
        ]
        return EvalResult(
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

    return lambda final_answer: evaluate_result(scenario.expectations, build(final_answer))


def test_scenarios_are_registered():
    import mantis.eval  # noqa: F401

    for name in ("multi-signal-recovery", "multi-signal-still-down"):
        scenario = default_scenarios.get(name)
        assert "awx_get_job_failure" in scenario.agent_tools
        assert "check_tcp_connectivity" in scenario.agent_tools
        assert "prometheus_query_range" in scenario.agent_tools
        assert scenario.tool_call_budget == 3
        assert scenario.expectations


# ---------------------------------------------------------------------------
# multi-signal-recovery
# ---------------------------------------------------------------------------


def test_recovery_good_answer_passes():
    score = _score("multi-signal-recovery")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host, a network reachability problem) at that time. "
        "Prometheus monitoring data for ferros-c01:9100 shows the up metric "
        "dropped to 0 for a few minutes around that same window, then "
        "recovered to 1 -- the scrape was interrupted during roughly that "
        "period. A current TCP connectivity check now succeeds connecting "
        "to ferros-c01:22. These are three separate observations at "
        "different times; the AWX failure and the monitoring gap occurred "
        "in a similar window, but this does not confirm a specific cause "
        "like a firewall or switch issue, and this single successful check "
        "only reflects one path checked at one moment.",
    )

    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_recovery_bad_answer_fails_on_permanently_fixed_claim():
    score = _score("multi-signal-recovery")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "at that time. Prometheus shows the up metric for ferros-c01:9100 "
        "dropped and recovered around then. A current TCP check now "
        "succeeds, so the incident is now fully resolved.",
    )

    assert evaluation.passed is False
    assert "does_not_claim_permanently_fixed" in evaluation.hard_failures


def test_recovery_bad_answer_fails_on_metric_overinterpretation():
    score = _score("multi-signal-recovery")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "at that time. Prometheus shows up for ferros-c01:9100 at 0 during "
        "that window, meaning the host was completely down. A current TCP "
        "check now succeeds connecting to ferros-c01:22.",
    )

    assert evaluation.passed is False
    assert "does_not_overinterpret_the_up_metric" in evaluation.hard_failures


def test_recovery_bad_answer_fails_on_unsupported_root_cause():
    score = _score("multi-signal-recovery")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "at that time. The switch definitely failed, causing the Prometheus "
        "up metric for ferros-c01:9100 to drop before recovering. A current "
        "TCP check now succeeds connecting to ferros-c01:22.",
    )

    assert evaluation.passed is False
    assert "unsupported_root_cause" in evaluation.hard_failures


def test_recovery_bad_answer_fails_when_a_required_source_is_missing():
    import mantis.eval  # noqa: F401

    scenario = default_scenarios.get("multi-signal-recovery")
    registry = scenario.build_registry()
    job_result = registry.get("awx_get_job_failure").handler(job_id=7301)
    tool_calls = [
        ToolCallSummary(1, "awx_get_job_failure", {"job_id": 7301}, "ok", "", job_result),
    ]
    result = EvalResult(
        scenario=scenario.name,
        scenario_version=scenario.version,
        model="test",
        started_at="t0",
        finished_at="t1",
        elapsed_seconds=1.0,
        outcome="ok",
        final_answer="AWX previously observed job 7301 fail reaching ferros-c01 on port 22.",
        tool_calls=tool_calls,
        iterations=2,
    )
    evaluation = evaluate_result(scenario.expectations, result)

    assert evaluation.passed is False
    assert "required_tool_call:check_tcp_connectivity" in evaluation.hard_failures
    assert "required_tool_call:prometheus_query_range" in evaluation.hard_failures


# ---------------------------------------------------------------------------
# multi-signal-still-down
# ---------------------------------------------------------------------------


def test_still_down_good_answer_passes():
    score = _score("multi-signal-still-down")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host) at that time. Prometheus monitoring data for "
        "ferros-c01:9100 shows the up metric dropped to 0 around that window "
        "and has not recovered, meaning the scrape is still failing. A "
        "current TCP connectivity check also still fails right now (host "
        "unreachable). All three observations point the same direction, but "
        "this does not confirm a specific cause such as a firewall or "
        "switch failure -- that remains a possibility, not a conclusion.",
    )

    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_still_down_bad_answer_fails_on_unsupported_certainty_even_when_signals_agree():
    score = _score("multi-signal-still-down")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "at that time. Prometheus shows up for ferros-c01:9100 still at 0. "
        "A current TCP check also still fails -- the host is still "
        "unreachable. The switch definitely failed.",
    )

    assert evaluation.passed is False
    assert "unsupported_root_cause" in evaluation.hard_failures


def test_still_down_bad_answer_fails_on_metric_overinterpretation():
    score = _score("multi-signal-still-down")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "at that time. Prometheus shows up for ferros-c01:9100 still at 0, "
        "meaning the host was powered off. A current TCP check also still "
        "fails.",
    )

    assert evaluation.passed is False
    assert "does_not_overinterpret_the_up_metric" in evaluation.hard_failures
