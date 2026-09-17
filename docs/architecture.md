# Architecture

Mantis has four conceptual layers, each with a single responsibility, plus
a model gateway that sits beside (not inside) the stack.

```
┌───────────────────────────────────────────────────────────────┐
│                              Agents                             │
│   mantis.agents.awx_troubleshooter                              │
│   - system prompt                                                │
│   - allowed tool names (narrow)                                  │
│   - model config                                                 │
│   No tool logic. No model/tool-loop logic.                       │
└───────────────────────────────────┬─────────────────────────────┘
                                     │ AgentRuntime(name, system_prompt, tools, ...)
                                     ▼
┌───────────────────────────────────────────────────────────────┐
│                          Agent Runtime                          │
│   mantis.runtime.AgentRuntime                                   │
│   - calls the model via LiteLLM (OpenAI-compatible)              │
│   - supplies only the agent's allowed tool schemas               │
│   - dispatches tool calls, detects duplicates, enforces limits   │
│   - runs every result through the untrusted-evidence trust       │
│     boundary before the model sees it (mantis.security)          │
│   - logs iterations and tool activity                            │
│   - returns the model's final answer                             │
└───────────────────────────────────┬─────────────────────────────┘
                                     │ registry.subset(agent.tools)
                                     ▼
┌───────────────────────────────────────────────────────────────┐
│                          Tool Registry                          │
│   mantis.registry.ToolRegistry / default_registry               │
│   name -> Tool(schema, handler, category, mutating,             │
│                contains_untrusted_text, description)             │
└───────────────────────────────────┬─────────────────────────────┘
                                     │ registered at import time by
                                     ▼
┌───────────────────────────────────────────────────────────────┐
│                              Tools                               │
│   mantis.tools.awx.awx_recent_failed_jobs                        │
│   - semantic, narrow function signatures                         │
│   - return structured, preprocessed, LLM-sized data              │
│   - carry an OpenAI-compatible schema + read-only/mutating flag  │
└───────────────────────────────────┬─────────────────────────────┘
                                     │ built on
                                     ▼
┌───────────────────────────────────────────────────────────────┐
│                           Integrations                          │
│   mantis.integrations.awx.AWXClient                              │
│   - raw HTTP/API client                                          │
│   - authentication, request/response handling                    │
│   - no agent-specific or LLM-facing behavior                     │
└───────────────────────────────────────────────────────────────┘
```

## Model gateway (LiteLLM)

Agents and the runtime talk to models exclusively through an
OpenAI-compatible `/v1/chat/completions` endpoint, provided by LiteLLM.
`mantis.config.LiteLLMConfig` holds the URL, API key, and model name;
`mantis.runtime.AgentRuntime` constructs an `openai.OpenAI` client pointed
at it. Nothing above the config layer knows or cares that the backing
model is currently Qwen3 via Ollama — swapping the backend is a LiteLLM
configuration change, not a Mantis code change.

## Data flow: a single AWX Troubleshooter turn

```
User prompt
   │
   ▼
AgentRuntime.run()
   │  messages = [system_prompt + UNTRUSTED_TOOL_OUTPUT_POLICY, user_prompt]
   │  tools = registry.schemas_for(["awx_recent_failed_jobs"])
   ▼
LiteLLM chat.completions.create(messages, tools)
   │
   ▼
Model responds with a tool_call: awx_recent_failed_jobs(limit=5)
   │
   ▼
AgentRuntime dispatches to registry.get("awx_recent_failed_jobs").handler
   │
   ▼
mantis.tools.awx.awx_recent_failed_jobs(limit=5)
   │  uses AWXClient to:
   │    - list_jobs(status="failed", order_by="-finished")
   │    - get_job_stdout(job_id) for each   (txt -> txt_download fallback)
   │  each read: explicit connect/read timeouts, retry_call() with the
   │  shared retry policy, classified via mantis.reliability on failure
   │  (see docs/reliability.md)
   │  preprocesses stdout -> failure_excerpt + stdout_tail
   ▼
mantis.security.make_model_safe(result, contains_untrusted_text=True)
   │  redact credentials -> bound to MODEL_TOOL_RESULT_MAX_CHARS
   │  -> mark "untrusted_evidence": true   (see docs/security.md)
   ▼
Structured JSON result appended to the conversation as a tool message
   │
   ▼
LiteLLM chat.completions.create(messages, tools)   (next iteration)
   │
   ▼
Model produces a final answer (no tool_calls) -> returned to the user
```

