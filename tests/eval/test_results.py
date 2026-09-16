"""Tests for mantis.eval.results: EvalResult/ToolCallSummary serialization
and versioning.
"""

from __future__ import annotations

import json

from mantis.eval.results import RESULT_FORMAT_VERSION, EvalResult, ToolCallSummary


def _make_result(**overrides) -> EvalResult:
    defaults = dict(
        scenario="awx-no-route",
        scenario_version="1.0",
        model="mantis-fast",
        started_at="2026-09-15T00:00:00+00:00",
        finished_at="2026-09-15T00:00:05+00:00",
        elapsed_seconds=5.0,
        outcome="ok",
        final_answer="the answer",
    )
    defaults.update(overrides)
    return EvalResult(**defaults)


def test_eval_result_to_dict_is_json_serializable():
    result = _make_result()

    payload = result.to_dict()

    json.dumps(payload)  # must not raise
    assert payload["scenario"] == "awx-no-route"
    assert payload["result_format_version"] == RESULT_FORMAT_VERSION


def test_eval_result_serializes_nested_tool_calls():
    result = _make_result(
        tool_calls=[
            ToolCallSummary(
                iteration=1,
                tool_name="awx_recent_failed_jobs",
                arguments={"limit": 5},
                outcome="ok",
                detail="",
                result={"returned_count": 1},
            )
        ]
    )

    payload = result.to_dict()

    assert payload["tool_calls"] == [
        {
            "iteration": 1,
            "tool_name": "awx_recent_failed_jobs",
            "arguments": {"limit": 5},
            "outcome": "ok",
            "detail": "",
            "result": {"returned_count": 1},
        }
    ]
    json.dumps(payload)  # nested structures must still be JSON-safe


def test_eval_result_defaults_are_empty_not_missing():
    result = _make_result()

    assert result.tool_calls == []
    assert result.usage == []
    assert result.duplicate_call_count == 0
    assert result.malformed_call_count == 0
    assert result.total_tokens is None
    assert result.error is None
    assert result.raw_message is None


def test_eval_result_raw_message_serializes_when_present():
    result = _make_result(
        final_answer="",
        raw_message={"content": "", "tool_calls": None, "reasoning_content": "..."},
    )

    payload = result.to_dict()

    json.dumps(payload)  # must not raise
    assert payload["raw_message"]["reasoning_content"] == "..."


def test_tool_call_summary_to_dict():
    summary = ToolCallSummary(
        iteration=2,
        tool_name="echo",
        arguments=None,
        outcome="bad_arguments",
        detail="malformed",
        result=None,
    )

    assert summary.to_dict() == {
        "iteration": 2,
        "tool_name": "echo",
        "arguments": None,
        "outcome": "bad_arguments",
        "detail": "malformed",
        "result": None,
    }
