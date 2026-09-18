# Local Development Setup & Usage

This is the practical, step-by-step guide to getting Mantis running on
your machine. For *why* the code is organized the way it is, see
[architecture.md](architecture.md); for *how to add* an integration,
tool, or agent, see [development.md](development.md).

Mantis agents are meant to eventually run hosted (EKS, GKE, ECS, or plain
Docker) with environment variables injected by that platform. Locally,
there's no such platform, so this guide's job is: get a Python
environment set up, point Mantis at real (or your own) AWX + LiteLLM
endpoints via a local env file, and run an agent from the CLI.

## 1. Prerequisites

- **Python 3.11+**
- **Network access to a LiteLLM gateway** — an OpenAI-compatible
  `/v1/chat/completions` endpoint backed by whatever model you like.
  Mantis has no code-level dependency on any specific model or provider.
- **Network access to an AWX (or AWX-API-compatible) instance** and an
  API token for it, ideally a read-only service account.

If you're working within the team that maintains this repo, dev
instances of both are already running and referenced in
`.env.local.example` (see step 3) — you mainly need your own credentials.

## 2. Install

```bash
git clone <this-repo>
cd mantis
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This is an editable install (`-e`): code changes under `src/mantis/` take
effect immediately, no reinstall needed. `.[dev]` pulls in `pytest`,
`pytest-mock`, and `respx` for testing.

## 3. Configure your local environment

Mantis reads all configuration from environment variables. Locally, it
loads them from a small `.env.*` file convention instead of requiring you
to `export` everything by hand — see [configuration.md](configuration.md)
for the full precedence rules. The short version for day-to-day use:

```bash
cp .env.local.example .env.local
```

Then open `.env.local` and fill in the credential fields it leaves as
placeholders:

- `AWX_TOKEN` — your AWX API token (read-only service account
  recommended; see [security.md](security.md)).
- `LITELLM_API_KEY` — your LiteLLM virtual key.
- `MANTIS_API_TOKEN` — a token *you* make up for local use (e.g. `openssl
  rand -hex 32`), shared between `mantis serve` and the CLI. This is the
  Mantis API's own credential, entirely separate from the two above —
  see [api.md](api.md#authentication).

`.env.local.example` is already pointed at this team's local dev
endpoints (`AWX_URL`, `LITELLM_URL`, `LITELLM_MODEL`), so if those are
correct for you, nothing else needs to change. `.env.local` is
git-ignored — it will never be committed, and you never need to `export`
these variables into your shell manually. `MANTIS_ENV` defaults to
`"local"`, which is exactly what makes `.env.local` get picked up
automatically.

If you need to point at a *different* AWX or LiteLLM instance (e.g. your
own local Ollama-backed LiteLLM), either edit `.env.local` directly, or
create a separate named environment:

```bash
cp .env.example .env.sandbox
# edit .env.sandbox
MANTIS_ENV=sandbox mantis awx-troubleshooter
```

### Verify configuration loads correctly

```bash
python -c "
from mantis.config import LiteLLMConfig, AWXConfig
print(LiteLLMConfig.from_env())
print(AWXConfig.from_env())
"
```

This should print your resolved LiteLLM and AWX settings with no
`ConfigurationError`. If it raises one, it names exactly which
environment variable is missing — double check `.env.local` was created
(not just `.env.local.example`) and that it's in the current directory.

## 4. Start the service and run the AWX Troubleshooter

Mantis runs as a persistent service; the CLI talks to it over HTTP (see
[api.md](api.md)) — there is no local/in-process agent execution path.
Start it in one terminal:

```bash
mantis serve
```

Then, in another terminal (`MANTIS_API_URL` defaults to
`http://localhost:8080`, so nothing to set if you're running both
locally):

```bash
mantis agents

mantis awx-troubleshooter \
  "Show me the last 3 failed AWX jobs and tell me whether they appear related."

# Equivalent, explicit generic form:
mantis run awx-troubleshooter "Show me the last 3 failed AWX jobs and tell me whether they appear related."

# Direct module invocation, independent of the service (useful for
# running one agent under a debugger without the HTTP hop):
python -m mantis.agents.awx_troubleshooter "Show me the last 5 failed AWX jobs and summarize them."
```

What you should see: the agent calls out to your AWX instance for recent
failed jobs, retrieves and preprocesses their stdout, sends that evidence
to your model via LiteLLM, and the CLI prints an evidence-based summary
plus the run's ID. See the [README](../README.md#example) for a worked
example of the kind of output to expect.

## 5. Run the tests

```bash
pytest
```

Tests are fully offline — no reachable AWX or LiteLLM needed. HTTP calls
are mocked with `respx`, and the model client is stubbed directly. Run
these before and after any change; see [development.md](development.md)
for the full contributor workflow (adding tools/agents, style
conventions) once you're past initial setup.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ConfigurationError: Missing required environment variable: ...` | `.env.local` doesn't exist yet, isn't in your current directory, or is missing that specific key. Re-check step 3. |
| `Could not reach the Mantis API: ...` | `mantis serve` isn't running, or `MANTIS_API_URL` points somewhere else. Start the service first (step 4) — the CLI never falls back to running an agent locally. |
| `Mantis API authentication failed` | `MANTIS_API_TOKEN` doesn't match between the `mantis serve` process and the CLI's own environment. |
| `openai.APIConnectionError` / connection refused talking to LiteLLM | `LITELLM_URL` unreachable — check VPN/network access, and that the URL includes the right port (`:4000`) and, if needed, `/v1`. |
| `AWXError: ... 401` or `403` | `AWX_TOKEN` is missing, wrong, or lacks permission on the target AWX organization/inventory. |
| SSL verification errors talking to AWX | Only disable via `AWX_VERIFY_SSL=false` for a known self-signed dev instance — never in anything resembling production. |
| Agent seems to loop or gives up after a few tool calls | Expected safety behavior, not a bug — see `AgentRuntime`'s duplicate-call detection and `max_iterations` cap in [architecture.md](architecture.md). Local/smaller models can be inconsistent about tool-calling discipline. |

## Where to go next

- [api.md](api.md) — the HTTP API the CLI calls: authentication, run semantics, curl examples
- [architecture.md](architecture.md) — the full layer stack and why they're separated
- [tools.md](tools.md) / [agents.md](agents.md) — how to extend Mantis with new capabilities
- [configuration.md](configuration.md) — full environment variable reference
- [security.md](security.md) — credential handling, least privilege, read-only-by-default
- [development.md](development.md) — contributor workflow once your local setup is working
