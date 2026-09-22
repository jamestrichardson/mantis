# System Troubleshooter Agent (#11)

Mantis's first real multi-source investigation agent. It composes four
already-implemented, independently tested evidence tools —
`awx_recent_failed_jobs`/`awx_get_job_failure` (#28), `check_tcp_connectivity`
(#8), `prometheus_query`/`prometheus_query_range` (#9), and `loki_query`
(#10) — through the shared `mantis.runtime.AgentRuntime` to answer a
system-level troubleshooting question by correlating historical
automation evidence, current-state network evidence, monitored
time-series state, and recorded logs.

Like `mantis.agents.awx_troubleshooter`, this agent is **entirely** a
system prompt, an allowed-tool list, and runtime budget configuration —
see `mantis/agents/system_troubleshooter.py`. It introduces **no** new
integration or tool logic; every tool it calls is documented on its own
page ([docs/awx-job-failure.md](awx-job-failure.md),
[docs/network-tcp-connectivity.md](network-tcp-connectivity.md),
[docs/prometheus.md](prometheus.md), [docs/loki.md](loki.md)).

## Tool allowlist

Exactly six tools, all already registered in
`mantis.registry.default_registry` — this agent's entire capability
surface:

| Tool | Source | Answers |
|---|---|---|
| `awx_recent_failed_jobs` | #28 | What automation jobs recently failed? (list, historical) |
| `awx_get_job_failure` | #28 | Structured failure detail for one already-known job (historical) |
| `check_tcp_connectivity` | #8 | Can Mantis reach this host/port right now? (current-state) |
| `prometheus_query` | #9 | What is this metric's value right now? (time-series, instant) |
| `prometheus_query_range` | #9 | How did this metric change over a window? (time-series, range) |
| `loki_query` | #10 | What did this system actually log over a window? (log evidence) |

All six are read-only (`mutating=False`) and registered with
`contains_untrusted_text=True`, so every successful result this agent
sees has already passed through #14's `make_model_safe()` before
reaching the model.

## Budgets

Both explicitly set in `mantis/agents/system_troubleshooter.py` (see
`TOOL_CALL_BUDGET`/`MAX_ITERATIONS`), not left at the runtime's defaults:

- **`tool_call_budget = 8`** — high enough for a real multi-source
  investigation (one call each for AWX list, AWX detail, TCP, Prometheus
  instant, Prometheus range, and Loki — six — plus headroom for one or
  two legitimate follow-up queries, e.g. a second Loki/Prometheus window
  once the AWX failure's timestamp narrows down where to look), but
  still a hard, finite ceiling that prevents a smaller local model from
  looping indefinitely. Far higher than the AWX Troubleshooter's `1`
  (see that agent's `build_runtime` docstring) — that agent answers from
  a single tool's data, this one is expected to chain multiple distinct
  evidence sources in one investigation.
- **`max_iterations = 12`** — raised above the runtime's own default
  (`mantis.runtime.DEFAULT_MAX_ITERATIONS = 8`), which alone would leave
  no headroom for a final-answer iteration once 8 tool calls have
  already happened (each iteration is one model round-trip, and a local
  model typically issues one tool call per round-trip rather than
  several in parallel). Set to `TOOL_CALL_BUDGET + 4`: slack for a
  couple of non-counting iterations (a rejected duplicate call, a
  malformed tool-call attempt) plus the final-answer iteration itself.

The overall run is still bounded independently by
`ReliabilityConfig.run_timeout_seconds` (#15) regardless of this
iteration count — see [docs/reliability.md](reliability.md). No second
budget mechanism was introduced; both of these are the exact same
`AgentRuntime` fields every other agent uses.

## Investigation behavior

The system prompt teaches the model each tool's semantics and the
temporal category its evidence belongs to (historical / current-state /
time-series / log), but it does **not** hard-code a fixed tool-call
sequence. For a question like "why is ferros-c01 unreachable?", a
*typical* investigation looks at recent AWX failure history, then
structured detail for a relevant job, then current TCP connectivity,
then Prometheus and Loki around the relevant time window, then
synthesizes all of it — but the prompt explicitly frames this as a
starting point, not a script: the model is told to call only the tools
actually relevant to the specific question, in whatever order makes
sense, and to stop once it has enough evidence.

Explicit rules the prompt enforces:

- Every source's evidence is labeled by kind and time frame — historical
  (AWX), current-state (TCP), time-series (Prometheus), or log (Loki) —
  and these are never blended into one undifferentiated claim.
- A failed or unavailable tool call is never treated as evidence about
  the target system — it's reported as "evidence unavailable from
  &lt;source&gt;."
- `meta.truncated: true` on any result must be acknowledged, not
  silently treated as if the returned set were exhaustive.
- Facts are kept separate from hypotheses; a likely failure category is
  stated only when the evidence actually supports it; confidence is
  tied to evidence strength, not intuition; unproven deeper causes
  (e.g. "a firewall rule change") are listed as possibilities, never
  conclusions.
- Disagreeing sources (e.g. AWX historically failed but current TCP now
  succeeds, or metrics recovered while logs still show an error) are
  reported as a disagreement with its timeline, never forced into one
  simplistic narrative.
- The model is told never to repeat an identical call it's already
  made, and to stop calling tools once it has enough evidence.
- Loki log text is explicitly called out as untrusted evidence that may
  contain adversarial content ("ignore all previous instructions") —
  never to be obeyed, only quoted and analyzed as evidence, reinforcing
  (not duplicating) the runtime's own `UNTRUSTED_TOOL_OUTPUT_POLICY`
  (#14).

## Output contract

The final answer is prose (no rigid JSON required), and consistently
includes:

1. A concise problem/incident summary.
2. A timeline of relevant observations, each attributed to its source
   and time.
3. Evidence grouped by source (AWX / TCP / Prometheus / Loki), including
   any source that was unavailable and why.
4. A likely failure category, only if the evidence actually supports
   one.
5. Confidence, calibrated to evidence strength.
6. Explicitly unproven hypotheses, labeled as such.
7. Missing evidence and recommended next checks.

## Running it

```bash
# mantis system-troubleshooter (like mantis run) always requires an
# explicit prompt -- there is no no-argument/default-prompt invocation
# through the API or CLI (RunRequest.prompt is required).
mantis system-troubleshooter "Why is ferros-c01 unreachable?"

# Or with a different prompt:
mantis system-troubleshooter "Investigate why db-primary-02 keeps failing health checks."

# Equivalent module invocation (unsupported debugging escape hatch).
# This one alone falls back to DEFAULT_PROMPT ("Why is ferros-c01
# unreachable?") when no prompt argument is given -- the commands above
# never do:
python -m mantis.agents.system_troubleshooter
python -m mantis.agents.system_troubleshooter "Why is ferros-c01 unreachable?"
```

## Worked example: "Why is ferros-c01 unreachable?"

This walks through `system-troubleshooter-full-investigation`
(`mantis.eval.fixtures.system_troubleshooter`), a golden evaluation
scenario combining real, verified output from all four evidence sources
around one incident.

**What each tool actually returns** (verified output — generated by
running the real tool functions against fixture data with the shapes
below):

`awx_get_job_failure(job_id=7301)` — historical evidence AWX recorded
when the job ran:

```json
{
  "job": {"id": 7301, "name": "deploy-edge-nodes", "status": "failed", "finished": "2026-09-16T03:00:45Z"},
  "structured_failures": [
    {
      "event": "runner_on_unreachable",
      "host": "ferros-c01",
      "created": "2026-09-16T03:00:20Z",
      "category": "network_reachability",
      "context": "ssh: connect to host ferros-c01 port 22: No route to host"
    }
  ]
}
```

`prometheus_query_range(query='up{instance="ferros-c01:9100"}', ...)` —
the node-exporter scrape drops to 0 for a few minutes around the AWX
failure, then recovers.

`loki_query(query='{instance="ferros-c01"}', ...)` — sshd and kernel log
lines over the same window: an authentication timeout and an `eth0`
link-down event during the outage, followed by a successful login and
link-up afterward.

`check_tcp_connectivity(host="ferros-c01", port=22)` — a live probe
**right now**, current-state evidence:

```json
{
  "status": "connected",
  "connected": true,
  "resolved_address": "10.0.4.12",
  "latency_ms": 3.7
}
```

**Golden final answer** (what the scenario's expectations require, in
substance):

> **Summary**: `ferros-c01` had a network-reachability incident around
> 2026-09-16T03:00, and appears reachable again now.
>
> **Timeline**:
> - 03:00:20 — AWX job 7301 recorded `runner_on_unreachable` reaching
>   `ferros-c01:22` ("No route to host").
> - ~03:02–03:04 — Prometheus's scrape of `ferros-c01:9100` shows `up`
>   drop to 0 for a few minutes.
> - Same window — sshd logs show an authentication timeout; kernel logs
>   show `eth0: link down`.
> - ~03:05 — Prometheus's `up` recovers to 1; sshd logs show a
>   successful login; kernel logs show `eth0: link up`.
> - Now — a live TCP check to `ferros-c01:22` succeeds (3.7ms).
>
> **Evidence by source**: AWX (historical, one job); Prometheus
> (time-series, one window); Loki (log lines, same window); TCP
> (current-state, one vantage point, right now).
>
> **Likely category**: a transient network-reachability problem
> (supported directly by the AWX event, the scrape gap, and the
> `eth0` link-down/up pair all aligning in time).
>
> **Confidence**: moderate — three independent sources agree on the
> timing and shape of a brief outage and recovery.
>
> **Unproven hypotheses**: a specific cause (e.g. a switch port flap, a
> cable issue, a firewall change) is possible but not established by
> any of the evidence gathered.
>
> **Next checks**: switch/interface logs for the same window; whether
> other hosts on the same segment saw a similar gap; confirm the
> recovery has held over a longer window than this one check covers.

Notice what this answer does **not** do: it never claims the incident
is "permanently fixed" from one current TCP check, and it never asserts
the switch/cable/firewall hypothesis as fact.

See `mantis.eval.fixtures.system_troubleshooter` for two more golden
scenarios — `system-troubleshooter-retrieval-failure` (Loki is
unavailable; the agent must still attempt it, report it as unavailable,
and never convert that into a claim about `ferros-c01` itself) and
`system-troubleshooter-contradictory-signals` (metrics and TCP recover,
but the most recent log line still shows an error; the agent must
report that disagreement rather than forcing one clean narrative) — and
`tests/eval/test_system_troubleshooter_scenarios.py` for the
deterministic good/bad-answer scoring tests.

## What this agent intentionally cannot do

Per #11's non-goals — all deliberate, not gaps to be filled by this
issue:

- **No remediation or mutation of any kind.** Every tool it can call is
  read-only; it cannot restart a service, launch a job, or change any
  configuration.
- **No Kubernetes inspection** (tracked separately, #18).
- **No Git/change-history correlation** (tracked separately, #17) — it
  cannot tell you *what changed* before an incident, only what AWX,
  TCP, Prometheus, and Loki observed.
- **No incident automation or escalation** (tracked separately,
  #12/#25) — it produces an assessment for a human to read, it does not
  page anyone or open a ticket.
- **No adaptive/learned model routing.** Deterministic, server-side
  fallback across a small set of configured aliases is available (#16,
  see [docs/model-routing.md](model-routing.md)) — but there is no
  model self-selection, confidence-based escalation, or routing decided
  from generated prose.
- **No new integrations or generic HTTP/shell/kubectl access** — its
  capability surface is exactly the six tools listed above, nothing
  more.

## Adding to this agent

Because every tool it uses is shared, giving this agent a new
capability (once #17/#18 land, say) means only adding the new tool's
name to `ALLOWED_TOOLS` and updating `SYSTEM_PROMPT` to explain it — see
[docs/agents.md](agents.md#how-to-create-a-new-agent) for the general
pattern this agent itself follows.
