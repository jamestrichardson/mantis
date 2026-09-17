"""Tests for mantis.eval.expectations: each deterministic check type,
pass and fail cases, and the hard/quality default for each.
"""

from __future__ import annotations

from mantis.eval.expectations import (
    ForbiddenAnswerPattern,
    ForbiddenToolCall,
    HypothesisLabeled,
    MaxIterations,
    MaxToolCalls,
    MustProduceFinalAnswer,
    NoRetrievalErrorMisattribution,
    NoUnexpectedEntities,
    RequiredAnswerPattern,
    RequiredToolAttempt,
    RequiredToolCall,
    ToolArgumentsMatch,
    TruncationAcknowledged,
    UnsupportedDefinitiveClaim,
)
from mantis.eval.results import EvalResult, ToolCallSummary


def _tool_call(tool_name: str, outcome: str = "ok", arguments=None, result=None) -> ToolCallSummary:
    return ToolCallSummary(
        iteration=1,
        tool_name=tool_name,
        arguments=arguments or {},
        outcome=outcome,
        detail="",
        result=result,
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
# MustProduceFinalAnswer
# ---------------------------------------------------------------------------


def test_must_produce_final_answer_is_hard_by_default():
    assert MustProduceFinalAnswer().hard is True


def test_must_produce_final_answer_passes_with_real_text():
    passed, _ = MustProduceFinalAnswer().check(_result(outcome="ok", final_answer="a real answer"))
    assert passed is True


def test_must_produce_final_answer_fails_on_error_outcome():
    passed, detail = MustProduceFinalAnswer().check(_result(outcome="error", final_answer=None))
    assert passed is False
    assert "error" in detail


def test_must_produce_final_answer_fails_on_whitespace_only():
    passed, _ = MustProduceFinalAnswer().check(_result(outcome="ok", final_answer="   \n"))
    assert passed is False


# ---------------------------------------------------------------------------
# RequiredToolCall
# ---------------------------------------------------------------------------


def test_required_tool_call_is_hard_by_default():
    assert RequiredToolCall("t").hard is True


def test_required_tool_call_passes_when_count_in_range():
    expectation = RequiredToolCall("awx_recent_failed_jobs", min_count=1, max_count=1)
    result = _result(tool_calls=[_tool_call("awx_recent_failed_jobs")])
    passed, detail = expectation.check(result)
    assert passed is True
    assert "1 time(s)" in detail


def test_required_tool_call_fails_when_never_called():
    passed, _ = RequiredToolCall("t").check(_result(tool_calls=[]))
    assert passed is False


def test_required_tool_call_fails_when_called_too_many_times():
    expectation = RequiredToolCall("t", min_count=1, max_count=1)
    result = _result(tool_calls=[_tool_call("t"), _tool_call("t")])
    passed, _ = expectation.check(result)
    assert passed is False


def test_required_tool_call_counts_duplicate_outcomes():
    expectation = RequiredToolCall("t", min_count=2, max_count=2)
    result = _result(tool_calls=[_tool_call("t", "ok"), _tool_call("t", "duplicate")])
    passed, _ = expectation.check(result)
    assert passed is True


def test_required_tool_call_ignores_error_outcomes():
    passed, _ = RequiredToolCall("t", min_count=1).check(_result(tool_calls=[_tool_call("t", "error")]))
    assert passed is False


def test_required_tool_call_default_name():
    assert RequiredToolCall("echo").resolved_name() == "required_tool_call:echo"


def test_required_tool_call_name_override():
    assert RequiredToolCall("echo", name="custom").resolved_name() == "custom"


# ---------------------------------------------------------------------------
# RequiredToolAttempt
# ---------------------------------------------------------------------------


def test_required_tool_attempt_is_hard_by_default():
    assert RequiredToolAttempt("t").hard is True


def test_required_tool_attempt_passes_on_a_successful_call():
    passed, _ = RequiredToolAttempt("t").check(_result(tool_calls=[_tool_call("t", "ok")]))
    assert passed is True


def test_required_tool_attempt_passes_on_an_integration_error_outcome():
    # The key distinction from RequiredToolCall: an attempt that failed
    # (e.g. a classified retrieval failure) still counts as an attempt.
    passed, _ = RequiredToolAttempt("t").check(_result(tool_calls=[_tool_call("t", "integration_error")]))
    assert passed is True


def test_required_tool_attempt_fails_when_never_called():
    passed, _ = RequiredToolAttempt("t").check(_result(tool_calls=[]))
    assert passed is False


def test_required_tool_attempt_respects_min_count():
    result = _result(tool_calls=[_tool_call("t", "integration_error")])
    passed, _ = RequiredToolAttempt("t", min_count=2).check(result)
    assert passed is False


def test_required_tool_attempt_default_name():
    assert RequiredToolAttempt("echo").resolved_name() == "required_tool_attempt:echo"


# ---------------------------------------------------------------------------
# ForbiddenToolCall
# ---------------------------------------------------------------------------


def test_forbidden_tool_call_is_hard_by_default():
    assert ForbiddenToolCall("t").hard is True


def test_forbidden_tool_call_passes_when_never_called():
    passed, _ = ForbiddenToolCall("dangerous_tool").check(_result(tool_calls=[_tool_call("echo")]))
    assert passed is True


def test_forbidden_tool_call_fails_when_called():
    passed, _ = ForbiddenToolCall("dangerous_tool").check(
        _result(tool_calls=[_tool_call("dangerous_tool")])
    )
    assert passed is False


# ---------------------------------------------------------------------------
# MaxToolCalls
# ---------------------------------------------------------------------------


def test_max_tool_calls_is_quality_by_default():
    assert MaxToolCalls(1).hard is False


def test_max_tool_calls_can_be_marked_hard():
    assert MaxToolCalls(1, hard=True).hard is True


def test_max_tool_calls_passes_within_budget():
    passed, _ = MaxToolCalls(2).check(_result(tool_calls=[_tool_call("t"), _tool_call("t")]))
    assert passed is True


def test_max_tool_calls_fails_over_budget():
    passed, _ = MaxToolCalls(1).check(_result(tool_calls=[_tool_call("t"), _tool_call("t")]))
    assert passed is False


def test_max_tool_calls_counts_every_outcome_kind():
    result = _result(tool_calls=[_tool_call("t", "ok"), _tool_call("t", "bad_arguments")])
    passed, _ = MaxToolCalls(1).check(result)
    assert passed is False


# ---------------------------------------------------------------------------
# ToolArgumentsMatch
# ---------------------------------------------------------------------------


def test_tool_arguments_match_is_hard_by_default():
    assert ToolArgumentsMatch("t", expected={}).hard is True


def test_tool_arguments_match_passes_on_exact_match():
    expectation = ToolArgumentsMatch("awx_recent_failed_jobs", expected={"limit": 5})
    result = _result(tool_calls=[_tool_call("awx_recent_failed_jobs", arguments={"limit": 5})])
    passed, _ = expectation.check(result)
    assert passed is True


def test_tool_arguments_match_passes_on_subset_match():
    # extra arguments beyond `expected` are fine.
    expectation = ToolArgumentsMatch("t", expected={"limit": 5})
    result = _result(tool_calls=[_tool_call("t", arguments={"limit": 5, "extra": "ok"})])
    passed, _ = expectation.check(result)
    assert passed is True


def test_tool_arguments_match_fails_on_mismatch():
    expectation = ToolArgumentsMatch("t", expected={"limit": 5})
    result = _result(tool_calls=[_tool_call("t", arguments={"limit": 3})])
    passed, _ = expectation.check(result)
    assert passed is False


def test_tool_arguments_match_fails_when_tool_never_called():
    passed, _ = ToolArgumentsMatch("t", expected={"limit": 5}).check(_result(tool_calls=[]))
    assert passed is False


# ---------------------------------------------------------------------------
# MaxIterations
# ---------------------------------------------------------------------------


def test_max_iterations_is_quality_by_default():
    assert MaxIterations(2).hard is False


def test_max_iterations_passes_within_budget():
    passed, _ = MaxIterations(2).check(_result(iterations=2))
    assert passed is True


def test_max_iterations_fails_over_budget():
    passed, _ = MaxIterations(2).check(_result(iterations=3))
    assert passed is False


# ---------------------------------------------------------------------------
# RequiredAnswerPattern
# ---------------------------------------------------------------------------


def test_required_answer_pattern_is_quality_by_default():
    assert RequiredAnswerPattern("x").hard is False


def test_required_answer_pattern_passes_on_substring():
    passed, detail = RequiredAnswerPattern("host03").check(_result(final_answer="host03 failed"))
    assert passed is True
    assert "host03" in detail


def test_required_answer_pattern_passes_on_regex():
    expectation = RequiredAnswerPattern(r"unable to reach .*host03")
    result = _result(final_answer="We were unable to reach the host03 server")
    passed, _ = expectation.check(result)
    assert passed is True


def test_required_answer_pattern_fails_when_absent():
    passed, _ = RequiredAnswerPattern("host03").check(_result(final_answer="no mention of the host"))
    assert passed is False


def test_required_answer_pattern_match_any_passes_on_one_of_several():
    expectation = RequiredAnswerPattern(["network issue", "network reachability"], match="any")
    passed, _ = expectation.check(_result(final_answer="this looks like a network issue"))
    assert passed is True


def test_required_answer_pattern_match_all_requires_every_pattern():
    expectation = RequiredAnswerPattern(["host03", "unreachable"], match="all")
    passed, _ = expectation.check(_result(final_answer="host03 was the problem"))
    assert passed is False


def test_required_answer_pattern_match_all_passes_when_all_present():
    expectation = RequiredAnswerPattern(["host03", "unreachable"], match="all")
    passed, _ = expectation.check(_result(final_answer="host03 was unreachable"))
    assert passed is True


def test_required_answer_pattern_is_case_insensitive_by_default():
    passed, _ = RequiredAnswerPattern("HOST03").check(_result(final_answer="something about host03"))
    assert passed is True


def test_required_answer_pattern_respects_case_sensitive_flag():
    expectation = RequiredAnswerPattern("HOST03", case_sensitive=True)
    passed, _ = expectation.check(_result(final_answer="something about host03"))
    assert passed is False


def test_required_answer_pattern_fails_when_final_answer_is_none():
    passed, _ = RequiredAnswerPattern("host03").check(_result(final_answer=None, outcome="error"))
    assert passed is False


# ---------------------------------------------------------------------------
# ForbiddenAnswerPattern
# ---------------------------------------------------------------------------


def test_forbidden_answer_pattern_is_quality_by_default():
    assert ForbiddenAnswerPattern("x").hard is False


def test_forbidden_answer_pattern_passes_when_absent():
    passed, _ = ForbiddenAnswerPattern("these are all").check(_result(final_answer="a partial list"))
    assert passed is True


def test_forbidden_answer_pattern_fails_when_present():
    expectation = ForbiddenAnswerPattern("these are all", reason="implies exhaustiveness")
    passed, detail = expectation.check(_result(final_answer="These are all the failures"))
    assert passed is False
    assert "implies exhaustiveness" in detail


def test_forbidden_answer_pattern_fails_on_any_of_multiple_patterns():
    expectation = ForbiddenAnswerPattern(["firewall caused", "due to a firewall"])
    passed, _ = expectation.check(_result(final_answer="this was due to a firewall"))
    assert passed is False


# ---------------------------------------------------------------------------
# UnsupportedDefinitiveClaim
# ---------------------------------------------------------------------------


def test_unsupported_definitive_claim_is_hard_by_default():
    assert UnsupportedDefinitiveClaim("firewall").hard is True


def test_unsupported_definitive_claim_passes_when_hedged():
    expectation = UnsupportedDefinitiveClaim(subject_patterns=["firewall", "routing"])
    result = _result(final_answer="Possible causes include a firewall or routing issue.")
    passed, _ = expectation.check(result)
    assert passed is True


def test_unsupported_definitive_claim_fails_on_definitive_language():
    expectation = UnsupportedDefinitiveClaim(subject_patterns=["firewall"])
    result = _result(final_answer="The firewall caused this outage.")
    passed, detail = expectation.check(result)
    assert passed is False
    assert "firewall" in detail


def test_unsupported_definitive_claim_fails_on_was_due_to():
    expectation = UnsupportedDefinitiveClaim(subject_patterns=["routing"])
    result = _result(final_answer="The problem was due to a routing table change.")
    passed, _ = expectation.check(result)
    assert passed is False


def test_unsupported_definitive_claim_passes_when_subject_absent():
    expectation = UnsupportedDefinitiveClaim(subject_patterns=["firewall"])
    result = _result(final_answer="host03 was unreachable via SSH.")
    passed, _ = expectation.check(result)
    assert passed is True


def test_unsupported_definitive_claim_does_not_cross_sentence_boundaries():
    # "caused" appears in a later, unrelated sentence — must not trigger
    # just because both terms exist somewhere in the whole answer.
    expectation = UnsupportedDefinitiveClaim(subject_patterns=["firewall"])
    result = _result(
        final_answer="A firewall might be involved. Separately, the outage caused some alerts to fire."
    )
    passed, _ = expectation.check(result)
    assert passed is True


# ---------------------------------------------------------------------------
# HypothesisLabeled
# ---------------------------------------------------------------------------


def test_hypothesis_labeled_is_quality_by_default():
    assert HypothesisLabeled("firewall").hard is False


def test_hypothesis_labeled_passes_when_subject_absent():
    passed, _ = HypothesisLabeled(subject_patterns=["firewall"]).check(
        _result(final_answer="host03 was unreachable.")
    )
    assert passed is True


def test_hypothesis_labeled_passes_when_hedged():
    expectation = HypothesisLabeled(subject_patterns=["firewall"])
    result = _result(final_answer="A firewall issue is possible but unconfirmed.")
    passed, _ = expectation.check(result)
    assert passed is True


def test_hypothesis_labeled_fails_when_mentioned_without_hedging():
    expectation = HypothesisLabeled(subject_patterns=["firewall"])
    result = _result(final_answer="This was a firewall problem.")
    passed, _ = expectation.check(result)
    assert passed is False


# ---------------------------------------------------------------------------
# TruncationAcknowledged
# ---------------------------------------------------------------------------


def test_truncation_acknowledged_is_quality_by_default():
    assert TruncationAcknowledged().hard is False


def test_truncation_acknowledged_passes_trivially_when_not_truncated():
    result = _result(tool_calls=[_tool_call("t", result={"meta": {"truncated": False}})])
    passed, detail = TruncationAcknowledged().check(result)
    assert passed is True
    assert "nothing to acknowledge" in detail


def test_truncation_acknowledged_passes_when_mentioned():
    result = _result(
        tool_calls=[_tool_call("t", result={"meta": {"truncated": True}})],
        final_answer="There may be more failed jobs beyond what's shown.",
    )
    passed, _ = TruncationAcknowledged().check(result)
    assert passed is True


def test_truncation_acknowledged_fails_when_not_mentioned():
    result = _result(
        tool_calls=[_tool_call("t", result={"meta": {"truncated": True}})],
        final_answer="Here is a summary of the failures.",
    )
    passed, _ = TruncationAcknowledged().check(result)
    assert passed is False


# ---------------------------------------------------------------------------
# NoRetrievalErrorMisattribution
# ---------------------------------------------------------------------------


def test_no_retrieval_error_misattribution_is_hard_by_default():
    assert NoRetrievalErrorMisattribution().hard is True


def test_no_retrieval_error_misattribution_passes_trivially_without_a_retrieval_error():
    result = _result(tool_calls=[_tool_call("t", result={"stdout_retrieval_error": None})])
    passed, detail = NoRetrievalErrorMisattribution().check(result)
    assert passed is True
    assert "nothing to misattribute" in detail


def test_no_retrieval_error_misattribution_passes_when_correctly_separated():
    result = _result(
        tool_calls=[
            _tool_call(
                "t",
                result={"stdout_retrieval_error": {"kind": "retrieval_error", "message": "503"}},
            )
        ],
        final_answer="Unable to retrieve stdout for this job. The cause of the job's failure is unclear.",
    )
    passed, _ = NoRetrievalErrorMisattribution().check(result)
    assert passed is True


def test_no_retrieval_error_misattribution_fails_when_blamed():
    result = _result(
        tool_calls=[
            _tool_call(
                "t",
                result={"stdout_retrieval_error": {"kind": "retrieval_error", "message": "503"}},
            )
        ],
        final_answer="The job failed because we could not retrieve the stdout, which caused the failure.",
    )
    passed, detail = NoRetrievalErrorMisattribution().check(result)
    assert passed is False
    assert "misattribut" in detail.lower() or "blame" in detail.lower()


def test_no_retrieval_error_misattribution_does_not_false_positive_on_unrelated_sentences():
    # "failed" and "retrieve" both present, but in different sentences
    # with no causal claim — must not trigger.
    result = _result(
        tool_calls=[
            _tool_call(
                "t",
                result={"stdout_retrieval_error": {"kind": "retrieval_error", "message": "503"}},
            )
        ],
        final_answer="AWX reports the job failed. Unable to retrieve stdout for this job (retrieval error).",
    )
    passed, _ = NoRetrievalErrorMisattribution().check(result)
    assert passed is True


def test_no_retrieval_error_misattribution_detects_kind_nested_anywhere():
    result = _result(
        tool_calls=[
            _tool_call(
                "t",
                result={"jobs": [{"stdout_retrieval_error": {"kind": "retrieval_error", "message": "x"}}]},
            )
        ],
        final_answer="The retrieval error caused the job to fail.",
    )
    passed, _ = NoRetrievalErrorMisattribution().check(result)
    assert passed is False


def test_no_retrieval_error_misattribution_detects_integration_error_outcome():
    # Regression test (#11): a #8/#9/#10-style raised IntegrationError
    # (network/Prometheus/Loki transport failure) is recorded by
    # AgentRuntime as outcome="integration_error" with result=None --
    # there is no ToolError-shaped dict for _any_tool_error_present to
    # find, so relying on that check alone would silently miss this
    # entire class of retrieval failure.
    result = _result(
        tool_calls=[_tool_call("prometheus_query_range", outcome="integration_error", result=None)],
        final_answer="Unable to retrieve Prometheus data. The cause of the outage is unclear.",
    )
    passed, _ = NoRetrievalErrorMisattribution().check(result)
    assert passed is True


def test_no_retrieval_error_misattribution_fails_when_integration_error_is_blamed():
    result = _result(
        tool_calls=[_tool_call("loki_query", outcome="integration_error", result=None)],
        final_answer="We could not retrieve the logs, which caused the outage.",
    )
    passed, detail = NoRetrievalErrorMisattribution().check(result)
    assert passed is False
    assert "misattribut" in detail.lower() or "blame" in detail.lower()


def test_no_retrieval_error_misattribution_treats_generic_error_outcome_the_same_way():
    result = _result(
        tool_calls=[_tool_call("check_tcp_connectivity", outcome="error", result=None)],
        final_answer="Unable to retrieve TCP connectivity data, which caused the host to be down.",
    )
    passed, _ = NoRetrievalErrorMisattribution().check(result)
    assert passed is False


# ---------------------------------------------------------------------------
# NoUnexpectedEntities
# ---------------------------------------------------------------------------


def test_no_unexpected_entities_is_hard_by_default():
    assert NoUnexpectedEntities().hard is True


def test_no_unexpected_entities_passes_with_only_known_entities():
    expectation = NoUnexpectedEntities(known_hosts=frozenset({"host03"}), known_job_ids=frozenset({"4231"}))
    result = _result(final_answer="Job 4231 on host03 failed.")
    passed, _ = expectation.check(result)
    assert passed is True


def test_no_unexpected_entities_fails_on_unknown_host():
    expectation = NoUnexpectedEntities(known_hosts=frozenset({"host03"}))
    result = _result(final_answer="host04 also seemed to be affected.")
    passed, detail = expectation.check(result)
    assert passed is False
    assert "host04" in detail


def test_no_unexpected_entities_fails_on_unknown_job_id():
    expectation = NoUnexpectedEntities(known_job_ids=frozenset({"4231"}))
    result = _result(final_answer="Job 9999 appears related.")
    passed, _ = expectation.check(result)
    assert passed is False


def test_no_unexpected_entities_is_case_insensitive_for_hosts():
    expectation = NoUnexpectedEntities(known_hosts=frozenset({"host03"}))
    result = _result(final_answer="HOST03 was unreachable.")
    passed, _ = expectation.check(result)
    assert passed is True


def test_no_unexpected_entities_passes_when_no_entities_mentioned():
    expectation = NoUnexpectedEntities(known_hosts=frozenset({"host03"}))
    result = _result(final_answer="A network reachability problem occurred.")
    passed, _ = expectation.check(result)
    assert passed is True
