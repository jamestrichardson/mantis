# Evaluation harness (Track 1)

This is the execution path for model qualification (#13): run the same
fixture-backed scenario through one or more LiteLLM model aliases via the
real `AgentRuntime`, and record what happened as a machine-readable result.
It deliberately does **not** implement subjective/LLM-as-judge scoring, a
dashboard, or a database yet — see "Non-goals" below and issue #34.

## Quickstart

```bash
# What's available:
mantis eval list-scenarios
mantis eval list-models          # queries LiteLLM's configured models directly

# Run one scenario against one or more models:
mantis eval run --scenario awx-no-route --models mantis-fast,mantis-reasoning

# Custom output path (default: eval-results/<scenario>-<timestamp>.jsonl):
mantis eval run --scenario awx-no-route --models mantis-fast --out /tmp/run1.jsonl
```

`run` prints a one-line summary per model to stdout and writes the full
result records as JSON Lines. Exit code is `0` only if every model's run
reached a final answer (`outcome == "ok"`); `1` if any model errored, so
this is CI/script-friendly for noticing regressions without parsing output.

## Why this exists

Before building scoring, the important thing to get right is the
*execution path*: calling models only through LiteLLM (never Ollama
directly — see [docs/architecture.md](architecture.md)), running against
fixture data so evaluation never depends on live AWX/Prometheus/Loki, and
recording enough detail that a human (or, later, an automated scorer) can
actually tell what a model did. Scoring, golden-scenario expansion, and
comparing runs over time build on top of this — tracked under #13 and
follow-on issues, not this one.

## Architecture

```
mantis.eval
├── scenarios.py   Scenario + ScenarioRegistry (default_scenarios)
├── fixtures/       Fixture-backed Tool builders, one module per system
│   └── awx.py         FixtureAWXClient + the awx-no-route scenario
├── runner.py       run_scenario() / run_comparison()
├── results.py      EvalResult / ToolCallSummary (the result record)
└── cli.py          `mantis eval run|list-scenarios|list-models`
```

A **scenario** (`mantis.eval.scenarios.Scenario`) pairs:

- a prompt and system prompt (real scenarios reuse the actual agent's
  `SYSTEM_PROMPT`/`ALLOWED_TOOLS`/`tool_call_budget`/`temperature` — see
  `awx-no-route`, which imports these directly from
  `mantis.agents.awx_troubleshooter` rather than maintaining a parallel
  eval-only prompt — so a scenario qualifies models against exactly what
  production runs, not an approximation of it), with
- a `build_registry()` callable returning a fresh, scenario-scoped
  `ToolRegistry` with fixture-backed tool handlers.

**Fixtures reuse real tool code, not a hand-faked shortcut of it.**
`mantis.tools.awx.awx_recent_failed_jobs` accepts an internal `_client`
override (keyword-only, leading underscore — never something a model's
JSON tool-call arguments could set). `FixtureAWXClient`
(`mantis/eval/fixtures/awx.py`) duck-types `AWXClient`'s public interface
(`list_jobs`, `get_job_stdout`) with canned data, so a scenario exercises
the real stdout-excerpt extraction, the real `mantis.contracts.QueryMeta`
adoption, real truncation detection — everything except the actual HTTP
call. Follow this same pattern for future Prometheus/Loki/network
scenarios: add a fixture client duck-typing that integration's client
interface, not a parallel reimplementation of the tool's logic.

Each run goes through the real `AgentRuntime` — the runner does not
reimplement any part of the model/tool loop, per Mantis's core
architectural rule (see [docs/architecture.md](architecture.md)).

## The result record

One `EvalResult` per (scenario, model) pair, in
`mantis.eval.results.EvalResult`:

| Field | Meaning |
|---|---|
| `scenario`, `scenario_version` | Which scenario, and which version of it |
| `model` | The LiteLLM model alias used |
| `started_at`, `finished_at`, `elapsed_seconds` | Wall-clock timing (ISO 8601 UTC / float seconds) |
| `outcome` | `"ok"` or `"error"` — never raises out of the runner; see below |
| `final_answer` | The agent's final answer text, `None` on error |
| `tool_calls` | Ordered `ToolCallSummary` list: `iteration`, `tool_name`, `arguments`, `outcome` (`ok`/`duplicate`/`unknown_tool`/`bad_arguments`/`error`), `detail`, `result` |
| `iterations` | Model round-trip count |
| `duplicate_call_count`, `malformed_call_count` | Convenience counts derived from `tool_calls` |
| `usage` | Per-iteration token usage dicts when LiteLLM returns them, else `None` per entry |
| `total_tokens` | Sum of `usage[*].total_tokens` when available, else `None` |
| `error` | Exception type/message when `outcome == "error"` |
| `result_format_version` | See versioning below |

`tool_calls`/`usage` are built directly from `AgentRuntime.call_log` /
`AgentRuntime.usage_log` after `run()` returns — the runner never
re-derives or duplicates that bookkeeping.

**One model's failure never aborts a multi-model comparison.** A model
that's unreachable, times out, or never converges within the iteration
budget is caught inside `run_scenario` and recorded as `outcome="error"`
with the exception type/message in `error` — `run_comparison` always
returns one result per requested model.

### Versioning

`mantis.eval.results.RESULT_FORMAT_VERSION` (currently `"1.0"`) is bumped
on a breaking change to `EvalResult`'s shape (a field removed or renamed).
Adding a new optional field does not require a bump. Same policy as
`mantis.contracts.CONTRACT_VERSION` — see
[docs/tools.md](tools.md#result-contracts-evidence-and-provenance).

### Output format

JSON Lines: one `EvalResult.to_dict()` per line, no wrapping array. This
is deliberately not a database — see "Non-goals".

## Adding a new scenario

1. Add a fixture module under `mantis/eval/fixtures/` (or extend an
   existing one) that builds a `ToolRegistry` with fixture-backed
   handlers for whatever tools the scenario's agent needs — following
   `fixtures/awx.py`'s pattern of wrapping the real production tool
   function with a duck-typed fixture client, not reimplementing it.
2. Register a `Scenario` into `mantis.eval.scenarios.default_scenarios`
   at import time (see the bottom of `fixtures/awx.py`).
3. Make sure the new fixture module is imported by
   `mantis/eval/fixtures/__init__.py` so registration actually happens.
4. Add tests mirroring `tests/eval/test_scenarios.py`'s coverage of
   `awx-no-route`: the fixture reproduces the intended evidence, and the
   scenario matches the real agent's prompt/tool configuration it's
   meant to qualify.

## Non-goals (this issue, #34)

- **No subjective/LLM-as-judge scoring.** Results are raw traces for a
  human to read, or for a future scorer to consume — this issue doesn't
  decide whether a given answer was "good."
- **No dashboard or database.** JSON Lines files only.
- **Not every future scenario.** `awx-no-route` is the one concrete
  example proving the execution path works; golden scenarios for
  oversized stdout, tool retrieval failure, ambiguous root cause,
  duplicate-tool temptation, and multi-step investigation are follow-on
  work under #13.
