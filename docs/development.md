# Development

This is the contributor workflow guide: running tests, understanding the
code layout, and adding new integrations/tools/agents. For first-time
setup (install, configure `.env.local`, run an agent, troubleshoot
connectivity), see [local-development.md](local-development.md) instead
— this doc assumes that's already done.

## Local development setup

```bash
git clone <this-repo>
cd mantis
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.local.example .env.local   # fill in AWX_TOKEN / LITELLM_API_KEY
```

Requires Python 3.11+. The package is laid out as a standard `src/`
layout (`src/mantis/...`), installed editable so code changes are picked
up immediately.

## Running tests

```bash
pytest
```

Tests never require a live AWX instance or a live LiteLLM/model backend —
HTTP interactions are mocked with `respx` (for AWX) and the OpenAI client
is stubbed directly (for the runtime). See `tests/conftest.py` for the
baseline environment variables tests run with.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and every push to
`main`. It never requires a live AWX/LiteLLM backend — same rule as
local tests above. Three independent jobs, all required to pass:

- **test** — `pytest` against the supported Python versions. 3.11 is the
  minimum supported version (see `requires-python` in `pyproject.toml`);
  CI also runs 3.12 so regressions against the newer interpreter surface
  before a user hits them.
- **build-package** — `python -m build` produces a wheel and sdist, to
  catch packaging metadata breakage independent of `pytest -e` editable
  installs.
- **docker-build** — builds the image from the repository `Dockerfile`.
  Never pushes from CI; publishing an image is release-triggered, added
  in a follow-on release-engineering workflow.

Commit messages should follow the
[conventional-commit](https://www.conventionalcommits.org/) convention
(`feat:`, `fix:`, `docs:`, `chore:`, ...) — `release-please` uses commit
type to drive version bumps and changelog entries. See
[docs/release.md](release.md) for the full policy.

To reproduce any of these locally before pushing:

```bash
pytest                              # same as the test job
python -m build                     # same as build-package (needs: pip install build)
docker build -t mantis:local .      # same as docker-build
```

## Running agents

```bash
mantis awx-troubleshooter
mantis awx-troubleshooter "Show me the last 3 failed AWX jobs and tell me whether they appear related."

# equivalently:
python -m mantis.agents.awx_troubleshooter
```

Running an agent for real requires a reachable LiteLLM gateway and AWX
instance, configured via `.env.local` (see
[docs/configuration.md](configuration.md) for the full `.env.*` file
convention).

## Code organization

```
src/mantis/
├── config.py                  # env-var configuration (LiteLLMConfig, AWXConfig)
├── contracts.py               # shared tool-result contract (QueryMeta, ToolError)
├── registry.py                # ToolRegistry, Tool, default_registry
├── runtime.py                 # AgentRuntime: shared model/tool loop
├── security.py                # untrusted tool-output trust boundary — see docs/security.md
├── reliability.py             # timeouts, retries, failure taxonomy, deadlines — see docs/reliability.md
├── cli.py                     # `mantis <agent-name> [prompt]` dispatcher
├── integrations/
│   └── awx.py                 # AWXClient: raw AWX API access
├── tools/
│   ├── _text.py               # excerpt/tail preprocessing helpers
│   ├── _awx_events.py         # AWX job-event selection/bounding — see docs/awx-job-failure.md
│   └── awx.py                 # awx_recent_failed_jobs, awx_get_job_failure (semantic tools)
├── agents/
│   └── awx_troubleshooter.py  # AWX Troubleshooting Agent
├── observability/              # structured logs + Prometheus metrics — see docs/observability.md
│   ├── logging.py             # JSONFormatter, log_event(), bound_for_log(), configure_logging()
│   └── metrics.py             # shared CollectorRegistry, metric definitions, start_metrics_server()
└── eval/                      # evaluation harness — see docs/evaluation.md
    ├── scenarios.py           # Scenario, ScenarioRegistry
    ├── expectations.py        # deterministic check vocabulary (RequiredToolCall, ...)
    ├── scoring.py             # evaluate_result(): expectations -> Evaluation
    ├── runner.py              # run_scenario(), run_comparison()
    ├── results.py             # EvalResult, ToolCallSummary
    ├── cli.py                 # `mantis eval run|list-scenarios|list-models`
    └── fixtures/
        └── awx.py             # FixtureAWXClient + seven golden AWX scenarios
```

See [docs/evaluation.md](evaluation.md) for the evaluation harness itself
(running scenarios, adding a new one, the result format), and
[docs/observability.md](observability.md) for the structured log event
schema and Prometheus metrics catalog.

## Adding an integration

Put raw API client code in `mantis/integrations/<system>.py`. It should:

- Accept a config object (add one to `mantis/config.py` if needed,
  following the `AWXConfig`/`LiteLLMConfig` pattern — `from_env()`
  classmethod, `ConfigurationError` on missing required vars). Type any
  credential field as `mantis.config.Secret` (via the `_require_secret()`
  helper), not a plain `str` — see [docs/security.md](security.md) for
  why. Only unwrap it with `.get_secret_value()` at the exact point the
  raw value is needed (building a header, constructing a client), never
  earlier.
- Know how to authenticate and make requests, and raise a
  system-specific exception (e.g. `AWXError`) on failure — subclassing
  `mantis.reliability.IntegrationError`, not a bare `RuntimeError`. Use
  explicit `httpx.Timeout(connect=..., read=...)` from
  `mantis.config.ReliabilityConfig`, wrap requests in
  `mantis.reliability.retry_call()`, and classify failures with
  `classify_http_status()`/`classify_httpx_exception()`. **Do not invent
  your own timeout/retry/error-taxonomy logic** — see
  [docs/reliability.md](reliability.md), which every integration adopts
  the same way `mantis/integrations/awx.py` does.
- Have zero knowledge of agents, prompts, or LLM schemas.
- If a single logical operation can fail in distinguishable ways that
  matter downstream (like AWX stdout retrieval vs. the job's own
  failure), model that with separate exception types — see
  `AWXStdoutError` vs. `AWXError`.

## Adding a tool

See [docs/tools.md](tools.md) for the full guide. Short version: write a
function in `mantis/tools/<system>.py` that uses the integration and
returns JSON-serializable, already-preprocessed data; write its
OpenAI-compatible schema next to it; register both as a `Tool` in
`default_registry`; add tests that mock the integration's HTTP layer.

## Adding an agent

See [docs/agents.md](agents.md) for the full guide. Short version: new
module in `mantis/agents/`, define `SYSTEM_PROMPT` and `ALLOWED_TOOLS`
(names of already-registered tools), a `build_runtime()` returning an
`AgentRuntime`, and a `main(argv)` CLI wrapper; register it in
`mantis/cli.py`'s `AGENTS` dict.

## Style/quality conventions

- Type hints throughout; `from __future__ import annotations` is used for
  forward-compatible typing.
- Docstrings on public functions/classes explaining behavior and
  non-obvious decisions; no comments restating what code already says.
- `logging`, not `print`, inside integrations/tools/runtime — `print` is
  reserved for actual CLI output.
- Broad `except Exception` is used deliberately in exactly one place
  (`AgentRuntime._dispatch_tool_call`, around a tool handler invocation)
  so a single bad tool call can never crash the agent loop; it is not a
  general pattern to copy elsewhere.
