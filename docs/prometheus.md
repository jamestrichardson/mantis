# Prometheus time-series evidence (`prometheus_query` / `prometheus_query_range`)

Mantis's first time-series evidence source (#9). Keep the three evidence
sources' semantics distinct:

| Source | Answers | Nature |
|---|---|---|
| AWX (`awx_get_job_failure`, #28) | What did automation observe when a job ran? | Historical, point-in-time-per-event |
| TCP (`check_tcp_connectivity`, #8) | Can Mantis reach this host/port right now? | Current-state, one vantage point |
| Prometheus (this page, #9) | What did monitored state do over time? | Time-series, a window of samples |

Never conflate them — a Prometheus `up==0` sample is not the same kind
of evidence as an AWX job failure or a failed TCP probe, even when they
correlate in time. See "Prometheus `up` semantics" below.

## Scope

Only two Prometheus HTTP API endpoints are used:

- `GET /api/v1/query` — instant PromQL query (`prometheus_query`)
- `GET /api/v1/query_range` — bounded range PromQL query (`prometheus_query_range`)

No generic HTTP fetching, no arbitrary API browsing, no alert-rule or
recording-rule management, no target/scrape configuration, no remote
write, no administration. The model can supply PromQL text; it cannot
supply a URL, endpoint path, HTTP method, headers, or auth parameters —
those are fixed by `mantis.integrations.prometheus.PrometheusClient`,
configured entirely through environment variables (below).

## Layering

```
src/mantis/integrations/prometheus.py
    HTTP/auth/API/reliability mechanics (PrometheusClient)

src/mantis/tools/prometheus.py
    PromQL/time/range validation, result shaping, provenance,
    cardinality/sample bounding, registry
```

Reuses the exact `AWXClient`-established `_get()`/`retry_call()`
pattern (see [docs/reliability.md](reliability.md)) — no second retry
helper, timeout config family, or breaker abstraction was introduced.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `MANTIS_PROMETHEUS_URL` | yes | — | Base URL of your Prometheus server, no trailing slash required (stripped). |
| `MANTIS_PROMETHEUS_BEARER_TOKEN` | no | — | Bearer token for `Authorization: Bearer <token>`. |
| `MANTIS_PROMETHEUS_BASIC_AUTH_USERNAME` | no | — | HTTP Basic auth username. |
| `MANTIS_PROMETHEUS_BASIC_AUTH_PASSWORD` | no | — | HTTP Basic auth password. |
| `MANTIS_PROMETHEUS_VERIFY_SSL` | no | `true` | Whether to verify TLS certificates. |

An unauthenticated endpoint works with no auth variables set. If both a
bearer token and basic auth credentials are configured, the bearer
token takes priority (the more common real-world pattern: a reverse
proxy adds one or the other, not both) — see
`mantis.integrations.prometheus.PrometheusClient._client`.

**`MANTIS_PROMETHEUS_VERIFY_SSL=false` disables TLS certificate
verification.** It defaults to `true`. Only disable it for a local/dev
endpoint with a self-signed certificate you control — doing so removes
protection against a machine-in-the-middle intercepting or tampering
with monitoring data in transit. This is the same explicit-opt-in,
on-by-default posture `AWX_VERIFY_SSL` already uses.

Credentials never appear in tool results, structured logs, exception
messages, model-facing output, or schemas — every diagnostic message
this client builds uses only the request path/action description, never
the `Authorization` header or the URL's query parameters (which never
carry credentials; auth is header/httpx-auth only, never a query
param).

Reliability settings (timeouts, retry attempts/backoff, deadlines,
short-circuit threshold) are the existing shared
`mantis.config.ReliabilityConfig` — no Prometheus-specific timeout
environment variables were added. See
[docs/configuration.md](configuration.md#reliability).

## Instant queries (`prometheus_query`)

```python
prometheus_query(query, time=None)
```

Answers "what is this metric's value right now (or at `time`)?" —
current monitored state, not historical automation evidence or a live
connectivity check.

- `query`: PromQL text, validated mechanically (see "PromQL validation"
  below) — never parsed.
- `time`: optional evaluation instant, RFC3339 or Unix timestamp. Omit
  to evaluate at Prometheus's own current time.

## Range queries (`prometheus_query_range`)

```python
prometheus_query_range(query, start, end, step)
```

Answers "how did this metric change over a window?" Bounded **before**
any HTTP request is made — see "Range-query bounding" below. A request
implying, say, 30 days at 1-second resolution is rejected outright, even
though Prometheus itself would accept it.

## Time input

Both an RFC3339 string (`"2026-09-17T12:00:00Z"`) and a plain Unix
timestamp (`1700000000`) are accepted for `time`/`start`/`end` — no
broader natural-language date parser was built. `step` is a plain
number of seconds (not a PromQL duration string like `"15s"`), keeping
range validation simple and unambiguous. The normalized requested
window is always reported back in the result's `query` section as
RFC3339, regardless of which input form was used.

A numeric `time`/`start`/`end`/`step` must be finite — `float("nan")`
and `+inf`/`-inf` are rejected explicitly (`math.isfinite()`), not just
checked against zero/negative. A bare range comparison alone isn't
enough: every comparison against NaN is `False`, so a NaN step would
otherwise silently pass a `step < MIN_RANGE_STEP_SECONDS` check and
later corrupt the range-window/point-density arithmetic, or blow up
timestamp formatting.

## PromQL validation

`mantis.tools.prometheus.validate_promql` does **not** parse PromQL
(out of scope) — only mechanical checks: the query is a string,
non-empty, at most `MAX_PROMQL_CHARS` (1000) characters, and free of
control characters. Normal PromQL syntax — operators, braces, regexes,
quotes, `[5m]` ranges, `by (...)` clauses — is never rejected on content
grounds.

An invalid `query`/`time`/range value returns
`query_error.type == "invalid_input"` as a **normal result**, never a
raised exception — the invalid text is untrusted, model-supplied data
(#14) and must flow through `mantis.security.make_model_safe()` like
any other tool result, not through the runtime's generic last-resort
exception path (which skips that pipeline). No HTTP request is made
when validation fails.

The rejected query and the validation message are themselves bounded
before being echoed back (`_invalid_input_result()`, reusing
`MAX_PROMQL_CHARS`/`MAX_WARNING_CHARS`), and `meta.truncated` is set
when either was shortened. Otherwise a query rejected specifically
*for being too long* would be echoed back at full, unbounded length —
exactly contradicting the point of rejecting it. Semantic-tool bounds
are meant to be the primary control here, with #14's global ceiling
only a final backstop.

## Range-query bounding

| Constant | Default | Purpose |
|---|---|---|
| `MIN_RANGE_STEP_SECONDS` | 1.0 | Smallest allowed step. |
| `MAX_RANGE_SECONDS` | 604800 (7 days) | Largest allowed `end - start` window. |
| `MAX_RANGE_POINTS_PER_SERIES` | 1000 | Largest allowed `(end - start) / step` — the real defense against "30 days at 1-second resolution". |

Validated in this order: `start < end`, `step > 0` and `>=
MIN_RANGE_STEP_SECONDS`, window `<= MAX_RANGE_SECONDS`, implied points
`<= MAX_RANGE_POINTS_PER_SERIES`. All four checks happen before any
HTTP call.

## Cardinality and sample bounding

Prometheus can return huge vectors/matrices in one response. Every
bound is a named constant in `mantis.tools.prometheus`:

| Constant | Default | Applies to |
|---|---|---|
| `MAX_SERIES_RETURNED` | 50 | Series in either a vector or matrix result. |
| `MAX_SAMPLES_PER_SERIES` | 200 | Samples per series in a matrix (range) result. |
| `MAX_TOTAL_SAMPLES` | 2000 | Samples across *all* returned series combined. |
| `MAX_LABELS_PER_SERIES` | 20 | Labels preserved per series. |
| `MAX_LABEL_KEY_CHARS` | 128 | Per label key. |
| `MAX_LABEL_VALUE_CHARS` | 256 | Per label value. |
| `MAX_SAMPLE_VALUE_CHARS` | 256 | Per sample `value` string (vector/matrix/scalar/string) — Prometheus's `string` result type can legitimately be large, and malformed/proxy-controlled data could smuggle a huge value through any sample otherwise. |
| `MAX_WARNING_CHARS` | 500 | Per Prometheus warning string. |
| `MAX_WARNINGS_RETURNED` | 20 | Number of warning strings — separate from `MAX_WARNING_CHARS`, which only bounds each string's length. |

These bounds are applied deterministically *before* #14's global
model-input size ceiling — #14's bound is a final safety backstop, not
the normal Prometheus result-control mechanism. Small and sized for
interactive troubleshooting with local models, not bulk export.

### Truncation correctness

`meta.truncated` means evidence was **actually omitted or completeness
can't be claimed** — never inferred merely from `len(result) ==
MAX_SERIES_RETURNED`. Since Prometheus returns the entire query result
in one HTTP response, Mantis generally *can* know whether local shaping
omitted something, by comparing the raw entry count to what was
actually kept after bounding:

- Exactly `MAX_SERIES_RETURNED` series exist → `truncated = false`.
- `MAX_SERIES_RETURNED + 1` series exist → `truncated = true`.
- Exactly `MAX_SAMPLES_PER_SERIES` samples exist for a series →
  `truncated = false`.
- One more than that → `truncated = true`.
- Exactly `MAX_WARNINGS_RETURNED` warnings exist → `truncated = false`.
- One more than that → `truncated = true`.

This also covers **shortening**, not just dropping: a label key or
value, a sample `value` string, a warning string, or a query error's
`type`/`message`, that gets cut down to its character limit is evidence
being reduced just as much as an omitted series — so
`_bounded_labels()`/`_normalize_sample()`/`_bound_warnings()`/the
query-error branch in `_shape_result()` all set the truncation flag on
that basis too, not only on count. The same applies to malformed
entries dropped during normalization (see "Malformed data" below) and
to a rejected invalid input's own echoed query/message (see "PromQL
validation" above) — any of these reduce or shorten what would
otherwise be shown, which is exactly what `truncated` tracks. See
`mantis.tools.prometheus._normalize_vector`/`_normalize_matrix`/
`_bounded_labels`/`_bound_warnings`/`_normalize_sample` and
`tests/test_prometheus_tools.py`'s exact-cap-vs-over-cap and
oversized-value tests for the direct proof.

### Deterministic ordering

Series are sorted by their normalized label set
(`mantis.tools.prometheus._label_sort_key` — a tuple of sorted
`(key, value)` pairs) before any cap is applied — never upstream
response order or Python dict-insertion order. The same query against
the same data always returns series in the same order, and the same
series always survive an over-the-cap truncation regardless of how
Prometheus happened to order its response. Samples *within* one
series preserve Prometheus's own chronological order.

### Malformed data

A structurally invalid sample (wrong shape, non-numeric timestamp, or a
timestamp so pathologically large that formatting it as a date would
raise `OverflowError`/`OSError`) is skipped rather than crashing the
whole query — see `mantis.tools.prometheus._normalize_sample`. The same
applies one level up: a vector/matrix entry that isn't an object, or
whose `"metric"` isn't itself an object (a string, a list, ...), or —
for a matrix entry — whose `"values"` isn't a list, is dropped entirely
rather than partially normalized or crashing on `.items()`. A dropped
sample, entry, or series counts toward `truncated` the same as a capped
one (see above): `raw_count` is captured **before** any filtering, so a
malformed entry mixed in among otherwise-valid ones is never silently
absorbed into an apparently-complete result — either way, the returned
evidence is incomplete relative to what Prometheus reported.

### Malformed result containers are not empty results

`_shape_result()` enforces the result contract Prometheus's own API
documents for every `resultType`:

- `"vector"`/`"matrix"` — `"result"` must be a list of series.
- `"scalar"`/`"string"` — `"result"` must be a valid
  `[timestamp, value]` pair.
- Anything else — including an unrecognized `resultType`, or a missing
  `resultType` entirely — doesn't match any shape Prometheus documents.

A response that violates its own contract is not the same thing as a
genuinely empty result. For example, a `"result": []`-shaped promise
fulfilled instead as `"result": {"unexpected": "object"}` is not an
empty vector — an empty vector already has real meaning ("no matching
series exist right now"), and silently iterating a malformed container
into what looks like an empty list would let a broken response
masquerade as that meaning. The same reasoning applies to a **missing**
`"result"` key: it is not treated as "no data" either, because
Prometheus's contract for a successful response always includes
`"result"` — only a `"result"` that is actually present, and actually
list-shaped, and actually empty (`"result": []`) counts as genuine
empty evidence. A scalar/string result has no "empty" case at all in
Prometheus's contract, so any value that isn't a valid
`[timestamp, value]` pair is malformed by definition, never normalized
to `value: null` as if the query had simply returned nothing.

Concretely: `_normalize_vector`/`_normalize_matrix` raise
`mantis.tools.prometheus.MalformedResultError` whenever `"result"`
isn't a list (including when it's absent), and `_normalize_sample`
returns a `None` sample for a scalar/string `"result"` that isn't a
valid pair. `_shape_result()` turns every one of these cases —
plus an unrecognized/missing `resultType` — into
`query_error: {"type": "malformed_result", "message": ...}` via the
shared `_malformed_result_response()` helper — a third `query_error.type`
alongside `"invalid_input"` (a Mantis-side rejection of the model's
input) and Prometheus's own `errorType` values: this one means
Prometheus reported success but its own response didn't match its
declared shape. `meta.truncated` is always `true` in this case —
completeness can't be claimed when the container itself couldn't be
interpreted.

## Result shape

```json
{
  "meta": {
    "source_system": "prometheus",
    "query_time": "2026-09-17T17:07:24.653279+00:00",
    "observation_time": null,
    "query_window": null,
    "truncated": false,
    "derived_fields": [],
    "contract_version": "1.0"
  },
  "query": {
    "promql": "up{instance=\"ferros-c01:9100\"}",
    "mode": "instant",
    "time": null
  },
  "result_type": "vector",
  "series": [
    {
      "metric": {
        "__name__": "up",
        "instance": "ferros-c01:9100",
        "job": "node"
      },
      "sample": {
        "timestamp": "2023-11-14T22:13:20+00:00",
        "value": "1"
      }
    }
  ],
  "value": null,
  "warnings": [],
  "query_error": null
}
```

(Real, verified output — generated by running `prometheus_query`
against a mocked HTTP response with the shape above.)

- `series` holds `vector`/`matrix` results — each entry has `metric`
  (bounded labels) plus either a single `sample` (vector) or an
  ordered `samples` list (matrix).
- `value` holds `scalar`/`string` results instead — a compact
  `{"timestamp", "value"}` shape rather than pretending a single value
  is a metric series. `series` stays `[]` for these result types.
- `derived_fields` is always `[]` for Prometheus results: nothing here
  is Mantis-computed interpretation of the data (unlike, say, #28's
  derived event `category`) — every value is Prometheus-reported, only
  bounded, reordered, or reformatted (a raw Unix timestamp becoming an
  ISO 8601 string is a format change, not interpretation).
- **`value` is always a string, never coerced to `float`.** Prometheus
  can legitimately report `"NaN"`, `"+Inf"`, `"-Inf"` — converting those
  to Python floats either loses that information or requires
  reinventing it; the raw string representation is simply safer. It is
  still bounded to `MAX_SAMPLE_VALUE_CHARS`, though — Prometheus's
  `string` result type can legitimately be large, and this bound (not
  #14's global backstop) is the primary control for that.

### `observation_time`

- **Instant query, `vector` result**: `None`. A vector can return
  multiple series, each with its own sample timestamp
  (`series[].sample.timestamp`) — no single batch-level value could
  represent all of them without being misleading, the same reasoning
  `awx_recent_failed_jobs` documents for its job list.
- **Instant query, `scalar`/`string` result**: set to that single
  value's own timestamp — this genuinely is one point-in-time
  observation.
- **Range query, `matrix` result**: always `None`. Many samples across
  the window, each with its own timestamp
  (`series[].samples[].timestamp`) — never invent one misleading
  observation timestamp for a multi-sample range.

## Empty results

A successful query matching nothing is **valid evidence**, not a
failure:

```json
{"result_type": "vector", "series": [], "query_error": null}
```

This must never become "retrieval failure", "host is down", or
"Prometheus unavailable" — it means exactly what it says: no series
currently match that query.

## Query errors vs. retrieval errors

Kept strictly separate:

- **Query/API error** (a PromQL parse or execution failure) —
  Prometheus itself reports this, via HTTP 400 (`bad_data`) or 422
  (`execution`), always with an error-shaped JSON body. Represented as
  `query_error: {"type": ..., "message": ...}` in the result — a normal
  return, never a raised exception, never retried (see
  `mantis.integrations.prometheus._QUERY_ERROR_STATUS_CODES`). **A
  PromQL parse/execution error is not evidence that the monitored
  system is unhealthy** — it's Mantis (or the model) asking Prometheus
  a malformed question.
- **Transport/retrieval failure** (timeout, connection refused, 401,
  403, 429, 502, 503, 504, ...) — raised as a classified
  `mantis.integrations.prometheus.PrometheusError` (an
  `IntegrationError` subclass), handled by `AgentRuntime` exactly like
  any other integration failure (#15): retried where appropriate,
  contributing to the run-local breaker, never silently flattened
  together with a query error into one generic "Prometheus failed."

A malformed envelope — non-JSON, missing/unrecognized `status`, or a
`"data"` value that isn't itself an object (`"data": []` rather than
`"data": {"resultType": ..., "result": ...}`) — also raises a classified
`PrometheusError` rather than letting an unrelated Python exception
(e.g. `AttributeError` from calling `.get()` on a list) leak out
unclassified. See `mantis.integrations.prometheus._parse_envelope`.

A third, narrower case sits between these two: the envelope itself
parses fine (`status="success"`) but `"result"` doesn't match what its
`resultType` promises (e.g. `resultType: "vector"` with `"result":
{"unexpected": "object"}}`, a missing `"result"` key, an invalid
scalar/string pair, or an unrecognized/missing `resultType` itself).
This is represented as `query_error: {"type": "malformed_result", ...}`
— see "Malformed result containers are not empty results" above —
rather than either a `PrometheusError` (the transport layer got a
perfectly good response) or silently becoming an empty result (which
has its own distinct meaning).

Note that Prometheus sometimes uses HTTP 503 for a query timeout
specifically — this module deliberately does **not** special-case that
into a query error. #15's reliability contract treats 503 as a
retryable transient transport signal uniformly across every
integration; carving out an exception here would contradict that
contract for one specific status code. See
`mantis.integrations.prometheus._QUERY_ERROR_STATUS_CODES`'s docstring.

## Warnings

Prometheus may return `warnings` alongside a successful result (e.g. a
query that hit an internal sample limit). Preserved in bounded form —
both the length of each string (`MAX_WARNING_CHARS`) and the number of
warnings returned (`MAX_WARNINGS_RETURNED`) are capped, so a response
with thousands of warnings never reaches the model as thousands of
bounded strings. Never discarded silently, never turned into a query
failure, never dumped unbounded. Warning text is
untrusted external data and flows through the same #14 pipeline as
everything else here.

## Security (#14)

PromQL query text, metric names, label names/values, warning text, and
API error strings are all untrusted external data. Both tools are
registered with `contains_untrusted_text=True`. Prompt-like text is
never stripped from labels/warnings — if Prometheus actually returned
an `instance` label containing `"IGNORE ALL PREVIOUS INSTRUCTIONS"`, it
remains visible as evidence; the existing runtime safety layer
(`mantis.security.make_model_safe()`) makes it model-safe (bounded,
marked untrusted) without deciding it doesn't count as evidence. No
second injection detector was added.

## Reliability (#15)

Every Prometheus request goes through the exact same
`PrometheusClient._get()`/`retry_call()` path as `AWXClient`: explicit
connect/read timeouts capped by the remaining `Deadline`, the shared
`RetryPolicy`, the shared `IntegrationErrorKind` taxonomy, and the same
run-local breaker participation for classified transport failures. No
second retry helper, timeout config family, or breaker abstraction was
introduced. See [docs/reliability.md](reliability.md).

## Prometheus `up` semantics

Be careful interpreting `up`. **`up == 0` means the Prometheus *scrape*
of that target failed** — it does not necessarily mean the host is
powered off, all services on it are down, or the network is
unreachable. Likewise, a missing series can mean the target
disappeared, service discovery configuration changed, the query's
labels no longer match anything, the retention window doesn't cover
the requested time, or other causes entirely unrelated to the target's
actual health.

Mantis does not embed a simplistic root-cause interpretation of `up` (or
any other metric) in the tool itself — that judgment belongs to the
model, informed by this documentation and by correlating `up` with
other evidence (AWX, TCP, other metrics), never by treating a single
scrape-health signal as proof of a specific failure mode.

## Example correlation with historical AWX evidence

The `multi-signal-recovery` and `multi-signal-still-down` evaluation
scenarios (`mantis.eval.fixtures.prometheus`) combine all three
evidence sources: AWX historically observed a `runner_on_unreachable`
failure reaching `ferros-c01:22`; a Prometheus range query for
`up{instance="ferros-c01:9100"}` over roughly the same window shows the
scrape drop to 0 and then either recover or stay down; a current
`check_tcp_connectivity` probe either succeeds or still fails. Golden
behavior in both cases:

- Distinguishes historical (AWX), monitoring-window (Prometheus), and
  current-state (TCP) evidence explicitly.
- May note a timeline correlation between the AWX failure and the
  Prometheus scrape gap — but never treats that correlation as proof of
  a shared cause.
- Never asserts an unsupported specific cause ("the firewall
  definitely blocked it", "the switch failed", "the host rebooted",
  "sshd crashed") — even when all three signals agree.
- Never treats `up == 0` as proof the host itself was completely down.
- Never claims the incident is permanently fixed from one current
  check at one vantage point.

See `tests/eval/test_prometheus_scenarios.py` for the deterministic
good/bad-answer scoring tests, and [docs/evaluation.md](evaluation.md)
for how to run a scenario against a live model.

## Example PromQL

```promql
up{instance="ferros-c01:9100"}
```
Is the node-exporter scrape for this host currently succeeding?

```promql
rate(node_network_receive_errs_total[5m])
```
Per-second rate of network receive errors over the last 5 minutes — a
concrete signal for correlating with a reported connectivity problem.

```promql
increase(node_boot_time_seconds[1h])
```
Whether `node_boot_time_seconds` changed in the last hour — a
non-zero result is evidence the host rebooted (`node_boot_time_seconds`
is a fixed boot timestamp that only changes on reboot), useful for
checking a hypothesis rather than asserting one.

## Non-goals

Deliberately excluded (see issue #9's guardrails): alert-rule
management, recording-rule management, target/scrape configuration,
remote write, push behavior, Prometheus administration, generic HTTP
fetching or arbitrary API browsing, a PromQL parser, and System
Troubleshooter orchestration (#11) — both tools are registered in the
shared registry, read-only and reusable, ready for a future
multi-tool agent to allowlist, not tied to one today.
