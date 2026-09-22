"""Model qualification (#13): run a fixed, versioned baseline suite of
golden scenarios against two or more LiteLLM aliases and produce
evidence-backed, role-qualification data.

This module introduces **no second evaluator** — it is a thin
orchestration layer over the existing ``mantis.eval.runner``/
``mantis.eval.scenarios``/``mantis.eval.scoring`` stack:

- The baseline suite is a checked-in, versioned list of *existing*
  scenario names (:data:`QUALIFICATION_SCENARIOS`), never a
  human-maintained list in documentation.
- Each (model alias, scenario) pair is executed by the real
  ``mantis.eval.runner.run_scenario``, which itself runs the real
  ``AgentRuntime`` through LiteLLM — there is no direct provider or
  Ollama shortcut anywhere in this module.
- Scoring is the same deterministic, non-LLM-judged
  ``mantis.eval.scoring.evaluate_result`` every other eval path uses.

What this module *adds* on top of that:

- :class:`QualificationRecord` — a bounded, safe-to-commit evidence
  record per (model, scenario) pair (see its docstring for the full
  field list, matching the issue's "Captured evidence" contract).
- :func:`qualify_models` — the multi-model/multi-scenario orchestration
  loop. Deliberately preserves ``run_scenario``'s own isolation
  boundary rather than widening it: a known model/backend failure never
  aborts the rest of the matrix (because ``run_scenario`` itself never
  raises for one), but an unexpected Mantis bug still propagates and
  aborts the whole run, exactly as ``run_scenario``/``run_comparison``
  already document. "One model failing must not abort the others" is
  not the same claim as "swallow every programming error" — this
  module never conflates the two.
- Deterministic role-eligibility rules (:func:`evaluate_role_eligibility`)
  — documented, machine-testable pass/fail logic, never a subjective
  "this one felt better" judgment call.
- :func:`format_result_matrix`/:func:`format_role_eligibility` — plain
  deterministic text formatting of a completed qualification run, for
  both the CLI's printed output and to help keep
  ``docs/model-qualification.md`` traceable to a real run.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from mantis import __version__ as MANTIS_VERSION
from mantis.config import LiteLLMConfig
from mantis.eval.results import EvalResult
from mantis.eval.runner import run_scenario
from mantis.eval.scenarios import default_scenarios

# ---------------------------------------------------------------------------
# The named, versioned baseline suite (checked into repository code, never
# a human-maintained doc list). Bump the version suffix -- and add a note
# here -- any time a scenario is added to or removed from this tuple.
# ---------------------------------------------------------------------------

QUALIFICATION_SUITE_NAME = "mantis-core-qualification"
QUALIFICATION_SUITE_VERSION = "v1"
QUALIFICATION_SUITE_ID = f"{QUALIFICATION_SUITE_NAME}-{QUALIFICATION_SUITE_VERSION}"

QUALIFICATION_SCENARIOS: tuple[str, ...] = (
    "awx-structured-unreachable",
    "system-troubleshooter-full-investigation",
    "incident-triage-git-correlation-no-deployment-proof",
    "incident-triage-conflicting-current-and-historical",
    "incident-triage-source-unavailable",
    "incident-triage-kubernetes-event-history",
    "incident-triage-untrusted-kubernetes-event",
    "awx-truncated-results",
    "awx-duplicate-call-temptation",
    "awx-prompt-injection",
)
"""``mantis-core-qualification-v1``'s exact scenario membership, in a
fixed, checked-in order (see ``tests/test_qualification.py`` for the
stability/order regression test). Coverage mapping, per the issue's
required categories:

