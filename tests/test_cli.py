"""Tests for mantis.cli.main(): the metrics-server opt-in gate, and the
HTTP-client-only dispatch for agent invocation (#83).

Mantis runs as a short-lived CLI process per invocation — the metrics
HTTP server must never start unless MANTIS_METRICS_ENABLED is explicitly
set, in any environment, including inside the Docker image (see
docs/observability.md and tests/test_dockerfile.py). Structured logging
has no such gate — it's always configured, since every invocation,
however short, should emit its event sequence.

Agent invocation (`mantis agents`/`mantis run <agent> <prompt>`/the
per-agent convenience commands) is HTTP-only: this file asserts the CLI
never constructs an AgentRuntime or imports an agent module to execute
it, and that an unreachable API is reported as an explicit error with no
local-execution fallback. See tests/test_api_client.py for the HTTP
client's own behavior and tests/test_api_app.py for a real end-to-end
CLI-against-real-service test.
"""

from __future__ import annotations

import pytest

from mantis import cli as cli_module
from mantis.api_client import (
    AgentInfo,
    ApiAuthError,
    ApiRequestError,
    ApiServerError,
    ApiTimeoutError,
    ApiUnavailableError,
    RunResult,
)


@pytest.fixture
def patched_metrics_server(monkeypatch):
    calls: list[None] = []
    monkeypatch.setattr(cli_module, "start_metrics_server", lambda *a, **kw: calls.append(None))
    return calls


@pytest.fixture
def patched_configure_logging(monkeypatch):
    calls: list[None] = []
    monkeypatch.setattr(cli_module, "configure_logging", lambda *a, **kw: calls.append(None))
    return calls


def test_main_does_not_start_metrics_server_by_default(monkeypatch, patched_metrics_server):
    monkeypatch.delenv("MANTIS_METRICS_ENABLED", raising=False)

    cli_module.main(["--help"])

    assert patched_metrics_server == []


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "True", "ON"])
def test_main_starts_metrics_server_when_explicitly_enabled(monkeypatch, patched_metrics_server, value):
    monkeypatch.setenv("MANTIS_METRICS_ENABLED", value)

    cli_module.main(["--help"])

    assert len(patched_metrics_server) == 1


@pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
def test_main_does_not_start_metrics_server_for_falsy_values(monkeypatch, patched_metrics_server, value):
    monkeypatch.setenv("MANTIS_METRICS_ENABLED", value)

    cli_module.main(["--help"])

    assert patched_metrics_server == []


def test_main_always_configures_logging_regardless_of_metrics_setting(
    monkeypatch, patched_configure_logging
):
    monkeypatch.delenv("MANTIS_METRICS_ENABLED", raising=False)

    cli_module.main(["--help"])

    assert len(patched_configure_logging) == 1


def test_help_lists_convenience_agents_and_serve():
    assert cli_module.main(["--help"]) == 0
    assert cli_module.CONVENIENCE_AGENTS == ("awx-troubleshooter", "system-troubleshooter")
    assert cli_module.SUBCOMMANDS["serve"] == "mantis.api.server"
    assert cli_module.SUBCOMMANDS["eval"] == "mantis.eval.cli"


# ---------------------------------------------------------------------------
# `mantis agents` / `mantis run` / convenience commands are HTTP-only: no
# AgentRuntime construction, no agent module import-and-execute, no
# local fallback when the API is unreachable.
# ---------------------------------------------------------------------------


def _client_returning(monkeypatch, method_name: str, return_value=None, exc: Exception | None = None):
    calls: list[tuple] = []

    def fake_method(self, *args, **kwargs):
        calls.append((args, kwargs))
        if exc is not None:
            raise exc
        return return_value

    monkeypatch.setattr(cli_module.MantisApiClient, method_name, fake_method)
    return calls


def test_agents_command_calls_list_agents_over_http(monkeypatch, capsys):
    agents = [
        AgentInfo(
            id="system-troubleshooter",
            display_name="System Troubleshooter",
            description="desc",
            read_only=True,
            available=True,
            unavailable_reason=None,
        )
    ]
    calls = _client_returning(monkeypatch, "list_agents", return_value=agents)

    exit_code = cli_module.main(["agents"])

    assert exit_code == 0
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "system-troubleshooter" in out
    assert "desc" in out


def test_agents_command_shows_unavailable_reason(monkeypatch, capsys):
    agents = [
        AgentInfo(
            id="awx-troubleshooter",
            display_name="AWX Troubleshooter",
            description="desc",
            read_only=True,
            available=False,
            unavailable_reason="misconfigured",
        )
    ]
    _client_returning(monkeypatch, "list_agents", return_value=agents)

    cli_module.main(["agents"])

    out = capsys.readouterr().out
    assert "unavailable: misconfigured" in out


def test_run_command_calls_create_run_with_agent_and_prompt(monkeypatch):
    result = RunResult(
        run_id="abc123",
        agent="system-troubleshooter",
        outcome="success",
        output="the answer",
        error_kind=None,
        error_message=None,
        started_at="t0",
        finished_at="t1",
        duration_ms=10,
    )
    calls = _client_returning(monkeypatch, "create_run", return_value=result)

    exit_code = cli_module.main(["run", "system-troubleshooter", "why", "is", "it", "down"])

    assert exit_code == 0
    assert calls == [(("system-troubleshooter", "why is it down"), {})]


