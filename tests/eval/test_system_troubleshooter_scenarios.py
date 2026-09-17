"""Deterministic good/bad-answer scoring tests for the #11 System
Troubleshooter golden scenarios (mantis.eval.fixtures.system_troubleshooter),
which reuse the real production agent's ALLOWED_TOOLS/SYSTEM_PROMPT/
TOOL_CALL_BUDGET/MAX_ITERATIONS and combine #28's historical AWX evidence, #9's
time-series Prometheus evidence, #8's current-state TCP evidence, and
#10's Loki log evidence.
"""

from __future__ import annotations

from mantis.eval.results import EvalResult, ToolCallSummary
from mantis.eval.scenarios import default_scenarios
from mantis.eval.scoring import evaluate_result

_FULL_INVESTIGATION = "system-troubleshooter-full-investigation"
_RETRIEVAL_FAILURE = "system-troubleshooter-retrieval-failure"
_CONTRADICTORY_SIGNALS = "system-troubleshooter-contradictory-signals"


def _load():
    import mantis.eval  # noqa: F401
    import mantis.agents.system_troubleshooter  # noqa: F401


def _job_args() -> dict:
    return {"job_id": 7301}


def _tcp_args() -> dict:
    return {"host": "ferros-c01", "port": 22}


def _prom_range_args() -> dict:
    return {"query": 'up{instance="ferros-c01:9100"}', "start": 1700000000, "end": 1700000360, "step": 60}


def _loki_args() -> dict:
    return {"query": '{instance="ferros-c01"}', "start": 1700000000, "end": 1700000360}


def _score(scenario_name: str, *, loki_outcome: str = "ok"):
    """Build a scorer for ``scenario_name`` that hand-constructs the four
    tool calls a correctly-behaving investigation would make, mirroring
    the fixture data each scenario's ``build_registry()`` actually
    returns (see ``mantis.eval.fixtures.loki.FixtureLokiClient`` for why
    a scenario whose Loki fixture is a raised exception cannot be scored
    by calling the handler directly the way the other three tools are)."""
    _load()
    scenario = default_scenarios.get(scenario_name)
    registry = scenario.build_registry()

    job_result = registry.get("awx_get_job_failure").handler(**_job_args())
    tcp_result = registry.get("check_tcp_connectivity").handler(**_tcp_args())
    prom_result = registry.get("prometheus_query_range").handler(**_prom_range_args())

    tool_calls = [
        ToolCallSummary(1, "awx_get_job_failure", _job_args(), "ok", "", job_result),
        ToolCallSummary(2, "check_tcp_connectivity", _tcp_args(), "ok", "", tcp_result),
        ToolCallSummary(3, "prometheus_query_range", _prom_range_args(), "ok", "", prom_result),
    ]
    if loki_outcome == "ok":
        loki_result = registry.get("loki_query").handler(**_loki_args())
        tool_calls.append(ToolCallSummary(4, "loki_query", _loki_args(), "ok", "", loki_result))
    else:
        tool_calls.append(ToolCallSummary(4, "loki_query", _loki_args(), loki_outcome, "connection refused", None))

    def build(final_answer: str) -> EvalResult:
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


def test_all_three_scenarios_are_registered_with_the_real_agent_wiring():
    _load()
    from mantis.agents.system_troubleshooter import (
        ALLOWED_TOOLS,
        MAX_ITERATIONS,
        SYSTEM_PROMPT,
        TOOL_CALL_BUDGET,
    )

    for name in (_FULL_INVESTIGATION, _RETRIEVAL_FAILURE, _CONTRADICTORY_SIGNALS):
        scenario = default_scenarios.get(name)
        assert scenario.agent_tools == ALLOWED_TOOLS
        assert scenario.system_prompt == SYSTEM_PROMPT
        assert scenario.tool_call_budget == TOOL_CALL_BUDGET
        assert scenario.max_iterations == MAX_ITERATIONS
        assert scenario.expectations


