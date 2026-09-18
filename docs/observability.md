# Observability

This page is about Mantis's own operational telemetry — how Mantis
reports on itself, not how it gathers evidence about the systems it
troubleshoots. For *that* (Mantis actively querying Prometheus/Loki as
evidence sources), see [docs/prometheus.md](prometheus.md) and
[docs/loki.md](loki.md) — those are a completely separate concern from
this page and don't change anything documented here.

For its own telemetry, Mantis exposes two independent,
collector-agnostic surfaces — it never holds credentials for or talks
directly to a collector or dashboard system to *emit* this telemetry:

- **Structured JSON logs** to stdout/stderr, for a host-side collector
  (Grafana Alloy) to ship to Loki.
- **A Prometheus `/metrics` HTTP endpoint**, for a Prometheus server to
  scrape.

Both are wired into `mantis.runtime.AgentRuntime` and `mantis.eval.runner`
directly (see `src/mantis/observability/`), so a production agent run and
an evaluation run share the exact same event schema, metric names, and
metrics registry — evaluation is not a parallel implementation.

## Production contract — current state

Mantis runs as a persistent service (`mantis serve`, #21) hosted in a
standalone Docker Compose deployment (see
[docs/deployment.md](deployment.md)) — every agent invocation happens
through that service's API (#83), not as a separate short-lived process.
That shapes what's actually true today for each surface:

- **Structured JSON logs are fully active in production.** Container
  stdout/stderr is collected by Grafana Alloy and shipped to Loki for
  the service's entire lifetime — every API-triggered run emits its
  complete event sequence, correlated by that run's `run_id` (see
  [docs/api.md](api.md#run-ids)).
- **Prometheus metrics are continuously scraped.** `mantis serve` is the
  one persistent process that owns the shared `CollectorRegistry` and
  holds `/metrics` open for the service's entire lifetime (#66) — the
  same registry every API-triggered agent run and every `mantis eval`
  invocation reports into. `up{job="mantis"}` staying `1` is now a
  meaningful signal for exactly this process; a `mantis eval ...`
  invocation remains its own short-lived local process with its own
  fresh registry (see [Prometheus metrics](#prometheus-metrics) below)
  and is not what a `mantis`-service dashboard should be scraping.

## Structured JSON logs

Enabled automatically by `mantis.cli.main()` (every `mantis <agent>` and
`mantis eval ...` invocation) — never something an agent module
configures itself, so log level/format changes never require touching
agent code. Configure with `MANTIS_LOG_LEVEL` (default `INFO`) — see
[docs/configuration.md](configuration.md).

Every line is one JSON object. Fields present on every event:

| Field | Description |
|---|---|
| `event` | Event type, e.g. `mantis_run_started`. |
| `timestamp` | ISO 8601 UTC. |
| `level` | `debug`/`info`/`warning`/`error`. |

Common fields present where applicable: `run_id`, `agent`, `model_alias`,
`iteration`, `tool`, `duration_seconds`, `outcome`, `error_kind`,
`scenario`.

### Event types

| Event | Emitted by | Notable fields |
|---|---|---|
| `mantis_run_started` | `AgentRuntime.run()` | `run_id`, `agent`, `model_alias` |
| `mantis_run_completed` | `AgentRuntime.run()` | `run_id`, `agent`, `model_alias`, `duration_seconds`, `outcome="ok"` |
| `mantis_run_failed` | `AgentRuntime.run()` | `run_id`, `agent`, `model_alias`, `duration_seconds`, `outcome` (`"error"` or `"max_iterations"`), `error_kind` |
| `mantis_model_call` | `AgentRuntime.run()`, once per model round-trip | `run_id`, `agent`, `model_alias`, `iteration`, `duration_seconds`, `tokens` |
| `mantis_tool_call` | `AgentRuntime._dispatch_tool_call()`, once per tool-call attempt, including rejections that never reach a tool handler | `run_id`, `agent`, `iteration`, `tool`, `outcome` (`ok`/`duplicate`/`unknown_tool`/`bad_arguments`/`error`), `duration_seconds` (executed calls only), `error_kind`, `bound_arguments`, `bound_result` |
| `mantis_eval_result` | `mantis.eval.runner.run_scenario()`, once per scenario/model run | `run_id` (shared with the underlying `AgentRuntime` run), `scenario`, `model_alias`, `outcome` (`pass`/`fail`/`error`/`unscored`), `score`, `max_score`, `duration_seconds` |
| `mantis_eval_check` | `run_scenario()`, once per expectation checked | `run_id`, `scenario`, `model_alias`, `check_name`, `outcome` (`pass`/`fail`), `hard`, `detail` |

`run_id` is generated fresh per `AgentRuntime.run()` call (or, for an
API-triggered run, assigned by `mantis.api.invocation.InvocationService`
*before* invocation and passed into `AgentRuntime.run(prompt,
run_id=...)` — see [docs/api.md](api.md#run-ids) — so it's the exact ID
returned to the HTTP caller) and threaded through every event that run
produces — including the `mantis_eval_*` events for the scenario run
built on top of it (via `AgentRuntime.last_run_id`) — so a full run's
events can be correlated in Loki with a single `run_id` filter. The API
layer adds its own `mantis_api_*` events (request/run lifecycle, see
[docs/api.md](api.md)) under the same `run_id`.

### Redaction and bounding

`mantis.observability.logging.bound_for_log()` is applied to
`bound_arguments`/`bound_result` before they're attached to a
`mantis_tool_call` event:

- Any key matching a credential-shaped name (`token`, `password`,
  `secret`, `api_key`, `authorization`, `credential`, case-insensitive)
  is replaced with `"***"`, recursively through nested dicts/lists.
- The serialized value is capped at 2000 characters (`MAX_LOGGED_VALUE_CHARS`)
  — well below `mantis.tools._text.STDOUT_TAIL_CHARS`, since a log line
  is one event among many, not the primary evidence surface an agent
  reads. An oversized value becomes a truncated string with a notice of
  how much was cut, never silently.
- Full prompts and raw model output are never logged — only the fields
  in the table above.

### Example: LogQL queries (Grafana → Loki)

```logql
# Every event for one run, in order
{container="mantis"} | json | run_id="a1b2c3d4..."

# All failed runs in the last hour
{container="mantis"} | json | event="mantis_run_failed"

# Tool errors by tool name
{container="mantis"} | json | event="mantis_tool_call" | outcome="error" | line_format "{{.tool}}: {{.error_kind}}"
```

## Prometheus metrics

`/metrics` is served by `mantis.observability.metrics.start_metrics_server()`.
`mantis.cli.main()` — the shared entry point `mantis agents`/`mantis
run`/the convenience commands go through — has no metrics code path at
all and never starts it. Ownership belongs entirely to the subcommand
that actually has a metrics lifecycle: `mantis serve`
(`mantis.api.server.run_server`, on by default) and `mantis eval`
(`mantis.eval.cli.main`, opt-in via `MANTIS_METRICS_ENABLED`, not on by
default — see [Current status of `/metrics`](#current-status-of-metrics)
for why). See [docs/configuration.md](configuration.md) for
`MANTIS_METRICS_PORT`/`MANTIS_METRICS_ADDR`, default `:9108`.

All metrics share one `CollectorRegistry`
(`mantis.observability.metrics.REGISTRY`) — production agent runs and
evaluation runs report into the exact same metric names, not a parallel
`eval_*`-prefixed set.

### Metric catalog

| Metric | Type | Labels |
|---|---|---|
| `mantis_runs_total` | Counter | `agent`, `model_alias`, `result`, `environment` |
| `mantis_run_duration_seconds` | Histogram | `agent`, `model_alias`, `result`, `environment` |
| `mantis_model_calls_total` | Counter | `agent`, `model_alias`, `environment` |
| `mantis_model_call_duration_seconds` | Histogram | `agent`, `model_alias`, `environment` |
| `mantis_model_tokens_total` | Counter | `agent`, `model_alias`, `environment` |
| `mantis_tool_calls_total` | Counter | `agent`, `tool`, `result`, `environment` |
| `mantis_tool_call_duration_seconds` | Histogram | `agent`, `tool`, `environment` |
| `mantis_tool_errors_total` | Counter | `agent`, `tool`, `error_kind`, `environment` |
| `mantis_eval_runs_total` | Counter | `scenario`, `model_alias`, `result`, `environment` |
| `mantis_eval_score_ratio` | Histogram | `scenario`, `model_alias`, `environment` |
| `mantis_eval_hard_failures_total` | Counter | `scenario`, `model_alias`, `environment` |

`result`'s value set depends on the metric: `mantis_runs_total` uses
`ok`/`error`/`max_iterations`; `mantis_tool_calls_total` uses
`ok`/`duplicate`/`unknown_tool`/`bad_arguments`/`error`;
`mantis_eval_runs_total` uses `pass`/`fail`/`error`/`unscored`.

### Label cardinality policy

Every label above is a small, bounded, non-user-controlled set: an agent
name, a configured LiteLLM model alias, a scenario name, one of the
fixed `result` vocabularies above, a tool name, an exception class name,
or an environment name. **Never** a `run_id`, a hostname/target, an AWX
job ID, a prompt, an exception message string, or any other
high-cardinality or user/tool-input-derived value — those belong in the
structured logs above, correlatable by `run_id`, not in a Prometheus
label. `tests/observability/test_metrics.py::test_every_metric_only_uses_allowed_low_cardinality_labels`
enforces this against every metric defined in
`mantis.observability.metrics`.

### Example PromQL queries

```promql
# Run success rate by agent, last 5 minutes
sum by (agent) (rate(mantis_runs_total{result="ok"}[5m]))
/ sum by (agent) (rate(mantis_runs_total[5m]))

# p95 tool-call latency by tool
histogram_quantile(0.95, sum by (le, tool) (rate(mantis_tool_call_duration_seconds_bucket[5m])))

# Evaluation pass rate by scenario/model
sum by (scenario, model_alias) (rate(mantis_eval_runs_total{result="pass"}[1h]))
/ sum by (scenario, model_alias) (rate(mantis_eval_runs_total[1h]))

# Tool error rate by error_kind
sum by (tool, error_kind) (rate(mantis_tool_errors_total[5m]))
```

## `mantis serve` and metrics lifecycle (#21/#66)

`mantis serve` (#21/#83) is the one persistent process that owns the
shared `CollectorRegistry` for its entire lifetime — it starts the
`/metrics` HTTP server once (`MANTIS_METRICS_ENABLED` defaults to `true`
for this command specifically, since a persistent process has none of
the port-contention problems a one-shot CLI invocation would), holds
`:9108` open continuously, and accumulates counters/histograms across
every API-triggered agent run for as long as the process lives —
exactly like the structured logs already worked regardless of process
lifetime. A service restart resets process-local metrics normally (a
fresh `CollectorRegistry`, same as any Prometheus counter reset on
process restart); it does not reset anything else.

`up{job="mantis"}` staying `1` is now a meaningful signal for the
`mantis serve` process. There is no competing metrics listener: no
per-CLI-invocation server is started for `mantis <agent>`/`mantis run`
(those are HTTP clients now, see [docs/api.md](api.md), and never touch
the metrics registry directly), and `mantis eval ...` keeps its own
existing, separate, opt-in (`MANTIS_METRICS_ENABLED=false` by default)
short-lived-process behavior for local scenario testing — see
[docs/configuration.md](configuration.md#observability) for both
defaults side by side.

Multiprocess Prometheus mode, a Pushgateway, or a sidecar metrics daemon
were deliberately not introduced — a single persistent process holding
one registry is the simplest correct answer once #21 exists, with no
remaining multi-process CLI execution model to work around.
