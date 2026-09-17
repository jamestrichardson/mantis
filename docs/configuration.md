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
| `LITELLM_MODEL`   | no       | `qwen3-opencode:latest` | Model name/alias as registered in LiteLLM. |

## AWX

| Variable         | Required | Default | Description |
|------------------|----------|---------|--------------|
| `AWX_URL`        | yes      | —       | Base URL of your AWX controller, no trailing slash required (it is stripped). |
| `AWX_TOKEN`      | yes      | —       | AWX API token. Use a read-only service-account token where possible — see [docs/security.md](security.md). |
| `AWX_VERIFY_SSL` | no       | `true`  | Whether to verify TLS certificates when talking to AWX. Accepts `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` (case-insensitive). Only disable for local/dev testing against a self-signed endpoint. |

## Observability

See [docs/observability.md](observability.md) for the full event schema
and metrics catalog.

| Variable                 | Required | Default    | Description |
|--------------------------|----------|------------|--------------|
| `MANTIS_LOG_LEVEL`       | no       | `INFO`     | Log level for the structured JSON logs `mantis.cli.main` configures at startup. |
| `MANTIS_ENVIRONMENT`     | no       | `local`    | Value of the `environment` label on every metric (e.g. `production`, `staging`). Purely a metrics label — unrelated to `MANTIS_ENV`'s `.env.*` file selection below. |
| `MANTIS_METRICS_ENABLED` | no       | `false`    | Starts the Prometheus `/metrics` HTTP server at process startup. Off by default everywhere, including the Docker image — each Mantis CLI invocation is a short-lived process, so a default-on server would bind `:9108` (and contend for it under concurrent invocations) for a window that closes when the command exits. Set explicitly for local/manual testing of the endpoint. See [docs/observability.md](observability.md#current-status-of-metrics). |
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
```

## Missing configuration

Required variables raise `mantis.config.ConfigurationError` immediately
when their config object is constructed (`LiteLLMConfig.from_env()` /
`AWXConfig.from_env()`), with a message naming the missing variable, so
misconfiguration fails fast and clearly rather than surfacing as a
confusing downstream error.
