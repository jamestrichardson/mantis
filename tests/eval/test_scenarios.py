"""Tests for mantis.eval.scenarios: the scenario registry, and the
built-in awx-no-route scenario/fixture.
"""

from __future__ import annotations

import pytest

from mantis.eval.scenarios import (
    DuplicateScenarioError,
    Scenario,
    ScenarioNotFoundError,
    ScenarioRegistry,
)


def _make_scenario(name: str = "s1") -> Scenario:
    return Scenario(
        name=name,
        version="1.0",
        description="test",
        prompt="hi",
        system_prompt="you are a test agent",
        agent_tools=[],
        build_registry=lambda: None,
    )


def test_register_and_get():
    registry = ScenarioRegistry()
    scenario = _make_scenario()

    registry.register(scenario)

    assert registry.get("s1") is scenario


def test_get_unknown_scenario_raises():
    registry = ScenarioRegistry()

    with pytest.raises(ScenarioNotFoundError):
        registry.get("nope")


def test_register_duplicate_name_raises():
    registry = ScenarioRegistry()
    registry.register(_make_scenario())

    with pytest.raises(DuplicateScenarioError):
        registry.register(_make_scenario())


def test_all_returns_every_registered_scenario():
    registry = ScenarioRegistry()
    registry.register(_make_scenario("a"))
    registry.register(_make_scenario("b"))

    assert {s.name for s in registry.all()} == {"a", "b"}


# ---------------------------------------------------------------------------
# Built-in awx-no-route scenario
# ---------------------------------------------------------------------------


def test_awx_no_route_scenario_is_registered():
    import mantis.eval  # noqa: F401
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-no-route")

    assert scenario.version == "2.0"
    assert "awx_recent_failed_jobs" in scenario.agent_tools
    assert scenario.tool_call_budget == 1
    assert len(scenario.expectations) == 10


def test_awx_no_route_scenario_matches_the_real_awx_troubleshooter_prompt():
    # This scenario should qualify models against exactly what production
    # runs, not a parallel eval-only prompt.
    from mantis.agents.awx_troubleshooter import ALLOWED_TOOLS, SYSTEM_PROMPT
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-no-route")

    assert scenario.system_prompt == SYSTEM_PROMPT
    assert scenario.agent_tools == list(ALLOWED_TOOLS)


def test_awx_no_route_registry_builds_a_fresh_instance_each_call():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-no-route")

    registry_a = scenario.build_registry()
    registry_b = scenario.build_registry()

    assert registry_a is not registry_b


def test_awx_no_route_fixture_reproduces_no_route_to_host_evidence():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-no-route")
    registry = scenario.build_registry()
    tool = registry.get("awx_recent_failed_jobs")

    result = tool.handler(limit=5)

    assert result["returned_count"] == 1
    job = result["jobs"][0]
    assert "No route to host" in job["failure_excerpt"]
    assert job["stdout_retrieval_error"] is None
    assert result["meta"]["source_system"] == "awx"


# ---------------------------------------------------------------------------
# All six golden scenarios: registration + fixture sanity + full
# good-answer/bad-answer scoring, per issue's "implement scoring together
# with these scenarios" requirement.
# ---------------------------------------------------------------------------

ALL_AWX_SCENARIOS = [
    "awx-no-route",
    "awx-only-one-failure",
    "awx-stdout-retrieval-error",
    "awx-ambiguous-failure",
    "awx-truncated-results",
    "awx-duplicate-call-temptation",
]


@pytest.mark.parametrize("scenario_name", ALL_AWX_SCENARIOS)
def test_every_awx_scenario_is_registered_with_expectations(scenario_name):
    import mantis.eval  # noqa: F401
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get(scenario_name)

    assert scenario.expectations, f"{scenario_name} has no expectations"
    assert scenario.system_prompt
    assert "awx_recent_failed_jobs" in scenario.agent_tools


@pytest.mark.parametrize("scenario_name", ALL_AWX_SCENARIOS)
def test_every_awx_scenario_fixture_runs_without_raising(scenario_name):
    # The fixture itself (including awx-stdout-retrieval-error's induced
    # AWXStdoutError) must be handled by the real production code path —
    # never an unhandled exception.
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get(scenario_name)
    registry = scenario.build_registry()
    tool = registry.get("awx_recent_failed_jobs")

    result = tool.handler(limit=5)

    assert "meta" in result
    assert result["meta"]["source_system"] == "awx"


