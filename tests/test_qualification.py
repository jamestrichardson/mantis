"""Tests for mantis.eval.qualification (#13): the model-qualification
baseline suite, multi-model/multi-scenario orchestration, deterministic
role-eligibility rules, and report formatting.

No live LiteLLM/model call anywhere in this file — orchestration tests
use a fake ``run_scenario_fn`` (mirroring tests/eval/test_runner.py's own
fake-OpenAI-client convention one layer down), and formatting/eligibility
tests work purely from hand-built ``QualificationRecord``/
``QualificationRun`` data.
"""

from __future__ import annotations

import json

import pytest

import mantis.eval  # noqa: F401  (registers the real scenarios this suite names)
from mantis.config import LiteLLMConfig, Secret
from mantis.eval.qualification import (
    FAST_QUALIFICATION_SCENARIOS,
    FAST_QUALIFICATION_SUITE_ID,
    QUALIFICATION_SCENARIOS,
    QUALIFICATION_SUITE_ID,
    QUALIFICATION_SUITE_VERSION,
    ROLE_MANTIS_CODER,
    ROLE_MANTIS_FAST,
    ROLE_MANTIS_REASONING,
    QualificationRecord,
    QualificationRun,
    evaluate_role_eligibility,
    format_result_matrix,
    format_role_eligibility,
    qualify_models,
    write_qualification_artifacts,
)
from mantis.eval.results import EvalResult
from mantis.eval.scenarios import Scenario, default_scenarios


def _config() -> LiteLLMConfig:
    return LiteLLMConfig(url="http://litellm.example.test", api_key=Secret("k"), model="unused")


# ---------------------------------------------------------------------------
# Suite registration / versioning / stable membership+order
# ---------------------------------------------------------------------------


def test_suite_id_matches_the_documented_name_and_version():
    assert QUALIFICATION_SUITE_ID == "mantis-core-qualification-v1"
    assert FAST_QUALIFICATION_SUITE_ID == "mantis-fast-qualification-v1"


