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
```

- `name` must match `schema["function"]["name"]` exactly (enforced at
  registration time).
- `handler` is a plain Python callable. Its keyword arguments must match
  the schema's `parameters`. It should return data structures (dicts,
  lists, primitives) that serialize cleanly to JSON — this is what the
  model sees.
- `mutating` defaults to `False`. Every tool in this milestone is
  read-only; see [docs/security.md](security.md) for how mutating
  tools will be handled later.

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
   integration-specific exceptions (see `AWXError`/`AWXStdoutError` for
   the pattern of distinguishing different failure classes).
2. Add a function in `mantis/tools/<system>.py` that calls the
   integration, applies any necessary preprocessing (see
   `mantis/tools/_text.py` for reusable helpers), and returns
   JSON-serializable data.
3. Write the OpenAI-compatible schema next to it, with a clear
   `description` — this is the model's *only* information about when and
   how to call the tool.
4. Register it: `default_registry.register(Tool(name=..., schema=...,
   handler=..., category=..., mutating=...))`.
5. Add the tool's name to any agent's `ALLOWED_TOOLS` that should use it.
6. Write unit tests that mock the integration's HTTP layer (see
   `tests/test_awx_tools.py`) — never require a live external system.

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
  is captured as `stdout_retrieval_error` and `failure_excerpt`/
  `stdout_tail` are left empty for that job — this is never conflated
  with `job_explanation`/`failed`, which reflect AWX's own report of what
  happened to the job.

This preprocessing lives entirely in `mantis/tools/_text.py` and
`mantis/tools/awx.py`; it can be improved (better heuristics, different
tail length, etc.) without any change to the AWX Troubleshooter agent.
