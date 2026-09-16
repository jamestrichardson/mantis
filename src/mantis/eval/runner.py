"""The evaluation runner: execute one scenario against one or more LiteLLM
model aliases through the real ``AgentRuntime``, recording what happened.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone

from mantis.config import LiteLLMConfig
from mantis.eval.results import EvalResult, ToolCallSummary
from mantis.eval.scenarios import Scenario
from mantis.runtime import AgentRuntime

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_scenario(
    scenario: Scenario,
    model_alias: str,
    *,
    base_model_config: LiteLLMConfig | None = None,
) -> EvalResult:
    """Run ``scenario`` once against ``model_alias`` and return the result.

    Never raises for a model/runtime failure (e.g. the model server is
    unreachable, or the model never converges within the iteration
    budget) — that's recorded as ``outcome="error"`` on the returned
    result instead, so a multi-model comparison in
    :func:`run_comparison` can't be aborted by one model's failure. Only
    a genuine programming error (a bug in the scenario/runner itself)
    propagates out.
    """
    model_config = dataclasses.replace(
        base_model_config or LiteLLMConfig.from_env(), model=model_alias
    )
    registry = scenario.build_registry()
    runtime = AgentRuntime(
        name=f"eval:{scenario.name}",
        system_prompt=scenario.system_prompt,
        tools=scenario.agent_tools,
        model_config=model_config,
        registry=registry,
        tool_call_budget=scenario.tool_call_budget,
        temperature=scenario.temperature,
    )

    started_at = _utc_now_iso()
    start_perf = time.perf_counter()
    final_answer: str | None = None
    outcome = "ok"
    error: str | None = None

    try:
        final_answer = runtime.run(scenario.prompt)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: see docstring
        outcome = "error"
        error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[eval] scenario=%s model=%s failed: %s", scenario.name, model_alias, error
        )

    elapsed_seconds = time.perf_counter() - start_perf
    finished_at = _utc_now_iso()

    tool_calls = [
        ToolCallSummary(
            iteration=entry.iteration,
            tool_name=entry.tool_name,
            arguments=entry.arguments,
            outcome=entry.outcome,
            detail=entry.detail,
            result=entry.result,
        )
        for entry in runtime.call_log
    ]
    duplicate_call_count = sum(1 for tc in tool_calls if tc.outcome == "duplicate")
    malformed_call_count = sum(1 for tc in tool_calls if tc.outcome == "bad_arguments")
    iterations = max((tc.iteration for tc in tool_calls), default=0)
    # If the run ended without any tool call (or before one), iterations
    # still happened (at least the final answer's own round-trip) — the
    # usage log has one entry per model round-trip regardless of whether
    # it produced a tool call, so it's the more reliable iteration count.
    iterations = max(iterations, len(runtime.usage_log))

    token_totals = [
        entry["total_tokens"]
        for entry in runtime.usage_log
        if entry is not None and "total_tokens" in entry
    ]
    total_tokens = sum(token_totals) if token_totals else None

    return EvalResult(
        scenario=scenario.name,
        scenario_version=scenario.version,
        model=model_alias,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_seconds=elapsed_seconds,
        outcome=outcome,
        final_answer=final_answer,
        tool_calls=tool_calls,
        iterations=iterations,
        duplicate_call_count=duplicate_call_count,
        malformed_call_count=malformed_call_count,
        usage=list(runtime.usage_log),
        total_tokens=total_tokens,
        error=error,
    )


def run_comparison(
    scenario: Scenario,
    model_aliases: list[str],
    *,
    base_model_config: LiteLLMConfig | None = None,
) -> list[EvalResult]:
    """Run ``scenario`` against each of ``model_aliases`` in turn.

    One model's failure never prevents the rest from running — see
    :func:`run_scenario`.
    """
    config = base_model_config or LiteLLMConfig.from_env()
    return [
        run_scenario(scenario, model_alias, base_model_config=config)
        for model_alias in model_aliases
    ]
