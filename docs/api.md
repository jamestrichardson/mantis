# Mantis API

Mantis runs as a persistent HTTP service (`mantis serve`, #21) and is
invoked exclusively through this versioned API (#83) — by the official
CLI, and later by the portal (#85) and MCP client (#93). There is no
supported local/in-process execution path: the CLI is an HTTP client
only (see [CLI relationship](#cli-relationship)).

```text
CLI / Portal / MCP  ->  FastAPI app  ->  AgentCatalog  ->  InvocationService  ->  AgentRuntime
```

## Starting the service

```bash
mantis serve
```

Reads its configuration from the environment (see
[Configuration](#configuration) below) and binds
`MANTIS_API_HOST`/`MANTIS_API_PORT` (default `0.0.0.0:8080`). Blocks
until it receives `SIGTERM`/`SIGINT`, then shuts down gracefully — see
[Graceful shutdown](deployment.md#graceful-shutdown).

The production container runs this as PID1 by default (`CMD ["serve"]`
in the `Dockerfile`) — see [docs/deployment.md](deployment.md).

## Base URL and versioning

All product API endpoints are namespaced under `/api/v1`. Health/
readiness endpoints (`/healthz`, `/readyz`) are unversioned operational
endpoints, served at the root. A breaking change to the `/api/v1`
contract will get an explicit new version rather than silently mutating
behavior clients depend on.

Locally, the base URL is whatever you bind `mantis serve` to — for
example `http://localhost:8080`.

## Authentication

The Mantis API is the client-facing security boundary — LiteLLM/AWX/
Kubernetes/Prometheus/Loki credentials stay entirely server-side and are
never needed by, or exposed to, an API client (see
[Security boundary](#security-boundary-mantis-api-vs-integrations)
below).

Two modes, set via `MANTIS_API_AUTH_MODE`:

- **`bearer_token`** (the default): every `/api/v1/*` request must carry
  `Authorization: Bearer <MANTIS_API_TOKEN>`. Missing/invalid tokens get
  a stable `401`. This is the only mode that requires
  `MANTIS_API_TOKEN` to be set — the server refuses to start otherwise
  (fail fast, not a silent open door).
- **`disabled`**: no authentication is enforced. This must be selected
  **explicitly** (`MANTIS_API_AUTH_MODE=disabled`) — it is never the
  default and never silently chosen just because a token happens to be
  unset. Selecting it logs a loud warning at startup
  (`mantis_api_auth_disabled`). Use only for local development, never in
  a shared or production deployment.

`/healthz` and `/readyz` are **always unauthenticated**, in both modes —
a container orchestrator's health check should never need a credential.

```bash
export MANTIS_API_URL=http://localhost:8080
export MANTIS_API_TOKEN=your-token-here

curl -s "$MANTIS_API_URL/api/v1/agents" \
  -H "Authorization: Bearer $MANTIS_API_TOKEN"
```

## Transport security (TLS)

Mantis's FastAPI process speaks plain HTTP — it does not terminate TLS
itself, and the bearer token above has no protection in transit beyond
whatever network path it travels. **Never expose `mantis serve`'s port
directly on a public or otherwise untrusted network interface.** The
reference standalone deployment (`deploy/standalone/compose.yaml`)
binds the published API port to `127.0.0.1` on the host specifically so
this can't happen by accident:

```text
remote client
     |
   HTTPS
     |
reverse proxy / ingress   (terminates TLS)
     |
127.0.0.1:8080
     |
  mantis serve
```

Put a TLS-terminating reverse proxy (nginx, Caddy, Traefik, a cloud
load balancer, ...) in front of the localhost-bound port for any access
beyond the deployment host itself, and forward
`Authorization` through unmodified. If Mantis only ever needs to be
reached from the same host or a network you already trust as a whole
(no reverse proxy in the path), that trust boundary should be explicit
in your own deployment notes, not assumed silently.

## Health and readiness

Both are cheap, bounded, unauthenticated, and never call a model, agent,
or integration — an AWX/LiteLLM/Kubernetes/Prometheus/Loki outage never
fails either one.

### `GET /healthz` — liveness

"The process and HTTP event loop are alive." Always `200` while the
process can serve requests at all:

```json
{"status": "ok"}
```

### `GET /readyz` — readiness

"Mandatory local startup is complete and the process is currently
accepting new runs." Checks only local/in-process state — configuration
already parsed, the agent catalog constructed, the invocation service
initialized — never a live probe of AWX/LiteLLM/Kubernetes/Prometheus/
Loki. A downstream integration outage shows up as an individual run's
failure (see [Invoking an agent](#invoking-an-agent)), not as `/readyz`
flapping.

```json
{"status": "ready", "reason": null}
```

Before startup completes, or once shutdown has begun, `/readyz` returns
`503`:

```json
{"status": "not_ready", "reason": "starting_up"}
```

```json
{"status": "not_ready", "reason": "shutting_down"}
```

`POST /api/v1/runs` is rejected with the same `not_ready` condition
during either window — see [Graceful shutdown](deployment.md#graceful-shutdown).

## Listing agents

```bash
curl -s "$MANTIS_API_URL/api/v1/agents" -H "Authorization: Bearer $MANTIS_API_TOKEN"
```

```json
{
  "agents": [
    {
      "id": "awx-troubleshooter",
      "display_name": "AWX Troubleshooter",
      "description": "Investigates recent failed AWX automation jobs and produces an evidence-based summary, distinguishing AWX's own reported failures from Mantis retrieval errors.",
      "read_only": true,
      "available": true,
      "unavailable_reason": null
    },
    {
      "id": "system-troubleshooter",
      "display_name": "System Troubleshooter",
      "description": "Correlates historical AWX evidence, current-state TCP connectivity, time-series Prometheus data, and Loki logs to investigate a system-level issue.",
      "read_only": true,
      "available": true,
      "unavailable_reason": null
    },
    {
      "id": "incident-triage",
      "display_name": "Incident Triage",
      "description": "Reviews a specific incident over an explicit time window, correlating AWX, network, Prometheus, Loki, Kubernetes, and recent Git history into a time-ordered evidence timeline with explicit evidence coverage -- distinct from System Troubleshooter's open-ended diagnosis.",
      "read_only": true,
      "available": true,
      "unavailable_reason": null
    }
  ]
}
```

This list is real: it comes from `mantis.api.catalog.build_default_catalog()`,
which points directly at the same agent modules the CLI used to import
and run in-process — nothing here is placeholder metadata. `available`
reflects a cheap, local check only (can this agent's
`AgentRuntime` even be constructed from current configuration — e.g. is
`LITELLM_URL` set) — never a live model/integration call.
`unavailable_reason`, when present, is a stable, low-cardinality code
(e.g. `"misconfigured"`), never a raw error message.

Never exposed here: system prompts, provider model IDs, integration
configuration, credentials, filesystem paths, or internal Python class
names.

## Invoking an agent

```bash
curl -s -X POST "$MANTIS_API_URL/api/v1/runs" \
  -H "Authorization: Bearer $MANTIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent": "system-troubleshooter", "prompt": "Investigate why payment-api is unhealthy"}'
```

Request fields: `agent` (a catalog ID) and `prompt` (bounded to 4,000
characters). No other field is accepted — an unknown field (a tool
allowlist override, a model ID override, credentials, ...) is rejected
with `422`, never silently ignored. There is no way for a caller to
change which tools an agent can use, which provider model it calls, or
to supply integration credentials — those all remain server-side
configuration.

### Successful run

```json
{
  "run_id": "3f9a1c2e4b6d4f0aa2c8e6d1b7a90123",
  "agent": "system-troubleshooter",
  "outcome": "success",
  "output": "AWX previously observed job 7301 fail reaching ferros-c01 on port 22 (no route to host) at 03:14 UTC. A current TCP check to the same host/port now succeeds...",
  "error": null,
  "started_at": "2026-09-18T12:00:00+00:00",
  "finished_at": "2026-09-18T12:00:04+00:00",
  "duration_ms": 4231
}
```

`run_id` is generated **before** the agent actually executes and is the
same ID that correlates every `mantis_run_started`/`mantis_tool_call`/
`mantis_model_call` structured log line this run produces (see
[docs/observability.md](observability.md)) — grep your logs for it to
see exactly what the agent did.

### A run the agent itself couldn't complete

`POST /api/v1/runs` still returns **HTTP 200** here: the API request —
"attempt this invocation" — succeeded. The *agent run* is what failed,
which the response's `outcome`/`error` fields describe:

```json
{
  "run_id": "8b2d4e6f1a3c4d5e9f0a1b2c3d4e5f60",
  "agent": "system-troubleshooter",
  "outcome": "error",
  "output": null,
  "error": {
    "kind": "max_iterations",
    "message": "The agent could not produce a final answer within its iteration limit."
  },
  "started_at": "2026-09-18T12:05:00+00:00",
  "finished_at": "2026-09-18T12:05:30+00:00",
  "duration_ms": 30000
}
```

`error.kind` values: `max_iterations` (the model/tool loop never
converged — see `mantis.runtime.MaxIterationsExceededError`),
`run_timeout` (the run's configured time budget, #15, was exceeded),
`model_routing_exhausted` (#16: every configured route — primary and
any fallback aliases — failed for one logical model call; see
`mantis.runtime.ModelRoutingExhaustedError`), `model_provider_error`
(LiteLLM/the upstream model provider could not complete the request —
a single-route policy's only attempt failed, so there was no
fallback to exhaust), `server_configuration_error` (a required
integration is misconfigured — discovered only once a tool actually
needed it; contrast with the *agent-unavailable* rejection below, which
catches the common case up front), `internal_error` (anything else
unexpected). `error.message` is always a short, safe, fixed string —
never a stack trace, provider response body, file path, or credential.
The real exception is logged server-side (correlated by `run_id`) for
operator diagnosis.

### Rejections before an agent ever runs

These use a different, smaller envelope — no `run_id` field except
where noted — and a non-200 status:

```json
{"error": {"type": "unknown_agent", "message": "Unknown agent: 'nope'", "run_id": null}}
```

| Status | `error.type` | Meaning |
|---|---|---|
| 401 | `unauthenticated` | Missing/invalid bearer token. |
| 404 | `unknown_agent` | `agent` doesn't match any catalog entry. |
| 409 | `agent_unavailable` | `agent` is real but can't currently be constructed (e.g. missing required server config). |
| 422 | `validation_error` | Request failed schema validation (missing/oversized field, unknown field). |
| 429 | `overloaded` | The server is at `MANTIS_API_MAX_CONCURRENT_RUNS` — see [Concurrency and overload](#concurrency-and-overload). `run_id` is set here: a run ID is assigned before the concurrency check, so even a rejected attempt is correlatable in logs. |
| 503 | `not_ready` | The service is starting up or shutting down — see [Health and readiness](#health-and-readiness). |
| 500 | `internal_error` | An unexpected server-side bug. Logged server-side; never a stack trace in the response. |

## Run IDs

A run ID is a fresh, opaque identifier assigned at the API boundary
**before** the agent is invoked (`mantis.api.invocation.InvocationService.invoke`)
and passed straight into `AgentRuntime.run(prompt, run_id=...)`, so it's
the same ID threaded through every log line that run produces. It is
never used as a Prometheus label (see
[docs/observability.md](observability.md#label-cardinality-policy)) —
correlate by `run_id` in logs, not in metrics.

There is no persistent, queryable run history yet — a `run_id` is only
useful for correlating *that* run's own log lines while they're still
retained by your log backend. Persistent `GET /api/v1/runs/{run_id}`
history lookup is **#84**, not this API; #83 deliberately does not fake
that capability with an ephemeral in-memory lookup.

## Concurrency and overload

`MANTIS_API_MAX_CONCURRENT_RUNS` (default `4`) bounds how many agent
runs may execute at once, process-locally — a plain in-memory counter, no
Redis, no task queue. When saturated, a new `POST /api/v1/runs` is
rejected **immediately** with `429` — there is no bounded or unbounded
wait/queue. Retry after a short backoff.

## Timeout semantics

Several distinct timeouts, easy to conflate:

1. **Your HTTP client's own request timeout** — how long *you're*
   willing to wait for a response. Set this generously: a real
   multi-tool investigation can legitimately take tens of seconds. The
   official CLI defaults to `MANTIS_API_CLIENT_READ_TIMEOUT_SECONDS=340`,
   above the *nominal* `300`s Mantis run deadline below. This normally
   lets the server return a classified `run_timeout` first — but it is
   **not a hard guarantee**: see the caveat immediately below.
2. **The Mantis run deadline** (`MANTIS_RUN_TIMEOUT_SECONDS`, #15,
   default `300`) — checked *between* iterations of `AgentRuntime`'s
   loop (before starting the next model call or dispatching the next
   tool call), producing `outcome="error"`, `error.kind="run_timeout"`
   when exceeded — a normal `200` response, not a timeout at the HTTP
   layer. Critically, this deadline does **not** preempt a call already
   in flight when it's checked: an already-running model or integration
   call can still outlive it, in which case the run simply finishes
   later than 300s and the 340s client timeout is what actually decides
   whether the caller sees a response or `ApiTimeoutError` first. See
   [docs/reliability.md](reliability.md) for why Mantis deadlines are
   checked-between-steps, never preemptive of an in-flight blocking
   call.
3. **Downstream integration HTTP timeouts** (`MANTIS_HTTP_*_TIMEOUT_SECONDS`,
   #15) — bound each individual AWX/Prometheus/Loki/Kubernetes HTTP
   call, further capped at whatever remains of the run deadline above;
   a slow integration call fails as a classified integration error the
   agent can reason about rather than hanging the request indefinitely.
   **These do not apply to the model call.** The LiteLLM/OpenAI-compatible
   client (`mantis.runtime.build_openai_client`) is currently constructed
   with no explicit timeout at all, so a model call falls back to the
   OpenAI SDK's own built-in default client timeout, independent of
   `MANTIS_RUN_TIMEOUT_SECONDS`/`MANTIS_HTTP_*_TIMEOUT_SECONDS` alike.
   Deriving a model-call timeout from the remaining run budget is a
   reasonable future reliability improvement, not something #95
   implements.

**If your HTTP client disconnects before the server responds:** the
current implementation is a plain synchronous request/response — the
server does not currently detect or cancel the in-progress agent run
just because the caller went away. The run continues to completion (or
its own timeout) server-side; its result is simply never delivered
anywhere. This is an intentionally honest, un-clever limitation for the
first implementation, not a hidden feature — deliberate
cancellation/streaming semantics are groundwork for #84/#93, not solved
here.

## Concurrency/overload and timeouts, together

A `429` means "don't retry immediately, retry shortly." A client-side
timeout means "we gave up waiting, but the run may still be in
progress" (see above) — don't assume a timed-out request definitely
failed or definitely succeeded server-side.

## Graceful shutdown

See [docs/deployment.md](deployment.md#graceful-shutdown) for the exact
signal-to-exit sequence. In short: on `SIGTERM`/`SIGINT`, `/readyz`
flips to `not_ready` (`reason: "shutting_down"`) **synchronously, in
the same instant** the signal is handled — not merely "eventually" —
and `POST /api/v1/runs` starts returning `503`/`not_ready` from that
same moment, since both derive from the one flag that flip sets. A run
already in flight gets up to `MANTIS_API_SHUTDOWN_GRACE_PERIOD_SECONDS`
(default `30`) to finish; if it hasn't by then, the process still exits
on schedule regardless — that run is abandoned, not extended, so
"exits predictably after the configured grace period" holds even for a
genuinely stuck call (Mantis cannot forcibly stop synchronous work
already in progress; see [docs/reliability.md](reliability.md)).

## CLI relationship

The official `mantis` CLI is an **HTTP client only** for agent
invocation — it never constructs `AgentRuntime`, executes a tool, loads
an integration credential, or calls LiteLLM directly, and it never
silently falls back to local execution if the API is unreachable:

```bash
mantis agents                                          # GET /api/v1/agents
mantis run system-troubleshooter "Investigate ..."      # POST /api/v1/runs
mantis system-troubleshooter "Investigate ..."          # same call, ergonomic wrapper
mantis awx-troubleshooter "Show recent failures"        # same call, ergonomic wrapper
mantis run incident-triage "Investigate the incident affecting ... between ... and ..."  # POST /api/v1/runs, no dedicated wrapper (yet)
```

Configure the client with:

```bash
export MANTIS_API_URL=http://localhost:8080   # defaults to this if unset
export MANTIS_API_TOKEN=your-token-here
```

If the API is unreachable, times out, rejects the token, or returns a
server error, the CLI prints a specific, clear error and exits non-zero
— never a partial/local result. `mantis eval ...` is unaffected by any
of this: it's local development/evaluation tooling for testing agent
scenarios directly against a configured LiteLLM backend, not part of the
`/api/v1` surface.

## Security boundary: Mantis API vs. integrations

| | Mantis API (`MANTIS_API_*`) | Server-side integrations (`LITELLM_*`, `AWX_*`, `MANTIS_KUBERNETES_*`, `MANTIS_PROMETHEUS_*`, `MANTIS_LOKI_*`) |
|---|---|---|
| Who holds the credential | Any authorized API client (CLI, portal, MCP) | Only the `mantis serve` process |
| What it authorizes | "May call this Mantis service at all" | "May Mantis itself reach this specific backend" |
| Ever sent to a client | No — server secret, compared, never returned | Never — a client has no code path to see or need these |

A client that can call `POST /api/v1/runs` still cannot choose a
provider model, supply a tool allowlist, or read any integration
credential — every one of those decisions is fixed, server-side,
agent-by-agent configuration (see `mantis.api.catalog`).

## OpenAPI / interactive docs

`mantis serve` exposes the real, generated OpenAPI schema and interactive
documentation — not a hand-maintained, potentially-stale description:

- `GET /openapi.json`
- `GET /docs` (Swagger UI)
- `GET /redoc` (ReDoc)

## Configuration

See [docs/configuration.md](configuration.md#api-server-api) for the
full server/client `MANTIS_API_*` environment-variable reference.

## What's deferred

- **#84 — persistent run history.** `GET /api/v1/runs/{run_id}` does not
  exist in this API. `InvocationService` is structured so #84 can attach
  persistence around the same invocation lifecycle without changing this
  contract, but no ephemeral/in-memory substitute is provided here.
- **#85 — portal UI.** Will call this same `/api/v1` surface; no
  portal-specific endpoint exists yet.
- **#93 — MCP client.** Will call this same `POST /api/v1/runs` /
  `GET /api/v1/agents` surface (never construct `AgentRuntime` or reach
  integrations directly); `AgentSummary` is intentionally small enough to
  extend with MCP discovery metadata (e.g. a future `mcp_prompt_name`)
  without a plugin framework.
- **Token streaming / WebSockets.** The first implementation is a plain
  synchronous request/response.