def test_baseline_suite_membership_and_order_is_stable():
    # A checked-in regression lock: changing this tuple's membership OR
    # order must be a deliberate act that also updates
    # QUALIFICATION_SUITE_VERSION and this test together -- never a
    # silent drift.
    assert QUALIFICATION_SCENARIOS == (
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
    assert QUALIFICATION_SUITE_VERSION == "v1"


def test_fast_suite_is_a_stable_checked_in_subset():
    assert FAST_QUALIFICATION_SCENARIOS == (
        "awx-structured-unreachable",
        "awx-prompt-injection",
        "incident-triage-source-unavailable",
        "awx-duplicate-call-temptation",
    )
    assert set(FAST_QUALIFICATION_SCENARIOS) <= set(QUALIFICATION_SCENARIOS)


def test_every_baseline_scenario_name_resolves_in_the_real_registry():
    # The whole point of a checked-in baseline is that it names *real*
    # scenarios -- a name here that doesn't resolve is exactly the kind
    # of drift this test exists to catch immediately, not at qualify
    # time.
    for name in QUALIFICATION_SCENARIOS:
        default_scenarios.get(name)  # raises ScenarioNotFoundError if missing


def test_incident_triage_is_well_represented_in_the_baseline():
    # #12's requirement: Incident Triage represents Mantis's most
    # demanding current multi-source reasoning workload in this suite.
    incident_triage_names = [n for n in QUALIFICATION_SCENARIOS if n.startswith("incident-triage-")]
    assert len(incident_triage_names) >= 4


# ---------------------------------------------------------------------------
# Orchestration: qualify_models against a fake run_scenario_fn
# ---------------------------------------------------------------------------


def _fake_scenario(name: str) -> Scenario:
    return Scenario(
        name=name,
        version="1.0",
        description="fake",
        prompt="p",
        system_prompt="s",
        agent_tools=[],
        build_registry=lambda: None,
    )


def _canned_ok_result(scenario: Scenario, model_alias: str, *, passed: bool = True, backend_model: str | None = None) -> EvalResult:
    return EvalResult(
        scenario=scenario.name,
        scenario_version=scenario.version,
        model=model_alias,
        started_at="t0",
        finished_at="t1",
        elapsed_seconds=1.5,
        outcome="ok",
        final_answer="the answer",
        tool_calls=[],
        iterations=2,
        duplicate_call_count=0,
        malformed_call_count=0,
        total_tokens=42,
        backend_model=backend_model,
        evaluation={
            "passed": passed,
            "score": 3 if passed else 2,
            "max_score": 3,
            "checks": [],
            "hard_failures": [] if passed else ["some_hard_check"],
        },
    )


def test_qualify_models_aggregates_with_real_registered_scenarios(monkeypatch):
    for name in ("q-scenario-1", "q-scenario-2"):
        monkeypatch.setitem(default_scenarios._scenarios, name, _fake_scenario(name))

    calls: list[tuple[str, str]] = []

    def fake_run_scenario(scenario, model_alias, *, base_model_config=None):
        calls.append((model_alias, scenario.name))
        return _canned_ok_result(scenario, model_alias, backend_model=f"resolved/{model_alias}")

    run = qualify_models(
        ["model-a", "model-b"],
        scenario_names=("q-scenario-1", "q-scenario-2"),
        base_model_config=_config(),
        run_scenario_fn=fake_run_scenario,
    )

    assert calls == [
        ("model-a", "q-scenario-1"),
        ("model-a", "q-scenario-2"),
        ("model-b", "q-scenario-1"),
        ("model-b", "q-scenario-2"),
    ]
    assert len(run.records) == 4
    assert len(run.raw_results) == 4
    assert all(r.outcome == "ok" for r in run.records)
    assert {r.resolved_backend_model for r in run.records} == {"resolved/model-a", "resolved/model-b"}


def _canned_error_result(scenario: Scenario, model_alias: str, *, error_summary: str = "APIConnectionError") -> EvalResult:
    """A *legitimate* model/backend failure result, exactly the shape
    the real ``run_scenario`` returns for one (never raises) -- see
    ``mantis.eval.runner.run_scenario``'s own isolation of
    ``openai.OpenAIError``/``mantis.runtime.RuntimeError_``."""
    return EvalResult(
        scenario=scenario.name,
        scenario_version=scenario.version,
        model=model_alias,
        started_at="t0",
        finished_at="t1",
        elapsed_seconds=1.0,
        outcome="error",
        final_answer=None,
        tool_calls=[],
        iterations=1,
        error="APIConnectionError: connection refused (full upstream body omitted here)",
        error_summary=error_summary,
    )


def test_qualify_models_one_models_legitimate_backend_failure_does_not_abort_the_others(monkeypatch):
    # run_scenario itself never raises for a known model/backend failure
    # -- it returns a normal EvalResult with outcome="error". This test
    # proves qualify_models' plain aggregation loop naturally continues
    # past that, with no special-case handling needed at this layer.
    for name in ("q-scenario-1", "q-scenario-2"):
        monkeypatch.setitem(default_scenarios._scenarios, name, _fake_scenario(name))

    def fake_run_scenario(scenario, model_alias, *, base_model_config=None):
        if model_alias == "flaky-model" and scenario.name == "q-scenario-1":
            return _canned_error_result(scenario, model_alias)
        return _canned_ok_result(scenario, model_alias)

    run = qualify_models(
        ["good-model", "flaky-model"],
        scenario_names=("q-scenario-1", "q-scenario-2"),
        base_model_config=_config(),
        run_scenario_fn=fake_run_scenario,
    )

    assert len(run.records) == 4  # all four pairs still produced a record
    by_pair = {(r.requested_alias, r.scenario): r for r in run.records}
    assert by_pair[("flaky-model", "q-scenario-1")].outcome == "error"
    assert by_pair[("flaky-model", "q-scenario-1")].error == "APIConnectionError"  # bounded/safe, not the raw text
    # The other three pairs, including flaky-model's second scenario,
    # were unaffected.
    assert by_pair[("flaky-model", "q-scenario-2")].outcome == "ok"
    assert by_pair[("good-model", "q-scenario-1")].outcome == "ok"
    assert by_pair[("good-model", "q-scenario-2")].outcome == "ok"


def test_qualify_models_an_unexpected_mantis_bug_propagates_and_aborts():
    # Deliberately the opposite of the test above: qualify_models must
    # NOT widen run_scenario's isolation boundary. A genuine programming
    # error (never returned as an EvalResult by the real run_scenario)
    # must invalidate the whole qualification run rather than being
    # silently recorded as if one model had "failed" -- see MAJOR review
    # finding: "one backend/model failure must not abort the rest" is
    # not the same claim as "swallow every programming error."

    def buggy_run_scenario(scenario, model_alias, *, base_model_config=None):
        raise KeyError("some unrelated Mantis bug, e.g. a bad fixture")

    with pytest.raises(KeyError):
        qualify_models(
            ["model-a", "model-b"],
            scenario_names=QUALIFICATION_SCENARIOS[:1],
            base_model_config=_config(),
            run_scenario_fn=buggy_run_scenario,
        )


def test_qualify_models_baseline_drift_still_raises(monkeypatch):
    # A name in the checked-in list that doesn't exist in the registry
    # is a Mantis bug (baseline/registry drift), not model evidence, and
    # must propagate loudly rather than being silently skipped or
    # misattributed to a model.
    from mantis.eval.scenarios import ScenarioNotFoundError

    with pytest.raises(ScenarioNotFoundError):
        qualify_models(
            ["model-a"],
            scenario_names=("does-not-exist",),
            base_model_config=_config(),
            run_scenario_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should never be called")),
        )


# ---------------------------------------------------------------------------
# Unavailable data stays unavailable -- never fabricated as zero
# ---------------------------------------------------------------------------


def test_unavailable_backend_model_and_cost_and_tokens_remain_none(monkeypatch):
    monkeypatch.setitem(default_scenarios._scenarios, "q-scenario-1", _fake_scenario("q-scenario-1"))

    def fake_run_scenario(scenario, model_alias, *, base_model_config=None):
        return EvalResult(
            scenario=scenario.name,
            scenario_version=scenario.version,
            model=model_alias,
            started_at="t0",
            finished_at="t1",
            elapsed_seconds=1.0,
            outcome="ok",
            final_answer="answer",
            tool_calls=[],
            iterations=1,
            total_tokens=None,       # backend never reported usage
            backend_model=None,      # backend never reported response.model
        )

    run = qualify_models(
        ["model-a"], scenario_names=("q-scenario-1",), base_model_config=_config(), run_scenario_fn=fake_run_scenario
    )

    record = run.records[0]
    assert record.total_tokens is None
    assert record.resolved_backend_model is None
    assert record.cost_usd is None  # never reported as 0.0
    assert record.litellm_version is None  # never guessed


def test_record_error_is_never_the_raw_provider_error_body(monkeypatch):
    # A real example encountered during a live qualification run: an
    # nginx 504 Gateway Time-out HTML page as an openai.OpenAIError's
    # own message. QualificationRecord.error must never carry that --
    # only EvalResult.error_summary (class name + status code), never
    # EvalResult.error (which run_scenario deliberately keeps full-detail
    # for the raw/uncommitted file).
    monkeypatch.setitem(default_scenarios._scenarios, "q-scenario-1", _fake_scenario("q-scenario-1"))
    html_body = "<html><head><title>504 Gateway Time-out</title></head><body>nginx</body></html>"

    def fake_run_scenario(scenario, model_alias, *, base_model_config=None):
        return EvalResult(
            scenario=scenario.name,
            scenario_version=scenario.version,
            model=model_alias,
            started_at="t0",
            finished_at="t1",
            elapsed_seconds=1.0,
            outcome="error",
            final_answer=None,
            tool_calls=[],
            iterations=1,
            error=f"InternalServerError: {html_body}",
            error_summary="InternalServerError (status=504)",
        )

    run = qualify_models(
        ["model-a"], scenario_names=("q-scenario-1",), base_model_config=_config(), run_scenario_fn=fake_run_scenario
    )

    record = run.records[0]
    assert record.error == "InternalServerError (status=504)"
    assert html_body not in record.error
    assert "nginx" not in record.error
    # Confirms this test would actually catch the regression: the raw
    # detail really is on the raw EvalResult, just never on the record.
    assert html_body in run.raw_results[0].error


# ---------------------------------------------------------------------------
# Deterministic role-eligibility rules
# ---------------------------------------------------------------------------


def _record(
    scenario: str,
    model_alias: str,
    *,
    outcome: str = "ok",
    hard_failures: tuple[str, ...] = (),
) -> QualificationRecord:
    return QualificationRecord(
        suite_id=QUALIFICATION_SUITE_ID,
        suite_version=QUALIFICATION_SUITE_VERSION,
        requested_alias=model_alias,
        resolved_backend_model=None,
        scenario=scenario,
        scenario_version="1.0",
        outcome=outcome,
        passed=(outcome == "ok" and not hard_failures),
        score=3,
        max_score=3,
        hard_failures=hard_failures,
        iterations=2,
        tool_call_count=1,
        duplicate_call_count=0,
        malformed_call_count=0,
        elapsed_seconds=1.0,
        total_tokens=100,
        cost_usd=None,
        final_answer_ref=None,
        error=None,
        mantis_version="1.8.0",
        mantis_commit=None,
        litellm_endpoint="http://litellm.example.test",
        litellm_version=None,
        generated_at="2026-09-22T00:00:00+00:00",
    )


def _all_passing_records(model_alias: str, scenario_names) -> list[QualificationRecord]:
    return [_record(name, model_alias) for name in scenario_names]


def test_mantis_reasoning_eligible_when_the_full_baseline_passes():
    records = _all_passing_records("good-model", QUALIFICATION_SCENARIOS)
    eligibility = evaluate_role_eligibility(ROLE_MANTIS_REASONING, "good-model", records)
    assert eligibility.eligible is True
    assert eligibility.reasons == ()


def test_mantis_reasoning_not_eligible_with_a_missing_scenario():
    records = _all_passing_records("model-a", QUALIFICATION_SCENARIOS[:-1])  # missing one
    eligibility = evaluate_role_eligibility(ROLE_MANTIS_REASONING, "model-a", records)
    assert eligibility.eligible is False
    assert any("missing" in r for r in eligibility.reasons)


def test_mantis_reasoning_not_eligible_with_a_hard_failure():
    records = _all_passing_records("model-a", QUALIFICATION_SCENARIOS)
    # Corrupt one record with a hard failure.
    records = [
        _record(r.scenario, "model-a", hard_failures=("bad_check",)) if r.scenario == QUALIFICATION_SCENARIOS[0] else r
        for r in records
    ]
    eligibility = evaluate_role_eligibility(ROLE_MANTIS_REASONING, "model-a", records)
    assert eligibility.eligible is False
    assert any("hard failure" in r for r in eligibility.reasons)


def test_mantis_reasoning_not_eligible_when_incident_triage_scenario_errors():
    incident_triage_name = next(n for n in QUALIFICATION_SCENARIOS if n.startswith("incident-triage-"))
    records = _all_passing_records("model-a", QUALIFICATION_SCENARIOS)
    records = [
        _record(r.scenario, "model-a", outcome="error") if r.scenario == incident_triage_name else r
        for r in records
    ]
    eligibility = evaluate_role_eligibility(ROLE_MANTIS_REASONING, "model-a", records)
    assert eligibility.eligible is False
    assert any("Incident Triage" in r for r in eligibility.reasons)


def test_mantis_fast_eligible_from_only_the_fast_subset():
    # A model only run against the fast subset (not the full baseline)
    # can still be eligible for mantis-fast.
    records = _all_passing_records("small-model", FAST_QUALIFICATION_SCENARIOS)
    eligibility = evaluate_role_eligibility(ROLE_MANTIS_FAST, "small-model", records)
    assert eligibility.eligible is True


def test_mantis_fast_not_eligible_with_a_hard_failure_in_the_subset():
    records = _all_passing_records("small-model", FAST_QUALIFICATION_SCENARIOS)
    records = [
        _record(r.scenario, "small-model", hard_failures=("injection_not_obeyed",))
        if r.scenario == "awx-prompt-injection"
        else r
        for r in records
    ]
    eligibility = evaluate_role_eligibility(ROLE_MANTIS_FAST, "small-model", records)
    assert eligibility.eligible is False


def test_mantis_coder_is_never_automatically_eligible():
    # No representative coding suite exists (#94/#91) -- this must never
    # return eligible=True regardless of what other evidence is passed.
    records = _all_passing_records("model-a", QUALIFICATION_SCENARIOS)
    eligibility = evaluate_role_eligibility(ROLE_MANTIS_CODER, "model-a", records)
    assert eligibility.eligible is False
    assert eligibility.reasons


def test_unknown_role_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        evaluate_role_eligibility("mantis-made-up-role", "model-a", [])


# ---------------------------------------------------------------------------
# Deterministic report formatting -- reproducible from canned data
# ---------------------------------------------------------------------------


def _canned_run() -> QualificationRun:
    records = tuple(_all_passing_records("model-a", ("s1", "s2")))
    return QualificationRun(
        suite_id=QUALIFICATION_SUITE_ID,
        suite_version=QUALIFICATION_SUITE_VERSION,
        generated_at="2026-09-22T00:00:00+00:00",
        mantis_version="1.8.0",
        mantis_commit="abc123",
        litellm_endpoint="http://litellm.example.test",
        model_aliases=("model-a",),
        scenario_names=("s1", "s2"),
        records=records,
        raw_results=(),
    )


def test_format_result_matrix_is_deterministic():
    run = _canned_run()
    first = format_result_matrix(run)
    second = format_result_matrix(run)
    assert first == second
    assert "model-a" in first
    assert "s1" in first and "s2" in first
    assert "PASS" in first


def test_format_result_matrix_reports_missing_pairs():
    run = QualificationRun(
        suite_id=QUALIFICATION_SUITE_ID,
        suite_version=QUALIFICATION_SUITE_VERSION,
        generated_at="t",
        mantis_version="1.8.0",
        mantis_commit=None,
        litellm_endpoint="http://x",
        model_aliases=("model-a",),
        scenario_names=("s1", "s2"),
        records=(_record("s1", "model-a"),),  # s2 never ran
        raw_results=(),
    )
    matrix = format_result_matrix(run)
    assert "MISSING" in matrix


def test_format_role_eligibility_is_deterministic_and_reflects_records():
    run = _canned_run()
    first = format_role_eligibility(run, roles=(ROLE_MANTIS_CODER,))
    second = format_role_eligibility(run, roles=(ROLE_MANTIS_CODER,))
    assert first == second
    assert "NOT ELIGIBLE" in first
    assert "mantis-coder" in first


# ---------------------------------------------------------------------------
# Bounded, safe on-disk artifacts
# ---------------------------------------------------------------------------


def test_write_qualification_artifacts_writes_bounded_records_and_a_raw_sibling(tmp_path, monkeypatch):
    monkeypatch.setitem(default_scenarios._scenarios, "q-scenario-1", _fake_scenario("q-scenario-1"))

    def fake_run_scenario(scenario, model_alias, *, base_model_config=None):
        return _canned_ok_result(scenario, model_alias, backend_model="resolved/model-a")

    run = qualify_models(
        ["model-a"], scenario_names=("q-scenario-1",), base_model_config=_config(), run_scenario_fn=fake_run_scenario
    )

    out_path = str(tmp_path / "qualification.jsonl")
    records_path, raw_path = write_qualification_artifacts(run, out_path)

    assert records_path == out_path
    assert raw_path.endswith(".raw.jsonl")

    with open(records_path) as f:
        lines = [json.loads(line) for line in f]
    assert len(lines) == 1
    assert lines[0]["requested_alias"] == "model-a"
    assert lines[0]["resolved_backend_model"] == "resolved/model-a"
    # Bounded: no full tool-call trace or verbose final-answer text in
    # the records file -- only a pointer into the raw sibling file.
    assert "tool_calls" not in lines[0]
    assert "final_answer" not in lines[0]
    assert lines[0]["final_answer_ref"] == f"{raw_path}#L1"

    with open(raw_path) as f:
        raw_lines = [json.loads(line) for line in f]
    assert len(raw_lines) == 1
    assert raw_lines[0]["final_answer"] == "the answer"


# ---------------------------------------------------------------------------
# CLI argument parsing -- no live LiteLLM/model call required
# ---------------------------------------------------------------------------


def test_cli_qualify_requires_at_least_two_models(capsys):
    from mantis.eval.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["qualify", "--models", "only-one-model"])
    exit_code = args.func(args)

    assert exit_code == 1
    assert "at least two" in capsys.readouterr().err


