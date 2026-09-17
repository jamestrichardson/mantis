# AWX structured job-failure evidence (`awx_get_job_failure`)

This page covers `awx_get_job_failure` (#28): a second AWX evidence tool,
alongside `awx_recent_failed_jobs`, that prefers AWX's own structured
job-event records over parsing raw stdout. See [docs/tools.md](tools.md)
for what a Mantis tool is in general and [docs/reliability.md](reliability.md)
for the shared timeout/retry/deadline/breaker contract this tool adopts
rather than reimplementing.

## Why structured events instead of raw stdout parsing

`awx_recent_failed_jobs` preprocesses raw Ansible stdout with a
marker-word heuristic (`FAILED!`, `fatal:`, `UNREACHABLE!`, ...) to find
high-value lines. That works, but it's still asking a regex to
reconstruct something AWX already knows precisely: every job records a
structured event stream (`/api/v2/jobs/{id}/job_events/`) where each
record has a real `event` type (`runner_on_failed`,
`runner_on_unreachable`, ...), the specific host and task involved, and
a timestamp — deterministic, typed data instead of a heuristic over
prose.

`awx_get_job_failure(job_id)` makes structured events the *primary*
evidence source for one job, and keeps stdout only as **supporting or
fallback context** — never the first thing a model has to parse. The
goal, stated plainly: never make a model wade through a huge raw event
stream or stdout dump when deterministic code can select the useful
failure evidence first.

## Event selection rules

Selection logic lives in `mantis.tools._awx_events`, entirely testable
without a model (see `tests/test_awx_events.py`).

An event is selected only if its AWX `event` type is one of:

| Event type | Meaning |
|---|---|
| `runner_on_failed` | A task failed. |
| `runner_on_unreachable` | A host could not be reached. |
| `runner_on_async_failed` | An async task failed. |
| `runner_item_on_failed` | A failed loop item within a task. |

Everything else (`runner_on_ok`, `runner_on_skipped`,
`playbook_on_stats`, ...) is not returned — this is a small, explicit
allowlist, not an attempt to interpret every AWX event type.

Each selected event is normalized into a bounded, allowlisted shape —
never the full raw `event_data` object, which can be large and varies
across AWX versions:

```json
{
  "id": 501,
  "counter": 12,
  "event": "runner_on_unreachable",
  "task": null,
  "host": "ferros-c01",
  "created": "2026-09-16T03:00:20Z",
  "failed": true,
  "unreachable": true,
  "category": "network_reachability",
  "context": "ssh: connect to host ferros-c01 port 22: No route to host"
}
```

`category` and `context` are **Mantis-derived**, not AWX-reported (see
"Provenance" below):

- `category` is a small, explicit normalization of the AWX `event` type
  — `network_reachability` for `runner_on_unreachable`, `task_failure`
  for the three `*_failed` variants, `other` otherwise. This is
  deliberately not a broader root-cause taxonomy — see the issue's
  non-goals. If a normalized category isn't clearly useful for a future
  event type, the raw AWX fields are preserved and no category is
  invented for it.
- `context` is a bounded excerpt of human-readable text — AWX's own
  per-event `stdout` field when present, falling back to
  `event_data.res.msg` — capped at
  `mantis.tools._awx_events.MAX_EVENT_CONTEXT_CHARS` (2,000 characters).

## Bounding and pagination

AWX job-event streams can be very large. Nothing here fetches or
inspects them unboundedly — every limit is a named constant in
`mantis.tools._awx_events`, sensible for smaller local models and easy
to change in one place:

| Constant | Default | Bounds |
|---|---|---|
| `MAX_EVENT_PAGES_INSPECTED` | 5 | Pages fetched from AWX for one tool call. |
| `MAX_EVENTS_INSPECTED` | 500 | Total raw event records inspected across all fetched pages. |
| `MAX_RETURNED_FAILURE_EVENTS` | 10 | Selected failure events actually returned. |
| `EVENT_PAGE_SIZE` | 100 | AWX `page_size` requested per page. |
| `MAX_EVENT_CONTEXT_CHARS` | 2,000 | Per-event bounded `context` text. |

A request is made with AWX's server-side `event__in` filter (e.g.
`event__in=runner_on_failed,runner_on_unreachable,...`) as an
optimization — but whether AWX actually honors that filter varies by
version and isn't something Mantis can verify, so the same client-side
selection (see above) is applied to every returned record regardless.
Correctness never depends on server-side filtering working.

### Two distinct truncation signals

