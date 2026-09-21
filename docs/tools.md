# Tools

A Mantis **tool** is a semantic, LLM-facing operation: a narrow Python
function plus an OpenAI-compatible schema, registered under a unique name
in a shared `ToolRegistry`. Tools are how agents actually do anything —
an agent that lists no tools can only talk.

## What constitutes a Mantis tool

Concretely, a tool is a `mantis.registry.Tool`:

```python
@dataclass(frozen=True)
class Tool:
    name: str
    schema: Mapping[str, Any]   # OpenAI-compatible function/tool schema
    handler: Callable[..., Any] # returns JSON-serializable data
    category: str = "general"
    mutating: bool = False
    description: str = ""
    contains_untrusted_text: bool = True
```

- `name` must match `schema["function"]["name"]` exactly (enforced at
  registration time).
- `handler` is a plain Python callable. Its keyword arguments must match
  the schema's `parameters`. It should return data structures (dicts,
  lists, primitives) that serialize cleanly to JSON — this is what the
  model sees. `AgentRuntime` runs every successful result through
  `mantis.security.make_model_safe()` before it reaches the model
  (redaction, size bounding, untrusted-evidence marking) — a tool never
  needs to implement its own prompt-injection defense, only its own
  domain-aware preprocessing/bounding on top. See
  [docs/security.md](security.md).
- `mutating` defaults to `False`. Every tool in this milestone is
  read-only; see [docs/security.md](security.md) for how mutating
  tools will be handled later.
- `contains_untrusted_text` defaults to `True`: does this tool's output
  potentially contain arbitrary external text Mantis doesn't control
  (AWX stdout, a Loki log line, Git content, a Kubernetes event
  message)? Leave the default alone unless a tool's result is something
  Mantis fully constructs itself (a small fixed status object, say) —
  external operational evidence should always default safely. See
  [docs/security.md](security.md).

Tools should be **semantic**, not raw API pass-throughs: they decide what
data actually matters to an agent and preprocess it accordingly (see "AWX
tool behavior" below). Compare this to `mantis.integrations`, which has no
opinion about what an LLM needs — it just knows how to talk to the
external API.

## Schema + handler relationship

The schema is what the model sees and reasons about (name, description,
parameter types/constraints); the handler is what actually runs. Keeping
them next to each other in the same module, under the same tool name,
keeps them from drifting apart. See `mantis/tools/awx.py` for the pattern:

```python
def awx_recent_failed_jobs(limit: int = 5) -> dict[str, Any]:
    ...

AWX_RECENT_FAILED_JOBS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "awx_recent_failed_jobs",
        "description": "...",
        "parameters": {"type": "object", "properties": {"limit": {...}}, "required": []},
    },
}

default_registry.register(
    Tool(
        name="awx_recent_failed_jobs",
        schema=AWX_RECENT_FAILED_JOBS_SCHEMA,
        handler=awx_recent_failed_jobs,
        category="awx",
        mutating=False,
    )
)
```

## Registration

Registration happens at **import time**, as a side effect of importing the
tool's module, into the shared `mantis.registry.default_registry`.
`mantis/tools/__init__.py` imports every built-in tool module so that a
plain `import mantis.tools` is enough to populate the registry. Agents
(via `AgentRuntime`) then resolve their declared tool *names* against this
registry — they never import or construct `Tool` objects themselves.

## Read-only vs. mutating classification

`Tool.mutating` is the architectural marker for "can this change external
system state." Every currently implemented tool has `mutating=False`.
This is not just documentation — it's the hook a future policy/approval
layer will gate on before allowing a mutating tool's handler to run (see
[docs/security.md](security.md)). When adding a new tool, set
`mutating=True` for anything that launches jobs, writes data, restarts
services, changes DNS, applies infrastructure changes, etc.

## How to add a new tool

1. If needed, add/extend an integration in `mantis/integrations/` that
   knows how to talk to the external system. Integrations should raise
   integration-specific exceptions subclassing
   `mantis.reliability.IntegrationError` (see `AWXError`/`AWXStdoutError`
   for the pattern of distinguishing different failure classes) and use
   the shared timeout/retry contract rather than inventing one — see
   [docs/reliability.md](reliability.md).
