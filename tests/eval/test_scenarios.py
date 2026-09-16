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

    assert scenario.version == "1.1"
    assert "awx_recent_failed_jobs" in scenario.agent_tools
    assert scenario.tool_call_budget == 1
    assert len(scenario.expectations) == 7


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
