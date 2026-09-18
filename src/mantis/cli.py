"""Top-level ``mantis`` CLI entry point.

For agent invocation, this is an **HTTP client only** (#83): ``mantis
agents``, ``mantis run <agent> <prompt>``, and the ergonomic per-agent
convenience commands (``mantis awx-troubleshooter "..."``, ``mantis
system-troubleshooter "..."``) all call the real, persistent Mantis API
(see ``mantis.api_client``/``mantis.api.app``) — this module never
constructs an :class:`~mantis.runtime.AgentRuntime`, executes a tool, or
calls LiteLLM directly, and there is no local-execution fallback: if the
API is unreachable, that is reported as an explicit error, never
silently downgraded to running an agent in-process. See ``docs/api.md``.

``mantis serve`` starts the persistent service itself
(``mantis.api.server``, #21). ``mantis eval ...`` remains local
dev/evaluation tooling (``mantis.eval.cli``) — it is not part of the
API surface #83 defines and deliberately keeps running scenarios
in-process against a configured LiteLLM backend directly.
"""

from __future__ import annotations

import sys
from typing import Callable, TypeVar

from mantis.api_client import (
    ApiAuthError,
    ApiClientError,
    ApiRequestError,
    ApiServerError,
    ApiTimeoutError,
    ApiUnavailableError,
    MantisApiClient,
)
from mantis.config import get_metrics_enabled
from mantis.observability.logging import configure_logging
from mantis.observability.metrics import start_metrics_server

T = TypeVar("T")

CONVENIENCE_AGENTS = ("awx-troubleshooter", "system-troubleshooter")
"""Agent IDs with a dedicated top-level ``mantis <agent> "prompt"``
command. Each is a thin wrapper over the exact same
``POST /api/v1/runs`` call ``mantis run <agent> "prompt"`` makes — never
a separate execution path. This list is a CLI-ergonomics convenience
only; the authoritative set of invokable agents is always
``GET /api/v1/agents``, not this tuple."""

# Subcommands that aren't agent invocations — dispatched to their own
# CLI module rather than treated as an agent name.
SUBCOMMANDS = {
    "eval": "mantis.eval.cli",
    "serve": "mantis.api.server",
}


def _call_api(fn: Callable[[], T]) -> tuple[T | None, int]:
    """Call ``fn()`` (a ``MantisApiClient`` method), returning
    ``(result, 0)`` on success or ``(None, exit_code)`` after printing a
    clear, specific error for every :class:`ApiClientError` case —
    unreachable, timed out, unauthenticated, rejected, or a server
    error. Never falls back to local execution."""
    try:
        return fn(), 0
    except ApiUnavailableError as exc:
        print(f"Could not reach the Mantis API: {exc}", file=sys.stderr)
    except ApiTimeoutError as exc:
        print(f"Mantis API request timed out: {exc}", file=sys.stderr)
    except ApiAuthError as exc:
        print(f"Mantis API authentication failed: {exc}", file=sys.stderr)
    except ApiRequestError as exc:
        print(f"Mantis API rejected the request ({exc.error_type}): {exc}", file=sys.stderr)
    except ApiServerError as exc:
        print(f"Mantis API error: {exc}", file=sys.stderr)
    except ApiClientError as exc:  # pragma: no cover - defensive catch-all
        print(f"Mantis API client error: {exc}", file=sys.stderr)
    return None, 1


def _cmd_agents(_rest: list[str]) -> int:
    client = MantisApiClient()
    agents, exit_code = _call_api(client.list_agents)
    if agents is None:
        return exit_code
    if not agents:
        print("No agents available.")
        return 0
    for agent in agents:
        availability = "" if agent.available else f" (unavailable: {agent.unavailable_reason})"
        print(f"{agent.id}{availability}")
        print(f"    {agent.description}")
    return 0


def _invoke(agent: str, prompt: str) -> int:
    client = MantisApiClient()
    result, exit_code = _call_api(lambda: client.create_run(agent, prompt))
    if result is None:
        return exit_code
    print(f"[run_id: {result.run_id}]", file=sys.stderr)
    if result.outcome == "success":
        print(result.output or "")
        return 0
    print(f"Agent run failed ({result.error_kind}): {result.error_message}", file=sys.stderr)
    return 1


def _cmd_run(rest: list[str]) -> int:
    if len(rest) < 2:
        print("Usage: mantis run <agent> <prompt>", file=sys.stderr)
        return 1
    agent, *prompt_parts = rest
    prompt = " ".join(prompt_parts).strip()
    if not prompt:
        print("Usage: mantis run <agent> <prompt>", file=sys.stderr)
        return 1
    return _invoke(agent, prompt)


def _cmd_convenience(agent: str, rest: list[str]) -> int:
    prompt = " ".join(rest).strip()
    if not prompt:
        print(f"Usage: mantis {agent} <prompt>", file=sys.stderr)
        return 1
    return _invoke(agent, prompt)


def _print_usage() -> None:
    print("Usage: mantis agents")
    print("       mantis run <agent> <prompt>")
    print("       mantis <agent-name> <prompt>")
    print("       mantis serve")
    print("       mantis eval <run|list-scenarios|list-models> ...")
    print("Convenience agent commands:")
    for agent_name in CONVENIENCE_AGENTS:
        print(f"  {agent_name}")
    print("Run 'mantis agents' against a running service for the full, live agent list.")


def main(argv: list[str] | None = None) -> int:
    # Wired centrally here — the single `mantis` entry point — rather
    # than in each subcommand, so log level/format and the metrics
    # endpoint are configurable without touching any subcommand's
    # implementation. Every invocation goes through this function.
    configure_logging()
    if get_metrics_enabled(default=False):
        start_metrics_server()

    argv = sys.argv[1:] if argv is None else argv

    if not argv or argv[0] in ("-h", "--help"):
        _print_usage()
        return 0

    name, *rest = argv

    if name in SUBCOMMANDS:
        import importlib

        module = importlib.import_module(SUBCOMMANDS[name])
        return module.main(rest)

    if name == "agents":
        return _cmd_agents(rest)
    if name == "run":
        return _cmd_run(rest)
    if name in CONVENIENCE_AGENTS:
        return _cmd_convenience(name, rest)

    print(f"Unknown command: '{name}'", file=sys.stderr)
    print(f"Available agent commands: {', '.join(CONVENIENCE_AGENTS)}", file=sys.stderr)
    print(f"Available subcommands: {', '.join(SUBCOMMANDS)}, agents, run", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
