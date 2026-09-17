"""Top-level ``mantis`` CLI entry point.

Currently a thin dispatcher to individual agent CLIs, plus the ``eval``
subcommand (see ``mantis.eval.cli``). Kept intentionally simple: this is
not a UI layer, just a convenience wrapper so agents can be run as
``mantis <agent-name> [prompt]`` instead of
``python -m mantis.agents.<agent_name>``.
"""

from __future__ import annotations

import os
import sys

from mantis.observability.logging import configure_logging
from mantis.observability.metrics import start_metrics_server

AGENTS = {
    "awx-troubleshooter": "mantis.agents.awx_troubleshooter",
}

# Subcommands that aren't agents — dispatched to their own CLI module
# rather than treated as an agent name.
SUBCOMMANDS = {
    "eval": "mantis.eval.cli",
}


def _getenv_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    # Wired centrally here — the single `mantis` entry point — rather than
    # in each agent module, so log level/format and the metrics endpoint
    # are configurable without touching any agent implementation. Every
    # agent and `mantis eval ...` invocation goes through this function.
    configure_logging()
    if _getenv_bool("MANTIS_METRICS_ENABLED", False):
        start_metrics_server()

    argv = sys.argv[1:] if argv is None else argv

    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: mantis <agent-name> [prompt]")
        print("       mantis eval <run|list-scenarios|list-models> ...")
        print("Available agents:")
        for agent_name in AGENTS:
            print(f"  {agent_name}")
        return 0

    name, *rest = argv

    import importlib

    if name in SUBCOMMANDS:
        module = importlib.import_module(SUBCOMMANDS[name])
        return module.main(rest)

    module_name = AGENTS.get(name)
    if module_name is None:
        print(f"Unknown agent: '{name}'", file=sys.stderr)
        print(f"Available agents: {', '.join(AGENTS)}", file=sys.stderr)
        print(f"Available subcommands: {', '.join(SUBCOMMANDS)}", file=sys.stderr)
        return 1

    agent_module = importlib.import_module(module_name)
    return agent_module.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
