"""Tests for mantis.api.invocation.InvocationService (#83): the one
shared server-side path every API route goes through.

Uses the real InvocationService and real AgentCatalog wired to fake
``AgentRuntime``-shaped objects (never a real LiteLLM/model call) so
these tests stay deterministic and require no live backend.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from openai import APIConnectionError

from mantis.api.catalog import (
    AgentCatalog,
    AgentCatalogEntry,
    AgentUnavailableError,
    UnknownAgentError,
)
from mantis.api.invocation import ConcurrencyLimitExceededError, InvocationService, _run_in_daemon_thread
from mantis.config import ConfigurationError
from mantis.runtime import MaxIterationsExceededError, RunDeadlineExceededError


class _FakeRuntime:
    """A minimal stand-in for AgentRuntime -- only `.run(prompt, *,
    run_id=...)` is ever called by InvocationService."""

    def __init__(self, *, result: str | None = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.calls: list[tuple[str, str | None]] = []

    def run(self, prompt: str, *, run_id: str | None = None) -> str:
        self.calls.append((prompt, run_id))
        if self._exc is not None:
            raise self._exc
        return self._result


def _entry(agent_id: str, *, build_runtime) -> AgentCatalogEntry:
    return AgentCatalogEntry(
        id=agent_id,
        display_name=agent_id,
        description="test agent",
        read_only=True,
        build_runtime=build_runtime,
        default_prompt="default prompt",
    )


def _service(entries: list[AgentCatalogEntry], *, max_concurrent_runs: int = 4) -> InvocationService:
    return InvocationService(AgentCatalog(entries), max_concurrent_runs=max_concurrent_runs)


def test_successful_invocation_returns_success_outcome():
    runtime = _FakeRuntime(result="the answer")
    service = _service([_entry("agent", build_runtime=lambda: runtime)])

    result = asyncio.run(service.invoke("agent", "do the thing"))

    assert result.outcome == "success"
    assert result.output == "the answer"
    assert result.error is None
    assert result.agent == "agent"
    assert result.run_id
    assert result.duration_ms >= 0


def test_run_id_is_assigned_before_execution_and_passed_into_runtime_run():
    runtime = _FakeRuntime(result="ok")
    service = _service([_entry("agent", build_runtime=lambda: runtime)])

    result = asyncio.run(service.invoke("agent", "prompt"))

    assert len(runtime.calls) == 1
    called_prompt, called_run_id = runtime.calls[0]
    assert called_prompt == "prompt"
    assert called_run_id == result.run_id  # the exact ID returned to the caller correlates the run


def test_unknown_agent_raises_before_any_runtime_construction():
    built = []
    service = _service([_entry("agent", build_runtime=lambda: built.append(1) or _FakeRuntime(result="x"))])

    with pytest.raises(UnknownAgentError) as exc_info:
        asyncio.run(service.invoke("does-not-exist", "prompt"))

    assert exc_info.value.http_status == 404
    assert built == []


def test_agent_unavailable_when_build_runtime_raises_configuration_error():
    def _broken():
        raise ConfigurationError("Missing required environment variable: LITELLM_URL")

    service = _service([_entry("agent", build_runtime=_broken)])

    with pytest.raises(AgentUnavailableError) as exc_info:
        asyncio.run(service.invoke("agent", "prompt"))

    assert exc_info.value.http_status == 409
    assert "LITELLM_URL" not in str(exc_info.value)  # never leak the raw ConfigurationError text


@pytest.mark.parametrize(
    "exc,expected_kind",
    [
        (MaxIterationsExceededError("exceeded"), "max_iterations"),
        (RunDeadlineExceededError("exceeded"), "run_timeout"),
        (ConfigurationError("Missing required environment variable: AWX_TOKEN"), "server_configuration_error"),
        (APIConnectionError(request=None), "model_provider_error"),
        (ValueError("something broke"), "internal_error"),
    ],
)
def test_execution_failure_is_classified_and_never_raised(exc, expected_kind):
    runtime = _FakeRuntime(exc=exc)
    service = _service([_entry("agent", build_runtime=lambda: runtime)])

    result = asyncio.run(service.invoke("agent", "prompt"))  # must not raise

    assert result.outcome == "error"
    assert result.output is None
    assert result.error.kind == expected_kind
    # The safe message must never contain the raw exception text.
    assert "AWX_TOKEN" not in result.error.message
    assert "something broke" not in result.error.message


def test_concurrency_saturation_logs_the_rejected_run_id(caplog):
    # docs/api.md promises a 429's run_id is correlatable in server-side
    # logs even though the run itself never executes -- without a
    # dedicated event here, that ID never actually appeared in any log
    # line (mantis_api_run_started only fires *after* this check).
    caplog.set_level(logging.INFO, logger="mantis.api.invocation")
    started = threading.Event()
    release = threading.Event()

    def _slow_run(prompt: str, *, run_id: str | None = None) -> str:
        started.set()
        release.wait(timeout=5)
        return "done"

    class _SlowRuntime:
        run = staticmethod(_slow_run)

    service = _service([_entry("slow", build_runtime=lambda: _SlowRuntime())], max_concurrent_runs=1)

    async def scenario():
        first = asyncio.create_task(service.invoke("slow", "p1"))
        await asyncio.to_thread(started.wait, 5)

        with pytest.raises(ConcurrencyLimitExceededError) as exc_info:
            await service.invoke("slow", "p2")

        release.set()
        await first
        return exc_info.value.run_id

    rejected_run_id = asyncio.run(scenario())

    rejected_events = [r for r in caplog.records if getattr(r, "event", None) == "mantis_api_run_rejected"]
    assert len(rejected_events) == 1
    assert rejected_events[0].run_id == rejected_run_id
    assert rejected_events[0].agent == "slow"
    assert rejected_events[0].reason == "overloaded"


def test_concurrency_saturation_rejects_immediately_with_the_assigned_run_id():
    started = threading.Event()
    release = threading.Event()

    def _slow_run(prompt: str, *, run_id: str | None = None) -> str:
        started.set()
        release.wait(timeout=5)
        return "done"

    class _SlowRuntime:
        run = staticmethod(_slow_run)

    service = _service([_entry("slow", build_runtime=lambda: _SlowRuntime())], max_concurrent_runs=1)

    async def scenario():
        first = asyncio.create_task(service.invoke("slow", "p1"))
        await asyncio.to_thread(started.wait, 5)

        with pytest.raises(ConcurrencyLimitExceededError) as exc_info:
            await service.invoke("slow", "p2")
        assert exc_info.value.http_status == 429
        assert exc_info.value.run_id  # a run ID is still assigned before the concurrency check

        release.set()
        return await first

    result = asyncio.run(scenario())
    assert result.outcome == "success"


def test_concurrency_slot_is_released_after_a_failed_run():
    service = _service(
        [_entry("agent", build_runtime=lambda: _FakeRuntime(exc=ValueError("boom")))],
        max_concurrent_runs=1,
    )

    asyncio.run(service.invoke("agent", "p1"))
    # If the slot weren't released after the failure above, this second
    # call would incorrectly raise ConcurrencyLimitExceededError.
    result = asyncio.run(service.invoke("agent", "p2"))

    assert result.outcome == "error"


def test_concurrency_slot_is_released_after_a_successful_run():
    service = _service([_entry("agent", build_runtime=lambda: _FakeRuntime(result="ok"))], max_concurrent_runs=1)

    asyncio.run(service.invoke("agent", "p1"))
    result = asyncio.run(service.invoke("agent", "p2"))

    assert result.outcome == "success"


# ---------------------------------------------------------------------------
# _run_in_daemon_thread -- the primitive that lets the process exit on
# schedule during shutdown even when a run never finishes. See
# tests/test_api_server.py::test_shutdown_exits_promptly_even_with_a_blocked_in_flight_run
# for the full server-level proof; these are the focused unit-level checks.
# ---------------------------------------------------------------------------


def test_run_in_daemon_thread_returns_the_function_result():
    async def scenario():
        return await _run_in_daemon_thread(lambda: 42)

    assert asyncio.run(scenario()) == 42


def test_run_in_daemon_thread_propagates_exceptions():
    def _boom():
        raise ValueError("nope")

    async def scenario():
        return await _run_in_daemon_thread(_boom)

    with pytest.raises(ValueError, match="nope"):
        asyncio.run(scenario())


def test_run_in_daemon_thread_spawns_a_daemon_thread():
    seen_daemon_flag = []

    def _worker():
        seen_daemon_flag.append(threading.current_thread().daemon)
        return "done"

    async def scenario():
        return await _run_in_daemon_thread(_worker)

    result = asyncio.run(scenario())

    assert result == "done"
    assert seen_daemon_flag == [True]


def test_run_in_daemon_thread_passes_args_and_kwargs():
    def _fn(a, *, b):
        return a + b

    async def scenario():
        return await _run_in_daemon_thread(_fn, 1, b=2)

    assert asyncio.run(scenario()) == 3