2. Add a function in `mantis/tools/<system>.py` that calls the
   integration, applies any necessary preprocessing (see
   `mantis/tools/_text.py` for reusable helpers), and returns
   JSON-serializable data. Attach a `meta` key built from
   `mantis.contracts.QueryMeta` — see "Result contracts" below — and
   represent any tool-level retrieval failure as a
   `mantis.contracts.ToolError` instead of a bare string. If a failure is
   swallowed into partial evidence rather than raised (see
   `mantis.tools.awx._summarize_job`'s per-job stdout handling), also
   accept a `_reliability_report` callback and call it with the failure's
   classification — otherwise the run-local short circuit never learns
   about it. See [docs/reliability.md](reliability.md#run-local-short-circuit).
3. Write the OpenAI-compatible schema next to it, with a clear
   `description` — this is the model's *only* information about when and
   how to call the tool.
4. Register it: `default_registry.register(Tool(name=..., schema=...,
   handler=..., category=..., mutating=..., contains_untrusted_text=...))`.
   Leave `contains_untrusted_text` at its default (`True`) for any tool
   returning external evidence — which, so far, is every tool.
5. Add the tool's name to any agent's `ALLOWED_TOOLS` that should use it.
6. Write unit tests that mock the integration's HTTP layer (see
   `tests/test_awx_tools.py`) — never require a live external system.

## Result contracts: evidence and provenance

Every tool eventually needs to answer the same questions for an agent —
and, later, for logic that correlates evidence across multiple systems —
without each one inventing its own shape: which system did this come
from, when was it queried, was the result truncated, and did anything go
wrong *retrieving* it that's separate from what the evidence itself shows
about the system under investigation. `mantis.contracts` defines that
shared, small vocabulary:

- **`QueryMeta`** — provenance for a single tool call: `source_system`
  (e.g. `"awx"`, `"prometheus"`), `query_time` (when Mantis queried,
  defaults to now), `observation_time` (when the evidence itself was
  observed — set only for tools returning one point-in-time result, e.g.
  a Prometheus instant query; left `None` for tools returning multiple
  records that each carry their own natural timestamp, like AWX's job
  list, since no single batch-level value could represent that without
  being misleading — document which per-record field serves that purpose
  instead), `query_window` (for range-style queries; `None` for
  point-in-time ones), `truncated` (more matching evidence exists than
  was returned — distinct from *fewer records existing* than were
  requested), `derived_fields` (names of fields in each record that are
  Mantis-computed interpretation rather than source-reported data — this
  is what makes "evidence vs. interpretation" a machine-checkable
  distinction instead of only a naming convention), and
  `contract_version`. Attach it as a `meta: QueryMeta(...).to_dict()` key
  on the tool's result.
- **`ToolErrorKind` / `ToolError`** — a consistent, typed way to report a
  *tool-level* failure (`RETRIEVAL_ERROR`, `TIMEOUT`, `AUTH_ERROR`,
  `NOT_FOUND`, `RATE_LIMITED`, `UPSTREAM_ERROR`, `UNKNOWN`). Never confuse
  this with the observed state of the system being investigated — a
  failed AWX job is evidence a tool successfully retrieved, not a
  `ToolError`; a network timeout fetching that job's stdout is. Use
  `ToolError(kind=..., message=...).to_dict()` anywhere a tool previously
  returned a bare error string.

**This is deliberately additive, not a rigid `{meta, records, errors}`
envelope.** With only one real tool (AWX) implemented so far, guessing
the fully-normalized shape that will actually fit Prometheus/Loki/network
tools too would be premature. `awx_recent_failed_jobs` adopts it by
adding a `meta` key onto its existing shape and retyping
`stdout_retrieval_error` from a string to a `ToolError.to_dict()` —
every previously existing field (`id`, `failure_excerpt`, `stdout_tail`,
...) stays exactly where it was. Follow this same pattern for
Prometheus/Loki/network tools: add `meta`, type errors as `ToolError`,
keep everything else tool-specific.

Classifying *every* integration failure into the right `ToolErrorKind`
(auth vs. timeout vs. rate-limit vs. server error) is handled by the
shared reliability contract, not by this module or by each integration
guessing independently — see [docs/reliability.md](reliability.md).
`mantis.reliability.IntegrationError.to_tool_error_kind()` is the one
place that maps the richer internal classification
(`mantis.reliability.IntegrationErrorKind`) onto this stable
`ToolErrorKind` contract, so AWX (and any future integration) reports a
real, specific kind — a stdout timeout is `TIMEOUT`, a 500 from AWX
itself is `UPSTREAM_ERROR` — never a single generic catch-all.

`CONTRACT_VERSION` bumps on a breaking change to this shape (a field
removed or renamed); adding a new optional field does not require a bump
since every field besides `source_system`/`kind`/`message` has a default.

## How agents select tools

An agent declares tool access as a plain list of names:

```python
ALLOWED_TOOLS = ["awx_recent_failed_jobs"]
```

`AgentRuntime` resolves this list against the registry once, at
construction time (`registry.subset(self.tools)`), and only ever sends
schemas for *that* resolved set to the model — regardless of what else is
registered globally. This is what keeps tool exposure narrow per agent
even as the shared registry grows.

## AWX tool behavior

`awx_recent_failed_jobs(limit: int = 5)`:

- Queries `GET /api/v2/jobs/` with `status=failed`, `order_by=-finished`,
  and a `page_size` clamped to `limit` (max 10).
- For each job, collects: `id`, `name`, `status`, `started`, `finished`,
  `elapsed`, `failed`, `job_explanation`, plus human-readable `inventory`,
  `project`, and `job_template` names (from AWX's `summary_fields` when
  present, otherwise the raw id).
- Retrieves stdout via `GET /api/v2/jobs/{id}/stdout/?format=txt` with
  `Accept: text/plain`. If AWX's response looks like its "too large to
  display, use the download feature" notice, transparently retries with
  `format=txt_download`.
- Preprocesses stdout into:
  - `failure_excerpt`: lines matching high-value markers (`FAILED!`,
    `fatal:`, `UNREACHABLE!`, `ERROR!`, `Traceback`, `exception`,
    `PLAY RECAP`, `failed=`, `unreachable=`, `rescued=`, `ignored=`),
    biased toward the most recent matches, with a small context window.
  - `stdout_tail`: the final ~12,000 characters of stdout, with a leading
    notice if earlier output was omitted.
- If stdout retrieval itself fails (network error, AWX API error), that
  is captured as `stdout_retrieval_error` — a `ToolError.to_dict()`
  (`{"kind": "retrieval_error", "message": ...}`), `None` when retrieval
  succeeded — and `failure_excerpt`/`stdout_tail` are left empty for that
  job. This is never conflated with `job_explanation`/`failed`, which
  reflect AWX's own report of what happened to the job.
- The overall result carries a top-level `meta` (`QueryMeta.to_dict()`,
  `source_system="awx"`). `meta.truncated` is `True` when AWX reports more
  matching failed jobs (`count` in its API response) than were actually
  returned — i.e. more evidence exists than what's in `jobs`. This is
  distinct from simply fewer failed jobs existing than the requested
  `limit`, which is not truncation.
- `meta.derived_fields` is `["failure_excerpt", "stdout_tail"]`
  (`mantis.tools.awx.DERIVED_JOB_FIELDS`) — every other job field is
  AWX-reported verbatim. `meta.observation_time` is left `None`: this
  call returns multiple jobs, each with its own `finished` timestamp, so
  no single batch-level observation time applies.

This preprocessing lives entirely in `mantis/tools/_text.py` and
`mantis/tools/awx.py`; it can be improved (better heuristics, different
tail length, etc.) without any change to the AWX Troubleshooter agent.

### `awx_get_job_failure` (#28): structured job-event evidence

`awx_get_job_failure(job_id: int)` fetches deterministically selected,
bounded AWX job-*event* records (task failures, unreachable hosts) as
the preferred evidence source for one job, with bounded stdout used only
as supporting/fallback context rather than the primary source — see
[docs/awx-job-failure.md](awx-job-failure.md) for the full design
(selection rules, pagination/inspection caps, provenance, partial-success
behavior, and a concrete sample result). Selection logic lives in
`mantis/tools/_awx_events.py`, independently testable without a model.

## Network tool behavior

### `check_tcp_connectivity` (#8): current-state TCP connectivity

`check_tcp_connectivity(host: str, port: int)` is Mantis's first
current-state (not historical) evidence tool: a bounded, deadline-aware
TCP connect check answering "can Mantis reach this host/port right
now?" — see [docs/network-tcp-connectivity.md](network-tcp-connectivity.md)
for the full design (status vocabulary, IPv4/IPv6 multi-address
semantics, failure precedence, deadline handling, private-network/SSRF
posture, and how it correlates with #28's historical AWX evidence in an
evaluation scenario). DNS/socket mechanics live in
`mantis/integrations/network.py`; semantic shaping in
`mantis/tools/network.py`.

### `dns_lookup` (#109): DNS evidence from a chosen resolver perspective

`dns_lookup(name: str, record_type: str = "A", resolver_alias: str = "internal")`
is Mantis's first DNS evidence tool — a bounded, deadline-aware query
against exactly one server-side-configured resolver *profile*,
answering "what does this specific resolver perspective report for
this name right now?" — deliberately not "what is the globally correct
answer" (there often isn't one; see split-horizon DNS). See
[docs/dns-lookup.md](dns-lookup.md) for the full design (resolver
profile configuration, split-horizon semantics, supported record types,
the `ok`/`nxdomain`/`no_data`/`servfail`/`refused` status vocabulary and
how it stays distinct from transport failures, deterministic
multi-server failover, CNAME-chain bounding, and security rationale for
alias-only resolver selection). Query/failover mechanics live in
`mantis/integrations/dns.py`; semantic shaping in `mantis/tools/dns.py`.

## HTTP tool behavior

### `http_probe` (#110): bounded HTTP(S) evidence from a chosen origin

`http_probe(target_alias: str, path: str = "/", method: str = "GET")`
sends a single bounded GET/HEAD request against exactly one
server-side-configured HTTP(S) target, answering "what does this
origin's endpoint currently return?" — deliberately not a general web
fetch. See [docs/http-probe.md](http-probe.md) for the full design
(target-profile configuration, why proxy trust and redirect-following
are disabled, the header/body/path bounds, why every status code
100-599 is normal successful evidence rather than an error, and
security rationale for alias-only target selection). Request/streaming
mechanics live in `mantis/integrations/http.py`; semantic shaping in
`mantis/tools/http.py`.

## TLS tool behavior

### `tls_certificate_inspect` (#111): certificate metadata independent of verification

`tls_certificate_inspect(target_alias: str)` inspects the TLS
certificate presented at exactly one server-side-configured direct TLS
endpoint, deliberately independent of whether it would pass
verification — a self-signed, expired, not-yet-valid, or
hostname-mismatched certificate still returns full metadata, never
collapsed into "no certificate available." See
[docs/tls-certificate-inspection.md](tls-certificate-inspection.md) for
the full design (the "inspect != verify" requirement, the two-handshake
mechanism, why chain trust/hostname match/time validity stay
independent dimensions, and the SNI/trust-store posture). Handshake/
certificate-parsing mechanics live in `mantis/integrations/tls.py`;
semantic shaping in `mantis/tools/tls.py`.

## Prometheus tool behavior

### `prometheus_query` / `prometheus_query_range` (#9): time-series evidence

`prometheus_query(query, time=None)` and
`prometheus_query_range(query, start, end, step)` are Mantis's first
time-series evidence tools — bounded, deterministically ordered
PromQL evidence answering "what did monitored state do (at an instant,
or over a window)?", distinct from #28's historical AWX evidence and
#8's current-state TCP evidence. See [docs/prometheus.md](prometheus.md)
for the full design (PromQL/time/range validation, cardinality/sample
bounding, truncation correctness, deterministic ordering, query errors
vs. retrieval failures, and `up` metric semantics). HTTP/auth/reliability
mechanics live in `mantis/integrations/prometheus.py`; semantic
shaping in `mantis/tools/prometheus.py`.

