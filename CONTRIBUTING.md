# Contributing to Mantis

Thanks for helping improve Mantis. Contributions are welcome, including bug reports, documentation fixes, tests, integrations, tools, agent improvements, and reliability/security work.

## Before you start

For a non-trivial change, check the existing issues first and open or comment on an issue before doing substantial implementation work. This helps avoid duplicate work and keeps architectural changes aligned with the project's roadmap.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md) in project spaces.

Security vulnerabilities should not be filed as ordinary public bug reports. Follow the repository's GitHub Security policy/reporting flow instead.

## Development setup

Mantis requires Python 3.11+.

```bash
git clone https://github.com/jamestrichardson/mantis.git
cd mantis
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.local.example .env.local
```

Fill in local credentials only when you need to run against real services. Never commit `.env.local`, tokens, passwords, private keys, or other secrets.

For the full local setup and configuration flow, see:

- [docs/local-development.md](docs/local-development.md)
- [docs/configuration.md](docs/configuration.md)
- [docs/development.md](docs/development.md)

## Running tests

The deterministic test suite must not require live AWX or LiteLLM access.

```bash
pytest
```

Before opening a pull request, also reproduce the package/container checks when your change could affect them:

```bash
python -m build
docker build -t mantis:local .
```

CI runs deterministic tests on Python 3.11 and 3.12, builds the Python package, and validates the container image.

## Architecture expectations

Mantis deliberately keeps responsibilities separated:

- `src/mantis/integrations/` contains raw external-system clients.
- `src/mantis/tools/` contains semantic, model-facing operations.
- `src/mantis/agents/` contains thin agent definitions: prompt, allowed tools, and model/runtime configuration.
- `AgentRuntime` and the shared tool registry provide common execution behavior.

New integrations and tools should reuse the shared contracts for security, evidence, observability, and reliability instead of implementing parallel behavior.

See:

- [docs/architecture.md](docs/architecture.md)
- [docs/tools.md](docs/tools.md)
- [docs/agents.md](docs/agents.md)
- [docs/security.md](docs/security.md)
- [docs/evaluation.md](docs/evaluation.md)

## Coding conventions

- Use type hints throughout.
- Add docstrings to public functions/classes and explain non-obvious design choices.
- Use `logging`, not `print`, inside integrations, tools, and runtime code.
- Keep model-dependent/live evaluation separate from deterministic CI.
- Do not add a new framework, database, queue, or service abstraction unless the problem clearly requires it.
- Preserve evidence vs. hypothesis distinctions in troubleshooting behavior.
- Treat external tool output as untrusted data and follow the shared security boundary.

## Commits and pull requests

Use [Conventional Commits](https://www.conventionalcommits.org/) because release-please uses commit types to determine release intent and changelog entries.

Examples:

```text
feat(network): add bounded TCP connectivity check
fix(runtime): stop retrying authorization failures
docs: clarify local deployment workflow
```

Keep pull requests focused. A good PR should explain:

- what problem it solves;
- what changed;
- important design decisions or non-goals;
- how it was tested;
- which issue it closes or relates to.

If behavior changes, update the relevant documentation in the same PR.

## Adding an integration, tool, or agent

Detailed guidance already lives in [docs/development.md](docs/development.md). In short:

- Integration code belongs under `mantis.integrations` and should not know about prompts or LLM schemas.
- Tool code should return bounded, structured, JSON-serializable evidence and register through the shared `ToolRegistry`.
- Agents should reuse registered tools and the shared `AgentRuntime`; do not create bespoke model/tool loops.

## Review expectations

Review may focus on more than whether the happy path works. Expect questions about:

- failure behavior and bounded execution;
- secret handling and prompt-injection boundaries;
- result-size/cardinality limits;
- deterministic test coverage;
- evidence provenance and unsupported conclusions;
- whether the implementation introduces unnecessary infrastructure.

Small, explicit designs are preferred over premature frameworks.