def test_cli_qualify_parses_models_and_out():
    from mantis.eval.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["qualify", "--models", "alias-a,alias-b", "--out", "/tmp/q.jsonl"])

    assert args.command == "qualify"
    assert args.models == "alias-a,alias-b"
    assert args.out == "/tmp/q.jsonl"
    assert args.suite == "core"  # default


def test_cli_qualify_suite_flag_selects_the_fast_subset(tmp_path, monkeypatch):
    import mantis.eval.cli as cli_module

    captured: dict = {}

    def fake_qualify_models(model_aliases, *, scenario_names, suite_id, suite_version, base_model_config):
        captured["scenario_names"] = scenario_names
        captured["suite_id"] = suite_id
        captured["suite_version"] = suite_version
        return _canned_run()

    monkeypatch.setattr(cli_module, "qualify_models", fake_qualify_models)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "k")

    parser = cli_module.build_parser()
    # Explicit --out into tmp_path -- without it, _cmd_qualify falls back
    # to writing into the real eval-results/ directory relative to the
    # process's actual cwd, which would leak a stray file into the repo
    # on every test run.
    out_path = str(tmp_path / "q.jsonl")
    args = parser.parse_args(["qualify", "--models", "a,b", "--suite", "fast", "--out", out_path])
    exit_code = args.func(args)

    assert exit_code == 0
    assert captured["scenario_names"] == FAST_QUALIFICATION_SCENARIOS
    assert captured["suite_id"] == FAST_QUALIFICATION_SUITE_ID


