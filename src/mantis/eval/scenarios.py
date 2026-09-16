"""Scenario definitions and registry for the evaluation harness.

A :class:`Scenario` pairs a prompt and an allowed-tool list (exactly what
an agent needs) with a *fixture-backed* tool registry, so a scenario runs
through the real :class:`~mantis.runtime.AgentRuntime` without touching any
live external system. Mirrors :class:`mantis.registry.ToolRegistry`'s
name-based registration pattern for consistency with the rest of the
codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mantis.registry import ToolRegistry


@dataclass(frozen=True)
class Scenario:
    """One evaluation scenario.

    Args:
        name: Unique scenario identifier, e.g. ``"awx-no-route"``.
        version: Scenario version — bump when the fixture data or prompt
            changes meaningfully, so old result records stay interpretable
            (see ``mantis.eval.results.EvalResult.scenario_version``).
        description: Short human-readable description of what this
            scenario exercises.
        prompt: The user prompt sent to the agent.
        system_prompt: The agent's system prompt for this scenario.
        agent_tools: Tool names the agent is allowed to call — passed
            straight through to ``AgentRuntime(tools=...)``.
        build_registry: Builds a fresh, scenario-scoped
            :class:`~mantis.registry.ToolRegistry` containing
            fixture-backed handlers for every name in ``agent_tools``. A
            fresh registry per call (not a shared singleton) keeps runs
            isolated — nothing about one run's fixtures can leak into
            another's.
        tool_call_budget: Optional ``AgentRuntime(tool_call_budget=...)``
            passthrough, for scenarios that should mirror a specific
            agent's runtime tuning (e.g. the AWX Troubleshooter's
            ``tool_call_budget=1``).
        temperature: Optional ``AgentRuntime(temperature=...)`` passthrough,
            same rationale as ``tool_call_budget`` — a scenario should
            reproduce the real agent's tuning, not evaluate a model under
            different conditions than production actually uses.
    """

    name: str
    version: str
    description: str
    prompt: str
    system_prompt: str
    agent_tools: list[str]
    build_registry: Callable[[], ToolRegistry]
    tool_call_budget: int | None = None
    temperature: float | None = None


class ScenarioNotFoundError(KeyError):
    """Raised when an unregistered scenario name is requested."""


class DuplicateScenarioError(ValueError):
    """Raised when registering a scenario name that's already registered."""


class ScenarioRegistry:
    """In-memory registry mapping scenario names to :class:`Scenario`."""

    def __init__(self) -> None:
        self._scenarios: dict[str, Scenario] = {}

    def register(self, scenario: Scenario) -> None:
        if scenario.name in self._scenarios:
            raise DuplicateScenarioError(f"Scenario already registered: {scenario.name}")
        self._scenarios[scenario.name] = scenario

    def get(self, name: str) -> Scenario:
        try:
            return self._scenarios[name]
        except KeyError as exc:
            raise ScenarioNotFoundError(name) from exc

    def all(self) -> list[Scenario]:
        return list(self._scenarios.values())


default_scenarios = ScenarioRegistry()
"""Process-wide scenario registry. Built-in scenario modules under
``mantis.eval.fixtures`` register into this as an import-time side effect —
mirrors ``mantis.registry.default_registry``'s pattern."""
