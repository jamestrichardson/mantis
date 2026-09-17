# Reliability

Mantis is moving from one remote integration (AWX) to several
(Prometheus, Loki, network, Git, Kubernetes). This page is the shared
reliability contract every integration adopts — explicit timeouts, one
failure taxonomy, safe bounded retries, run/tool deadlines, and a
run-local failure guard — implemented once in `mantis.reliability` and
`mantis.runtime.AgentRuntime`, not reinvented per integration. If you're
adding a new integration, this page (plus `mantis.reliability`'s module
docstring) is what you adopt instead of writing your own retry loop.

This is deliberately small. **Not** a distributed resilience platform —
no persistent/global circuit breaker, no Redis/database state, no
background health probes, no async task-queue infrastructure. See
"Non-goals" in issue #15 for the full exclusion list.

## Retry budget vs. tool-call budget

Four mechanisms are easy to conflate. Keep them separate:

| Mechanism | What it counts | Owned by |
|---|---|---|
| `AgentRuntime.tool_call_budget` | Model-requested *successful* tool calls allowed in one run | `AgentRuntime` |
| retry/attempt budget | Transport attempts *inside one logical tool call* | `mantis.reliability.RetryPolicy` |
| duplicate-call replay | Reusing a cached result for an exact-repeat model request — never triggers another integration attempt | `AgentRuntime` |
| run-local short circuit | Fail-fast after repeated transient failures *within one run* — no request at all | `mantis.reliability.RunLocalBreaker` |

A model asking for the same tool call twice is **one** entry against
`tool_call_budget`'s cousin concept (successful calls) and **zero**
additional integration attempts (replay). A single tool call that fails
twice before succeeding is **one** tool call and **three** integration
attempts (the retry budget). These numbers are independent — nothing
here makes a duplicate model request consume retry budget, and nothing
makes a retried request look like two "tool calls" to the model.

## Worked examples

**Example A — transient failure, then success:**

```text
logical AWX read
  attempt 1 -> 503
  backoff
  attempt 2 -> 200
=> one tool call, two integration attempts
```

**Example B — retry budget exhausted:**

```text
logical AWX read
  attempt 1 -> timeout
  attempt 2 -> timeout
  attempt 3 -> timeout
=> retry budget exhausted
=> classified integration failure returned (ToolErrorKind.TIMEOUT)
```

Both are real, verified behavior — see
`tests/test_awx_tools.py::test_transient_failure_then_success_consumes_two_attempts_one_logical_call`
and `::test_retry_exhaustion_raises_after_named_max_attempts`.

## Defaults

| Setting | Default | Env var |
|---|---|---|
| Connect timeout | 5.0s | `MANTIS_HTTP_CONNECT_TIMEOUT_SECONDS` |
| Read timeout | 25.0s | `MANTIS_HTTP_READ_TIMEOUT_SECONDS` |
| Max attempts (retry budget) | 3 | `MANTIS_RETRY_MAX_ATTEMPTS` |
| Backoff base | 0.5s | `MANTIS_RETRY_BACKOFF_BASE_SECONDS` |
| Backoff cap | 8.0s | `MANTIS_RETRY_BACKOFF_CAP_SECONDS` |
| Per-tool-call deadline | 45.0s | `MANTIS_TOOL_TIMEOUT_SECONDS` |
| Overall run deadline | 300.0s | `MANTIS_RUN_TIMEOUT_SECONDS` |
| Run-local short-circuit threshold | 3 | `MANTIS_SHORT_CIRCUIT_THRESHOLD` |

All eight are `mantis.config.ReliabilityConfig` fields. `AgentRuntime`
and each integration client (e.g. `AWXClient`) each construct their own
`ReliabilityConfig.from_env()` instance rather than sharing one object,
but all of them use the same schema and the same environment-variable
defaults, so reliability behavior is configured consistently across the
runtime and every integration rather than each one inventing its own
knobs.

`ReliabilityConfig` validates ranges at construction (`__post_init__`),
not just types — a value that parses fine but is nonsensical (a zero or
negative timeout, `retry_max_attempts=0`, a `retry_backoff_cap_seconds`
below `retry_backoff_base_seconds`, ...) raises `ConfigurationError`
immediately rather than surfacing later as a confusing internal failure
(e.g. `retry_max_attempts=0` used to skip `retry_call()`'s loop entirely
with no attempt ever made). This applies to every construction path,
including a test or caller building one directly, not only
`.from_env()`.

### Rationale

