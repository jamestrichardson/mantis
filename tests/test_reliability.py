"""Tests for mantis.reliability: the shared failure taxonomy, retry
policy, deadlines, and run-local short circuit (#15).

No real sleeps anywhere in this file — every retry test injects a
recording no-op ``sleep``. No live AWX/LiteLLM dependency.
"""

from __future__ import annotations

import pytest

from mantis.contracts import ToolErrorKind
from mantis.reliability import (
    DEFAULT_SHORT_CIRCUIT_THRESHOLD,
    RETRYABLE_KINDS,
    Deadline,
    DeadlineExceededError,
    IntegrationError,
    IntegrationErrorKind,
    RetryPolicy,
    RunLocalBreaker,
    classify_http_status,
    classify_httpx_exception,
    retry_call,
)


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------


def test_integration_error_carries_classification_fields():
    err = IntegrationError("boom", kind=IntegrationErrorKind.TIMEOUT, source_system="awx", status_code=None)
    assert err.kind == IntegrationErrorKind.TIMEOUT
    assert err.source_system == "awx"
    assert err.retryable is True


@pytest.mark.parametrize(
    "kind",
    [
        IntegrationErrorKind.TIMEOUT,
        IntegrationErrorKind.CONNECTION,
        IntegrationErrorKind.RATE_LIMIT,
        IntegrationErrorKind.SERVER_ERROR,
    ],
)
def test_retryable_kinds_are_marked_retryable(kind):
    assert IntegrationError("x", kind=kind, source_system="awx").retryable is True


@pytest.mark.parametrize(
    "kind",
    [
        IntegrationErrorKind.AUTHENTICATION,
        IntegrationErrorKind.AUTHORIZATION,
        IntegrationErrorKind.NOT_FOUND,
        IntegrationErrorKind.BAD_REQUEST,
        IntegrationErrorKind.UNKNOWN,
    ],
)
def test_permanent_kinds_are_not_retryable(kind):
    assert IntegrationError("x", kind=kind, source_system="awx").retryable is False


def test_diagnostic_message_is_bounded():
    huge = "x" * 10_000
    err = IntegrationError(huge, kind=IntegrationErrorKind.UNKNOWN, source_system="awx")
    assert len(str(err)) < 1000


@pytest.mark.parametrize(
    "kind,tool_error_kind",
    [
        (IntegrationErrorKind.TIMEOUT, ToolErrorKind.TIMEOUT),
        (IntegrationErrorKind.CONNECTION, ToolErrorKind.RETRIEVAL_ERROR),
        (IntegrationErrorKind.AUTHENTICATION, ToolErrorKind.AUTH_ERROR),
        (IntegrationErrorKind.AUTHORIZATION, ToolErrorKind.AUTH_ERROR),
        (IntegrationErrorKind.RATE_LIMIT, ToolErrorKind.RATE_LIMITED),
        (IntegrationErrorKind.NOT_FOUND, ToolErrorKind.NOT_FOUND),
        (IntegrationErrorKind.BAD_REQUEST, ToolErrorKind.UPSTREAM_ERROR),
        (IntegrationErrorKind.SERVER_ERROR, ToolErrorKind.UPSTREAM_ERROR),
        (IntegrationErrorKind.UNKNOWN, ToolErrorKind.UNKNOWN),
    ],
)
def test_to_tool_error_kind_maps_onto_the_existing_contract(kind, tool_error_kind):
    err = IntegrationError("x", kind=kind, source_system="awx")
    assert err.to_tool_error_kind() == tool_error_kind


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, IntegrationErrorKind.AUTHENTICATION),
        (403, IntegrationErrorKind.AUTHORIZATION),
        (404, IntegrationErrorKind.NOT_FOUND),
        (429, IntegrationErrorKind.RATE_LIMIT),
        (400, IntegrationErrorKind.BAD_REQUEST),
        (422, IntegrationErrorKind.BAD_REQUEST),
        (500, IntegrationErrorKind.SERVER_ERROR),
        (502, IntegrationErrorKind.SERVER_ERROR),
        (503, IntegrationErrorKind.SERVER_ERROR),
        (504, IntegrationErrorKind.SERVER_ERROR),
        (418, IntegrationErrorKind.BAD_REQUEST),  # unmapped 4xx falls back sensibly
    ],
)
def test_classify_http_status(status, expected):
    assert classify_http_status(status) == expected


def test_classify_httpx_connect_timeout():
    import httpx

    exc = httpx.ConnectTimeout("connect timed out")
    assert classify_httpx_exception(exc) == IntegrationErrorKind.TIMEOUT


def test_classify_httpx_read_timeout():
    import httpx

    exc = httpx.ReadTimeout("read timed out")
    assert classify_httpx_exception(exc) == IntegrationErrorKind.TIMEOUT


def test_classify_httpx_connect_error():
    import httpx

    exc = httpx.ConnectError("connection refused")
    assert classify_httpx_exception(exc) == IntegrationErrorKind.CONNECTION


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