A single ambiguous `truncated: true` flag can't answer two different
questions, so this tool reports both:

- **`meta.truncated`** (the existing `mantis.contracts.QueryMeta`
  field): true when more *relevant* failure events likely exist than
  were returned — either `MAX_RETURNED_FAILURE_EVENTS` was reached, or
  inspection itself was capped before the full stream was scanned (see
  next point), so completeness can't be claimed either way.
- **`event_inspection.inspection_capped`**: true only when a hard
  inspection cap (`MAX_EVENT_PAGES_INSPECTED` or `MAX_EVENTS_INSPECTED`)
  stopped scanning the *raw* event stream before AWX reported no further
  page — independent of how many failures were actually found. A job
  could have its full stream scanned (`inspection_capped: false`) while
  still returning a capped set of failures (`meta.truncated: true`), or
  have inspection stop early with zero failures found so far
  (`inspection_capped: true`, `meta.truncated: true`, `structured_failures: []`).

`event_inspection` also reports `events_inspected` and `pages_inspected`
so the actual scale of what was looked at is never a mystery.

## Result shape

```json
{
  "meta": {
    "source_system": "awx",
    "query_time": "2026-09-16T03:01:00+00:00",
    "observation_time": null,
    "query_window": null,
    "truncated": false,
    "derived_fields": ["category", "context"],
    "contract_version": "1.0"
  },
  "job": {
    "id": 7301,
    "name": "deploy-edge-nodes",
    "status": "failed",
    "started": "2026-09-16T03:00:00Z",
    "finished": "2026-09-16T03:00:45Z",
    "job_explanation": "",
    "job_template": "deploy-edge-nodes",
    "project": "site-ops",
    "inventory": "edge"
  },
  "structured_failures": [
    {
      "id": 501,
      "counter": 12,
      "event": "runner_on_unreachable",
      "task": null,
      "host": "ferros-c01",
      "created": "2026-09-16T03:00:20Z",
      "failed": true,
      "unreachable": true,
      "category": "network_reachability",
      "context": "ssh: connect to host ferros-c01 port 22: No route to host"
    }
  ],
  "structured_failures_error": null,
  "event_inspection": {
    "events_inspected": 1,
    "pages_inspected": 1,
    "inspection_capped": false
  },
  "stdout_context": {
    "role": "supporting",
    "excerpt": "PLAY RECAP\nferros-c01: unreachable=1",
    "tail": "PLAY RECAP\nferros-c01: unreachable=1"
  },
  "stdout_retrieval_error": null
}
```

(This is real, verified output — generated by running
`awx_get_job_failure` against a mocked AWX response with the shapes
above; not a hand-written mockup.)

`job` is just enough metadata to interpret the evidence (id, name,
status, template, project, inventory, timing) — never the full AWX job
record. `job_template`/`project`/`inventory` reuse the exact same
summary-field name-resolution helper `awx_recent_failed_jobs` uses.

## Provenance / derived-field semantics