def test_run_command_prints_output_and_run_id(monkeypatch, capsys):
    result = RunResult(
        run_id="abc123",
        agent="system-troubleshooter",
        outcome="success",
        output="the answer",
        error_kind=None,
        error_message=None,
        started_at="t0",
        finished_at="t1",
        duration_ms=10,
    )
    _client_returning(monkeypatch, "create_run", return_value=result)

    cli_module.main(["run", "system-troubleshooter", "prompt"])

    captured = capsys.readouterr()
    assert "the answer" in captured.out
    assert "abc123" in captured.err


def test_run_command_with_too_few_args_is_a_usage_error(capsys):
    exit_code = cli_module.main(["run", "system-troubleshooter"])

    assert exit_code == 1
    assert "Usage" in capsys.readouterr().err


def test_run_command_reports_agent_execution_failure(monkeypatch, capsys):
    result = RunResult(
        run_id="abc123",
        agent="system-troubleshooter",
        outcome="error",
        output=None,
        error_kind="max_iterations",
        error_message="The agent could not produce a final answer within its iteration limit.",
        started_at="t0",
        finished_at="t1",
        duration_ms=10,
    )
    _client_returning(monkeypatch, "create_run", return_value=result)

    exit_code = cli_module.main(["run", "system-troubleshooter", "prompt"])

    assert exit_code == 1
    assert "max_iterations" in capsys.readouterr().err


@pytest.mark.parametrize("agent_name", ["awx-troubleshooter", "system-troubleshooter"])
def test_convenience_command_calls_create_run_with_matching_agent_id(monkeypatch, agent_name):
    result = RunResult(
        run_id="x",
        agent=agent_name,
        outcome="success",
        output="ok",
        error_kind=None,
        error_message=None,
        started_at="t0",
        finished_at="t1",
        duration_ms=1,
    )
    calls = _client_returning(monkeypatch, "create_run", return_value=result)

    exit_code = cli_module.main([agent_name, "investigate", "this"])

    assert exit_code == 0
    assert calls == [((agent_name, "investigate this"), {})]


def test_convenience_command_with_no_prompt_is_a_usage_error(capsys):
    exit_code = cli_module.main(["awx-troubleshooter"])

    assert exit_code == 1
    assert "Usage" in capsys.readouterr().err


def test_unknown_command_is_rejected(capsys):
    exit_code = cli_module.main(["not-a-real-command"])

    assert exit_code == 1
    assert "Unknown command" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# No local-execution fallback: every ApiClientError surfaces as a clear,
# distinct CLI error, never a silently-swallowed retry-in-process.
# ---------------------------------------------------------------------------


def test_api_unavailable_is_reported_and_never_falls_back_locally(monkeypatch, capsys):
    _client_returning(monkeypatch, "list_agents", exc=ApiUnavailableError("connection refused"))

    exit_code = cli_module.main(["agents"])

    assert exit_code == 1
    assert "Could not reach the Mantis API" in capsys.readouterr().err


def test_api_timeout_is_reported(monkeypatch, capsys):
    _client_returning(monkeypatch, "list_agents", exc=ApiTimeoutError("timed out"))

    exit_code = cli_module.main(["agents"])

    assert exit_code == 1
    assert "timed out" in capsys.readouterr().err.lower()


def test_api_auth_failure_is_reported(monkeypatch, capsys):
    _client_returning(monkeypatch, "list_agents", exc=ApiAuthError("bad token"))

    exit_code = cli_module.main(["agents"])

    assert exit_code == 1
    assert "authentication failed" in capsys.readouterr().err.lower()


def test_api_request_error_includes_error_type(monkeypatch, capsys):
    _client_returning(
        monkeypatch, "create_run", exc=ApiRequestError(404, "unknown_agent", "Unknown agent: 'nope'")
    )

    exit_code = cli_module.main(["run", "nope", "prompt"])

    assert exit_code == 1
    assert "unknown_agent" in capsys.readouterr().err


def test_overload_error_displays_the_servers_run_id(monkeypatch, capsys):
    # The server deliberately assigns a run ID before rejecting an
    # overloaded request; the CLI must surface it, not discard it.
    _client_returning(
        monkeypatch,
        "create_run",
        exc=ApiRequestError(429, "overloaded", "try again shortly", run_id="abc123"),
    )

    exit_code = cli_module.main(["run", "system-troubleshooter", "prompt"])

    assert exit_code == 1
    assert "abc123" in capsys.readouterr().err


def test_unknown_agent_error_has_no_run_id_to_display(monkeypatch, capsys):
    _client_returning(
        monkeypatch, "create_run", exc=ApiRequestError(404, "unknown_agent", "Unknown agent: 'nope'")
    )

    cli_module.main(["run", "nope", "prompt"])

    assert "run_id" not in capsys.readouterr().err


def test_api_server_error_is_reported(monkeypatch, capsys):
    _client_returning(monkeypatch, "list_agents", exc=ApiServerError("HTTP 500"))

    exit_code = cli_module.main(["agents"])

    assert exit_code == 1
    assert "Mantis API error" in capsys.readouterr().err


def test_cli_module_never_imports_agent_modules_directly():
    # Regression guard for the architectural rule (#83): the CLI module
    # itself must not import mantis.agents.* or mantis.runtime -- those
    # are exclusively server-side (mantis.api.catalog/invocation)
    # concerns now.
    import mantis.cli as cli_mod

    assert not hasattr(cli_mod, "AGENTS")
    source_globals = vars(cli_mod)
    assert "AgentRuntime" not in source_globals
