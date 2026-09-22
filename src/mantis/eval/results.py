"""The evaluation result record: one scenario run against one model.

This is the machine-readable output of ``mantis eval run`` — see
``docs/evaluation.md`` for the on-disk format (JSON Lines, one record per
line) and versioning policy.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

RESULT_FORMAT_VERSION = "2.0"
"""Bumped on a breaking change to :class:`EvalResult`'s shape (a field
removed or renamed). Adding a new optional field does not require a bump.
2.0: replaced ``score`` (``{"checks", "passed", "total"}``) with
``evaluation`` (``{"passed", "score", "max_score", "checks", "hard_failures"}``)
— see ``mantis.eval.scoring.Evaluation``."""


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
        backend_model: The backend-resolved model identity
            (``response.model``, see ``AgentRuntime.backend_model_log``)
            from the last iteration that reported one — distinct from
            ``model`` (the requested LiteLLM *alias*). ``None`` when no
            iteration's response carried this field; never guessed.
        requested_primary_alias: The routing policy's primary alias for
            this run (``AgentRuntime.routing_policy.primary_alias``, #16)
            — the alias that was *asked for*, distinct from
            ``final_alias`` below (which alias actually produced the
            response). Equal to ``model`` for every run that used no
            explicit routing policy (the default one-route case).
        final_alias: The alias of the last successful attempt in
            ``route_attempts`` (i.e. whichever route actually produced
            the run's last model response) — ``None`` if no attempt
            ever succeeded (e.g. every route failed).
        route_attempts: The full, bounded model-call attempt history for
            this run (see ``mantis.routing.ModelCallAttempt.to_dict``) —
            every attempt across every iteration, including attempts
            that failed and triggered a fallback. Distinct from
            ``tool_calls``: this is model-*call* history, never inflated
            by, and never inflating, actual tool execution counts.
        error: Exception type and message when ``outcome == "error"``,
            else ``None``. May contain a raw provider/upstream error body
            (e.g. an nginx error page) for some ``openai.OpenAIError``
            subclasses — fine for a local/raw result file, but never
            safe to copy into a bounded, committed artifact.
        error_summary: A bounded, safe summary of the same failure
            (exception class name + HTTP status code only, see
            ``mantis.routing.safe_model_call_detail``) — ``None``
            whenever ``error`` is. This is what
            ``mantis.eval.qualification.QualificationRecord`` copies
            into its own committed-safe ``error`` field; ``error``
            above is never copied there directly.
        raw_message: The raw final-message payload (via
            ``AgentRuntime.diagnostic_raw_message``), captured only when a
            run ended with neither usable answer text nor a tool call —
            e.g. a model spent completion tokens but they landed in a
            provider-specific field (like ``reasoning_content``) this
            runtime doesn't read, or a malformed tool-call attempt never
            parsed into ``tool_calls``. ``None`` for a normal run, so this
            never bloats the common case with a redundant dump of data
            already in ``final_answer``.
        evaluation: Deterministic scoring against the scenario's
            ``expectations`` (see ``mantis.eval.scoring.Evaluation.to_dict``),
            computed and attached by ``run_scenario`` —
            ``{"passed": bool, "score": N, "max_score": M, "checks": [...],
            "hard_failures": [...]}``. ``passed`` is governed solely by
            ``hard_failures`` being empty, independent of the numeric
            score — a run can score less than max and still pass (missed
            quality checks only), or score highly and still fail (one
            hard requirement violated). ``None`` when the scenario
            declares no expectations (unscored), not when scoring ran and
            found failures — an unscored run and an all-failing scored
            run are different things and must stay distinguishable.
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
    backend_model: str | None = None
    requested_primary_alias: str | None = None
    final_alias: str | None = None
    route_attempts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_summary: str | None = None
    raw_message: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None
    result_format_version: str = RESULT_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        # dataclasses.asdict recurses into the nested ToolCallSummary
        # dataclasses in tool_calls automatically.
        return dataclasses.asdict(self)
