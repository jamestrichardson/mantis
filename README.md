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
  tool (AWX, TCP connectivity, DNS, Prometheus, Loki, and Kubernetes
  today) is
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

Mantis runs as a persistent HTTP service — the CLI (and, later, a
portal and MCP client) reaches every agent through one versioned API,
never by executing anything locally:

```
┌─────────────────────────────────────────────────────────┐
│ CLI / Portal / MCP  (HTTP client only, no local fallback) │
├─────────────────────────────────────────────────────────┤
│ FastAPI service     (`mantis serve`, PID1)                 │
│   /healthz /readyz /api/v1/agents /api/v1/runs             │
├─────────────────────────────────────────────────────────┤
│ InvocationService   (run ID, concurrency, safe errors)      │
├─────────────────────────────────────────────────────────┤
│ Agent catalog       (id -> real agent build_runtime)        │
├─────────────────────────────────────────────────────────┤
│ Agents            (thin: prompt + allowed tools + model) │
│   awx_troubleshooter, system_troubleshooter               │
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
alongside (not inside) this stack. See
[docs/api.md](docs/api.md) for the API/CLI guide and
[docs/architecture.md](docs/architecture.md) for the full data-flow
diagram.

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
| `MANTIS_API_TOKEN` | Bearer token the CLI/API share (client-facing, separate from all of the above) |

Mantis runs as a service; start it, then invoke agents through the CLI
(an HTTP client — see [docs/api.md](docs/api.md)):

```bash
# terminal 1
mantis serve

# terminal 2 (MANTIS_API_URL defaults to http://localhost:8080)
mantis agents
```

## Running the AWX Troubleshooter

```bash
mantis awx-troubleshooter \
  "Show me the last 3 failed AWX jobs and tell me whether they appear related."

# Equivalent, explicit generic form:
mantis run awx-troubleshooter "Show me the last 3 failed AWX jobs and tell me whether they appear related."
```

Both go through the real Mantis API (`mantis serve` must already be running — see [Quickstart](#quickstart) above). There is no supported way to run an agent without it; see [docs/api.md](docs/api.md#cli-relationship).

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

## Running the System Troubleshooter

Mantis's first multi-source investigation agent (#11) — correlates
historical AWX evidence, current TCP connectivity, Prometheus
time-series data, and Loki logs. See
[docs/system-troubleshooter.md](docs/system-troubleshooter.md) for the
full design, budgets, and a worked example.

```bash
mantis system-troubleshooter "Investigate why db-primary-02 keeps failing health checks."

# Equivalent, explicit generic form:
mantis run system-troubleshooter "Investigate why db-primary-02 keeps failing health checks."
```

## Current limitations

- Read-only: no job launching, retries, or any mutation of AWX or any
  other system.
- Two agents implemented (AWX Troubleshooter, System Troubleshooter); the
  tool/runtime architecture is built to support more (see Roadmap).
- Mantis is a real persistent HTTP service (`mantis serve`) with a
  versioned, authenticated API, but no persistent run history, database,
  or web UI yet — see the Roadmap below and [docs/api.md](docs/api.md#whats-deferred).
- Local models (e.g. Qwen3 via Ollama) can be inconsistent about
  tool-calling discipline; the runtime's duplicate-call detection and
  iteration cap exist specifically to keep that bounded, not to make it
  perfect.

## Roadmap

- **Incident Triage Agent** — wires the Kubernetes evidence tools (#18,
  already implemented) and git/change history into the System
  Troubleshooter's evidence sources, correlating a live incident against
  recent changes.
- **Daily Operations Digest Agent** — scheduled summary across the same
  shared tool library.
- **Mutating tools and an approval/policy layer** — see
  [docs/security.md](docs/security.md) for the
  investigate → recommend → approve → remediate model this is designed
  to grow into.

## Documentation

- [docs/local-development.md](docs/local-development.md) — **start here**: install, configure, run, troubleshoot
- [docs/api.md](docs/api.md) — the HTTP API: authentication, run semantics, concurrency, CLI relationship, curl examples
- [docs/architecture.md](docs/architecture.md) — layers, data flow, why the boundaries are where they are
- [docs/deployment.md](docs/deployment.md) — standalone service deployment, health/readiness, graceful shutdown, rollback
- [docs/tools.md](docs/tools.md) — what a tool is, how to add one
- [docs/awx-job-failure.md](docs/awx-job-failure.md) — structured AWX job-event failure evidence: selection rules, bounding/pagination, provenance, stdout fallback
- [docs/network-tcp-connectivity.md](docs/network-tcp-connectivity.md) — current-state TCP connectivity tool: status vocabulary, IPv4/IPv6 multi-address behavior, deadline handling, SSRF posture
- [docs/dns-lookup.md](docs/dns-lookup.md) — DNS evidence tool: resolver profiles, split-horizon semantics, supported record types, status vocabulary, deterministic multi-server failover
- [docs/http-probe.md](docs/http-probe.md) — bounded HTTP(S) probe tool: target profiles, redirect/proxy/auth posture, body/header bounds, any-status-is-evidence semantics
- [docs/tls-certificate-inspection.md](docs/tls-certificate-inspection.md) — TLS certificate inspection tool: inspect-vs-verify, the two-handshake mechanism, independent verification dimensions
- [docs/prometheus.md](docs/prometheus.md) — time-series evidence tool: instant/range PromQL, result bounding/truncation, query errors vs. retrieval failures, `up` semantics
- [docs/loki.md](docs/loki.md) — log evidence tool: bounded LogQL range queries, stream/line bounding, truncation semantics, why Loki is treated as the highest-risk untrusted-text source
- [docs/system-troubleshooter.md](docs/system-troubleshooter.md) — first multi-source investigation agent: tool allowlist, budgets, investigation/output contract, worked `ferros-c01` example
- [docs/agents.md](docs/agents.md) — what an agent is, how to add one
- [docs/configuration.md](docs/configuration.md) — the `.env.*` convention and all environment variables
- [docs/security.md](docs/security.md) — least privilege, credentials, future mutation gates
- [docs/reliability.md](docs/reliability.md) — timeouts, retries, failure taxonomy, run/tool deadlines, run-local short circuit
- [docs/development.md](docs/development.md) — contributor workflow: tests, adding integrations/tools/agents, style
- [docs/evaluation.md](docs/evaluation.md) — model qualification/evaluation harness: running scenarios, adding a new one
- [docs/observability.md](docs/observability.md) — structured JSON log event schema, Prometheus metrics catalog, LogQL/PromQL examples
- [docs/release.md](docs/release.md) — conventional commits, SemVer policy, how release-please cuts a release
