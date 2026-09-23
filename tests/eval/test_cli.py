"""Tests for mantis.eval.cli: argument parsing, scenario listing, and
result persistence — with run_comparison mocked out (no model/network
dependency).
"""

from __future__ import annotations

import json

import mantis.eval.cli as eval_cli
from mantis.eval.results import EvalResult


def _fake_result(model: str, outcome: str = "ok", evaluation: dict | None = None) -> EvalResult:
    return EvalResult(
        scenario="awx-no-route",
        scenario_version="1.0",
        model=model,
        started_at="2026-09-15T00:00:00+00:00",
        finished_at="2026-09-15T00:00:01+00:00",
        elapsed_seconds=1.0,
        outcome=outcome,
        final_answer="answer" if outcome == "ok" else None,
        error=None if outcome == "ok" else "boom",
        evaluation=evaluation,
    )


def _evaluation(checks: list[dict]) -> dict:
    """Build an evaluation dict from explicit checks, mirroring
    mantis.eval.scoring.Evaluation.to_dict()'s derivation logic."""
    hard_failures = [c["name"] for c in checks if c["hard"] and not c["passed"]]
    return {
        "checks": checks,
        "passed": not hard_failures,
        "score": sum(1 for c in checks if c["passed"]),
        "max_score": len(checks),
        "hard_failures": hard_failures,
    }


def test_run_prints_a_note_when_raw_message_present(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    empty_answer_result = _fake_result("silent-model")
    empty_answer_result.final_answer = ""
    empty_answer_result.raw_message = {"content": "", "reasoning_content": "..."}
    monkeypatch.setattr(
        eval_cli, "run_comparison", lambda scenario, models, base_model_config=None: [
            empty_answer_result
        ]
    )

    eval_cli.main(
        ["run", "--scenario", "awx-no-route", "--models", "silent-model", "--out", str(tmp_path / "r.jsonl")]
    )

    captured = capsys.readouterr()
    assert "NOTE: empty answer" in captured.out


def test_run_writes_jsonl_and_returns_zero_on_success(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        eval_cli, "run_comparison", lambda scenario, models, base_model_config=None: [
            _fake_result(m) for m in models
        ]
    )

    out_path = str(tmp_path / "results.jsonl")
    exit_code = eval_cli.main(
        ["run", "--scenario", "awx-no-route", "--models", "model-a,model-b", "--out", out_path]
    )

    assert exit_code == 0
    lines = open(out_path).read().strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [r["model"] for r in records] == ["model-a", "model-b"]

    captured = capsys.readouterr()
    assert "model-a" in captured.out
    assert "Wrote 2 result(s)" in captured.out


def test_run_returns_nonzero_when_any_model_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        eval_cli, "run_comparison", lambda scenario, models, base_model_config=None: [
            _fake_result("good", "ok"),
            _fake_result("bad", "error"),
        ]
    )

    exit_code = eval_cli.main(
        ["run", "--scenario", "awx-no-route", "--models", "good,bad", "--out", str(tmp_path / "r.jsonl")]
    )

    assert exit_code == 1


