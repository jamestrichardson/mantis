# Agents

A Mantis **agent** is a specialized behavior defined almost entirely by
configuration: a system prompt, an allowed-tool list, and model settings.
Agents should be thin — all model-calling and tool-dispatch logic lives in
the shared runtime, and all tool logic lives in `mantis.tools`.

## What constitutes an agent

Concretely, an agent module (e.g. `mantis/agents/awx_troubleshooter.py`)
defines:

- `AGENT_NAME` — the stable canonical ID used to invoke it (matches its
  `mantis.api.catalog.AgentCatalogEntry.id` — see [Catalog
  registration](#catalog-registration-required-for-api-cli-access)
  below).
- `SYSTEM_PROMPT` — the agent's behavioral contract (what it should and
  should not claim, how to weigh evidence, tone, output structure).
- `ALLOWED_TOOLS` — a list of tool *names* already registered in
  `mantis.registry.default_registry`.
- `DEFAULT_PROMPT` — used by `main(argv)` for direct-module invocation
  with no prompt argument (mainly for local development/debugging — see
  below); `POST /api/v1/runs` always requires an explicit `prompt`.
- `build_runtime()` — constructs and returns an `AgentRuntime` configured
  with the above. This is the one factory `mantis.api.catalog` points
  at; it is never called from `mantis.cli` directly.
