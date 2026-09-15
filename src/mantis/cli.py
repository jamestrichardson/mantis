"""Top-level ``mantis`` CLI entry point.

Currently a thin dispatcher to individual agent CLIs. Kept intentionally
simple: this is not a UI layer, just a convenience wrapper so agents can be
run as ``mantis <agent-name> [prompt]`` instead of
``python -m mantis.agents.<agent_name>``.
"""

from __future__ import annotations

import sys

AGENTS = {
    "awx-troubleshooter": "mantis.agents.awx_troubleshooter",
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: mantis <agent-name> [prompt]")
        print("Available agents:")
        for agent_name in AGENTS:
            print(f"  {agent_name}")
        return 0

    agent_name, *rest = argv
    module_name = AGENTS.get(agent_name)
    if module_name is None:
        print(f"Unknown agent: '{agent_name}'", file=sys.stderr)
        print(f"Available agents: {', '.join(AGENTS)}", file=sys.stderr)
        return 1

    import importlib

    agent_module = importlib.import_module(module_name)
    return agent_module.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
