"""Tests for mantis.eval.cli: argument parsing, scenario listing, and
result persistence — with run_comparison mocked out (no model/network
dependency).
"""

from __future__ import annotations

import json

import mantis.eval.cli as eval_cli
from mantis.eval.results import EvalResult


def _fake_result(model: str, outcome: str = "ok") -> EvalResult:
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
    )


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
