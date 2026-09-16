"""Tests for mantis.eval.scoring: score_result() end to end."""

from __future__ import annotations

import json

from mantis.eval.expectations import ForbiddenClaim, RequiredEvidence
from mantis.eval.results import EvalResult
from mantis.eval.scoring import CheckResult, ScoreReport, score_result


def _result(final_answer: str) -> EvalResult:
    return EvalResult(
        scenario="s",
        scenario_version="1.0",
        model="m",
        started_at="t0",
        finished_at="t1",
        elapsed_seconds=1.0,
        outcome="ok",
        final_answer=final_answer,
    )


def test_score_result_evaluates_every_expectation_in_order():
    expectations = [
        RequiredEvidence("host03", label="cited host03"),
        ForbiddenClaim("firewall caused", label="did not blame the firewall"),
    ]
    result = _result("host03 was unreachable")

    report = score_result(expectations, result)

    assert [c.label for c in report.checks] == ["cited host03", "did not blame the firewall"]
    assert [c.passed for c in report.checks] == [True, True]
    assert report.passed_count == 2
    assert report.total_count == 2


def test_score_result_mixed_pass_and_fail():
    expectations = [
        RequiredEvidence("host03"),
        ForbiddenClaim("firewall caused"),
    ]
    result = _result("the firewall caused it, no mention of the host")

    report = score_result(expectations, result)

    assert report.passed_count == 0
    assert report.total_count == 2


def test_score_result_empty_expectations_list():
    report = score_result([], _result("anything"))

    assert report.checks == []
    assert report.passed_count == 0
    assert report.total_count == 0


def test_score_report_to_dict_is_json_serializable():
    report = ScoreReport(
        checks=[
            CheckResult(label="a", passed=True, detail="ok"),
            CheckResult(label="b", passed=False, detail="nope"),
        ]
    )

    payload = report.to_dict()

    json.dumps(payload)  # must not raise
    assert payload == {
        "checks": [
            {"label": "a", "passed": True, "detail": "ok"},
            {"label": "b", "passed": False, "detail": "nope"},
        ],
        "passed": 1,
        "total": 2,
    }