- single-tool structured evidence -> ``awx-structured-unreachable``
- multi-step System Troubleshooter -> ``system-troubleshooter-full-investigation``
- Incident Triage (Mantis's most demanding current multi-source
  reasoning workload, #12) -> every ``incident-triage-*`` scenario below
- retrieval failure -> ``incident-triage-source-unavailable``
- contradictory current/historical evidence -> ``incident-triage-conflicting-current-and-historical``
- truncation/incomplete evidence -> ``awx-truncated-results``
- untrusted/prompt-like evidence -> ``incident-triage-untrusted-kubernetes-event``, ``awx-prompt-injection``
- Git correlation without causal/deployment overclaim -> ``incident-triage-git-correlation-no-deployment-proof``
- Kubernetes / current-vs-historical state -> ``incident-triage-conflicting-current-and-historical``, ``incident-triage-kubernetes-event-history``
- duplicate/redundant call temptation -> ``awx-duplicate-call-temptation``
- stopping behavior -> implicit in every scenario's ``MaxToolCalls``/
  ``MaxIterations`` expectations, elevated to a hard requirement in
  ``awx-duplicate-call-temptation`` specifically
- malformed tool-call handling -> **not currently represented** by any
  golden scenario (only by direct ``AgentRuntime`` unit tests, e.g.
  ``tests/test_runtime.py``) -- intentionally omitted here rather than
  silently claimed; add a scenario and a new suite version once one
  exists.
"""

FAST_QUALIFICATION_SUITE_NAME = "mantis-fast-qualification"
FAST_QUALIFICATION_SUITE_VERSION = "v1"
FAST_QUALIFICATION_SUITE_ID = f"{FAST_QUALIFICATION_SUITE_NAME}-{FAST_QUALIFICATION_SUITE_VERSION}"

FAST_QUALIFICATION_SCENARIOS: tuple[str, ...] = (
    "awx-structured-unreachable",
    "awx-prompt-injection",
    "incident-triage-source-unavailable",
    "awx-duplicate-call-temptation",
)
"""``mantis-fast-qualification-v1``: a smaller, checked-in, versioned
subset of :data:`QUALIFICATION_SCENARIOS` for a candidate too
slow/expensive to justify the full baseline, while still including a
trust/injection-discipline check (``awx-prompt-injection``), a
retrieval/error-discipline check (``incident-triage-source-unavailable``),
and a stopping-behavior check (``awx-duplicate-call-temptation``), per
the issue's requirement. Every name here is deliberately a member of
:data:`QUALIFICATION_SCENARIOS` too, so a single full-suite qualification
run always carries enough evidence to evaluate *both* roles without a
second, separate invocation."""

assert set(FAST_QUALIFICATION_SCENARIOS) <= set(QUALIFICATION_SCENARIOS), (
    "FAST_QUALIFICATION_SCENARIOS must remain a subset of QUALIFICATION_SCENARIOS "
    "so one full-suite run always carries enough evidence for both roles"
)

ROLE_MANTIS_REASONING = "mantis-reasoning"
ROLE_MANTIS_FAST = "mantis-fast"
ROLE_MANTIS_CODER = "mantis-coder"


# ---------------------------------------------------------------------------
# Captured evidence
# ---------------------------------------------------------------------------

QUALIFICATION_RECORD_FORMAT_VERSION = "1.0"


@dataclass(frozen=True)
class QualificationRecord:
    """One (model alias, scenario) qualification data point — the
    bounded, safe-to-commit evidence record. Deliberately excludes the
    full tool-call trace and final-answer text (which can be long and,
    for some scenarios, deliberately contain adversarial content) —
    ``final_answer_ref`` instead points at where that raw evidence lives
    (see :func:`write_qualification_artifacts`), never inlining it here.

    Fields absent from the underlying backend/provider response are
    ``None`` — never fabricated as ``0``/``"unknown"``/a guess. See
    :func:`_record_from_eval_result`, the only way this gets built —
    ``qualify_models`` never constructs one from an unexpected
    exception; see its own docstring.
    """

    suite_id: str
    suite_version: str
    requested_alias: str
    """The LiteLLM alias qualified for this (model, scenario) pair."""
    resolved_backend_model: str | None
    scenario: str
    scenario_version: str
    outcome: str  # "ok" | "error" (model/backend failure -- see mantis.eval.runner.run_scenario)
    passed: bool | None
    score: int | None
    max_score: int | None
    hard_failures: tuple[str, ...]
    iterations: int
    tool_call_count: int
    duplicate_call_count: int
    malformed_call_count: int
    elapsed_seconds: float
    total_tokens: int | None
    cost_usd: float | None
    final_answer_ref: str | None
    error: str | None
    mantis_version: str
    mantis_commit: str | None
    litellm_endpoint: str
    litellm_version: str | None
    generated_at: str
    record_format_version: str = QUALIFICATION_RECORD_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["hard_failures"] = list(self.hard_failures)
        return d


def _detect_mantis_commit() -> str | None:
    """Best-effort ``git rev-parse HEAD`` of the running checkout — never
    guessed, ``None`` if this isn't a git checkout, ``git`` isn't
    available, or the command fails for any other reason (e.g. a
    production container image with no ``.git`` directory)."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=True
        )
    except Exception:  # noqa: BLE001 -- version metadata is best-effort, never fatal
        return None
    return completed.stdout.strip() or None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_from_eval_result(
    result: EvalResult,
    *,
    suite_id: str,
    suite_version: str,
    requested_alias: str,
    mantis_version: str,
    mantis_commit: str | None,
    litellm_endpoint: str,
    litellm_version: str | None,
    generated_at: str,
    raw_result_index: int,
) -> QualificationRecord:
    evaluation = result.evaluation
    return QualificationRecord(
        suite_id=suite_id,
        suite_version=suite_version,
        requested_alias=requested_alias,
        resolved_backend_model=result.backend_model,
        scenario=result.scenario,
        scenario_version=result.scenario_version,
        outcome=result.outcome,
        passed=evaluation["passed"] if evaluation is not None else None,
        score=evaluation["score"] if evaluation is not None else None,
        max_score=evaluation["max_score"] if evaluation is not None else None,
        hard_failures=tuple(evaluation["hard_failures"]) if evaluation is not None else (),
        iterations=result.iterations,
        tool_call_count=len(result.tool_calls),
        duplicate_call_count=result.duplicate_call_count,
        malformed_call_count=result.malformed_call_count,
        elapsed_seconds=result.elapsed_seconds,
        total_tokens=result.total_tokens,
        # LiteLLM's per-request cost is reported via HTTP response headers
        # on its proxy, which the plain OpenAI-SDK client AgentRuntime
        # uses does not currently capture -- never fabricated as 0.0.
        cost_usd=None,
        final_answer_ref=f"raw_result_index={raw_result_index}",
        # Bounded/safe (see EvalResult.error_summary's docstring) --
        # result.error itself may embed a raw provider/upstream error
        # body and must never be copied into this committed-safe record.
        error=result.error_summary,
        mantis_version=mantis_version,
        mantis_commit=mantis_commit,
        litellm_endpoint=litellm_endpoint,
        litellm_version=litellm_version,
        generated_at=generated_at,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualificationRun:
    """The complete output of one :func:`qualify_models` call."""

    suite_id: str
    suite_version: str
    generated_at: str
    mantis_version: str
    mantis_commit: str | None
    litellm_endpoint: str
    model_aliases: tuple[str, ...]
    scenario_names: tuple[str, ...]
    records: tuple[QualificationRecord, ...]
    raw_results: tuple[EvalResult, ...]


RunScenarioFn = Callable[..., EvalResult]


def qualify_models(
    model_aliases: Sequence[str],
    *,
    scenario_names: Sequence[str] = QUALIFICATION_SCENARIOS,
    suite_id: str = QUALIFICATION_SUITE_ID,
    suite_version: str = QUALIFICATION_SUITE_VERSION,
    base_model_config: LiteLLMConfig | None = None,
    litellm_version: str | None = None,
    run_scenario_fn: RunScenarioFn = run_scenario,
) -> QualificationRun:
    """Run ``scenario_names`` (default: the full checked-in
    :data:`QUALIFICATION_SCENARIOS` baseline) against every alias in
    ``model_aliases``, in turn, through the real
    ``mantis.eval.runner.run_scenario`` (injectable as
    ``run_scenario_fn`` purely for deterministic tests with a
    fake/fixture runner — production code never overrides it).

    Isolation boundary — deliberately **not** widened from
    ``run_scenario``'s own: a known model/backend failure
    (``openai.OpenAIError``, ``mantis.runtime.RuntimeError_``) never
    aborts the rest of the matrix, because ``run_scenario`` itself
    already isolates those
    as a per-scenario ``outcome="error"`` result and never raises for
    them. Anything else — an unexpected ``KeyError``, a bug in a
    scenario's fixture, a scoring bug — is a genuine Mantis defect, not
    model evidence, and propagates out of this function exactly as it
    would out of ``run_scenario``/``run_comparison``, invalidating the
    whole qualification run rather than being silently attributed to
    whichever model happened to be running. "One model/backend failure
    must not abort qualification of the remaining models" describes
    ``run_scenario``'s own existing isolation, never a license to
    swallow arbitrary programming errors at this layer too. Resolving
    each scenario name via ``default_scenarios.get`` follows the same
    rule: a baseline/registry drift bug (the checked-in list naming a
    scenario that no longer exists) propagates loudly.
    """
    config = base_model_config or LiteLLMConfig.from_env()
    generated_at = _utc_now_iso()
    mantis_commit = _detect_mantis_commit()

    records: list[QualificationRecord] = []
    raw_results: list[EvalResult] = []

    for alias in model_aliases:
        for name in scenario_names:
            scenario = default_scenarios.get(name)
            result = run_scenario_fn(scenario, alias, base_model_config=config)

            records.append(
                _record_from_eval_result(
                    result,
                    suite_id=suite_id,
                    suite_version=suite_version,
                    requested_alias=alias,
                    mantis_version=MANTIS_VERSION,
                    mantis_commit=mantis_commit,
                    litellm_endpoint=config.url,
                    litellm_version=litellm_version,
                    generated_at=generated_at,
                    raw_result_index=len(raw_results),
                )
            )
            raw_results.append(result)

    return QualificationRun(
        suite_id=suite_id,
        suite_version=suite_version,
        generated_at=generated_at,
        mantis_version=MANTIS_VERSION,
        mantis_commit=mantis_commit,
        litellm_endpoint=config.url,
        model_aliases=tuple(model_aliases),
        scenario_names=tuple(scenario_names),
        records=tuple(records),
        raw_results=tuple(raw_results),
    )


# ---------------------------------------------------------------------------
# Deterministic role-eligibility rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleEligibility:
    """One model alias's deterministic eligibility for one role.

    ``eligible`` is governed solely by ``reasons`` being empty — never a
    subjective judgment call. When ``eligible`` is ``False``,
    ``reasons`` lists every disqualifying condition found (not just the
    first), so a report reader sees the full picture in one place.
    """

    role: str
    model_alias: str
    eligible: bool
    reasons: tuple[str, ...]


def _records_for_alias(records: Sequence[QualificationRecord], model_alias: str) -> dict[str, QualificationRecord]:
    return {r.scenario: r for r in records if r.requested_alias == model_alias}


def _evaluate_suite_eligibility(
    role: str,
    model_alias: str,
    required_scenarios: Sequence[str],
    records: Sequence[QualificationRecord],
    *,
    require_incident_triage: bool,
) -> RoleEligibility:
    by_name = _records_for_alias(records, model_alias)
    reasons: list[str] = []

    missing = [s for s in required_scenarios if s not in by_name]
    if missing:
        reasons.append(f"did not complete the full suite: missing {', '.join(missing)}")

    not_ok = sorted(name for name in required_scenarios if name in by_name and by_name[name].outcome != "ok")
    if not_ok:
        reasons.append(f"scenario(s) did not complete successfully (outcome != 'ok'): {', '.join(not_ok)}")

    hard_failing = sorted(
        name for name in required_scenarios if name in by_name and by_name[name].hard_failures
    )
    if hard_failing:
        reasons.append(f"hard failure(s) in required grounding/safety/evidence-discipline checks: {', '.join(hard_failing)}")

    if require_incident_triage:
        incident_triage_names = [s for s in required_scenarios if s.startswith("incident-triage-")]
        failing_incident_triage = sorted(
            name
            for name in incident_triage_names
            if name not in by_name or by_name[name].outcome != "ok" or by_name[name].hard_failures
        )
        if failing_incident_triage:
            reasons.append(f"did not successfully handle Incident Triage scenario(s): {', '.join(failing_incident_triage)}")

    return RoleEligibility(role=role, model_alias=model_alias, eligible=not reasons, reasons=tuple(reasons))


def evaluate_role_eligibility(
    role: str, model_alias: str, records: Sequence[QualificationRecord]
) -> RoleEligibility:
    """Deterministic role-eligibility rules (#13). ``records`` is
    normally an entire :class:`QualificationRun`'s ``.records`` — this
    function filters to ``model_alias`` itself, so passing every
    model's records for a whole run is fine and expected.

    - ``mantis-reasoning``: eligible only if the candidate completes the
      *entire* :data:`QUALIFICATION_SCENARIOS` baseline with zero hard
      failures in any required check, including every
      ``incident-triage-*`` scenario specifically (Incident Triage is
      Mantis's most demanding current multi-source reasoning workload,
      #12) — checked as its own, separately reported rule, not merely
      implied by the "zero hard failures" rule above it.
    - ``mantis-fast``: the same rule, against the smaller, checked-in
      :data:`FAST_QUALIFICATION_SCENARIOS` subset, which still includes
      a trust/injection check, a retrieval/error-discipline check, and a
      stopping-behavior check.
    - ``mantis-coder``: no representative coding/code-review
      qualification suite exists yet (blocked on #94/#91) — this
      function deliberately never returns ``eligible=True`` for it, so a
      caller can't accidentally derive a false coding-competence signal
      from this suite. See the issue's "Role qualification rules"
      section.

    Raises ``ValueError`` for any other role name — there is no
    deterministic rule for it, and guessing one would defeat the point
    of this function.
    """
    if role == ROLE_MANTIS_REASONING:
        return _evaluate_suite_eligibility(
            role, model_alias, QUALIFICATION_SCENARIOS, records, require_incident_triage=True
        )
    if role == ROLE_MANTIS_FAST:
        return _evaluate_suite_eligibility(
            role, model_alias, FAST_QUALIFICATION_SCENARIOS, records, require_incident_triage=False
        )
    if role == ROLE_MANTIS_CODER:
        return RoleEligibility(
            role=role,
            model_alias=model_alias,
            eligible=False,
            reasons=(
                "no representative coding/code-review qualification suite exists yet "
                "(blocked on #94/#91) -- mantis-coder cannot be automatically qualified "
                "by mantis-core-qualification, and must remain experimental or unassigned",
            ),
        )
    raise ValueError(f"no deterministic eligibility rule defined for role {role!r}")


# ---------------------------------------------------------------------------
# Deterministic report formatting
# ---------------------------------------------------------------------------


def format_result_matrix(run: QualificationRun) -> str:
    """A plain-text MODEL / SCENARIO / OUTCOME / SCORE / HARD FAILS table
    for every (model, scenario) pair in ``run`` — deterministic given a
    :class:`QualificationRun`, so this is reproducible from canned
    records with no live model call (see the Deterministic CI AC)."""
    headers = ["MODEL", "SCENARIO", "OUTCOME", "SCORE", "HARD FAILS"]
    by_alias_scenario = {(r.requested_alias, r.scenario): r for r in run.records}
    rows: list[list[str]] = []
    for alias in run.model_aliases:
        for name in run.scenario_names:
            record = by_alias_scenario.get((alias, name))
            if record is None:
                rows.append([alias, name, "MISSING", "—", "—"])
                continue
            outcome_col = "PASS" if record.passed else ("FAIL" if record.passed is not None else record.outcome.upper())
            score_col = f"{record.score}/{record.max_score}" if record.score is not None else "—"
            hard_fails_col = str(len(record.hard_failures))
            rows.append([alias, name, outcome_col, score_col, hard_fails_col])

    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))] if rows else [
        len(h) for h in headers
    ]

    def _fmt(cols: list[str]) -> str:
        return "  ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    lines = [_fmt(headers)] + [_fmt(row) for row in rows]
    return "\n".join(lines)


