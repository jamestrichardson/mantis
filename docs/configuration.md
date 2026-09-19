# Configuration

Mantis is configured entirely through process environment variables via
`mantis.config`. No module outside `mantis/config.py` should read these
variables directly, and nothing in the codebase hardcodes credentials.

## Local-dev `.env.*` file convention

In a real deployment (EKS, GKE, ECS, plain Docker, ...) the hosting
platform injects environment variables directly — there are no `.env`
files, and Mantis doesn't need any. Locally, there's no such platform, so
Mantis supports loading env vars from a small set of `.env.*` files
instead.

**None of these real files are committed to git** — only `*.example`
templates are tracked (`.gitignore` enforces this: `.env*` is ignored,
`*.example` is explicitly un-ignored).

### Which files get loaded, and in what order

`MANTIS_ENV` selects the active local environment name (default:
`"local"`). On import, `mantis.config` looks for these files, most
specific first, and loads whichever exist:

1. `.env.<MANTIS_ENV>.local` — personal, machine-specific overrides for
   that environment (e.g. your own AWX token).
2. `.env.<MANTIS_ENV>` — the named environment's own settings (with
   `MANTIS_ENV` unset/default, this is `.env.local`).
3. `.env.local` — legacy/catch-all personal overrides.
4. `.env` — shared, environment-agnostic defaults.

Loading uses `override=False`, so:

- A real environment variable already set (by your shell, a container
  platform, CI, etc.) always wins over anything in a file — file-based
  config can never silently override a genuinely deployed value.
- Among the files themselves, whichever loads first (i.e. whichever is
  more specific in the list above) wins if the same variable appears in
  more than one.

### Getting started locally

```bash
cp .env.local.example .env.local
# edit .env.local: fill in AWX_TOKEN and LITELLM_API_KEY
```

With `MANTIS_ENV` unset (defaults to `"local"`), `.env.local` is picked
up automatically — no further setup needed. `.env.local.example` is
pre-filled with our team's local dev AWX and LiteLLM endpoints; only the
credentials need to be supplied.

`.env.example` is a more generic, environment-agnostic template — use it
as the basis for a differently-named environment (e.g. `cp .env.example
.env.staging`, then run with `MANTIS_ENV=staging`).

## LiteLLM / model gateway

