"""The evaluation result record: one scenario run against one model.

This is the machine-readable output of ``mantis eval run`` — see
``docs/evaluation.md`` for the on-disk format (JSON Lines, one record per
line) and versioning policy.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

RESULT_FORMAT_VERSION = "1.0"
"""Bumped on a breaking change to :class:`EvalResult`'s shape (a field
removed or renamed). Adding a new optional field does not require a bump."""


@dataclass
class ToolCallSummary:
    """One tool-call attempt, ordered as it happened during the run.

    Mirrors ``mantis.runtime.ToolCallLogEntry`` — built from it directly
    rather than redefining an incompatible shape.
    """

    iteration: int
    tool_name: str
    arguments: dict[str, Any] | None
    outcome: str  # "ok", "duplicate", "unknown_tool", "bad_arguments", "error"
    detail: str
    result: Any

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class EvalResult:
    """One scenario run against one model alias.

    Args:
        scenario: The scenario's name (``Scenario.name``).
        scenario_version: The scenario's version (``Scenario.version``),
            captured alongside the name so a result record stays
            meaningful even after the scenario definition changes later.
        model: The LiteLLM model alias this run used.
        started_at: ISO 8601 UTC timestamp when the run began.
        finished_at: ISO 8601 UTC timestamp when the run ended (success
            or failure).
        elapsed_seconds: Wall-clock duration of the run.
        outcome: ``"ok"`` if the agent produced a final answer, ``"error"``
            if the run raised (model/runtime failure — never raised out of
            the runner itself; see ``mantis.eval.runner.run_scenario``).
        final_answer: The agent's final answer text, or ``None`` on error.
        tool_calls: Ordered tool-call trace for this run (see
            :class:`ToolCallSummary`).
        iterations: Number of model round-trips this run took.
        duplicate_call_count: How many tool calls in ``tool_calls`` were
            exact-duplicate replays (see ``AgentRuntime``'s dedupe cache).
        malformed_call_count: How many tool calls had unparseable
            arguments.
        usage: Per-iteration token usage, when the backend returned it
            (see ``AgentRuntime.usage_log``) — one entry per iteration,
            ``None`` for an iteration without usage data.
        total_tokens: Convenience sum of ``total_tokens`` across every
            ``usage`` entry that has one; ``None`` if none do.
        error: Exception type and message when ``outcome == "error"``,
            else ``None``.
        result_format_version: See :data:`RESULT_FORMAT_VERSION`.
    """

    scenario: str
    scenario_version: str
    model: str
    started_at: str
    finished_at: str
    elapsed_seconds: float
    outcome: str
    final_answer: str | None
    tool_calls: list[ToolCallSummary] = field(default_factory=list)
    iterations: int = 0
    duplicate_call_count: int = 0
    malformed_call_count: int = 0
    usage: list[dict[str, Any] | None] = field(default_factory=list)
    total_tokens: int | None = None
    error: str | None = None
    result_format_version: str = RESULT_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        # dataclasses.asdict recurses into the nested ToolCallSummary
        # dataclasses in tool_calls automatically.
        return dataclasses.asdict(self)