def test_run_unknown_scenario_fails_cleanly(capsys):
    exit_code = eval_cli.main(["run", "--scenario", "does-not-exist", "--models", "x"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Unknown scenario" in captured.err


def test_run_default_output_path_is_under_eval_results_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        eval_cli, "run_comparison", lambda scenario, models, base_model_config=None: [
            _fake_result(m) for m in models
        ]
    )

    exit_code = eval_cli.main(["run", "--scenario", "awx-no-route", "--models", "model-a"])

    assert exit_code == 0
    written = list((tmp_path / eval_cli.DEFAULT_RESULTS_DIR).glob("*.jsonl"))
    assert len(written) == 1
    assert written[0].name.startswith("awx-no-route-")


def test_list_scenarios_includes_the_built_in_scenario(capsys):
    exit_code = eval_cli.main(["list-scenarios"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "awx-no-route" in captured.out


def test_list_models_prints_model_ids(monkeypatch, capsys):
    class FakeModel:
        def __init__(self, id):
            self.id = id

    class FakeModelsResponse:
        data = [FakeModel("mantis-fast"), FakeModel("mantis-reasoning")]

    class FakeModelsClient:
        def list(self):
            return FakeModelsResponse()

    class FakeClient:
        models = FakeModelsClient()

    monkeypatch.setattr(eval_cli, "build_openai_client", lambda config: FakeClient())

    exit_code = eval_cli.main(["list-models"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "mantis-fast" in captured.out
    assert "mantis-reasoning" in captured.out


def test_models_flag_is_comma_split(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen_models = {}

    def fake_run_comparison(scenario, models, base_model_config=None):
        seen_models["value"] = models
        return [_fake_result(m) for m in models]

    monkeypatch.setattr(eval_cli, "run_comparison", fake_run_comparison)

    eval_cli.main(
        ["run", "--scenario", "awx-no-route", "--models", " model-a, model-b ,model-c"]
    )

    assert seen_models["value"] == ["model-a", "model-b", "model-c"]


# ---------------------------------------------------------------------------
# Scoring output: PASS/FAIL breakdown + comparison table
# ---------------------------------------------------------------------------


def test_run_prints_pass_fail_breakdown_and_score(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    evaluation = _evaluation(
        [
            {"name": "check 0", "passed": True, "hard": False, "detail": ""},
            {"name": "check 1", "passed": True, "hard": False, "detail": ""},
            {"name": "did not blame the firewall", "passed": False, "hard": True, "detail": ""},
        ]
    )
    scored = _fake_result("model-a", evaluation=evaluation)
    monkeypatch.setattr(
        eval_cli, "run_comparison", lambda scenario, models, base_model_config=None: [scored]
    )

    eval_cli.main(["run", "--scenario", "awx-no-route", "--models", "model-a"])

    captured = capsys.readouterr()
    assert "PASS: check 0" in captured.out
    assert "PASS: check 1" in captured.out
    assert "HARD FAIL: did not blame the firewall" in captured.out
    assert "score: 2/3" in captured.out
    assert "Result: FAIL" in captured.out


def test_run_without_expectations_prints_no_evaluation_section(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    unscored = _fake_result("model-a", evaluation=None)
    monkeypatch.setattr(
        eval_cli, "run_comparison", lambda scenario, models, base_model_config=None: [unscored]
    )

    eval_cli.main(["run", "--scenario", "awx-no-route", "--models", "model-a"])

    captured = capsys.readouterr()
    assert "Result:" not in captured.out
    assert "PASS:" not in captured.out
    assert "FAIL:" not in captured.out


def test_run_prints_comparison_table_when_scored(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    all_pass = _evaluation([{"name": f"c{i}", "passed": True, "hard": True, "detail": ""} for i in range(3)])
    one_hard_fail = _evaluation(
        [
            {"name": "c0", "passed": True, "hard": False, "detail": ""},
            {"name": "c1", "passed": True, "hard": False, "detail": ""},
            {"name": "c2", "passed": False, "hard": True, "detail": ""},
        ]
    )
    results = [
        _fake_result("qwen3:30b", evaluation=all_pass),
        _fake_result("gpt-oss:20b", evaluation=one_hard_fail),
    ]
    monkeypatch.setattr(
        eval_cli, "run_comparison", lambda scenario, models, base_model_config=None: results
    )

    eval_cli.main(["run", "--scenario", "awx-no-route", "--models", "qwen3:30b,gpt-oss:20b"])

    captured = capsys.readouterr()
    assert "MODEL" in captured.out and "RESULT" in captured.out and "TOKENS" in captured.out
    assert "qwen3:30b" in captured.out
    assert "3/3" in captured.out
    assert "gpt-oss:20b" in captured.out
    assert "2/3" in captured.out


def test_run_omits_comparison_table_when_nothing_scored(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        eval_cli, "run_comparison", lambda scenario, models, base_model_config=None: [
            _fake_result("model-a", evaluation=None)
        ]
    )

    eval_cli.main(["run", "--scenario", "awx-no-route", "--models", "model-a"])

    captured = capsys.readouterr()
    assert "MODEL" not in captured.out


# ---------------------------------------------------------------------------
# Metrics ownership: `mantis eval` owns its own opt-in metrics-server
# gate directly (default off) rather than relying on mantis.cli's
# generic entry point, which has no metrics code path at all -- see
# tests/test_cli.py and tests/test_api_server.py for the other two
# owners (HTTP-client commands: never; `mantis serve`: on by default).
# ---------------------------------------------------------------------------


def test_main_does_not_start_metrics_server_by_default(monkeypatch):
    monkeypatch.delenv("MANTIS_METRICS_ENABLED", raising=False)
    calls: list[None] = []
    monkeypatch.setattr(eval_cli, "start_metrics_server", lambda *a, **kw: calls.append(None))

    eval_cli.main(["list-scenarios"])

    assert calls == []


def test_main_starts_metrics_server_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("MANTIS_METRICS_ENABLED", "true")
    calls: list[None] = []
    monkeypatch.setattr(eval_cli, "start_metrics_server", lambda *a, **kw: calls.append(None))

    eval_cli.main(["list-scenarios"])

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# `qualify` exit status: a scored hard-failure must exit nonzero even
# though nothing crashed (outcome == "ok").
# ---------------------------------------------------------------------------


def _fake_qualification_run(*, passed: bool | None):
    from mantis.eval.qualification import QualificationRecord, QualificationRun

    record = QualificationRecord(
        suite_id="mantis-fast-qualification-v1",
        suite_version="v1",
        requested_alias="model-a",
        final_alias="model-a",
        failed_route_attempts=(),
        resolved_backend_model=None,
        scenario="s1",
        scenario_version="1.0",
        outcome="ok",
        passed=passed,
        score=2,
        max_score=3,
        hard_failures=() if passed is not False else ("bad_check",),
        iterations=1,
        tool_call_count=1,
        duplicate_call_count=0,
        malformed_call_count=0,
        elapsed_seconds=1.0,
        total_tokens=10,
        cost_usd=None,
        final_answer_ref=None,
        error=None,
        mantis_version="1.9.0",
        mantis_commit=None,
        litellm_endpoint="http://litellm.example.test",
        litellm_version=None,
        generated_at="2026-09-22T00:00:00+00:00",
    )
    return QualificationRun(
        suite_id="mantis-fast-qualification-v1",
        suite_version="v1",
        generated_at="2026-09-22T00:00:00+00:00",
        mantis_version="1.9.0",
        mantis_commit=None,
        litellm_endpoint="http://litellm.example.test",
        model_aliases=("model-a",),
        scenario_names=("s1",),
        records=(record,),
        raw_results=(),
    )


def test_qualify_exits_nonzero_when_a_record_fails_scoring_despite_outcome_ok(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "k")
    monkeypatch.setattr(eval_cli, "qualify_models", lambda *a, **kw: _fake_qualification_run(passed=False))

    exit_code = eval_cli.main(
        ["qualify", "--models", "model-a,model-b", "--out", str(tmp_path / "q.jsonl")]
    )

    assert exit_code == 1


def test_qualify_exits_zero_when_every_record_passes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_URL", "http://litellm.example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "k")
    monkeypatch.setattr(eval_cli, "qualify_models", lambda *a, **kw: _fake_qualification_run(passed=True))

    exit_code = eval_cli.main(
        ["qualify", "--models", "model-a,model-b", "--out", str(tmp_path / "q.jsonl")]
    )

    assert exit_code == 0
