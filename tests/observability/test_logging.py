"""Tests for mantis.observability.logging: the JSON formatter, redaction/
bounding, and log_event's field passthrough.
"""

from __future__ import annotations

import json
import logging

import pytest

from mantis.observability.logging import (
    MAX_LOGGED_VALUE_CHARS,
    JSONFormatter,
    bound_for_log,
    log_event,
    new_run_id,
)


@pytest.fixture
def json_logger():
    logger = logging.getLogger("mantis.test.observability")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    records: list[logging.LogRecord] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _CaptureHandler()
    logger.addHandler(handler)
    return logger, records


def test_new_run_id_returns_unique_values():
    assert new_run_id() != new_run_id()


def test_log_event_produces_valid_json_with_expected_fields(json_logger):
    logger, records = json_logger

    log_event(
        logger,
        "mantis_run_started",
        run_id="abc123",
        agent="awx-troubleshooter",
        model_alias="mantis-fast",
    )

    assert len(records) == 1
    payload = json.loads(JSONFormatter().format(records[0]))
    assert payload["event"] == "mantis_run_started"
    assert payload["run_id"] == "abc123"
    assert payload["agent"] == "awx-troubleshooter"
    assert payload["model_alias"] == "mantis-fast"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_log_event_omits_fields_not_passed(json_logger):
    # No `tool`/`scenario`/etc. passed — they must not appear as null noise.
    logger, records = json_logger
    log_event(logger, "mantis_run_started", run_id="x", agent="y")

    payload = json.loads(JSONFormatter().format(records[0]))
    assert "tool" not in payload
    assert "scenario" not in payload


def test_log_event_respects_level(json_logger):
    logger, records = json_logger
    log_event(logger, "mantis_run_failed", level=logging.WARNING, run_id="x")

    payload = json.loads(JSONFormatter().format(records[0]))
    assert payload["level"] == "warning"


def test_log_event_captures_exception_info(json_logger):
    logger, records = json_logger
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("mantis_run_failed", extra={"event": "mantis_run_failed"})

    payload = json.loads(JSONFormatter().format(records[0]))
    assert "ValueError: boom" in payload["exception"]


def test_bound_for_log_redacts_sensitive_keys():
    value = {"token": "sekrit", "nested": {"api_key": "also-sekrit"}, "fine": "value"}
    redacted = bound_for_log(value)

    assert redacted["token"] == "***"
    assert redacted["nested"]["api_key"] == "***"
    assert redacted["fine"] == "value"


def test_bound_for_log_redacts_inside_lists():
    value = [{"password": "sekrit"}, {"ok": True}]
    redacted = bound_for_log(value)

    assert redacted[0]["password"] == "***"
    assert redacted[1]["ok"] is True


def test_bound_for_log_truncates_oversized_values():
    value = {"stdout": "x" * (MAX_LOGGED_VALUE_CHARS * 2)}
    bounded = bound_for_log(value)

    assert isinstance(bounded, str)
    assert "truncated" in bounded
    assert len(bounded) < len(json.dumps(value))


def test_bound_for_log_leaves_small_values_unbounded_and_untruncated():
    value = {"job_id": 4231, "status": "failed"}
    assert bound_for_log(value) == value


def test_bound_for_log_never_raises_on_circular_reference():
    circular: dict = {}
    circular["self"] = circular

    # Must not raise RecursionError/ValueError — the cycle is replaced
    # with a placeholder before it ever reaches json.dumps.
    result = bound_for_log(circular)
    assert result == {"self": "<circular reference>"}