## Loki tool behavior

### `loki_query` (#10): log evidence, the highest-risk untrusted-text source

`loki_query(query, start, end, direction=None)` is Mantis's first
log-evidence tool — a bounded LogQL range query answering "what did a
system actually log over this window?", distinct from #28's historical
AWX evidence, #8's current-state TCP evidence, and #9's time-series
Prometheus evidence. See [docs/loki.md](loki.md) for the full design
(LogQL/time/direction validation, stream/line/total-output bounding,
truncation correctness, deterministic ordering, nanosecond timestamp
precision, query errors vs. retrieval failures, and why raw log text is
treated as Mantis's highest-risk untrusted evidence source).
HTTP/auth/reliability mechanics live in `mantis/integrations/loki.py`;
semantic shaping in `mantis/tools/loki.py`.

## Kubernetes tool behavior

### `kubernetes_list_pods` / `kubernetes_list_deployments` / `kubernetes_list_nodes` / `kubernetes_list_events` (#18): cluster evidence

Four read-only, bounded Kubernetes evidence tools — pod/workload status,
Deployment rollout status, node conditions, and recent events — answering
"what does the cluster API currently report?", distinct from #28's
historical AWX evidence, #8's current-state TCP evidence, #9's
time-series Prometheus evidence, and #10's log evidence. See
[docs/kubernetes.md](kubernetes.md) for the full design (auth modes,
RBAC guidance, bounding constants, truncation semantics, provenance, and
what pod/deployment/node/event state does and does not prove). Auth/
config/API mechanics live in `mantis/integrations/kubernetes.py`;
semantic shaping in `mantis/tools/kubernetes.py`. Deliberately no
mutation, `exec`/`attach`/`port-forward`, or generic arbitrary
Kubernetes API browsing — see #18's non-goals.
