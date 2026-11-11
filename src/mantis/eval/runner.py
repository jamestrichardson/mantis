"""The evaluation runner: execute one scenario against one or more LiteLLM
model aliases through the real ``AgentRuntime``, recording what happened.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone

from openai import OpenAIError

from mantis.config import LiteLLMConfig, ModelRoutingPolicy
from mantis.eval.results import EvalResult, ToolCallSummary
from mantis.eval.scenarios import Scenario
from mantis.eval.scoring import Evaluation, evaluate_result
from mantis.observability import metrics
from mantis.observability.logging import log_event
from mantis.routing import safe_model_call_detail
from mantis.runtime import DEFAULT_MAX_ITERATIONS, AgentRuntime, RuntimeError_

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_scenario(
    scenario: Scenario,
    model_alias: str,
    *,
    base_model_config: LiteLLMConfig | None = None,
    routing_policy: ModelRoutingPolicy | None = None,
) -> EvalResult:
    """Run ``scenario`` once against ``model_alias`` and return the result.

    ``routing_policy`` (#16) is optional and defaults to ``None``, which
    ``AgentRuntime`` itself turns into a one-route policy wrapping
    ``model_alias`` — identical to every call site that predates this
    parameter. Pass an explicit policy (with ``model_alias`` as its
    ``primary_alias``) to exercise a real primary+fallback routing
    policy through the eval/qualification path, e.g. to qualify a
    candidate alias *and* its configured fallback(s) together rather
    than only ever the single-route default.

    Only two categories of failure are caught and recorded as
    ``outcome="error"`` instead of raising:

    - ``openai.OpenAIError`` (and subclasses) — the model/backend itself
      misbehaved: unreachable, timed out, rate-limited, authentication
      failure, a malformed response, etc.
    - ``mantis.runtime.RuntimeError_`` (and subclasses, e.g.
      ``MaxIterationsExceededError``, ``ModelRoutingExhaustedError``) —
      the *model's own behavior* was disqualifying (never converged,
      looped on tool calls, exhausted every configured route).

    Both are legitimate qualification signal about the model being
    evaluated, which is why one model's failure must never abort a
    multi-model comparison in :func:`run_comparison`.

    Anything else — an ``AttributeError``, ``TypeError``, a bug in a
    scenario's fixture code, or any other exception not in those two
    categories — is a bug in Mantis itself (the harness, the runtime, or
    a scenario), not evidence about the model, and must propagate rather
    than being recorded as if the model had failed. Silently attributing
    a Mantis defect to "model X errored" would corrupt qualification
    data in exactly the way this harness exists to prevent — callers
    (e.g. ``mantis.eval.qualification.qualify_models``) must preserve
    this distinction rather than catching more broadly.
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
        max_iterations=(
            scenario.max_iterations
            if scenario.max_iterations is not None
            else DEFAULT_MAX_ITERATIONS
        ),
        routing_policy=routing_policy,
    )

    started_at = _utc_now_iso()
    start_perf = time.perf_counter()
    final_answer: str | None = None
    outcome = "ok"
    error: str | None = None
    error_summary: str | None = None

    try:
        final_answer = runtime.run(scenario.prompt)
    except (OpenAIError, RuntimeError_) as exc:
        outcome = "error"
        error = f"{type(exc).__name__}: {exc}"
        # Bounded/safe (class name + HTTP status code only, see
        # mantis.routing.safe_model_call_detail) -- never the raw
        # exception message, which for an openai.OpenAIError can embed
        # an arbitrary, potentially large upstream/provider error body
        # (a real example: an nginx 504 Gateway Time-out HTML page).
        # `error` above keeps the full detail for local/raw-file
        # debugging; `error_summary` is what's safe to carry into a
        # bounded, committed artifact (see
        # mantis.eval.qualification.QualificationRecord).
        error_summary = safe_model_call_detail(exc)
        # Logged bounded/safe (error_summary), never the raw error string
        # above -- a log line is exactly as exposed as a committed
        # artifact (aggregated, retained, often shipped off-box), so it
        # gets the same treatment.
        logger.warning(
            "[eval] scenario=%s model=%s failed: %s", scenario.name, model_alias, error_summary
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
    backend_model = next(
        (m for m in reversed(runtime.backend_model_log) if m is not None), None
    )
    # #16: route-attempt history, always populated (even for a run that
    # used no explicit routing policy -- see AgentRuntime.__post_init__'s
    # default one-route wrapping), never inflating tool_calls/tool_call
    # counts above, which come entirely from runtime.call_log.
    requested_primary_alias = runtime.routing_policy.primary_alias
    final_alias = next(
        (attempt.requested_alias for attempt in reversed(runtime.model_call_log) if attempt.outcome == "ok"),
        None,
    )
    route_attempts = [attempt.to_dict() for attempt in runtime.model_call_log]

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
        backend_model=backend_model,
        requested_primary_alias=requested_primary_alias,
        final_alias=final_alias,
        route_attempts=route_attempts,
        error=error,
        error_summary=error_summary,
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