| Variable          | Required | Default | Description |
|-------------------|----------|---------|--------------|
| `LITELLM_URL`     | yes      | —       | Base URL of your LiteLLM gateway, e.g. `http://bespin.cosprings.teknofile.net:4000/v1`. `AgentRuntime` appends `/v1` if not already present, so either form works. |
| `LITELLM_API_KEY` | yes      | —       | LiteLLM virtual key. Use a scoped virtual key, not a raw upstream provider key — see [docs/security.md](security.md). |
| `LITELLM_MODEL`   | no       | `qwen3-opencode:latest` | Default model name/alias as registered in LiteLLM — used by any agent with no per-agent override set below. |
| `MANTIS_AWX_TROUBLESHOOTER_MODEL` | no | — (falls back to `LITELLM_MODEL`) | Model alias for the AWX Troubleshooter agent specifically. See [Per-agent model overrides](#per-agent-model-overrides) below. |
| `MANTIS_SYSTEM_TROUBLESHOOTER_MODEL` | no | — (falls back to `LITELLM_MODEL`) | Model alias for the System Troubleshooter agent specifically. See [Per-agent model overrides](#per-agent-model-overrides) below. |

### Per-agent model overrides

Each agent resolves its model independently, via
`LiteLLMConfig.from_env(model_env=...)`: the agent's own env var (if set
to a non-empty value) wins, otherwise `LITELLM_MODEL` is used, otherwise
the built-in default (`qwen3-opencode:latest`). `LITELLM_URL`/
`LITELLM_API_KEY` are unaffected — every agent still talks to the same
LiteLLM gateway with the same credential, only the model *alias* sent in
each request can differ.

This is a minimal precursor to **#16**'s full model routing/escalation
policy, not that policy itself: there is no fallback, retry, or
escalation across models, no per-model budget, and no adaptive
selection — each agent simply resolves one fixed model alias at
`build_runtime()` time. Model selection also remains entirely
server-side configuration: it is not, and must never become, a field on
`POST /api/v1/runs` or a CLI argument — see
[docs/api.md](api.md#invoking-an-agent).

Existing deployments that only set `LITELLM_MODEL` are unaffected — both
agents simply keep resolving that same value, exactly as before this
existed.

## AWX

| Variable         | Required | Default | Description |
|------------------|----------|---------|--------------|
| `AWX_URL`        | yes      | —       | Base URL of your AWX controller, no trailing slash required (it is stripped). |
| `AWX_TOKEN`      | yes      | —       | AWX API token. Use a read-only service-account token where possible — see [docs/security.md](security.md). |
| `AWX_VERIFY_SSL` | no       | `true`  | Whether to verify TLS certificates when talking to AWX. Accepts `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` (case-insensitive). Only disable for local/dev testing against a self-signed endpoint. |

## Prometheus

See [docs/prometheus.md](prometheus.md) for the full contract: instant/
range PromQL, result bounding, truncation semantics, and query errors
vs. retrieval failures.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MANTIS_PROMETHEUS_URL` | yes | — | Base URL of your Prometheus server, no trailing slash required (stripped). |
| `MANTIS_PROMETHEUS_BEARER_TOKEN` | no | — | Bearer token for `Authorization: Bearer <token>`. |
| `MANTIS_PROMETHEUS_BASIC_AUTH_USERNAME` | no | — | HTTP Basic auth username. |
| `MANTIS_PROMETHEUS_BASIC_AUTH_PASSWORD` | no | — | HTTP Basic auth password. |
| `MANTIS_PROMETHEUS_VERIFY_SSL` | no | `true` | Whether to verify TLS certificates when talking to Prometheus. Only disable for local/dev testing against a self-signed endpoint. |

An unauthenticated Prometheus endpoint works with none of the auth
variables set. If both a bearer token and basic auth are configured,
the bearer token takes priority.

## Loki

See [docs/loki.md](loki.md) for the full contract: bounded LogQL range
queries, stream/line bounding, truncation semantics, and query errors
vs. retrieval failures.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MANTIS_LOKI_URL` | yes | — | Base URL of your Loki server, no trailing slash required (stripped). |
| `MANTIS_LOKI_BEARER_TOKEN` | no | — | Bearer token for `Authorization: Bearer <token>`. |
| `MANTIS_LOKI_BASIC_AUTH_USERNAME` | no | — | HTTP Basic auth username. |
| `MANTIS_LOKI_BASIC_AUTH_PASSWORD` | no | — | HTTP Basic auth password. |
| `MANTIS_LOKI_TENANT_ID` | no | — | Sent as a static `X-Scope-OrgID` header on every request (Loki's multi-tenancy convention) — deployment configuration, never model-supplied. |
| `MANTIS_LOKI_VERIFY_SSL` | no | `true` | Whether to verify TLS certificates when talking to Loki. Only disable for local/dev testing against a self-signed endpoint. |

An unauthenticated Loki endpoint works with none of the auth variables
set. If both a bearer token and basic auth are configured, the bearer
token takes priority.

## Kubernetes

See [docs/kubernetes.md](kubernetes.md) for the full contract: auth
modes, RBAC guidance, bounded pod/deployment/node/event evidence,
truncation semantics, and provenance.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MANTIS_KUBERNETES_AUTH_MODE` | yes | — | `kubeconfig` or `in_cluster`. A single explicit mode — Mantis never guesses between credential sources. |
| `MANTIS_KUBERNETES_KUBECONFIG` | only in `kubeconfig` mode | — | Path to a kubeconfig file. Deployment configuration, never model/tool input. Must **not** be set in `in_cluster` mode (rejected at config construction — ambiguous precedence otherwise). |
| `MANTIS_KUBERNETES_CONTEXT` | no | — | Kubeconfig context to use. Recommended (rather than relying on the kubeconfig's own `current-context`) so context selection is explicit and stable. Must **not** be set in `in_cluster` mode. |
| `MANTIS_KUBERNETES_CLUSTER_NAME` | yes | — | Human-safe logical cluster name used in every result's provenance (e.g. `"home-k3s"`) — never a raw API-server URL, which could be unsafe to expose. Required in both auth modes so provenance stays stable across deployment methods. |
| `MANTIS_KUBERNETES_VERIFY_SSL` | no | `true` | Whether to verify TLS certificates when talking to the Kubernetes API server. Only disable for local/dev testing against a self-signed endpoint. |

**Which mode to use:** `in_cluster` is recommended when Mantis itself
runs inside the cluster it inspects — it uses the mounted
service-account token/CA the Kubernetes runtime already provides, with
no credential material in Mantis's own configuration at all. `kubeconfig`
is for Mantis running outside the cluster (e.g. local development,
homelab) — the token/cert material lives inside the kubeconfig file
itself, read directly by the Kubernetes client library; Mantis never
extracts, logs, or returns it.

**Precedence/validation:** `auth_mode` must be exactly `kubeconfig` or
`in_cluster` — there is no third "guess from whatever's present" mode.
`kubeconfig` mode requires `MANTIS_KUBERNETES_KUBECONFIG`; `in_cluster`
mode rejects `MANTIS_KUBERNETES_KUBECONFIG`/`MANTIS_KUBERNETES_CONTEXT`
outright rather than silently ignoring them, so precedence is never
ambiguous. `KubernetesConfig.from_env()` only parses configuration — it
never opens the kubeconfig file or contacts a cluster; that happens only
when a tool actually runs, via `KubernetesClient.from_config()`.

**Provenance vs. credentials:** `cluster_name`, `context`, and
`auth_mode` are safe, model-facing provenance, attached to every
successful Kubernetes tool result. `MANTIS_KUBERNETES_KUBECONFIG`'s
*value* (the path) is never included in any tool result, log line, or
error message — only the two fields above are.

**RBAC:** grant Mantis's service account (in-cluster) or kubeconfig user
(local) a narrowly scoped **read-only** `ClusterRole` covering only
`pods`, `deployments` (`apps/v1`), `nodes`, and `events` `get`/`list`/
`watch` — see [docs/kubernetes.md](kubernetes.md#rbac) for a worked
example manifest. Never grant `create`/`update`/`patch`/`delete`, `exec`,
or access to `secrets` — Mantis has no code path that would use it, and
granting it anyway widens blast radius for no benefit.

## DNS

See [docs/dns-lookup.md](dns-lookup.md) for the full contract: split-
horizon semantics, supported record types, the status vocabulary,
deterministic multi-server failover, and bounds/timeouts.

Unlike every other integration in this file, DNS has no fixed set of
named environment variables — an operator configures an open-ended set
of named **resolver profiles**, one environment variable per alias:

```bash
MANTIS_DNS_RESOLVER_<ALIAS>=server1,server2,...
```

| Example | Meaning |
|---|---|
| `MANTIS_DNS_RESOLVER_INTERNAL=172.30.0.53,172.30.0.54` | Defines the `internal` resolver profile (`dns_lookup`'s default `resolver_alias`), with two servers. |
| `MANTIS_DNS_RESOLVER_CLOUDFLARE=1.1.1.1,1.0.0.1` | Defines an example public resolver profile named `cloudflare`. |
| `MANTIS_DNS_RESOLVER_GOOGLE=8.8.8.8,8.8.4.4` | Defines an example public resolver profile named `google`. |

`<ALIAS>` is case-insensitive and becomes exactly the `resolver_alias`
value `dns_lookup` accepts. **None of `internal`/`cloudflare`/`google`
is required, hardcoded, or special-cased** — they're only example alias
names; configure whichever profiles are actually useful in your
environment (including none at all, though `dns_lookup`'s
`resolver_alias` default is `"internal"`, so at least that alias needs
configuring for the tool's zero-argument default to resolve anything).
Each server must be an IP literal (IPv4 or IPv6), never a hostname — a
resolver's own address must not itself require DNS resolution to reach.
`resolver_alias` is the **only** resolver-selecting input a model/API
caller may supply; a caller can never provide a resolver IP, hostname,
port, or any other DNS endpoint directly, and an alias with no matching
configured profile is rejected before any query is attempted — see
[docs/dns-lookup.md](dns-lookup.md#security-and-bounds).

`DNSConfig.from_env()` performs no network access; it only parses and
validates these variables.

## Reliability

See [docs/reliability.md](reliability.md) for the full contract:
timeouts, retries, the failure taxonomy, run/tool deadlines, and the
run-local short circuit. All eight are `mantis.config.ReliabilityConfig`
fields — every HTTP integration (AWX, Prometheus, and Loki today) and
`AgentRuntime` use the same schema and environment-variable defaults, not one set of knobs per
integration, even though each constructs its own
`ReliabilityConfig.from_env()` instance rather than sharing one object.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MANTIS_HTTP_CONNECT_TIMEOUT_SECONDS` | no | `5.0` | HTTP connect timeout for every outbound integration request. |
| `MANTIS_HTTP_READ_TIMEOUT_SECONDS` | no | `25.0` | HTTP read timeout for every outbound integration request. |
| `MANTIS_RETRY_MAX_ATTEMPTS` | no | `3` | Retry/attempt budget: max transport attempts per logical integration read. |
| `MANTIS_RETRY_BACKOFF_BASE_SECONDS` | no | `0.5` | Base for exponential-with-full-jitter backoff between retry attempts. |
| `MANTIS_RETRY_BACKOFF_CAP_SECONDS` | no | `8.0` | Ceiling on any single backoff wait. |
| `MANTIS_TOOL_TIMEOUT_SECONDS` | no | `45.0` | Per-tool-call wall-clock deadline — distinct from `tool_call_budget`; see [docs/reliability.md](reliability.md#retry-budget-vs-tool-call-budget). |
| `MANTIS_RUN_TIMEOUT_SECONDS` | no | `300.0` | Overall `AgentRuntime.run()` wall-clock deadline. |
| `MANTIS_SHORT_CIRCUIT_THRESHOLD` | no | `3` | Consecutive classified-transient failures against one integration, within one run, before that integration fails fast for the rest of the run. |

## API server (`mantis serve`) {#api-server-api}

See [docs/api.md](api.md) for the full guide (authentication, run
semantics, concurrency/overload, health/readiness, CLI relationship).
All `mantis.config.ApiServerConfig` fields.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MANTIS_API_AUTH_MODE` | no | `bearer_token` | `bearer_token` (secure default) or `disabled` (explicit, logged, development-only — see [docs/api.md](api.md#authentication)). |
| `MANTIS_API_TOKEN` | only in `bearer_token` mode | — | The API's own client-facing secret. Never reused from/for LITELLM/AWX/Kubernetes/Prometheus/Loki credentials. |
| `MANTIS_API_HOST` | no | `0.0.0.0` | Bind address. |
| `MANTIS_API_PORT` | no | `8080` | Bind port. |
| `MANTIS_API_MAX_CONCURRENT_RUNS` | no | `4` | Bounded, process-local concurrent-run limit — see [docs/api.md](api.md#concurrency-and-overload). |
| `MANTIS_API_SHUTDOWN_GRACE_PERIOD_SECONDS` | no | `30` | How long an in-flight run is given to finish during graceful shutdown — see [docs/deployment.md](deployment.md#graceful-shutdown). |

## API client (the `mantis` CLI)

All `mantis.config.ApiClientConfig` fields — read only by the CLI, never
by the server.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MANTIS_API_URL` | no | `http://localhost:8080` | Base URL of the Mantis API to call. |
| `MANTIS_API_TOKEN` | only if the server requires auth | — | The bearer token to send. Same variable name as the server's own setting above — they're read by different processes; a client only ever needs this one credential, never the integration credentials. |
| `MANTIS_API_CLIENT_CONNECT_TIMEOUT_SECONDS` | no | `5.0` | HTTP connect timeout for CLI-to-API requests. |
| `MANTIS_API_CLIENT_READ_TIMEOUT_SECONDS` | no | `340.0` | HTTP read timeout for CLI-to-API requests — deliberately above `MANTIS_RUN_TIMEOUT_SECONDS`'s own default (`300.0`) below, so the client never gives up on a run before the server itself would. |

## Observability

See [docs/observability.md](observability.md) for the full event schema
and metrics catalog.

| Variable                 | Required | Default    | Description |
|--------------------------|----------|------------|--------------|
| `MANTIS_LOG_LEVEL`       | no       | `INFO`     | Log level for the structured JSON logs `mantis.cli.main` configures at startup. |
| `MANTIS_ENVIRONMENT`     | no       | `local`    | Value of the `environment` label on every metric (e.g. `production`, `staging`). Purely a metrics label — unrelated to `MANTIS_ENV`'s `.env.*` file selection below. |
| `MANTIS_METRICS_ENABLED` | no       | `false` for `mantis eval`, `true` for `mantis serve` | Starts the Prometheus `/metrics` HTTP server at process startup. Each owning subcommand reads this independently — `mantis serve` (`mantis.api.server.run_server`) defaults it *on*, since it's the one persistent process #66 gives metrics a real, continuously-held-open home in; `mantis eval` (`mantis.eval.cli.main`) defaults it *off*, since a one-shot local process has no such home and a default-on server would contend for `:9108` across concurrent invocations. `mantis agents`/`mantis run`/the per-agent convenience commands are pure HTTP clients (#83) and never start a metrics server at all — this variable has no effect on them, regardless of its value. Either owning subcommand's default can be overridden explicitly. See [docs/observability.md](observability.md#prometheus-metrics). |
| `MANTIS_METRICS_PORT`    | no       | `9108`     | Port the metrics server binds. |
| `MANTIS_METRICS_ADDR`    | no       | `0.0.0.0`  | Address the metrics server binds. |

## Other

| Variable     | Required | Default | Description |
|--------------|----------|---------|--------------|
| `MANTIS_ENV` | no       | `local` | Selects which `.env.<name>[.local]` files to load locally. Has no effect in a deployment that injects real environment variables and ships no `.env.*` files. |

## Example `.env.local`

```bash
LITELLM_URL=http://bespin.cosprings.teknofile.net:4000/v1
LITELLM_API_KEY=sk-my-virtual-key
LITELLM_MODEL=qwen3-opencode:latest

AWX_URL=https://awx.cosprings.teknofile.net
AWX_TOKEN=eyJhbGciOi...
AWX_VERIFY_SSL=true

MANTIS_API_TOKEN=a-locally-generated-token
```

## Missing configuration

Required variables raise `mantis.config.ConfigurationError` immediately
when their config object is constructed (`LiteLLMConfig.from_env()` /
`AWXConfig.from_env()`), with a message naming the missing variable, so
misconfiguration fails fast and clearly rather than surfacing as a
confusing downstream error.