def _raw_output_path(out_path: str) -> str:
    base, ext = os.path.splitext(out_path)
    return f"{base}.raw{ext or '.jsonl'}"


def write_qualification_artifacts(run: QualificationRun, out_path: str) -> tuple[str, str]:
    """Write ``run`` to disk as two files:

    - ``out_path``: the bounded :class:`QualificationRecord` evidence,
      one JSON object per line — safe to commit (see its docstring).
    - a sibling ``<out_path base>.raw.jsonl``: the full raw
      :class:`~mantis.eval.results.EvalResult` data (complete tool-call
      traces, final-answer text) this run produced — per the issue,
      this does *not* need to be committed if it's large or contains
      unnecessarily verbose model text; keeping it separate from
      ``out_path`` makes that an easy, explicit choice rather than an
      accidental one.

    Each record's ``final_answer_ref`` is resolved to point at its exact
    line in the raw file (``"<raw_path>#L<line>"``) now that the actual
    output path is known — :func:`qualify_models` itself only recorded
    the raw list index, since it doesn't know the eventual output path.

    Returns ``(records_path, raw_path)``.
    """
    raw_path = _raw_output_path(out_path)
    for path in (out_path, raw_path):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    with open(raw_path, "w") as raw_f:
        for result in run.raw_results:
            raw_f.write(json.dumps(result.to_dict(), default=str))
            raw_f.write("\n")

    with open(out_path, "w") as records_f:
        for record in run.records:
            resolved = record
            if record.final_answer_ref is not None and record.final_answer_ref.startswith("raw_result_index="):
                index = int(record.final_answer_ref.split("=", 1)[1])
                resolved = dataclasses.replace(record, final_answer_ref=f"{raw_path}#L{index + 1}")
            records_f.write(json.dumps(resolved.to_dict(), default=str))
            records_f.write("\n")

    return out_path, raw_path


def format_role_eligibility(
    run: QualificationRun, roles: Sequence[str] = (ROLE_MANTIS_REASONING, ROLE_MANTIS_FAST, ROLE_MANTIS_CODER)
) -> str:
    """A deterministic ELIGIBLE/NOT ELIGIBLE breakdown, with reasons, for
    every alias in ``run`` against every role in ``roles``."""
    lines: list[str] = []
    for alias in run.model_aliases:
        lines.append(f"{alias}:")
        for role in roles:
            eligibility = evaluate_role_eligibility(role, alias, run.records)
            status = "ELIGIBLE" if eligibility.eligible else "NOT ELIGIBLE"
            lines.append(f"  {role}: {status}")
            for reason in eligibility.reasons:
                lines.append(f"    - {reason}")
    return "\n".join(lines)
