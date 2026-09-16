"""Tests for mantis.eval.expectations: each deterministic check type,
pass and fail cases.
"""

from __future__ import annotations

from mantis.eval.expectations import (
    ForbiddenClaim,
    MaxToolCalls,
    MustProduceFinalAnswer,
    RequiredEvidence,
    RequiredToolCall,
)
from mantis.eval.results import EvalResult, ToolCallSummary


def _tool_call(tool_name: str, outcome: str = "ok") -> ToolCallSummary:
    return ToolCallSummary(
        iteration=1, tool_name=tool_name, arguments={}, outcome=outcome, detail="", result=None
    )


def _result(**overrides) -> EvalResult:
    defaults = dict(
        scenario="s",
        scenario_version="1.0",
        model="m",
        started_at="t0",
        finished_at="t1",
        elapsed_seconds=1.0,
        outcome="ok",
        final_answer="the answer mentions host03 and a network reachability problem",
    )
    defaults.update(overrides)
    return EvalResult(**defaults)


# ---------------------------------------------------------------------------
# RequiredToolCall
# ---------------------------------------------------------------------------


def test_required_tool_call_passes_when_count_in_range():
    expectation = RequiredToolCall("awx_recent_failed_jobs", min_count=1, max_count=1)
    result = _result(tool_calls=[_tool_call("awx_recent_failed_jobs")])

    passed, detail = expectation.check(result)

    assert passed is True
    assert "1 time(s)" in detail


def test_required_tool_call_fails_when_never_called():
    expectation = RequiredToolCall("awx_recent_failed_jobs")
    result = _result(tool_calls=[])

    passed, _ = expectation.check(result)

    assert passed is False


def test_required_tool_call_fails_when_called_too_many_times():
    expectation = RequiredToolCall("awx_recent_failed_jobs", min_count=1, max_count=1)
    result = _result(
        tool_calls=[_tool_call("awx_recent_failed_jobs"), _tool_call("awx_recent_failed_jobs")]
    )

    passed, _ = expectation.check(result)

    assert passed is False


def test_required_tool_call_counts_duplicate_outcomes_as_calls():
    # A duplicate call still represents the model getting real (replayed)
    # data — it should count toward satisfying "called at least once".
    expectation = RequiredToolCall("awx_recent_failed_jobs", min_count=2, max_count=2)
    result = _result(
        tool_calls=[_tool_call("awx_recent_failed_jobs", "ok"), _tool_call("awx_recent_failed_jobs", "duplicate")]
    )

    passed, _ = expectation.check(result)

    assert passed is True


def test_required_tool_call_ignores_error_outcomes():
    expectation = RequiredToolCall("awx_recent_failed_jobs", min_count=1)
    result = _result(tool_calls=[_tool_call("awx_recent_failed_jobs", "error")])

    passed, _ = expectation.check(result)

    assert passed is False


def test_required_tool_call_default_label_reflects_exact_count():
    expectation = RequiredToolCall("echo", min_count=1, max_count=1)

    assert expectation.display_label() == "called echo exactly 1 time(s)"


def test_required_tool_call_label_override():
    expectation = RequiredToolCall("echo", label="called it just right")

    assert expectation.display_label() == "called it just right"


# ---------------------------------------------------------------------------
# MaxToolCalls
# ---------------------------------------------------------------------------


def test_max_tool_calls_passes_within_budget():
    expectation = MaxToolCalls(2)
    result = _result(tool_calls=[_tool_call("echo"), _tool_call("echo")])

    passed, _ = expectation.check(result)

    assert passed is True


def test_max_tool_calls_fails_over_budget():
    expectation = MaxToolCalls(1)
    result = _result(tool_calls=[_tool_call("echo"), _tool_call("echo")])

    passed, _ = expectation.check(result)

    assert passed is False


def test_max_tool_calls_counts_every_outcome_kind():
    # Malformed/duplicate/unknown attempts count too — this is a guard
    # against runaway behavior of any kind, not just successful calls.
    expectation = MaxToolCalls(1)
    result = _result(tool_calls=[_tool_call("echo", "ok"), _tool_call("echo", "bad_arguments")])

    passed, _ = expectation.check(result)

    assert passed is False


# ---------------------------------------------------------------------------
# RequiredEvidence
# ---------------------------------------------------------------------------


def test_required_evidence_passes_when_substring_present():
    expectation = RequiredEvidence("host03")
    result = _result(final_answer="the failure involved host03")

    passed, detail = expectation.check(result)

    assert passed is True
    assert "host03" in detail


def test_required_evidence_fails_when_absent():
    expectation = RequiredEvidence("host03")
    result = _result(final_answer="no mention of the host")

    passed, _ = expectation.check(result)

    assert passed is False


def test_required_evidence_passes_on_any_of_multiple_patterns():
    expectation = RequiredEvidence(["network reachability", "network issue"])
    result = _result(final_answer="this looks like a network issue")

    passed, _ = expectation.check(result)

    assert passed is True


def test_required_evidence_is_case_insensitive_by_default():
    expectation = RequiredEvidence("HOST03")
    result = _result(final_answer="something about host03 happened")

    passed, _ = expectation.check(result)

    assert passed is True


def test_required_evidence_respects_case_sensitive_flag():
    expectation = RequiredEvidence("HOST03", case_sensitive=True)
    result = _result(final_answer="something about host03 happened")

    passed, _ = expectation.check(result)

    assert passed is False


def test_required_evidence_fails_when_final_answer_is_none():
    expectation = RequiredEvidence("host03")
    result = _result(final_answer=None, outcome="error")

    passed, _ = expectation.check(result)

    assert passed is False


# ---------------------------------------------------------------------------
# ForbiddenClaim
# ---------------------------------------------------------------------------


def test_forbidden_claim_passes_when_phrase_absent():
    expectation = ForbiddenClaim("firewall caused")
    result = _result(final_answer="network reachability issue, cause unclear")

    passed, _ = expectation.check(result)

    assert passed is True


def test_forbidden_claim_fails_when_phrase_present():
    expectation = ForbiddenClaim("firewall caused")
    result = _result(final_answer="the firewall caused this outage")

    passed, detail = expectation.check(result)

    assert passed is False
    assert "firewall caused" in detail


def test_forbidden_claim_fails_on_any_of_multiple_patterns():
    expectation = ForbiddenClaim(["firewall caused", "due to a firewall"])
    result = _result(final_answer="this was due to a firewall blocking traffic")

    passed, _ = expectation.check(result)

    assert passed is False


# ---------------------------------------------------------------------------
# MustProduceFinalAnswer
# ---------------------------------------------------------------------------


def test_must_produce_final_answer_passes_with_real_text():
    expectation = MustProduceFinalAnswer()
    result = _result(outcome="ok", final_answer="a real answer")

    passed, _ = expectation.check(result)

    assert passed is True


def test_must_produce_final_answer_fails_on_error_outcome():
    expectation = MustProduceFinalAnswer()
    result = _result(outcome="error", final_answer=None)

    passed, detail = expectation.check(result)

    assert passed is False
    assert "error" in detail


def test_must_produce_final_answer_fails_on_empty_text():
    expectation = MustProduceFinalAnswer()
    result = _result(outcome="ok", final_answer="")

    passed, _ = expectation.check(result)

    assert passed is False


def test_must_produce_final_answer_fails_on_whitespace_only_text():
    expectation = MustProduceFinalAnswer()
    result = _result(outcome="ok", final_answer="   \n")

    passed, _ = expectation.check(result)

    assert passed is False
