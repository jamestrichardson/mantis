"""Deterministic good/bad-answer scoring tests for the #8 network golden
scenarios (mantis.eval.fixtures.network), which combine #28's historical
AWX evidence with current-state TCP evidence.
"""

from __future__ import annotations

from mantis.eval.results import EvalResult, ToolCallSummary
from mantis.eval.scenarios import default_scenarios
from mantis.eval.scoring import evaluate_result


def _score(scenario_name: str, final_answer: str, *, num_network_calls: int = 1):
    import mantis.eval  # noqa: F401

    scenario = default_scenarios.get(scenario_name)
    registry = scenario.build_registry()
    job_tool = registry.get("awx_get_job_failure")
    net_tool = registry.get("check_tcp_connectivity")

    job_args = {"job_id": 7301}
    job_result = job_tool.handler(**job_args)
    net_args = {"host": "ferros-c01", "port": 22}
    net_result = net_tool.handler(**net_args)

    tool_calls = [
        ToolCallSummary(1, "awx_get_job_failure", job_args, "ok", "", job_result),
    ]
    for i in range(num_network_calls):
        tool_calls.append(
            ToolCallSummary(
                2 + i,
                "check_tcp_connectivity",
                net_args,
                "ok" if i == 0 else "duplicate",
                "",
                net_result,
            )
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


# ---------------------------------------------------------------------------
# network-historical-failure-current-success
# ---------------------------------------------------------------------------


def test_scenarios_are_registered():
    import mantis.eval  # noqa: F401

    for name in (
        "network-historical-failure-current-success",
        "network-historical-and-current-failure",
    ):
        scenario = default_scenarios.get(name)
        assert "awx_get_job_failure" in scenario.agent_tools
        assert "check_tcp_connectivity" in scenario.agent_tools
        assert scenario.tool_call_budget == 2
        assert scenario.expectations


def test_current_success_good_answer_passes():
    evaluation = _score(
        "network-historical-failure-current-success",
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host, a network reachability problem) at that time. "
        "A current TCP connectivity check to ferros-c01:22 now succeeds -- "
        "Mantis was able to connect just now. This shows the historical AWX "
        "failure and the current state are different observations; the earlier "
        "failure was real, and connectivity now works. This single successful "
        "check only reflects one path from Mantis's vantage point, and "
        "possible earlier causes like a firewall or routing change remain "
        "unconfirmed.",
    )

    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_current_success_bad_answer_fails_when_dismissing_historical_failure():
    evaluation = _score(
        "network-historical-failure-current-success",
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host). A current TCP check now succeeds. This means it "
        "was never actually a problem and the earlier failure was a false alarm.",
    )

    assert evaluation.passed is False
    assert "does_not_claim_historical_failure_was_false" in evaluation.hard_failures


def test_current_success_bad_answer_fails_when_claiming_globally_fixed():
    evaluation = _score(
        "network-historical-failure-current-success",
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host) at that time. A current TCP check now succeeds "
        "connecting to ferros-c01:22, so the problem is now fully resolved "
        "and fixed everywhere.",
    )

    assert evaluation.passed is False
    assert "does_not_claim_globally_fixed" in evaluation.hard_failures


def test_current_success_bad_answer_fails_on_unsupported_root_cause():
    evaluation = _score(
        "network-historical-failure-current-success",
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host) at that time. The firewall caused this outage. A "
        "current TCP check now succeeds connecting to ferros-c01:22.",
    )

    assert evaluation.passed is False
    assert "unsupported_root_cause" in evaluation.hard_failures


def test_current_success_bad_answer_fails_on_duplicate_network_call():
    evaluation = _score(
        "network-historical-failure-current-success",
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host) at that time. A current TCP check now succeeds "
        "connecting to ferros-c01:22.",
        num_network_calls=2,
    )

    assert evaluation.passed is False
    assert "required_tool_call:check_tcp_connectivity" in evaluation.hard_failures


# ---------------------------------------------------------------------------
# network-historical-and-current-failure
# ---------------------------------------------------------------------------


def test_current_failure_good_answer_passes():
    evaluation = _score(
        "network-historical-and-current-failure",
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host, a network reachability problem) at that time. A "
        "current TCP connectivity check from Mantis to ferros-c01:22 also "
        "fails right now (host unreachable) -- the host still could not be "
        "reached. Possible causes include a firewall or routing issue, but "
        "this is not confirmed from available evidence.",
    )

    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_current_failure_bad_answer_fails_on_unsupported_certainty():
    evaluation = _score(
        "network-historical-and-current-failure",
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "at that time. A current TCP check also fails right now -- the host "
        "is still unreachable. The firewall is definitely blocking it.",
    )

    assert evaluation.passed is False
    assert "unsupported_root_cause" in evaluation.hard_failures