Adopts the same `mantis.contracts.QueryMeta`/`ToolError` contract as
`awx_recent_failed_jobs` (#23):

- `meta.source_system` is always `"awx"`.
- `meta.observation_time` is deliberately left `None` — every returned
  event carries its own `created` timestamp, and no single
  batch-level value could represent that without being misleading (the
  same reasoning `awx_recent_failed_jobs` documents for `finished`).
- `meta.derived_fields` names which per-event fields
  (`category`, `context`) are Mantis-computed interpretation rather than
  AWX-reported data — machine-checkable, not just a naming convention.
  Every other event field, and every `job` field, is AWX-reported
  (`job_template`/`project`/`inventory` are AWX's own `summary_fields`
  names, resolved from an id the same way `awx_recent_failed_jobs`
  already does — not treated as "derived" for the same reason that tool
  doesn't).

## Stdout fallback behavior

Stdout is fetched via the exact same `AWXClient.get_job_stdout` and
`extract_excerpt`/`tail` helpers `awx_recent_failed_jobs` uses — no
second stdout parser. Its role is labeled explicitly in
`stdout_context.role`:

- **`"fallback"`** — no relevant structured failure event was found (or
  event retrieval itself failed); stdout is now the primary evidence
  available.
- **`"supporting"`** — one or more structured failures were already
  found; stdout is extra context only, never more authoritative than
  `structured_failures`.

Stdout is attempted in both cases (unless the run-local breaker is
already open — see below), so a structured-failures result still gets a
typed `stdout_retrieval_error` if stdout itself couldn't be fetched,
without ever discarding the structured evidence already gathered.

## Partial-success semantics

| Scenario | Behavior |
|---|---|
| Job context fetch fails | Propagates (raises) — without it, nothing coherent can be returned. Mirrors `list_jobs`'s failure handling in `awx_recent_failed_jobs`. |
| Event retrieval succeeds, failures found, stdout fails | `structured_failures` returned in full; `stdout_retrieval_error` set; structured evidence is never discarded. |
| Event retrieval succeeds, no relevant failures | `structured_failures: []`; stdout attempted as fallback (`role: "fallback"`). |
| Event retrieval fails | `structured_failures_error` set (a classified retrieval failure, never phrased as evidence about the target system); job context and a stdout fallback attempt are still returned. |

A sub-read failure that's swallowed into the result (events or stdout)
still reports into the run-local breaker via the `_reliability_report`
callback introduced in #72, exactly the same pattern
`awx_recent_failed_jobs` uses — no second breaker-reporting channel. If
the events sub-read is the one that opens the breaker, stdout is
**skipped** rather than immediately attempted too (`stdout_context:
null`, a `stdout_retrieval_error` explaining it was skipped) — the same
within-call short-circuit reasoning applied to `awx_recent_failed_jobs`
in #72's second review round.

## Event API failure vs. target-system evidence

A `structured_failures_error` (or `stdout_retrieval_error`) means Mantis
failed to *retrieve* evidence — never a fact about the job or the
target system. This is the same distinction `awx_recent_failed_jobs`
already draws for `stdout_retrieval_error` vs. `job_explanation`/
`failed`, extended to the events endpoint. The job's own AWX-reported
`status`/`job_explanation` in `job` is completely unaffected by an event
or stdout retrieval failure.

## Untrusted-output behavior (inherited from #14)

Event `stdout`/`context`, `task`, `host`, and any other free-form text
are external, Mantis-uncontrolled evidence — the tool is registered with
`contains_untrusted_text=True`, the same as `awx_recent_failed_jobs`.
This tool does not strip prompt-like text (e.g. an event containing
`"IGNORE ALL PREVIOUS INSTRUCTIONS"` remains visible verbatim in the
tool's own result) — that's `AgentRuntime`'s model-input boundary to
enforce via `mantis.security.make_model_safe()`, not this tool's job to
reimplement. See [docs/security.md](security.md).

## Reliability behavior (inherited from #15)

Every AWX request this tool makes — job detail, job events, stdout —
goes through the exact same `AWXClient._get()`/`retry_call()` path as
`awx_recent_failed_jobs`: explicit connect/read timeouts capped by the
remaining `Deadline`, the shared `RetryPolicy`, the shared
`IntegrationErrorKind` taxonomy, and the same run-local breaker
participation. No second retry helper, timeout config family, or
circuit-breaker abstraction was added. See
[docs/reliability.md](reliability.md).

## Relationship to `awx_recent_failed_jobs` and the AWX Troubleshooter agent

`awx_get_job_failure` does not replace `awx_recent_failed_jobs` — the
two are complementary (list recent failures vs. deep-dive on one job's
structured evidence). It's registered in the shared tool registry
(`category="awx"`) but is **not** currently in the AWX Troubleshooter
agent's `ALLOWED_TOOLS`: wiring a second, job-id-scoped AWX tool into a
live agent's tool sequencing/system prompt is a separate design decision
better made alongside a multi-tool investigation agent (see #11, out of
scope here). It's available today for the evaluation harness (see
below) and for a future agent to adopt.

## Evaluation scenario

`awx-structured-unreachable` (`mantis.eval.fixtures.awx`) reproduces the
`runner_on_unreachable` example from issue #28: a single job whose only
evidence is a structured unreachable event for host `ferros-c01`. Golden
behavior:

- cite the structured AWX evidence (not raw stdout parsing);
- recognize it as an AWX-observed network reachability failure at a
  specific point in time;
- avoid asserting an unsupported specific cause ("the firewall is
  broken", "sshd was down") as a conclusion — hedge it as a possibility
  instead, or omit it;
- never claim the host **is currently** unreachable/down — historical
  AWX evidence is not live proof of present state (a hard check in this
  scenario, not just a quality signal).

See `tests/eval/test_scenarios.py`'s `test_awx_structured_unreachable_*`
tests for deterministic good/bad-answer scoring, and
`docs/evaluation.md` for how to run a scenario against a live model.
