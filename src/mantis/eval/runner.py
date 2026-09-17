"""The evaluation runner: execute one scenario against one or more LiteLLM
model aliases through the real ``AgentRuntime``, recording what happened.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone

from openai import OpenAIError

from mantis.config import LiteLLMConfig
from mantis.eval.results import EvalResult, ToolCallSummary
from mantis.eval.scenarios import Scenario
from mantis.eval.scoring import Evaluation, evaluate_result
from mantis.observability import metrics
from mantis.observability.logging import log_event
from mantis.runtime import AgentRuntime, RuntimeError_

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

    Only two categories of failure are caught and recorded as
    ``outcome="error"`` instead of raising:

    - ``openai.OpenAIError`` (and subclasses) — the model/backend itself
      misbehaved: unreachable, timed out, rate-limited, authentication
      failure, a malformed response, etc.
    - ``mantis.runtime.RuntimeError_`` (and subclasses, e.g.
      ``MaxIterationsExceededError``) — the *model's own behavior* was
      disqualifying (never converged, looped on tool calls).

    Both are legitimate qualification signal about the model being
    evaluated, which is why one model's failure must never abort a
    multi-model comparison in :func:`run_comparison`.

    Anything else — an ``AttributeError``, ``TypeError``, a bug in a
    scenario's fixture code, or any other exception not in those two
    categories — is a bug in Mantis itself (the harness, the runtime, or
    a scenario), not evidence about the model, and must propagate rather
    than being recorded as if the model had failed. Silently attributing
    a Mantis defect to "model X errored" would corrupt qualification
    data in exactly the way this harness exists to prevent.
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
    except (OpenAIError, RuntimeError_) as exc:
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

    result = EvalResult(
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
        raw_message=runtime.diagnostic_raw_message,
    )

    evaluation = None
    if scenario.expectations:
        evaluation = evaluate_result(scenario.expectations, result)
        result = dataclasses.replace(result, evaluation=evaluation.to_dict())

    _emit_eval_observability(scenario, model_alias, result, evaluation, run_id=runtime.last_run_id)

    return result


def _emit_eval_observability(
    scenario: Scenario,
    model_alias: str,
    result: EvalResult,
    evaluation: Evaluation | None,
    *,
    run_id: str | None,
) -> None:
    """Emit ``mantis_eval_result``/``mantis_eval_check`` events and record
    ``mantis_eval_*`` metrics — onto the *same* registry/event schema
    ``AgentRuntime`` itself uses, not a parallel eval-only implementation.
    """
    env = metrics.environment()

    if result.outcome == "error":
        eval_outcome = "error"
    elif evaluation is not None:
        eval_outcome = "pass" if evaluation.passed else "fail"
    else:
        eval_outcome = "unscored"

    log_event(
        logger,
        "mantis_eval_result",
        run_id=run_id,
        scenario=scenario.name,
        model_alias=model_alias,
        outcome=eval_outcome,
        duration_seconds=result.elapsed_seconds,
        score=evaluation.score if evaluation is not None else None,
        max_score=evaluation.max_score if evaluation is not None else None,
    )
    metrics.EVAL_RUNS_TOTAL.labels(
        scenario=scenario.name, model_alias=model_alias, result=eval_outcome, environment=env
    ).inc()

    if evaluation is None:
        return

    metrics.EVAL_SCORE_RATIO.labels(
        scenario=scenario.name, model_alias=model_alias, environment=env
    ).observe(evaluation.score / evaluation.max_score)
    if evaluation.hard_failures:
        metrics.EVAL_HARD_FAILURES_TOTAL.labels(
            scenario=scenario.name, model_alias=model_alias, environment=env
        ).inc(len(evaluation.hard_failures))

    for check in evaluation.checks:
        log_event(
            logger,
            "mantis_eval_check",
            run_id=run_id,
            scenario=scenario.name,
            model_alias=model_alias,
            check_name=check.name,
            outcome="pass" if check.passed else "fail",
            hard=check.hard,
            detail=check.detail,
        )


def run_comparison(
    scenario: Scenario,
    model_aliases: list[str],
    *,
    base_model_config: LiteLLMConfig | None = None,
) -> list[EvalResult]:
    """Run ``scenario`` against each of ``model_aliases`` in turn.

    One model/backend failure never prevents the rest from running — but a
    genuine bug in Mantis itself still raises and aborts the comparison
    rather than being misattributed to whichever model was running at the
    time. See :func:`run_scenario`.
    """
    config = base_model_config or LiteLLMConfig.from_env()
    return [
        run_scenario(scenario, model_alias, base_model_config=config)
        for model_alias in model_aliases
    ]