- **Connect (5s) shorter than read (25s):** a healthy service should
  accept a TCP connection almost immediately; a slow *connect* is a much
  stronger unhealthy signal than a slow *response* (AWX can legitimately
  take a while to assemble a large job list). Conservative enough for
  interactive troubleshooting, short enough that a genuinely dead
  dependency doesn't stall a run for the runtime's own deadlines to
  catch it.
- **3 attempts, exponential-with-full-jitter backoff capped at 8s:**
  enough to ride out a brief blip (a load balancer failover, a single
  dropped connection) without turning a dead service into a long hang.
  Jitter avoids every concurrent caller retrying in lockstep against an
  already-struggling service.
- **45s per tool call:** comfortably covers a full 3-attempt retry
  sequence against AWX's 25s read timeout in the worst realistic case
  (one timeout, one retry, one success — not three full 25s timeouts
  back to back; the deadline check between attempts stops that well
  before the third full timeout would complete, see "What this does and
  does not guarantee" below), while still bounding a single tool call to
  a fraction of the overall run.
- **300s (5 min) per run:** generous for a multi-iteration
  investigation with several tool calls, bounded well short of "the
  operator gave up and closed the terminal."
- **Short-circuit threshold of 3:** small enough to stop hammering a
  visibly-down integration well before a run's tool-call budget would
  naturally end the conversation, large enough that one or two isolated
  blips don't trip it.

## Shared failure taxonomy

`mantis.reliability.IntegrationErrorKind` — every integration classifies
its failures into this vocabulary; `mantis.reliability.IntegrationError`
carries the classification plus a bounded (≤500 char) diagnostic message
(never a full response body, never a credential — integration code is
responsible for not embedding one).

| Kind | Retryable | Example HTTP status | Maps to `ToolErrorKind` |
|---|---|---|---|
| `timeout` | ✅ | connect/read timeout (no response) | `TIMEOUT` |
| `connection` | ✅ | connection refused/reset (no response) | `RETRIEVAL_ERROR` |
| `rate_limit` | ✅ | 429 | `RATE_LIMITED` |
| `server_error` | ✅ | 500, 502, 503, 504 | `UPSTREAM_ERROR` |
| `authentication` | ❌ | 401 | `AUTH_ERROR` |
| `authorization` | ❌ | 403 | `AUTH_ERROR` |
| `not_found` | ❌ | 404 | `NOT_FOUND` |
| `bad_request` | ❌ | 400, 422, other 4xx | `UPSTREAM_ERROR` |
| `unknown` | ❌ | anything unclassifiable | `UNKNOWN` |

`IntegrationErrorKind` is the richer, retry/short-circuit-decision
vocabulary; `mantis.contracts.ToolErrorKind` (#23) remains the stable,
tool-result-facing contract — `IntegrationError.to_tool_error_kind()` is
the one place that translates between them, so a tool's result shape
never needs to change as the internal taxonomy gets more precise.
`AWXError`/`AWXStdoutError` (`mantis.integrations.awx`) both subclass
`IntegrationError` — `AgentRuntime` catches the shared base class
generically and never imports an AWX-specific type, which is what lets
a future Prometheus/Loki client plug into the exact same runtime
handling by subclassing `IntegrationError` the same way.

Classification never depends on string-matching exception text: HTTP
statuses go through `classify_http_status()`, transport-level exceptions
(no response received at all) through `classify_httpx_exception()` —
both are generic, reusable by any future HTTP-based integration.

## Safe retries

`mantis.reliability.retry_call()` wraps a zero-argument thunk (one
integration read) with `mantis.reliability.RetryPolicy`. Only a
retryable-classified `IntegrationError` triggers a retry — anything else
(a non-retryable `IntegrationError`, or any other exception) propagates
on the first attempt, no retry budget consumed. **Never used for
mutating operations** — retry safety depends entirely on the operation
being idempotent, and every tool `mantis.reliability` retries today is a
GET.

Backoff is exponential with full jitter (`random.uniform(0, min(base *
2^(attempt-1), cap))`), or the server's own `Retry-After` value
(capped at `backoff_cap_seconds`) when the response is a 429 that
provided one. `sleep` is injectable (defaults to real `time.sleep`) so
tests never really wait.

## Run/tool deadlines

Two `mantis.reliability.Deadline`s, both using monotonic time:

- **Per-tool-call** (`ReliabilityConfig.tool_timeout_seconds`) — bounded
  by both its own named default and whatever remains of the run budget
  (`min(tool_timeout_seconds, run_deadline.remaining())`), so a tool
  call late in a long run never gets a full fresh budget when the run
  itself is nearly out of time.
- **Overall run** (`ReliabilityConfig.run_timeout_seconds`) — checked at
  the top of every `AgentRuntime` iteration; once exhausted, the runtime
  raises `mantis.runtime.RunDeadlineExceededError` rather than starting
  another model/tool round-trip. This never depends on the model
  cooperating — it's enforced entirely in Python, before any new work
  starts.

A tool handler that wants its integration's retry loop to respect the
remaining tool-call budget declares a keyword-only `_deadline` parameter
(same leading-underscore, never-model-settable convention as `_client` —
see `mantis.tools.awx.awx_recent_failed_jobs`); `AgentRuntime` passes the
computed `Deadline` automatically to any handler that declares it.

`AWXClient` threads that `Deadline` one step further: the effective
per-request connect/read timeout it configures on `httpx.Client` is
capped at whatever remains of the deadline (`min(configured_timeout,
deadline.remaining())`), recomputed on every retry attempt. A request
starting with only 2s of tool budget left is never still configured with
the full 25s default read timeout — this narrows (though, per the
guarantee below, can never fully close) the gap between the advertised
budget and one blocking call's actual worst-case duration.

### What this does and does not guarantee

Be precise about this, because it's easy to overstate: **Python cannot
forcibly interrupt arbitrary synchronous code.** A deadline here means
Mantis will not *start* a new attempt, backoff sleep, or iteration past
it — it does not mean an already-in-flight blocking call gets cut off
the instant the deadline passes. If a single HTTP request hangs for its
full read timeout, that request still runs to completion (or timeout)
before the next deadline check happens. The actual worst-case duration
of any one blocking call is bounded separately, by that call's own
connect/read timeout — this is exactly why explicit HTTP timeouts and
deadline-based scheduling are two different, complementary mechanisms in
this contract, not one. Do not design an integration assuming a deadline
will preempt a call already in progress; it won't, and this page will
not pretend otherwise.

## Run-local short circuit

`mantis.reliability.RunLocalBreaker` — a plain in-memory guard, **not**
a circuit breaker in the persistent/distributed sense. A fresh instance
is created at the start of every `AgentRuntime.run()` call and discarded
at the end; nothing survives to the next run, nothing is shared across
processes.

```text
run starts
  AWX fails (classified transient/service failure)
  AWX fails again
  threshold reached (default 3)

later AWX tool call, same run
  -> fail fast, no HTTP request, outcome="short_circuited"

next AgentRuntime.run()
  -> fresh state, first call reaches the handler normally
```

Only kinds in `mantis.reliability.RETRYABLE_KINDS` (`timeout`,
`connection`, `rate_limit`, `server_error`) count toward the threshold —
a `not_found` (the user asked for something that genuinely doesn't
exist) or `bad_request` (a malformed query) never indicates the
integration itself is unavailable, so neither ever trips it, no matter
how many times it happens in a run. A success resets the count for that
`source_system` to zero.

### Partial-success tools and the breaker

A tool handler is not required to raise `IntegrationError` on every
failure. `awx_recent_failed_jobs`, for instance, deliberately swallows a
single job's stdout-retrieval failure into that job's own
`stdout_retrieval_error` rather than failing the whole call — losing one
job's evidence shouldn't discard the other jobs' evidence too. Left
alone, this would make the breaker structurally blind to a degrading
integration: the swallowed failure never reaches the `except
IntegrationError` branch in `_dispatch_tool_call`, and the call's own
unconditional "it returned, so it succeeded" would then reset the
breaker's count to zero on top of that — meaning a run could make many
retried, failing stdout requests against an unhealthy AWX across several
jobs and several tool calls while the breaker never once opens.

A handler that degrades failures this way declares a keyword-only
`_reliability_report` parameter (same leading-underscore,
never-model-settable convention as `_client`/`_deadline`); `AgentRuntime`
passes a callback bound to the current call's tool category and calls
into `RunLocalBreaker.record_failure()` on the handler's behalf. Calling
it also marks the call as having had a degraded failure, so the
success path at the end of `_dispatch_tool_call` skips
`record_success()` for that call instead of wiping the just-recorded
failure back out. See `mantis.tools.awx._summarize_job` for the pattern.

That closes the gap *between* tool calls, but a handler iterating over
several items in one call (one stdout fetch per job, in
`awx_recent_failed_jobs`) could still keep hammering an integration
*within* that same call after the threshold is crossed — the breaker
wouldn't affect anything until the *next* tool call. `_reliability_report`
returns whether the breaker is now open for the call's category, so a
handler can stop issuing further requests as soon as it opens instead of
finishing out every remaining item first. `awx_recent_failed_jobs` uses
this to stop requesting stdout for any jobs after the one whose failure
tripped the breaker — those jobs get a `stdout_retrieval_error` that says
retrieval was skipped, distinct from one that was attempted and failed
(see `mantis.tools.awx._skipped_job_summary`).

## Tool-facing result behavior

A retrieval/integration failure is never phrased as if it were a fact
about the target system:

> ❌ "Host is unreachable because AWX timed out."
>
> ✅ "Mantis could not retrieve AWX evidence because the AWX API request
> timed out. This does not establish the target host's current
> reachability."

Every classified-failure tool message includes exactly this kind of
distinguishing note (see `AgentRuntime._dispatch_tool_call`) and goes
through the same #14 model-input safety pipeline
(`mantis.security.make_model_safe()`) as a successful result — redacted,
bounded, and marked untrusted evidence, never a special unprotected
path just because it's an error.

## Observability

Structured events (`mantis.observability.logging`, same system as
everywhere else — no bespoke reliability telemetry store):

| Event | Emitted when |
|---|---|
| `mantis_integration_retry` | Every failed integration attempt, with `attempt`, `max_attempts`, `error_kind`, `will_retry` — the last one for a given call with `will_retry=false` (and the failure was itself retryable) is retry exhaustion. |
| `mantis_tool_call` (`outcome="integration_error"`) | A classified `IntegrationError` propagated out of a tool handler. |
| `mantis_tool_call` (`outcome="short_circuited"`) | The run-local breaker was open for that tool's category. |
| `mantis_tool_call` (`outcome="budget_exceeded"`) | Either the per-tool-call or the run deadline was already exhausted before the handler could start (`error_kind` distinguishes `tool_deadline_exceeded` from `run_deadline_exceeded`). |
| `mantis_run_deadline_exceeded` | The run deadline was exhausted at the top of an iteration, before a new model call. |
| `mantis_run_failed` (`outcome="run_deadline_exceeded"`) | The run ultimately ended via `RunDeadlineExceededError`. |

Existing metrics (`mantis.observability.metrics`) are reused, not
duplicated: `mantis_tool_calls_total{result=...}` covers
`short_circuited`/`budget_exceeded`/`integration_error` the same way it
already covered `ok`/`error`; `mantis_tool_errors_total{error_kind=...}`
uses the classified `IntegrationErrorKind` value (e.g. `upstream_error`)
instead of a raw exception class name when the failure is a classified
one — lower cardinality, more useful than before. No new metric was
added purely to count retry attempts; the `mantis_integration_retry` log
event already gives per-attempt visibility, and adding a
per-attempt-cardinality metric on top would risk exactly the kind of
label growth this contract is trying to avoid. `error_kind` and
`source_system` are both small, bounded vocabularies — never a `run_id`,
hostname, job ID, or raw exception message in a label.

## Adding a new integration

1. Build your HTTP client the way `mantis.integrations.awx.AWXClient`
   does: explicit `httpx.Timeout(connect=..., read=...)` from
   `ReliabilityConfig`, an injectable `sleep` field, requests wrapped in
   `retry_call()`.
2. Raise a client-specific exception that subclasses
   `mantis.reliability.IntegrationError` (see `AWXError`) — never a bare
   `RuntimeError` — with a real `kind` from `classify_http_status()` /
   `classify_httpx_exception()`.
3. In your tool function, catch that exception where a failure should
   degrade gracefully into the result (see
   `mantis.tools.awx._summarize_job`'s per-job stdout handling) using
   `exc.to_tool_error_kind()`, or let it propagate where the whole
   result can't be produced without it (see `awx_recent_failed_jobs`'s
   `list_jobs` call) — `AgentRuntime` handles the propagating case
   generically via the shared `IntegrationError` base class. **If you
   take the degrade-gracefully path, also accept a keyword-only
   `_reliability_report: Callable[[IntegrationErrorKind], bool] | None =
   None` parameter and call it with the exception's `kind`** — otherwise
   the run-local breaker never learns about that failure (see "Run-local
   short circuit" above). If your tool degrades failures across several
   items in a loop (one request per item), check the callback's return
   value (`True` once the breaker opens) and stop issuing further
   requests for that same call instead of finishing out every item —
   see "Partial-success tools and the breaker" above.
4. If you want your retries to respect the caller's remaining tool-call
   budget, add a keyword-only `_deadline: Deadline | None = None`
   parameter to your tool function and thread it into your client calls.
5. You do not need to implement your own retry loop, timeout
   configuration, error taxonomy, run deadline, or short-circuit logic —
   all of it is inherited automatically once you're raising
   `IntegrationError` subclasses and (optionally) accepting `_deadline`
   / `_reliability_report`.
