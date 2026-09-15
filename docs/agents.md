# Agents

A Mantis **agent** is a specialized behavior defined almost entirely by
configuration: a system prompt, an allowed-tool list, and model settings.
Agents should be thin — all model-calling and tool-dispatch logic lives in
the shared runtime, and all tool logic lives in `mantis.tools`.

## What constitutes an agent

Concretely, an agent module (e.g. `mantis/agents/awx_troubleshooter.py`)
defines:

- `SYSTEM_PROMPT` — the agent's behavioral contract (what it should and
  should not claim, how to weigh evidence, tone, output structure).
- `ALLOWED_TOOLS` — a list of tool *names* already registered in
  `mantis.registry.default_registry`.
- `DEFAULT_PROMPT` — what to run if the user gives no prompt.
- `build_runtime()` — constructs and returns an `AgentRuntime` configured
  with the above.
- `main(argv)` — a small CLI wrapper around `build_runtime().run(prompt)`.

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
3. **Model configuration** — which model/settings to use (defaults to
   `LiteLLMConfig.from_env()`, but can be overridden per-agent if needed).

Agents should *not* specialize by embedding tool or integration logic
directly in the agent module.

## How to create a new agent

1. Create `mantis/agents/<agent_name>.py`.
2. Define `SYSTEM_PROMPT`, `ALLOWED_TOOLS` (names of tools already
   registered — add new tools first if needed, per
   [docs/tools.md](tools.md)), and `DEFAULT_PROMPT`.
3. Add a `build_runtime()` function returning
   `AgentRuntime(name=..., system_prompt=SYSTEM_PROMPT, tools=ALLOWED_TOOLS)`.
4. Add a `main(argv)` CLI wrapper (copy the pattern from
   `awx_troubleshooter.py`).
5. Register it in `mantis/cli.py`'s `AGENTS` dict so `mantis <agent-name>`
   works.
6. Write tests for anything agent-specific (there usually isn't much,
   since the runtime and tools are already tested independently).

## AWX Troubleshooter example

`mantis/agents/awx_troubleshooter.py` is the reference implementation:

- **Goal**: investigate recent failed AWX jobs and produce an
  evidence-based summary.
- **Tools**: `["awx_recent_failed_jobs"]` — deliberately just one, and
  read-only.
- **Prompt discipline**: never invent details; treat
  `stdout_retrieval_error` as separate from AWX's own reported failure;
  prefer `failure_excerpt` over `stdout_tail` for root-cause evidence;
  only call something a "recurring pattern" with at least two
  corroborating jobs; state explicitly when fewer failed jobs exist than
  requested or when root cause is uncertain.
- **Runtime tuning**: `tool_call_budget=1` (one successful call is all
  this agent ever needs — see
  [docs/architecture.md](architecture.md#withholding-tools-once-an-agent-has-what-it-needs))
  and `temperature=0.1` for focused, consistently formatted output from
  smaller local models.

Run it with `mantis awx-troubleshooter` or
`python -m mantis.agents.awx_troubleshooter`.

## Envisioned future agents

These reuse the same runtime and largely the same tools — see
[docs/architecture.md](architecture.md) for why this composition is
cheap:

- **System Troubleshooting Agent** — `awx_recent_failed_jobs` +
  `awx_get_job` + `check_tcp_connectivity` + `prometheus_query` +
  `loki_query`. Broader operational diagnosis than AWX alone.
- **Incident Triage Agent** — adds Kubernetes and git/change-history
  tools to correlate a live incident against recent changes.
- **Daily Operations Digest Agent** — a scheduled, read-only agent that
  summarizes the prior day's AWX activity, alerts, and log anomalies
  using the same shared tools with a digest-oriented prompt.

None of these require changes to the runtime, the registry, or existing
tools — only new tool implementations (where genuinely new capability is
needed) and a new thin agent module.
