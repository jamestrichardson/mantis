"""Tests for mantis.eval.scoring: evaluate_result() end to end, and the
hard-failure-gates-pass/fail design (the core reason this module exists —
see docstring in mantis.eval.scoring).
"""

from __future__ import annotations

import json

from mantis.eval.expectations import ForbiddenAnswerPattern, RequiredAnswerPattern
from mantis.eval.results import EvalResult
from mantis.eval.scoring import CheckResult, Evaluation, evaluate_result


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


def test_evaluate_result_evaluates_every_expectation_in_order():
    expectations = [
        RequiredAnswerPattern("host03", name="cited_host03"),
        ForbiddenAnswerPattern("firewall caused", name="no_firewall_blame"),
    ]
    result = _result("host03 was unreachable")

    evaluation = evaluate_result(expectations, result)

    assert [c.name for c in evaluation.checks] == ["cited_host03", "no_firewall_blame"]
    assert [c.passed for c in evaluation.checks] == [True, True]
    assert evaluation.score == 2
    assert evaluation.max_score == 2


def test_evaluate_result_mixed_pass_and_fail():
    expectations = [
        RequiredAnswerPattern("host03"),
        ForbiddenAnswerPattern("firewall caused"),
    ]
    result = _result("the firewall caused it, no mention of the host")

    evaluation = evaluate_result(expectations, result)

    assert evaluation.score == 0
    assert evaluation.max_score == 2


def test_evaluate_result_empty_expectations_list():
    evaluation = evaluate_result([], _result("anything"))

    assert evaluation.checks == []
    assert evaluation.score == 0
    assert evaluation.max_score == 0
    assert evaluation.passed is True  # no hard failures possible with no checks


def test_evaluate_result_disambiguates_duplicate_default_names():
    expectations = [
        RequiredAnswerPattern("host03"),
        RequiredAnswerPattern("host03"),
        RequiredAnswerPattern("host03"),
    ]
    evaluation = evaluate_result(expectations, _result("host03 mentioned"))

    names = [c.name for c in evaluation.checks]
    assert names == ["required_answer_pattern:host03", "required_answer_pattern:host03#2", "required_answer_pattern:host03#3"]


# ---------------------------------------------------------------------------
# The central design: hard failures gate pass/fail independent of score
# ---------------------------------------------------------------------------


def test_high_score_with_one_hard_failure_still_fails():
    # This is the exact scenario the design exists to prevent: 9/10
    # passing checks must not "average out" to a pass if the one failure
    # is a hard requirement (e.g. an invented root cause).
    expectations = [RequiredAnswerPattern(f"quality check {i}", hard=False) for i in range(9)]
    expectations.append(ForbiddenAnswerPattern("firewall caused", hard=True))
    # Satisfy all 9 quality checks, then violate the one hard one.
    text = " ".join(f"quality check {i}" for i in range(9)) + " the firewall caused it"

    evaluation = evaluate_result(expectations, _result(text))

    assert evaluation.score == 9
    assert evaluation.max_score == 10
    assert evaluation.passed is False
    assert len(evaluation.hard_failures) == 1


def test_low_score_with_zero_hard_failures_still_passes():
    # The inverse: missing several quality checks (but no hard ones) must
    # not be treated as an overall failure.
    expectations = [
        RequiredAnswerPattern("host03", hard=True),  # satisfied
        RequiredAnswerPattern("nonexistent phrase one", hard=False),  # missed
        RequiredAnswerPattern("nonexistent phrase two", hard=False),  # missed
    ]

    evaluation = evaluate_result(expectations, _result("host03 was involved"))

    assert evaluation.score == 1
    assert evaluation.max_score == 3
    assert evaluation.passed is True
    assert evaluation.hard_failures == []


def test_perfect_score_passes():
    expectations = [RequiredAnswerPattern("host03", hard=True)]
    evaluation = evaluate_result(expectations, _result("host03 mentioned"))

    assert evaluation.passed is True
    assert evaluation.score == evaluation.max_score


def test_hard_failures_lists_only_failed_hard_checks():
    expectations = [
        RequiredAnswerPattern("present", name="quality_pass", hard=False),
        RequiredAnswerPattern("missing-quality", name="quality_fail", hard=False),
        RequiredAnswerPattern("present", name="hard_pass", hard=True),
        RequiredAnswerPattern("missing-hard", name="hard_fail", hard=True),
    ]
    evaluation = evaluate_result(expectations, _result("present"))

    assert evaluation.hard_failures == ["hard_fail"]


# ---------------------------------------------------------------------------
# Evaluation / CheckResult serialization
# ---------------------------------------------------------------------------


def test_evaluation_to_dict_matches_the_documented_shape():
    evaluation = Evaluation(
        checks=[
            CheckResult(name="a", passed=True, hard=True, detail="ok"),
            CheckResult(name="b", passed=False, hard=False, detail="nope"),
        ]
    )

    payload = evaluation.to_dict()

    json.dumps(payload)  # must not raise
    assert payload == {
        "passed": True,  # the failed check is not hard
        "score": 1,
        "max_score": 2,
        "checks": [
            {"name": "a", "passed": True, "hard": True, "detail": "ok"},
            {"name": "b", "passed": False, "hard": False, "detail": "nope"},
        ],
        "hard_failures": [],
    }


def test_evaluation_to_dict_reflects_a_failing_run():
    evaluation = Evaluation(
        checks=[CheckResult(name="required_tool_call", passed=False, hard=True, detail="never called")]
    )

    payload = evaluation.to_dict()

    assert payload["passed"] is False
    assert payload["hard_failures"] == ["required_tool_call"]
