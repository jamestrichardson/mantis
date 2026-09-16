# Evaluation harness (Track 1)

This is model qualification (#13): run the same fixture-backed scenario
through one or more LiteLLM model aliases via the real `AgentRuntime`,
record what happened as a machine-readable result (#34), and score it
against deterministic, non-LLM-judged expectations (#36) — so `mantis
eval run` tells you PASS/FAIL per behavior and a comparison table across
models, not just raw traces a human has to read. It deliberately does
**not** implement subjective/LLM-as-judge scoring or a dashboard/database
— see "Non-goals" below.

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

Before building scoring, the important thing to get right was the
*execution path*: calling models only through LiteLLM (never Ollama
directly — see [docs/architecture.md](architecture.md)), running against
fixture data so evaluation never depends on live AWX/Prometheus/Loki, and
recording enough detail that a human (or an automated scorer) can
actually tell what a model did. Scoring builds directly on top of that —
it's a pure post-processing layer over `EvalResult`, requiring no changes
to how a scenario runs. Golden-scenario expansion and comparing runs over
time build on top of *this* — tracked under #13 and follow-on issues, not
here.

## Architecture

```
mantis.eval
├── scenarios.py     # Scenario + ScenarioRegistry (default_scenarios)
├── expectations.py  # Deterministic check vocabulary (RequiredToolCall, ...)
├── scoring.py       # score_result(): expectations -> ScoreReport
├── fixtures/        # Fixture-backed Tool builders, one module per system
│   └── awx.py       # FixtureAWXClient + the awx-no-route scenario
├── runner.py        # run_scenario() / run_comparison()
├── results.py       # EvalResult / ToolCallSummary (the result record)
└── cli.py           # `mantis eval run|list-scenarios|list-models`
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
| `raw_message` | The raw final-message payload, captured **only** when a run ended with neither usable answer text nor a tool call — `None` otherwise. See "Diagnosing an empty/silent run" below. |
| `score` | Deterministic scoring against the scenario's `expectations` — `{"checks": [...], "passed": N, "total": M}`. `None` when the scenario declares no expectations (unscored) — never confuse this with a scored run that failed every check, which has `score={"passed": 0, ...}`, not `score=None`. See "Deterministic scoring" below. |
| `result_format_version` | See versioning below |

`tool_calls`/`usage` are built directly from `AgentRuntime.call_log` /
`AgentRuntime.usage_log` after `run()` returns — the runner never
re-derives or duplicates that bookkeeping.

**A model/backend failure never aborts a multi-model comparison — but a
Mantis bug does, deliberately.** `run_scenario` only catches two
exception categories and records them as `outcome="error"`: `openai.
OpenAIError` (the backend is unreachable, times out, rate-limits, etc.)
and `mantis.runtime.RuntimeError_` (e.g. `MaxIterationsExceededError` —
the model never converged). Both are real signal *about the model being
evaluated*. Anything else — an `AttributeError`, a bug in a scenario's
fixture, any exception outside those two categories — is a bug in Mantis
itself, and propagates out of `run_comparison` rather than being silently
recorded as if that model had failed. An evaluation harness that
swallowed its own bugs as "model X errored" would corrupt exactly the
qualification data it exists to produce.

### Diagnosing an empty/silent run

A model can end a run with `outcome="ok"` but an empty `final_answer` and
zero `tool_calls` — that's not a Mantis failure, it's the model itself
producing nothing usable, and it's a real qualification finding (this
happened during initial local qualification runs: two of five candidate
models did this on `awx-no-route`, one spending real completion tokens to
do it). Two different things can cause it:

- **Zero completion tokens** (`usage[*].completion_tokens == 0`): the
  model/backend didn't generate anything at all — check that the alias
  actually resolves to a working model in LiteLLM, and that any
  model-specific required parameters (e.g. Qwen3's `enable_thinking`)
  aren't missing.
- **Nonzero completion tokens, still empty `content`/`tool_calls`**: the
  model generated *something*, but it landed somewhere this runtime
  doesn't read as a standard OpenAI response — most often a
  provider-specific field (e.g. `reasoning_content`) or a tool-call
  syntax the backend didn't translate into the standard `tool_calls`
  field (gpt-oss's Harmony format is a known case of this depending on
  Ollama/LiteLLM version).

When this happens, `AgentRuntime` captures the *entire* raw message
(`message.model_dump()`, which the OpenAI SDK's message model allows to
include provider-specific extra fields) into
`diagnostic_raw_message`/`EvalResult.raw_message` — so the JSONL record
itself tells you which of the two cases you're looking at, without a live
re-run. `mantis eval run`'s summary prints a `NOTE:` line pointing at it.

## Deterministic scoring

A scenario can carry a list of `expectations` — small, deterministic
checks against a completed `EvalResult`. No LLM-as-judge: every check is
substring/count matching against `final_answer`/`tool_calls`, not
semantic judgment. The vocabulary lives in `mantis.eval.expectations`:

| Type | Checks |
|---|---|
| `RequiredToolCall(tool_name, min_count=1, max_count=None)` | The named tool was called within `[min_count, max_count]` times. Counts `"ok"` and `"duplicate"` outcomes (a duplicate still means the model got real, replayed data). |
| `MaxToolCalls(count)` | No more than `count` tool-call attempts *total*, any tool, any outcome — a broad anti-runaway-behavior guard, independent of any single tool's own bound. |
| `RequiredEvidence(patterns, case_sensitive=False)` | The final answer contains at least one of `patterns` (a string, or a list — pass several acceptable phrasings of the same claim). |
| `ForbiddenClaim(patterns, case_sensitive=False)` | The final answer contains **none** of `patterns` — for claims that overstate the evidence, e.g. asserting a specific unproven root cause. |
| `MustProduceFinalAnswer()` | `outcome == "ok"` and the final answer is non-empty/non-whitespace. Fails exactly the "empty/silent run" case above. |

Every type is a frozen dataclass exposing `display_label()` (the
PASS/FAIL line's text — override with `label=...` for a scenario-specific
phrasing) and `check(result) -> (passed, detail)`. New expectation types
don't need to inherit from anything, only satisfy that shape (see the
`Expectation` Protocol in `expectations.py`), so a scenario-specific check
doesn't require touching this module.

### Example: `awx-no-route`

```python
expectations=[
    RequiredToolCall("awx_recent_failed_jobs", min_count=1, max_count=1, label="called AWX exactly once"),
    RequiredEvidence("host03", label="cited host03"),
    RequiredEvidence(
        ["network reachability", "network issue", "reachability problem", "unreachable"],
        label="classified the evidence as a network reachability problem",
    ),
    RequiredEvidence(
        ["1 failed job", "one failed job", "only 1", "single failed job"],
        label="correctly reported only one failed job exists",
    ),
    ForbiddenClaim(
        ["firewall caused", "due to a firewall", "firewall rule", "firewall issue", "firewall misconfiguration"],
        label="did not assert firewall was the root cause",
    ),
    MaxToolCalls(1, label="stopped after receiving sufficient evidence"),
    MustProduceFinalAnswer(),
]
```

`mantis eval run --scenario awx-no-route --models qwen3:30b,gpt-oss:20b`
prints this per model:

```
    PASS: called AWX exactly once
    PASS: cited host03
    PASS: classified the evidence as a network reachability problem
    FAIL: correctly reported only one failed job exists
    FAIL: did not assert firewall was the root cause
    PASS: stopped after receiving sufficient evidence
    PASS: produced a final answer

    Score: 5/7
```

...and a comparison table across every model in that invocation:

```
MODEL        PASS  TOOL ERRORS  TIME   TOKENS
qwen3:30b    7/7   0            18.3s  1842
gpt-oss:20b  5/7   0            18.3s  1842
```

`TOOL ERRORS` counts tool calls with outcome `"error"` (a handler/
integration exception) — distinct from the PASS score, which is about
what the *model* did, not integration health.

A scenario with an empty (default) `expectations` list runs exactly as
before scoring existed: `score` stays `None`, and `mantis eval run` falls
back to the plain answer-preview output with no PASS/FAIL section or
table.

### Scoring is computed once, at run time, and persisted

`run_scenario` computes `score_result(scenario.expectations, result)` and
attaches it to the `EvalResult` before it's written to the JSONL — not
lazily at report/print time. This means:

- Past result files stay self-contained and comparable even if a
  scenario's expectations change later (the file reflects what the
  expectations *were* at run time — genuinely different from re-scoring
  old raw traces against today's expectations, which would silently
  change historical results).
- No separate "scoring pass" over old files is needed to see PASS/FAIL —
  it's already in the JSONL.

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
4. Add an `expectations` list encoding the scenario's golden behavior as
   deterministic checks (see "Deterministic scoring" above) — a scenario
   without expectations still runs, it just isn't scored, so this step is
   strongly recommended but not required to register a scenario.
5. Add tests mirroring `tests/eval/test_scenarios.py`'s coverage of
   `awx-no-route`: the fixture reproduces the intended evidence, and the
   scenario matches the real agent's prompt/tool configuration it's
   meant to qualify. If you added new expectation types, test them in
   isolation too — see `tests/eval/test_expectations.py`.

## Non-goals

- **No subjective/LLM-as-judge scoring.** Every expectation is
  deterministic substring/count matching — a permanent design boundary,
  not a "not yet." A future scorer that asks another model to judge an
  answer's quality would be a different, clearly-labeled capability, not
  a `mantis.eval.expectations` type.
- **No dashboard or database.** JSON Lines files only.
- **Not every future scenario.** `awx-no-route` is the one concrete
  example proving the execution path and scoring both work end to end;
  golden scenarios for oversized stdout, tool retrieval failure,
  ambiguous root cause, duplicate-tool temptation, and multi-step
  investigation are follow-on work under #13.
- **No cross-run regression tracking / trend dashboards.** Each JSONL
  file is self-contained and comparable to others by hand; automated
  "did this get worse since last week" tooling is a later Track 1
  follow-up once there's enough run history to compare against.
