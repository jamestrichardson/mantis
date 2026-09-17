"""Tests for mantis.observability.metrics: metric definitions, the
label-cardinality policy, and the /metrics HTTP server.
"""

from __future__ import annotations

import socket
import urllib.request

from prometheus_client import generate_latest

from mantis.observability import metrics

# Every label name used anywhere in metrics.py must be on this list — a
# guard against someone casually adding a high-cardinality label (run_id,
# a hostname, an AWX job ID, ...) to a metric later. See docs/observability.md.
ALLOWED_LABEL_NAMES = {"agent", "model_alias", "scenario", "result", "tool", "error_kind", "environment"}

ALL_METRICS = [
    metrics.RUNS_TOTAL,
    metrics.RUN_DURATION_SECONDS,
    metrics.MODEL_CALLS_TOTAL,
    metrics.MODEL_CALL_DURATION_SECONDS,
    metrics.MODEL_TOKENS_TOTAL,
    metrics.TOOL_CALLS_TOTAL,
    metrics.TOOL_CALL_DURATION_SECONDS,
    metrics.TOOL_ERRORS_TOTAL,
    metrics.EVAL_RUNS_TOTAL,
    metrics.EVAL_SCORE_RATIO,
    metrics.EVAL_HARD_FAILURES_TOTAL,
]


def test_every_metric_only_uses_allowed_low_cardinality_labels():
    for metric in ALL_METRICS:
        offending = set(metric._labelnames) - ALLOWED_LABEL_NAMES
        assert not offending, f"{metric._name} uses disallowed label(s): {offending}"


def test_environment_defaults_to_local(monkeypatch):
    monkeypatch.delenv("MANTIS_ENVIRONMENT", raising=False)
    assert metrics.environment() == "local"


def test_environment_reads_env_var(monkeypatch):
    monkeypatch.setenv("MANTIS_ENVIRONMENT", "production")
    assert metrics.environment() == "production"


def test_counter_increments_are_reflected_in_the_shared_registry():
    before = metrics.RUNS_TOTAL.labels(
        agent="test-agent", model_alias="test-model", result="ok", environment="test"
    )._value.get()

    metrics.RUNS_TOTAL.labels(
        agent="test-agent", model_alias="test-model", result="ok", environment="test"
    ).inc()

    after = metrics.RUNS_TOTAL.labels(
        agent="test-agent", model_alias="test-model", result="ok", environment="test"
    )._value.get()
    assert after == before + 1


def test_histogram_observation_is_reflected_in_the_shared_registry():
    metric = metrics.RUN_DURATION_SECONDS.labels(
        agent="test-agent-hist", model_alias="test-model", result="ok", environment="test"
    )
    before = metric._sum.get()
    metric.observe(1.5)
    assert metric._sum.get() == before + 1.5


def test_generate_latest_produces_valid_prometheus_exposition_format():
    metrics.RUNS_TOTAL.labels(
        agent="exposition-test", model_alias="m", result="ok", environment="test"
    ).inc()

    output = generate_latest(metrics.REGISTRY).decode("utf-8")

    assert "mantis_runs_total" in output
    assert "# HELP mantis_runs_total" in output
    assert "# TYPE mantis_runs_total counter" in output


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_start_metrics_server_exposes_a_scrapeable_endpoint():
    port = _free_port()
    metrics.RUNS_TOTAL.labels(
        agent="scrape-test", model_alias="m", result="ok", environment="test"
    ).inc()

    metrics.start_metrics_server(port=port, addr="127.0.0.1")

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
        assert response.status == 200
        body = response.read().decode("utf-8")

    assert "mantis_runs_total" in body
    assert 'agent="scrape-test"' in body
