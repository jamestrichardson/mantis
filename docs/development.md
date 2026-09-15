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
├── config.py              # env-var configuration (LiteLLMConfig, AWXConfig)
├── contracts.py            # shared tool-result contract (QueryMeta, ToolError)
├── registry.py               # ToolRegistry, Tool, default_registry
├── runtime.py                  # AgentRuntime: shared model/tool loop
├── cli.py                        # `mantis <agent-name> [prompt]` dispatcher
├── integrations/
│   └── awx.py                        # AWXClient: raw AWX API access
├── tools/
│   ├── _text.py                        # excerpt/tail preprocessing helpers
│   └── awx.py                            # awx_recent_failed_jobs (semantic tool)
└── agents/
    └── awx_troubleshooter.py               # AWX Troubleshooting Agent
```

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
  system-specific exception (e.g. `AWXError`) on failure.
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