def test_deadline_not_expired_when_time_remains():
    clock = {"t": 0.0}
    deadline = Deadline.after(10.0, clock=lambda: clock["t"])
    assert not deadline.expired()
    assert deadline.remaining() == 10.0


def test_deadline_expires_after_its_budget():
    clock = {"t": 0.0}
    deadline = Deadline.after(5.0, clock=lambda: clock["t"])
    clock["t"] = 5.1
    assert deadline.expired()
    assert deadline.remaining() == 0.0


def test_deadline_remaining_never_goes_negative():
    clock = {"t": 0.0}
    deadline = Deadline.after(1.0, clock=lambda: clock["t"])
    clock["t"] = 100.0
    assert deadline.remaining() == 0.0


# ---------------------------------------------------------------------------
# retry_call: the worked examples from the issue itself
# ---------------------------------------------------------------------------


def test_example_a_transient_failure_then_success_is_one_tool_call_two_attempts():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise IntegrationError("server error", kind=IntegrationErrorKind.SERVER_ERROR, source_system="awx")
        return "ok"

    sleeps = []
    result = retry_call(flaky, policy=RetryPolicy(max_attempts=3), source_system="awx", sleep=sleeps.append)

    assert result == "ok"
    assert len(calls) == 2
    assert len(sleeps) == 1  # one backoff, between attempt 1 and 2


def test_example_b_retry_exhaustion_returns_the_classified_failure():
    calls = []

    def always_times_out():
        calls.append(1)
        raise IntegrationError("timeout", kind=IntegrationErrorKind.TIMEOUT, source_system="awx")

    with pytest.raises(IntegrationError) as excinfo:
        retry_call(
            always_times_out,
            policy=RetryPolicy(max_attempts=3),
            source_system="awx",
            sleep=lambda _: None,
        )

    assert len(calls) == 3  # exactly the named max-attempt budget
    assert excinfo.value.kind == IntegrationErrorKind.TIMEOUT


def test_no_real_sleep_happens_in_retry_tests(monkeypatch):
    # Belt-and-suspenders: fail the test outright if retry_call's default
    # sleep parameter is ever exercised unmocked in this file.
    import time as time_module

    def boom(*_a, **_kw):
        raise AssertionError("retry_call attempted a real time.sleep in a test")

    monkeypatch.setattr(time_module, "sleep", boom)

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise IntegrationError("x", kind=IntegrationErrorKind.TIMEOUT, source_system="awx")
        return "ok"

    result = retry_call(flaky, policy=RetryPolicy(max_attempts=3), source_system="awx", sleep=lambda _: None)
    assert result == "ok"


@pytest.mark.parametrize(
    "kind",
    [
        IntegrationErrorKind.AUTHENTICATION,
        IntegrationErrorKind.AUTHORIZATION,
        IntegrationErrorKind.BAD_REQUEST,
        IntegrationErrorKind.NOT_FOUND,
    ],
)
def test_permanent_failures_are_never_retried(kind):
    calls = []

    def fails():
        calls.append(1)
        raise IntegrationError("x", kind=kind, source_system="awx")

    with pytest.raises(IntegrationError):
        retry_call(fails, policy=RetryPolicy(max_attempts=5), source_system="awx", sleep=lambda _: None)

    assert len(calls) == 1  # zero retries consumed


def test_rate_limit_is_retried():
    calls = []

    def rate_limited():
        calls.append(1)
        if len(calls) < 2:
            raise IntegrationError("429", kind=IntegrationErrorKind.RATE_LIMIT, source_system="awx")
        return "ok"

    result = retry_call(rate_limited, policy=RetryPolicy(max_attempts=3), source_system="awx", sleep=lambda _: None)
    assert result == "ok"
    assert len(calls) == 2


@pytest.mark.parametrize("status_kind", [IntegrationErrorKind.SERVER_ERROR])
def test_representative_5xx_is_retried(status_kind):
    # 502/503/504 all classify to SERVER_ERROR (see classify_http_status)
    # — retry behavior is identical regardless of which one.
    calls = []

    def upstream_down():
        calls.append(1)
        if len(calls) < 3:
            raise IntegrationError("502", kind=status_kind, source_system="awx")
        return "ok"

    result = retry_call(upstream_down, policy=RetryPolicy(max_attempts=3), source_system="awx", sleep=lambda _: None)
    assert result == "ok"
    assert len(calls) == 3


def test_rate_limit_respects_retry_after():
    def rate_limited():
        raise IntegrationError(
            "429", kind=IntegrationErrorKind.RATE_LIMIT, source_system="awx", retry_after=2.5
        )

    sleeps = []
    with pytest.raises(IntegrationError):
        retry_call(
            rate_limited,
            policy=RetryPolicy(max_attempts=2, backoff_cap_seconds=10.0),
            source_system="awx",
            sleep=sleeps.append,
        )

    assert sleeps == [2.5]