- `main(argv)` — a small wrapper around `build_runtime().run(prompt)`
  for running the module directly (`python -m mantis.agents.<name>`).
  This is **not** a supported execution path — it's an unsupported,
  low-level debugging escape hatch (e.g. for stepping through one
  agent's code without an HTTP hop), with none of the API's
  authentication, request bounds, concurrency limiting, run ID, or
  (once #84 lands) history. The official `mantis` CLI never calls this
  — see [docs/api.md](api.md#cli-relationship).

That's the whole shape. There is no agent-specific subclass of the
runtime, no agent-specific tool implementation, and no agent-specific
model-calling code.

## Shared runtime

Every agent runs through `mantis.runtime.AgentRuntime`, which:

- Talks to the model via LiteLLM's OpenAI-compatible API.
- Sends only the agent's resolved tool schemas.
- Dispatches tool calls to the matching registered handler.
- Feeds tool results back into the conversation as `role: "tool"`
  messages.
- Loops until the model produces a plain-text answer (no further tool
  calls), or `max_iterations` is reached (`MaxIterationsExceededError`).
- Detects exact duplicate tool calls (same name, same arguments, already
  executed) and replays the *cached prior result* instead of
  re-executing — a smaller local model that re-asks for the same data
  still gets real evidence, never an error that would starve it and
  risk it echoing that error back as a "final answer."
- Can be given a `tool_call_budget`: once that many tool calls have
  succeeded, tool schemas are withheld on later iterations so the model
  is forced to answer. This both guarantees the model can't loop on
  repeat calls and — for local models where `tools` triggers
  grammar-constrained decoding for the whole response — keeps the final
  answer fast. See [docs/architecture.md](architecture.md#withholding-tools-once-an-agent-has-what-it-needs).
- Never sends `tool_choice` (some providers behind LiteLLM, e.g. Ollama,
  reject or mishandle it).
- Optionally passes `temperature` through to the model call, when an
  agent sets one — useful for keeping smaller local models focused and
  consistently formatted.
- Catches and cleanly reports (never raises past the loop): malformed
  tool-call arguments, unknown tool names, and exceptions raised by a
  tool's handler.

See [docs/architecture.md](architecture.md) for the full data-flow
diagram and [docs/tools.md](tools.md) for how tools are built.

## Prompts

The system prompt is the primary lever for agent behavior and should be
specific about:

- What evidence-gathering discipline to follow (e.g. "only report what a
  tool actually returned").
- How to distinguish tool/integration errors from the underlying system's
  own reported failures.
- How confident to be, and when to say "uncertain" instead of guessing.
- What output structure operators expect.

See `SYSTEM_PROMPT` in `mantis/agents/awx_troubleshooter.py` for a
worked example enforcing evidence-based, non-overstated conclusions.

## Allowed tool lists

An agent's `ALLOWED_TOOLS` is its entire capability surface — reading it
tells you exactly what the agent can observe or do. Keep it as narrow as
the agent's job requires; add to it only when the agent genuinely needs a
new capability. Because tools are shared, adding an existing tool to a new
agent's list is free — no reimplementation needed.

## Specialization

Agents specialize primarily through:

1. **System prompt** — domain framing, evidentiary standards, output
   shape.
2. **Allowed tools** — what it can actually observe/do.
3. **Model configuration** — which model/settings to use. Every
   built-in agent resolves both a `model_config` (via
   `LiteLLMConfig.from_env(model_env="MANTIS_<AGENT>_MODEL")`) and a
   `routing_policy` (via `ModelRoutingPolicy.from_env(model_env=...,
   fallback_env=..., max_attempts_env=...)`, #16) — an optional
   agent-specific environment variable (e.g.
   `MANTIS_AWX_TROUBLESHOOTER_MODEL`/`MANTIS_AWX_TROUBLESHOOTER_MODEL_FALLBACKS`)
   can pin that agent to a different LiteLLM alias, and a small ordered
   fallback list, than the global `LITELLM_MODEL`/`LITELLM_MODEL_FALLBACKS`
   defaults — see
   [docs/configuration.md](configuration.md#per-agent-model-overrides)
   and [docs/model-routing.md](model-routing.md) for the full design.
   This stays entirely server-side configuration — never a field a
   caller sets through the API or CLI — and fallback is bounded,
   deterministic model-*call* routing within one logical model step,
   never a whole-run restart.

Agents should *not* specialize by embedding tool or integration logic
directly in the agent module.

## How to create a new agent

1. Create `mantis/agents/<agent_name>.py`.
2. Define `AGENT_NAME`, `SYSTEM_PROMPT`, `ALLOWED_TOOLS` (names of tools
   already registered — add new tools first if needed, per
   [docs/tools.md](tools.md)), and `DEFAULT_PROMPT`.
3. Add a `build_runtime()` function returning
   `AgentRuntime(name=AGENT_NAME, system_prompt=SYSTEM_PROMPT, tools=ALLOWED_TOOLS)`.
4. Add a `main(argv)` wrapper (copy the pattern from
   `awx_troubleshooter.py`) — an unsupported debugging escape hatch for
   direct-module invocation, not a development workflow to build on
   (see [What constitutes an agent](#what-constitutes-an-agent) above).
5. Register it in `mantis.api.catalog.build_default_catalog()` (one
   `AgentCatalogEntry` pointing at this module's `build_runtime`/
   `AGENT_NAME`/`DEFAULT_PROMPT`, plus a `display_name`/`description`/
   `read_only` — see [docs/api.md](api.md)) — this is what makes it
   reachable via `GET /api/v1/agents`/`POST /api/v1/runs` and therefore
   `mantis run <agent_name> "..."`. Optionally add its ID to
   `mantis.cli.CONVENIENCE_AGENTS` for a dedicated top-level command.
6. Write tests for anything agent-specific (there usually isn't much,
   since the runtime and tools are already tested independently).

### Catalog registration (required for API/CLI access)

Steps 1–4 above make an agent *runnable* only via the unsupported
`python -m mantis.agents.<name>` debugging escape hatch; step 5 is what
makes it *invokable* through the API and therefore the official CLI —
the only supported path. An agent module with no
catalog entry is dead code from the API/CLI's perspective — the catalog
in `mantis.api.catalog` is the single source of truth both the HTTP API
and the CLI resolve agent IDs against (see
[docs/architecture.md](architecture.md)).

## AWX Troubleshooter example

`mantis/agents/awx_troubleshooter.py` is the reference implementation:

- **Goal**: investigate recent failed AWX jobs and produce an
  evidence-based summary.
- **Tools**: `["awx_recent_failed_jobs", "awx_get_job_failure"]` — both
  read-only. `awx_recent_failed_jobs` lists recent failures;
  `awx_get_job_failure` (#28) fetches structured, deterministically
  selected job-event evidence for one already-known job id — see
  [docs/awx-job-failure.md](awx-job-failure.md). The system prompt tells
  the model which one actually answers a given request.
- **Prompt discipline**: never invent details; treat
  `stdout_retrieval_error` (either tool) as separate from AWX's own
  reported failure; prefer `failure_excerpt`/`structured_failures` over
  `stdout_tail`/`stdout_context` for root-cause evidence; only call
  something a "recurring pattern" with at least two corroborating jobs;
  state explicitly when fewer failed jobs exist than requested or when
  root cause is uncertain.
- **Runtime tuning**: `tool_call_budget=1` (one successful call — to
  whichever of the two tools the request calls for — is all this agent
  ever needs per turn; chaining both is left to a future, more capable
  agent, see [docs/awx-job-failure.md](awx-job-failure.md) — and see
  [docs/architecture.md](architecture.md#withholding-tools-once-an-agent-has-what-it-needs))
  and `temperature=0.1` for focused, consistently formatted output from
  smaller local models.

Run it via the API/CLI: `mantis awx-troubleshooter "..."` or `mantis run
awx-troubleshooter "..."` — both HTTP calls to a running `mantis serve`
(see [docs/api.md](api.md)). The only other way to run it,
`python -m mantis.agents.awx_troubleshooter`, is the unsupported
debugging escape hatch described above — not a normal way to use this
agent.

## System Troubleshooter example

`mantis/agents/system_troubleshooter.py` (#11) is Mantis's first real
multi-source investigation agent — see
[docs/system-troubleshooter.md](system-troubleshooter.md) for the full
design and a worked `ferros-c01` example:

- **Goal**: investigate a system-level troubleshooting question by
  correlating historical AWX evidence, current-state TCP evidence,
  time-series Prometheus evidence, and Loki log evidence in one
  investigation.
- **Tools**: `["awx_recent_failed_jobs", "awx_get_job_failure",
  "check_tcp_connectivity", "prometheus_query", "prometheus_query_range",
  "loki_query"]` — all six already implemented (#28/#8/#9/#10), all
  read-only. No integration or tool logic lives in this agent module.
- **Prompt discipline**: does not hard-code a fixed investigation
  sequence; distinguishes historical/current-state/time-series/log
  evidence explicitly; never treats a failed or unavailable tool call as
  evidence about the target system; separates facts from hypotheses;
  ties confidence to evidence strength; preserves disagreement between
  sources rather than forcing one narrative; discourages repeated
  identical calls; requires acknowledgment of `meta.truncated` results.
- **Runtime tuning**: `tool_call_budget=8` and `max_iterations=12` —
  both far higher than the AWX Troubleshooter's `1`/default 8, since
  this agent is expected to chain multiple distinct evidence sources
  (potentially all six tools plus a follow-up query or two) in one
  investigation rather than answer from a single tool's data. See
  [docs/system-troubleshooter.md](system-troubleshooter.md#budgets) for
  the full rationale.

Run it via the API/CLI: `mantis system-troubleshooter "..."` or `mantis
run system-troubleshooter "..."`. The only other way to run it,
`python -m mantis.agents.system_troubleshooter`, is the unsupported
debugging escape hatch described above — not a normal way to use this
agent.

## Incident Triage example

`mantis/agents/incident_triage.py` is Mantis's first agent requiring an
explicit incident window as input — see
[docs/incident-triage.md](incident-triage.md) for the full design,
its distinction from System Troubleshooter, and a worked example:

- **Goal**: review a *specific* incident over an *explicit* time window
  and report what the evidence actually shows during that window, what
  remains unproven, and what to check next — rather than System
  Troubleshooter's open-ended "why is X broken" diagnosis.
- **Tools**: `["awx_recent_failed_jobs", "awx_get_job_failure",
  "check_tcp_connectivity", "prometheus_query", "prometheus_query_range",
  "loki_query", "git_recent_changes", "kubernetes_list_pods",
  "kubernetes_list_deployments", "kubernetes_list_nodes",
  "kubernetes_list_events"]` — all eleven already implemented
  (#28/#8/#9/#10/#17/#18), all read-only. No integration or tool logic
  lives in this agent module. `build_runtime()` additionally asserts
  every one of these is registered read-only before constructing the
  runtime, raising `ConfigurationError` (never silently accepting a
  mutating tool) if that ever stops being true.
- **Prompt discipline**: requires an explicit incident target and time
  window before investigating anything, and asks for missing
  information rather than guessing or exploring broadly to find it;
  keeps current-state observations (TCP, an instant Prometheus query,
  current Kubernetes state) explicitly separate from the incident's
  historical timeline; keeps "a commit exists in history," "a commit
  was deployed," and "a commit caused the incident" as three separate
  claims; requires an explicit evidence-coverage report (queried
  successfully / no match / unavailable / not queried) rather than
  implying comprehensive review.
- **Runtime tuning**: `tool_call_budget=13` and `max_iterations=17` —
  sized for a full incident review that may touch every evidence
  category at least once. See
  [docs/incident-triage.md](incident-triage.md#budgets) for the full
  rationale.

Run it via the API/CLI: `mantis run incident-triage "..."`. The only
other way to run it, `python -m mantis.agents.incident_triage`, is the
unsupported debugging escape hatch described above — not a normal way
to use this agent.

## Envisioned future agents

These reuse the same runtime and largely the same tools — see
[docs/architecture.md](architecture.md) for why this composition is
cheap:

- **Daily Operations Digest Agent** — a scheduled, read-only agent that
  summarizes the prior day's AWX activity, alerts, and log anomalies
  using the same shared tools with a digest-oriented prompt.

None of these require changes to the runtime, the registry, or existing
tools — only new tool implementations (where genuinely new capability is
needed) and a new thin agent module.
