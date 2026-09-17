# Mantis

[![CI](https://github.com/jamestrichardson/mantis/actions/workflows/ci.yml/badge.svg)](https://github.com/jamestrichardson/mantis/actions/workflows/ci.yml)

**M**onitoring, **A**utomation, **N**etwork **T**riage & **I**nfrastructure **S**ystem

Mantis is an operations-focused AI agent framework. It provides specialized
operational agents (an AWX troubleshooter today; a system troubleshooter,
incident triage agent, and daily ops digest in the roadmap) that share a
common runtime and a reusable library of tools and integrations.

## Objectives

Mantis exists to give operators AI agents that can actually be trusted to
investigate real infrastructure, not a generic chatbot wrapper. Concretely:

- **Automate the first-look investigation.** Turn "go read the AWX job
  output and figure out what broke" into a single prompt that returns an
  evidence-based summary, so a human's time goes to judgment calls, not
  log spelunking.
- **Build a reusable operational toolset, not one-off scripts.** Every
  tool (AWX today; Prometheus, Loki, Kubernetes, TCP checks planned) is
  written once and shared across every agent that needs it — see
  [Philosophy](#philosophy) below.
- **Stay evidence-based and honest about uncertainty.** Agents are
  instructed to distinguish what a tool actually returned from what they
  are hypothesizing, and to say so explicitly when root cause is
  unclear — see [docs/security.md](docs/security.md) and each agent's
  system prompt.
- **Default to read-only, and be explicit about the one future exception.**
  Mutating actions (launching a job, restarting a service, ...) are
  architecturally distinct and will require an approval step — Mantis is
  designed to grow from investigation into recommendation, and only later,
  deliberately, into gated remediation.
- **Run anywhere, without provider lock-in.** Agents talk to models only
  through an OpenAI-compatible gateway ([LiteLLM](https://www.litellm.ai/)),
  and are built to run locally today and hosted (EKS/GKE/ECS/Docker)
  later, with no code change in between — see
  [docs/local-development.md](docs/local-development.md) for how that
  local/hosted split works today.

## Philosophy

- **Agents are thin.** An agent is a system prompt, an allowed-tool list,
  and model config — not a place to write bespoke logic.
- **Tools are shared, not owned by an agent.** `awx_recent_failed_jobs`
  can be used by the AWX Troubleshooter today and by a future System
  Troubleshooting or Incident Triage agent tomorrow, unmodified.
- **Integrations know APIs; tools know how to talk to an LLM.** Raw HTTP
  client code lives in `mantis.integrations`. Semantic, LLM-facing,
  preprocessed operations live in `mantis.tools`.
- **Read-only by default.** Every tool in this milestone is read-only.
  Mutating tools (launching a job, restarting a service, ...) are an
  explicitly separate, future category — see [docs/security.md](docs/security.md).
- **The model provider is abstracted away.** Agents only ever see an
  OpenAI-compatible interface via [LiteLLM](https://www.litellm.ai/). No
  agent or tool code depends on Ollama, OpenAI, Anthropic, or any other
  specific provider.

See [docs/architecture.md](docs/architecture.md) for the full picture.

## Architecture overview

```
┌─────────────────────────────────────────────────────────┐
│ Agents            (thin: prompt + allowed tools + model) │
│   awx_troubleshooter                                     │
├─────────────────────────────────────────────────────────┤
│ Agent Runtime     (shared model/tool loop)                │
│   mantis.runtime.AgentRuntime                             │
├─────────────────────────────────────────────────────────┤
│ Tool Registry     (name -> schema + handler + metadata)   │
│   mantis.registry.ToolRegistry                            │
├─────────────────────────────────────────────────────────┤
│ Tools             (semantic, LLM-facing, preprocessed)    │
│   mantis.tools.awx.awx_recent_failed_jobs                 │
├─────────────────────────────────────────────────────────┤
│ Integrations      (raw API clients)                       │
│   mantis.integrations.awx.AWXClient                       │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
              External systems (AWX, ...)
```

Model access goes through LiteLLM's OpenAI-compatible API, sitting
alongside (not inside) this stack — see
[docs/architecture.md](docs/architecture.md) for the data-flow diagram.

## Prerequisites

- Python 3.11+
- A running [LiteLLM](https://docs.litellm.ai/) gateway (proxy mode)
  exposing an OpenAI-compatible `/v1/chat/completions` endpoint, backed by
  whatever model you like. Our current local setup is Qwen3 via Ollama
  behind LiteLLM, but Mantis code has no dependency on that pairing.
- An AWX (or AWX-API-compatible, e.g. Ansible Automation Platform)
  instance and an API token, ideally for a read-only service account.

## Quickstart

Full walkthrough (install, configure, troubleshoot): **[docs/local-development.md](docs/local-development.md)**.
The short version:

```bash
git clone <this-repo>
cd mantis
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.local.example .env.local   # fill in AWX_TOKEN and LITELLM_API_KEY
```

`.env.local` is loaded automatically and is git-ignored (only `*.example`
templates are committed). In a real deployment (EKS/GKE/ECS/Docker) the
platform injects environment variables directly instead — no `.env` file
is used or needed there. See [docs/configuration.md](docs/configuration.md)
for the full `.env.*` file convention and every variable. In short:

| Variable          | Purpose                                   |
|-------------------|--------------------------------------------|
| `LITELLM_URL`     | Base URL of your LiteLLM gateway            |
| `LITELLM_API_KEY` | LiteLLM virtual key                         |
| `LITELLM_MODEL`   | Model name/alias as registered in LiteLLM (defaults to `qwen3-opencode:latest`) |
| `AWX_URL`         | Base URL of your AWX controller             |
| `AWX_TOKEN`       | AWX API token (read-only service account)   |
| `AWX_VERIFY_SSL`  | Verify TLS certs when talking to AWX (default `true`) |

## Running the AWX Troubleshooter

```bash
# Default prompt: "Show me the last 5 failed AWX jobs and summarize them."
mantis awx-troubleshooter

# Or with an explicit prompt:
mantis awx-troubleshooter \
  "Show me the last 3 failed AWX jobs and tell me whether they appear related."

# Equivalent module invocation:
python -m mantis.agents.awx_troubleshooter "Show me the last 5 failed AWX jobs and summarize them."
```

### What happens

1. The runtime sends your prompt, the system prompt, and the
   `awx_recent_failed_jobs` tool schema to your model via LiteLLM.
2. The model calls `awx_recent_failed_jobs(limit=...)`.
3. Mantis queries AWX's `/api/v2/jobs/` for the most recently finished
   `status=failed` jobs, fetches each job's stdout (falling back from
   `format=txt` to `format=txt_download` when AWX reports the output is
   too large to display inline), and preprocesses that stdout into a
   `failure_excerpt` (high-value lines) and a bounded `stdout_tail`.
4. The tool result is fed back to the model, which produces an
   evidence-based summary — distinguishing AWX-reported failures from any
   Mantis-side stdout retrieval errors, and separating direct evidence
   from hypotheses.

## Example

Given several failed jobs whose stdout contains:

```
fatal: [host03]: UNREACHABLE! => {"msg": "ssh: connect to host host03 port 22: No route to host"}
```

the AWX Troubleshooter reports this as a network reachability problem
(supported directly by the evidence), suggests checking host availability,
network paths, and SSH connectivity, and explicitly avoids overstating it
as (for example) a proven firewall misconfiguration.

## Current limitations

- Read-only: no job launching, retries, or any mutation of AWX or any
  other system.
- Single agent implemented (AWX Troubleshooter); the tool/runtime
  architecture is built to support more (see Roadmap).
- No persistent storage, database, or web UI — this is a CLI-first
  foundation.
- Local models (e.g. Qwen3 via Ollama) can be inconsistent about
  tool-calling discipline; the runtime's duplicate-call detection and
  iteration cap exist specifically to keep that bounded, not to make it
  perfect.

## Roadmap

- **System Troubleshooting Agent** — reuses `awx_recent_failed_jobs` plus
  new tools: TCP connectivity checks, host reachability, Prometheus/Loki
  queries.
- **Incident Triage Agent** — AWX + Prometheus + Loki + Kubernetes +
  git/change history, correlating across systems.
- **Daily Operations Digest Agent** — scheduled summary across the same
  shared tool library.
- **Mutating tools and an approval/policy layer** — see
  [docs/security.md](docs/security.md) for the
  investigate → recommend → approve → remediate model this is designed
  to grow into.

## Documentation

- [docs/local-development.md](docs/local-development.md) — **start here**: install, configure, run, troubleshoot
- [docs/architecture.md](docs/architecture.md) — layers, data flow, why the boundaries are where they are
- [docs/tools.md](docs/tools.md) — what a tool is, how to add one
- [docs/awx-job-failure.md](docs/awx-job-failure.md) — structured AWX job-event failure evidence: selection rules, bounding/pagination, provenance, stdout fallback
- [docs/network-tcp-connectivity.md](docs/network-tcp-connectivity.md) — current-state TCP connectivity tool: status vocabulary, IPv4/IPv6 multi-address behavior, deadline handling, SSRF posture
- [docs/agents.md](docs/agents.md) — what an agent is, how to add one
- [docs/configuration.md](docs/configuration.md) — the `.env.*` convention and all environment variables
- [docs/security.md](docs/security.md) — least privilege, credentials, future mutation gates
- [docs/reliability.md](docs/reliability.md) — timeouts, retries, failure taxonomy, run/tool deadlines, run-local short circuit
- [docs/development.md](docs/development.md) — contributor workflow: tests, adding integrations/tools/agents, style
- [docs/evaluation.md](docs/evaluation.md) — model qualification/evaluation harness: running scenarios, adding a new one
- [docs/observability.md](docs/observability.md) — structured JSON log event schema, Prometheus metrics catalog, LogQL/PromQL examples
- [docs/release.md](docs/release.md) — conventional commits, SemVer policy, how release-please cuts a release