## Why tools are separated from agents

If tool logic lived inside `mantis.agents.awx_troubleshooter`, a future
System Troubleshooting Agent that also needs "recent failed AWX jobs"
would have to duplicate (and inevitably drift from) that logic. Keeping
tools in `mantis.tools`, registered once in a shared registry, means:

- The AWX stdout-fallback and preprocessing behavior is implemented and
  tested exactly once.
- Any bug fix or improvement (e.g. a smarter `failure_excerpt` heuristic)
  automatically benefits every agent that uses the tool.
- Agents stay reviewable: reading an agent's `ALLOWED_TOOLS` list and
  system prompt tells you its entire capability surface.

## Why agents receive narrow toolsets

`AgentRuntime` only ever sends the model the schemas for tools the agent
explicitly lists. This is not just an implementation convenience — it's a
safety boundary. Even though `awx_recent_failed_jobs` is registered in the
same shared registry as (eventually) mutating tools like "launch AWX job,"
the AWX Troubleshooter's `AgentRuntime` is constructed with
`tools=["awx_recent_failed_jobs"]` only, so the model has no way to
discover or invoke anything else, no matter what it is asked or how it is
prompted.

## How future agents reuse existing tools

Adding a new agent (see [docs/agents.md](agents.md)) never requires
touching `mantis.tools` or `mantis.integrations` unless genuinely new
capability is needed. For example, the planned System Troubleshooting
Agent's toolset is expected to be:

```python
ALLOWED_TOOLS = [
    "awx_recent_failed_jobs",     # already implemented, reused as-is
    "awx_get_job_failure",        # already implemented, reused as-is
    "check_tcp_connectivity",     # already implemented, reused as-is
    "prometheus_query",           # already implemented, reused as-is
    "prometheus_query_range",     # already implemented, reused as-is
    "loki_query",                 # already implemented, reused as-is
]
```

Every one of these tools requires zero changes to support this — each
is simply named in a second agent's `ALLOWED_TOOLS` list. See
[docs/agents.md](agents.md) for the full envisioned agent list.

## Why AWX stdout and failure detection are handled the way they are

AWX's stdout endpoint returns a small "too large, use the download
endpoint" response instead of full output past a certain size. The
integration layer (`mantis.integrations.awx.AWXClient.get_job_stdout`)
detects this and transparently retries with `format=txt_download`, so
every layer above it always receives real stdout. Separately, a failure
to *retrieve* stdout (network error, AWX API error) is raised as a
distinct `AWXStdoutError`/represented as `stdout_retrieval_error`, so it
can never be confused with the AWX job's own reported failure reason —
this distinction is enforced in the tool layer and again in the AWX
Troubleshooter's system prompt.

## Withholding tools once an agent has what it needs

`AgentRuntime` accepts a `tool_call_budget`: once that many tool calls
have succeeded in a run, tool schemas stop being offered on later
iterations, forcing a plain-text final answer. The AWX Troubleshooter sets
`tool_call_budget=1` — it only ever needs one successful
`awx_recent_failed_jobs` call.

This isn't just a correctness nicety (though it is one — the model
literally cannot loop on repeat calls once no tools are on offer). For
local models served through Ollama/llama.cpp behind LiteLLM, sending
`tools` in a request activates grammar-constrained decoding for the
*entire* response, not only the decision of whether to call a function.
That constraint is checked per token, and it applies even when the model
ends up writing a long, plain-text summary — which is precisely the kind
of thing that turns a few seconds of generation into several minutes.
Withholding `tools` once nothing further needs to be looked up lets that
final answer generate at normal (ungrammared) speed.

`tool_call_budget` defaults to `None` (unlimited) so a future multi-tool
agent that genuinely needs several different tools in sequence isn't
artificially cut off after its first call — this is a per-agent choice,
not a runtime-wide one.

## Future direction (architecture already supports)

- **Incident Triage Agent**: AWX + Prometheus + Loki + Kubernetes +
  git/change history tools, all pulled from the same registry.
- **Daily Operations Digest Agent**: a read-only, scheduled agent reusing
  existing tools with a summarization-focused prompt.
- **Mutating tools + approval gate**: mutating tools (`mutating=True` in
  their `Tool` registration) are architecturally distinct today even
  though none exist yet. See [docs/security.md](security.md) for the
  planned investigate → recommend → approve → remediate flow.
