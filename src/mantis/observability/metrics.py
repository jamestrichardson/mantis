"""Prometheus metrics: a ``/metrics`` HTTP endpoint for scraping.

Mantis never pushes metrics — no Pushgateway, no remote-write. This
module only exposes a registry a Prometheus server scrapes (see
``docs/observability.md`` for the scrape target and example queries).

All metrics live on one module-level :class:`~prometheus_client.CollectorRegistry`
(``REGISTRY``) rather than ``prometheus_client``'s process-global default
registry, so tests can inspect exact before/after values without cross-test
state leaking through a shared global, and so it's unambiguous that
production agent runs and evaluation runs report into the *same* registry
under the *same* metric names — evaluation does not maintain a parallel
metrics implementation.

Label cardinality policy: every label used below is a small, bounded set
(agent name, LiteLLM model alias, scenario name, a fixed outcome/result
vocabulary, tool name, exception class name, environment name) — never a
``run_id``, hostname, AWX job ID, prompt text, or other high-cardinality/
user-controlled value. See ``docs/observability.md`` for the full policy
and ``tests/observability/test_metrics.py`` for cardinality-guard tests.
"""

from __future__ import annotations

import os

from prometheus_client import CollectorRegistry, Counter, Histogram, start_http_server

DEFAULT_PORT = 9108
DEFAULT_ADDR = "0.0.0.0"

REGISTRY = CollectorRegistry()


def environment() -> str:
    """The ``environment`` label value for every metric below — sourced
    from ``MANTIS_ENVIRONMENT`` (e.g. ``"production"``, ``"local"``),
    defaulting to ``"local"`` so an unconfigured dev run never fails to
    record a metric, it's just labeled as local."""
    return os.environ.get("MANTIS_ENVIRONMENT", "local")


RUNS_TOTAL = Counter(
    "mantis_runs_total",
    "Total AgentRuntime.run() invocations",
    ["agent", "model_alias", "result", "environment"],
    registry=REGISTRY,
)
RUN_DURATION_SECONDS = Histogram(
    "mantis_run_duration_seconds",
    "AgentRuntime.run() wall-clock duration",
    ["agent", "model_alias", "result", "environment"],
    registry=REGISTRY,
)
MODEL_CALLS_TOTAL = Counter(
    "mantis_model_calls_total",
    "Total model completion calls",
    ["agent", "model_alias", "environment"],
    registry=REGISTRY,
)
MODEL_CALL_DURATION_SECONDS = Histogram(
    "mantis_model_call_duration_seconds",
    "Model completion call duration",
    ["agent", "model_alias", "environment"],
    registry=REGISTRY,
)
MODEL_TOKENS_TOTAL = Counter(
    "mantis_model_tokens_total",
    "Total tokens reported by model completion calls",
    ["agent", "model_alias", "environment"],
    registry=REGISTRY,
)
TOOL_CALLS_TOTAL = Counter(
    "mantis_tool_calls_total",
    "Total tool call attempts",
    ["agent", "tool", "result", "environment"],
    registry=REGISTRY,
)
TOOL_CALL_DURATION_SECONDS = Histogram(
    "mantis_tool_call_duration_seconds",
    "Tool call handler duration (executed calls only, not cache replays or rejections)",
    ["agent", "tool", "environment"],
    registry=REGISTRY,
)
TOOL_ERRORS_TOTAL = Counter(
    "mantis_tool_errors_total",
    "Total tool call errors, by exception class",
    ["agent", "tool", "error_kind", "environment"],
    registry=REGISTRY,
)
EVAL_RUNS_TOTAL = Counter(
    "mantis_eval_runs_total",
    "Total evaluation scenario runs",
    ["scenario", "model_alias", "result", "environment"],
    registry=REGISTRY,
)
EVAL_SCORE_RATIO = Histogram(
    "mantis_eval_score_ratio",
    "Evaluation score / max_score ratio per scored run",
    ["scenario", "model_alias", "environment"],
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    registry=REGISTRY,
)
EVAL_HARD_FAILURES_TOTAL = Counter(
    "mantis_eval_hard_failures_total",
    "Total hard-requirement check failures across evaluation runs",
    ["scenario", "model_alias", "environment"],
    registry=REGISTRY,
)


def start_metrics_server(port: int | None = None, addr: str | None = None) -> None:
    """Start the Prometheus ``/metrics`` HTTP server in a background
    thread for the lifetime of this process.

    ``port``/``addr`` default to ``MANTIS_METRICS_PORT``/``MANTIS_METRICS_ADDR``
    (in turn defaulting to ``9108``/``0.0.0.0``). Call at most once per
    process — owned by ``mantis.api.server.run_server`` (``mantis
    serve``, on by default: the one persistent process #66 gives
    metrics a real, continuously-held-open home in) and
    ``mantis.eval.cli.main`` (``mantis eval``, opt-in via
    ``MANTIS_METRICS_ENABLED`` for its own short-lived local process),
    never ``mantis.cli.main`` itself — the shared entry point
    ``mantis agents``/``mantis run``/the convenience commands go
    through has no metrics code path at all and must never call this.
    """
    resolved_port = port if port is not None else int(os.environ.get("MANTIS_METRICS_PORT", DEFAULT_PORT))
    resolved_addr = addr if addr is not None else os.environ.get("MANTIS_METRICS_ADDR", DEFAULT_ADDR)
    start_http_server(resolved_port, resolved_addr, registry=REGISTRY)