def test_backoff_is_capped():
    policy = RetryPolicy(backoff_base_seconds=100.0, backoff_cap_seconds=1.0)
    # Even at a high attempt number, backoff never exceeds the cap.
    assert policy.backoff_seconds(10) <= 1.0


def test_on_attempt_hook_fires_for_every_attempt():
    calls = []

    def flaky():
        if len(calls) == 0:
            calls.append(1)
            raise IntegrationError("x", kind=IntegrationErrorKind.TIMEOUT, source_system="awx")
        calls.append(1)
        return "ok"

    observed = []
    retry_call(
        flaky,
        policy=RetryPolicy(max_attempts=3),
        source_system="awx",
        sleep=lambda _: None,
        on_attempt=lambda attempt, error: observed.append((attempt, error)),
    )

    assert [a for a, _ in observed] == [1, 2]
    assert observed[0][1] is not None  # first attempt failed
    assert observed[1][1] is None  # second attempt succeeded


# ---------------------------------------------------------------------------
# retry_call: interaction with a Deadline
# ---------------------------------------------------------------------------


def test_retry_stops_when_backoff_would_exceed_remaining_budget():
    calls = []

    def always_fails():
        calls.append(1)
        raise IntegrationError("x", kind=IntegrationErrorKind.TIMEOUT, source_system="awx")

    clock = {"t": 0.0}
    deadline = Deadline(expires_at=0.01, clock=lambda: clock["t"])
    sleeps = []

    with pytest.raises(IntegrationError):
        retry_call(
            always_fails,
            policy=RetryPolicy(max_attempts=5, backoff_base_seconds=5.0),
            deadline=deadline,
            source_system="awx",
            sleep=sleeps.append,
        )

    assert len(calls) == 1  # one attempt made, no backoff attempted/slept
    assert sleeps == []


def test_deadline_already_expired_before_first_attempt_raises_deadline_exceeded():
    calls = []

    def never_called():
        calls.append(1)
        return "unreachable"

    deadline = Deadline.after(0.0)
    with pytest.raises(DeadlineExceededError) as excinfo:
        retry_call(never_called, policy=RetryPolicy(max_attempts=3), deadline=deadline, source_system="awx")

    assert len(calls) == 0
    assert excinfo.value.scope == "tool"


def test_retryable_kinds_constant_matches_error_retryable_property():
    for kind in IntegrationErrorKind:
        expected = kind in RETRYABLE_KINDS
        assert IntegrationError("x", kind=kind, source_system="awx").retryable is expected


# ---------------------------------------------------------------------------
# RunLocalBreaker
# ---------------------------------------------------------------------------


def test_breaker_starts_closed():
    breaker = RunLocalBreaker()
    assert not breaker.is_open("awx")


def test_breaker_opens_at_threshold():
    breaker = RunLocalBreaker(threshold=3)
    for _ in range(2):
        breaker.record_failure("awx", IntegrationErrorKind.SERVER_ERROR)
        assert not breaker.is_open("awx")
    breaker.record_failure("awx", IntegrationErrorKind.SERVER_ERROR)
    assert breaker.is_open("awx")


def test_breaker_default_threshold_matches_named_constant():
    breaker = RunLocalBreaker()
    for _ in range(DEFAULT_SHORT_CIRCUIT_THRESHOLD - 1):
        breaker.record_failure("awx", IntegrationErrorKind.TIMEOUT)
    assert not breaker.is_open("awx")
    breaker.record_failure("awx", IntegrationErrorKind.TIMEOUT)
    assert breaker.is_open("awx")


def test_breaker_success_resets_the_failure_count():
    breaker = RunLocalBreaker(threshold=2)
    breaker.record_failure("awx", IntegrationErrorKind.TIMEOUT)
    breaker.record_success("awx")
    breaker.record_failure("awx", IntegrationErrorKind.TIMEOUT)
    assert not breaker.is_open("awx")  # count was reset, not cumulative


@pytest.mark.parametrize("kind", [IntegrationErrorKind.NOT_FOUND, IntegrationErrorKind.BAD_REQUEST])
def test_non_availability_failures_never_open_the_breaker(kind):
    breaker = RunLocalBreaker(threshold=1)
    for _ in range(10):
        breaker.record_failure("awx", kind)
    assert not breaker.is_open("awx")


def test_breaker_state_is_isolated_per_source_system():
    breaker = RunLocalBreaker(threshold=1)
    breaker.record_failure("awx", IntegrationErrorKind.TIMEOUT)
    assert breaker.is_open("awx")
    assert not breaker.is_open("prometheus")


def test_fresh_breaker_instance_has_no_memory_of_a_previous_one():
    breaker_a = RunLocalBreaker(threshold=1)
    breaker_a.record_failure("awx", IntegrationErrorKind.TIMEOUT)
    assert breaker_a.is_open("awx")

    breaker_b = RunLocalBreaker(threshold=1)
    assert not breaker_b.is_open("awx")