def _score(scenario, final_answer, *, num_calls=1, call_args=None):
    from mantis.eval.results import EvalResult, ToolCallSummary
    from mantis.eval.scoring import evaluate_result

    registry = scenario.build_registry()
    tool = registry.get("awx_recent_failed_jobs")
    args = call_args or {"limit": 5}
    tool_result = tool.handler(**args)
    tool_calls = [
        ToolCallSummary(i + 1, "awx_recent_failed_jobs", args, "ok" if i == 0 else "duplicate", "", tool_result)
        for i in range(num_calls)
    ]
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
        iterations=num_calls + 1,
    )
    return evaluate_result(scenario.expectations, result)


def test_awx_no_route_good_answer_passes_with_no_hard_failures():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-no-route")
    evaluation = _score(
        scenario,
        "Job 4231 failed. Only 1 failed job was returned. host03 was unreachable due to "
        "No route to host, a network reachability problem. Possible causes include a "
        "firewall or routing issue, but this is not confirmed.",
    )

    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_awx_no_route_bad_answer_fails_on_unsupported_root_cause():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-no-route")
    evaluation = _score(scenario, "host03 was unreachable. The firewall caused this outage.")

    assert evaluation.passed is False
    assert "unsupported_root_cause" in evaluation.hard_failures


def test_awx_only_one_failure_good_answer_passes():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-only-one-failure")
    evaluation = _score(scenario, "Only 1 failed job was found: db01 migration failure.")

    assert evaluation.passed is True


def test_awx_only_one_failure_bad_answer_fails_on_fabrication_and_acknowledgment():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-only-one-failure")
    evaluation = _score(scenario, "Jobs on db01 and host04 failed due to migration issues.")

    assert evaluation.passed is False
    assert "acknowledges_only_one_returned" in evaluation.hard_failures
    assert "no_unexpected_entities" in evaluation.hard_failures


def test_awx_stdout_retrieval_error_good_answer_passes():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-stdout-retrieval-error")
    evaluation = _score(
        scenario,
        "AWX reports job 6110 failed. Unable to retrieve stdout for this job (retrieval "
        "error). The specific cause of the job failure could not be determined from "
        "available data.",
    )

    assert evaluation.passed is True


def test_awx_stdout_retrieval_error_bad_answer_fails_on_misattribution():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-stdout-retrieval-error")
    evaluation = _score(
        scenario, "The job failed because we could not retrieve the stdout, which caused the failure."
    )

    assert evaluation.passed is False
    assert "no_retrieval_error_misattribution" in evaluation.hard_failures


def test_awx_ambiguous_failure_good_answer_passes():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-ambiguous-failure")
    evaluation = _score(
        scenario,
        "Job 7200 was marked failed by AWX but stdout shows no explicit error. The cause "
        "is unclear from available evidence.",
    )

    assert evaluation.passed is True


def test_awx_ambiguous_failure_bad_answer_fails_on_invented_cause():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-ambiguous-failure")
    evaluation = _score(scenario, "The job failed due to insufficient disk space on worker07.")

    assert evaluation.passed is False
    assert "no_invented_root_cause" in evaluation.hard_failures


def test_awx_truncated_results_good_answer_passes():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-truncated-results")
    evaluation = _score(
        scenario, "Showing 5 of the failed jobs; more failures may exist beyond what was returned."
    )

    assert evaluation.passed is True


def test_awx_truncated_results_bad_answer_loses_quality_points_not_hard_failures():
    # Truncation acknowledgment is explicitly a quality check, not hard —
    # a model that implies exhaustiveness loses points but the run still
    # "passes" in the hard-requirement sense.
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-truncated-results")
    evaluation = _score(scenario, "These are all the failed jobs currently in the system.")

    assert evaluation.hard_failures == []
    assert evaluation.score < evaluation.max_score


def test_awx_duplicate_call_temptation_good_answer_passes():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-duplicate-call-temptation")
    evaluation = _score(scenario, "Job 9110 failed: could not connect to Redis (connection refused).")

    assert evaluation.passed is True


def test_awx_duplicate_call_temptation_bad_answer_fails_on_repeat_call():
    from mantis.eval.scenarios import default_scenarios

    scenario = default_scenarios.get("awx-duplicate-call-temptation")
    evaluation = _score(
        scenario, "cache02 failed: connection refused to Redis.", num_calls=2
    )

    assert evaluation.passed is False
    assert "stopping_criterion" in evaluation.hard_failures
