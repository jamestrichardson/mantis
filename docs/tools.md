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