def test_every_scenario_registry_resolves_the_full_real_allowlist():
    # Regression test: ALLOWED_TOOLS names all six real tools, so every
    # scenario's fixture registry must provide all six (even the two a
    # given scenario's golden path doesn't require calling) or
    # AgentRuntime.__post_init__ raises ToolNotFoundError.
    _load()
    from mantis.agents.system_troubleshooter import ALLOWED_TOOLS

    for name in (_FULL_INVESTIGATION, _RETRIEVAL_FAILURE, _CONTRADICTORY_SIGNALS):
        scenario = default_scenarios.get(name)
        registry = scenario.build_registry()
        resolved = {tool.name for tool in registry.subset(scenario.agent_tools)}
        assert resolved == set(ALLOWED_TOOLS)


# ---------------------------------------------------------------------------
# system-troubleshooter-full-investigation
# ---------------------------------------------------------------------------


def test_full_investigation_good_answer_passes():
    score = _score(_FULL_INVESTIGATION)
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host, a network reachability problem) at that time. "
        "Prometheus monitoring data for ferros-c01:9100 shows the up metric "
        "dropped to 0 for a few minutes around that same window, then "
        "recovered to 1. The sshd and kernel logs for ferros-c01 over that "
        "window show an authentication timeout and an eth0 link-down event "
        "during the outage, followed by a successful login and link-up "
        "afterward. A current TCP connectivity check now succeeds connecting "
        "to ferros-c01:22. These four observations occurred in a similar "
        "window, but this does not confirm a specific cause like a firewall "
        "or switch issue -- that remains a possibility, not a conclusion -- "
        "and the single successful check only reflects one path checked at "
        "one moment.",
    )

    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_full_investigation_bad_answer_fails_on_permanently_fixed_claim():
    score = _score(_FULL_INVESTIGATION)
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22. "
        "Prometheus and the logs both show trouble around then, but a current "
        "TCP check now succeeds, so the incident is now fully resolved.",
    )

    assert evaluation.passed is False
    assert "does_not_claim_permanently_fixed" in evaluation.hard_failures


def test_full_investigation_bad_answer_fails_on_unsupported_root_cause():
    score = _score(_FULL_INVESTIGATION)
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22. "
        "The switch definitely failed, causing the sshd timeout and the "
        "Prometheus up metric to drop before recovering. A current TCP check "
        "now succeeds connecting to ferros-c01:22.",
    )

    assert evaluation.passed is False
    assert "unsupported_root_cause" in evaluation.hard_failures


def test_full_investigation_bad_answer_fails_when_a_required_source_is_missing():
    _load()
    scenario = default_scenarios.get(_FULL_INVESTIGATION)
    registry = scenario.build_registry()
    job_result = registry.get("awx_get_job_failure").handler(**_job_args())
    tool_calls = [ToolCallSummary(1, "awx_get_job_failure", _job_args(), "ok", "", job_result)]
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
    assert "required_tool_call:loki_query" in evaluation.hard_failures


# ---------------------------------------------------------------------------
# system-troubleshooter-retrieval-failure
# ---------------------------------------------------------------------------


