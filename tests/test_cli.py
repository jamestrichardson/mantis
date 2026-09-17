"""Tests for mantis.cli.main(): the metrics-server opt-in gate.

Mantis runs as a short-lived CLI process per invocation — the metrics
HTTP server must never start unless MANTIS_METRICS_ENABLED is explicitly
set, in any environment, including inside the Docker image (see
docs/observability.md#current-status-of-metrics and
tests/test_dockerfile.py). Structured logging has no such gate — it's
always configured, since every invocation, however short, should emit
its event sequence.
"""

from __future__ import annotations

import pytest

from mantis import cli as cli_module


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


def test_getenv_bool_defaults_to_false_when_unset(monkeypatch):
    monkeypatch.delenv("MANTIS_METRICS_ENABLED", raising=False)
    assert cli_module._getenv_bool("MANTIS_METRICS_ENABLED", False) is False


def test_system_troubleshooter_is_registered_as_an_agent():
    # Regression test (#11): "mantis system-troubleshooter ..." must
    # dispatch to the real agent module, following the exact same
    # existing-CLI-pattern convention as "mantis awx-troubleshooter ...".
    assert cli_module.AGENTS["system-troubleshooter"] == "mantis.agents.system_troubleshooter"


def test_system_troubleshooter_module_resolves_and_exposes_main(monkeypatch):
    import importlib

    module = importlib.import_module(cli_module.AGENTS["system-troubleshooter"])
    assert hasattr(module, "main")