def test_cli_qualify_reports_configuration_error_without_network(monkeypatch, capsys):
    # No LITELLM_URL/LITELLM_API_KEY set -- must fail fast on
    # configuration, never attempt any network I/O.
    import socket

    from mantis.eval.cli import build_parser

    monkeypatch.delenv("LITELLM_URL", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)

    def _forbidden(*args, **kwargs):
        raise AssertionError("must never touch the network before configuration is validated")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)

    parser = build_parser()
    args = parser.parse_args(["qualify", "--models", "alias-a,alias-b"])
    exit_code = args.func(args)

    assert exit_code == 1
    assert "Configuration error" in capsys.readouterr().err


def test_cli_qualify_end_to_end_with_a_faked_qualify_models(tmp_path, monkeypatch, capsys):
    # Exercises the full CLI command handler deterministically by
    # faking mantis.eval.cli's own qualify_models reference -- no live
    # model/network call anywhere in this test.
    import mantis.eval.cli as cli_module

    fake_run = _canned_run()
    monkeypatch.setattr(cli_module, "qualify_models", lambda *a, **kw: fake_run)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "k")

    parser = cli_module.build_parser()
    out_path = str(tmp_path / "q.jsonl")
    args = parser.parse_args(["qualify", "--models", "model-a,model-b", "--out", out_path])
    exit_code = args.func(args)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert QUALIFICATION_SUITE_ID in captured.out
    assert "model-a" in captured.out
    with open(out_path) as f:
        assert len(f.readlines()) == len(fake_run.records)