def test_retrieval_failure_good_answer_passes():
    score = _score(_RETRIEVAL_FAILURE, loki_outcome="integration_error")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "(no route to host) at that time. Prometheus monitoring data for "
        "ferros-c01:9100 shows the up metric recovered to 1. A current TCP "
        "connectivity check now succeeds connecting to ferros-c01:22. Logs "
        "for ferros-c01 were unavailable -- the Loki query failed (connection "
        "refused), so I could not retrieve that evidence. Given the missing "
        "log evidence, the picture is incomplete; the historical failure and "
        "current recovery are consistent with a transient network issue, but "
        "that remains a possibility, not a conclusion.",
    )

    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_retrieval_failure_bad_answer_fails_when_loki_was_never_attempted():
    _load()
    scenario = default_scenarios.get(_RETRIEVAL_FAILURE)
    registry = scenario.build_registry()
    job_result = registry.get("awx_get_job_failure").handler(**_job_args())
    tcp_result = registry.get("check_tcp_connectivity").handler(**_tcp_args())
    prom_result = registry.get("prometheus_query_range").handler(**_prom_range_args())
    tool_calls = [
        ToolCallSummary(1, "awx_get_job_failure", _job_args(), "ok", "", job_result),
        ToolCallSummary(2, "check_tcp_connectivity", _tcp_args(), "ok", "", tcp_result),
        ToolCallSummary(3, "prometheus_query_range", _prom_range_args(), "ok", "", prom_result),
    ]
    result = EvalResult(
        scenario=scenario.name,
        scenario_version=scenario.version,
        model="test",
        started_at="t0",
        finished_at="t1",
        elapsed_seconds=1.0,
        outcome="ok",
        final_answer=(
            "AWX previously observed job 7301 fail reaching ferros-c01 on port 22. "
            "Prometheus shows recovery. A current TCP check now succeeds."
        ),
        tool_calls=tool_calls,
        iterations=4,
    )
    evaluation = evaluate_result(scenario.expectations, result)

    assert evaluation.passed is False
    assert "required_tool_attempt:loki_query" in evaluation.hard_failures


def test_retrieval_failure_bad_answer_fails_when_failure_is_blamed_on_target_system():
    score = _score(_RETRIEVAL_FAILURE, loki_outcome="integration_error")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22. "
        "We could not retrieve the logs, which caused the host to be down. "
        "Prometheus shows recovery and TCP now succeeds.",
    )

    assert evaluation.passed is False
    assert "does_not_blame_loki_failure_on_target_system" in evaluation.hard_failures


def test_retrieval_failure_bad_answer_fails_when_missing_logs_are_treated_as_proof():
    score = _score(_RETRIEVAL_FAILURE, loki_outcome="integration_error")
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22. "
        "Logs could not be retrieved. Prometheus shows recovery and a current "
        "TCP check succeeds, so logs confirm the host is healthy.",
    )

    assert evaluation.passed is False
    assert "does_not_treat_missing_logs_as_proof_of_system_state" in evaluation.hard_failures


# ---------------------------------------------------------------------------
# system-troubleshooter-contradictory-signals
# ---------------------------------------------------------------------------


def test_contradictory_signals_good_answer_passes():
    score = _score(_CONTRADICTORY_SIGNALS)
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "at that time. Prometheus shows the up metric for ferros-c01:9100 "
        "recovered back to 1, and a current TCP check now succeeds connecting "
        "to ferros-c01:22. However, the sshd logs still show a timeout before "
        "authentication in the most recent entry, logged after the metrics "
        "recovery point -- an inconsistency between the recovered metrics and "
        "the log evidence. This does not confirm a specific cause like a "
        "firewall issue, which remains only a possibility.",
    )

    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_contradictory_signals_bad_answer_fails_when_it_forces_a_fully_resolved_narrative():
    score = _score(_CONTRADICTORY_SIGNALS)
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22. "
        "Prometheus shows recovery and TCP now succeeds, so the incident is "
        "now fully resolved. Logs show a timeout before authentication.",
    )

    assert evaluation.passed is False
    assert "does_not_claim_permanently_fixed" in evaluation.hard_failures


def test_contradictory_signals_bad_answer_fails_when_it_forces_a_still_down_narrative():
    score = _score(_CONTRADICTORY_SIGNALS)
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22. "
        "The sshd logs still show a timeout before authentication, so "
        "ferros-c01 remains unreachable and has not recovered.",
    )

    assert evaluation.passed is False
    assert "does_not_claim_still_completely_down" in evaluation.hard_failures


def test_contradictory_signals_bad_answer_fails_when_it_ignores_the_log_discrepancy():
    score = _score(_CONTRADICTORY_SIGNALS)
    evaluation = score(
        "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 "
        "at that time. Prometheus shows the up metric recovered back to 1, "
        "and a current TCP check now succeeds connecting to ferros-c01:22.",
    )

    assert evaluation.passed is False
    assert "cites_the_later_log_error" in evaluation.hard_failures
