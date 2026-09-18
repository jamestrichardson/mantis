"""The agent catalog: the shared source of truth for which agents the
API (and, later, MCP — #93) may invoke (#83).

Deliberately a small, explicit registry — not a generalized plugin
framework. Each entry maps a stable, public agent ID onto the *real*
agent module's own ``build_runtime``/``DEFAULT_PROMPT`` — never a copy
of its system prompt, tool list, or runtime budget. Adding a new
invokable agent means adding one :class:`AgentCatalogEntry` here that
points at that agent's existing ``mantis.agents.<name>`` module; it does
not mean writing a new route handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mantis.config import ConfigurationError
from mantis.runtime import AgentRuntime

# Low-cardinality, safe-to-expose reason codes only -- never a raw
# exception message (which could name a missing environment variable,
# a file path, or other server-internal detail). See docs/api.md.
UNAVAILABLE_REASON_MISCONFIGURED = "misconfigured"


class AgentCatalogError(Exception):
    """Base class for catalog-level failures. Carries a stable ``kind``
    and the HTTP status the API layer should map it to, mirroring
    ``mantis.reliability.IntegrationError``'s classification pattern —
    see ``mantis.api.invocation``."""

    def __init__(self, message: str, *, kind: str, http_status: int) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status


class DuplicateAgentError(ValueError):
    """Raised by :meth:`AgentCatalog.__init__` when two entries share an
    ``id``. Not an :class:`AgentCatalogError` — this is a catalog
    *construction* bug (a programming error in
    :func:`build_default_catalog` or a custom catalog), never a
    per-request condition an API route needs to classify/handle, so it
    deliberately doesn't carry an HTTP status. The catalog is becoming
    Mantis's capability/authorization boundary for agent invocation, so
    silently letting one entry shadow another ("last one wins") is
    exactly the kind of ambiguity that boundary must not have."""


class UnknownAgentError(AgentCatalogError):
    """Raised when a requested agent ID is not in the catalog at all —
    a caller/client mistake, rejected before any model/tool execution."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"Unknown agent: {agent_id!r}", kind="unknown_agent", http_status=404)
        self.agent_id = agent_id


class AgentUnavailableError(AgentCatalogError):
    """Raised when a requested agent is a real catalog entry but cannot
    currently be constructed (e.g. missing required server-side
    configuration such as ``LITELLM_URL``). Distinct from
    :class:`UnknownAgentError` — the agent ID is valid, it just isn't
    usable right now. The message is always a safe, low-cardinality
    reason, never the underlying :class:`~mantis.config.ConfigurationError`
    text (which is safe in isolation but still server-internal detail
    that doesn't belong in a client-facing error)."""

    def __init__(self, agent_id: str, *, reason: str) -> None:
        super().__init__(
            f"Agent {agent_id!r} is currently unavailable: {reason}",
            kind="agent_unavailable",
            http_status=409,
        )
        self.agent_id = agent_id
        self.reason = reason


@dataclass(frozen=True)
class AgentCatalogEntry:
    """One invokable agent's catalog metadata.

    ``build_runtime`` is the real agent module's own factory (e.g.
    ``mantis.agents.awx_troubleshooter.build_runtime``) — constructing a
    fresh :class:`~mantis.runtime.AgentRuntime` per call, exactly as the
    CLI already did before #83, never a shared/cached instance (multiple
    concurrent API runs of the same agent must never share one
    ``AgentRuntime``'s mutable ``call_log``/``usage_log`` state).

    ``display_name``/``description``/``read_only`` are catalog-level
    presentation metadata Mantis itself writes and owns — not duplicated
    from, or a substitute for, the agent's own ``SYSTEM_PROMPT``.
    """

    id: str
    display_name: str
    description: str
    read_only: bool
    build_runtime: Callable[[], AgentRuntime]
    default_prompt: str

    def probe_availability(self) -> tuple[bool, str | None]:
        """Cheaply check whether this agent can currently be
        constructed — no model call, no tool call, no network I/O:
        :class:`~mantis.runtime.AgentRuntime` construction only parses
        configuration and builds an (unconnected) OpenAI client object.
        Returns ``(available, reason)``; ``reason`` is always a stable,
        low-cardinality string safe to expose to a client, never a raw
        exception message."""
        try:
            self.build_runtime()
        except ConfigurationError:
            return False, UNAVAILABLE_REASON_MISCONFIGURED
        return True, None


class AgentCatalog:
    """In-memory registry mapping agent ID to :class:`AgentCatalogEntry`
    — deliberately as small as ``mantis.registry.ToolRegistry``, not a
    plugin/discovery framework. See :func:`build_default_catalog`."""

    def __init__(self, entries: list[AgentCatalogEntry]) -> None:
        self._entries: dict[str, AgentCatalogEntry] = {}
        for entry in entries:
            if entry.id in self._entries:
                raise DuplicateAgentError(f"Duplicate agent ID: {entry.id!r}")
            self._entries[entry.id] = entry

    def list(self) -> list[AgentCatalogEntry]:
        return list(self._entries.values())

    def get(self, agent_id: str) -> AgentCatalogEntry:
        try:
            return self._entries[agent_id]
        except KeyError:
            raise UnknownAgentError(agent_id) from None


def build_default_catalog() -> AgentCatalog:
    """Build the catalog of every agent Mantis currently ships,
    constructed from the real agent modules' own ``AGENT_NAME``/
    ``build_runtime``/``DEFAULT_PROMPT`` — see #83's "shared source of
    truth" requirement. Importing the agent modules also registers their
    tools into ``mantis.registry.default_registry`` as a side effect,
    same as every existing agent CLI entry point already relies on.
    """
    # Local imports: these modules import mantis.tools at module scope
    # (registering every built-in tool as a side effect) purely so the
    # catalog build order is obvious from this one function, matching
    # each agent module's own existing `import mantis.tools` convention.
    import mantis.agents.awx_troubleshooter as awx_troubleshooter
    import mantis.agents.system_troubleshooter as system_troubleshooter

    return AgentCatalog(
        [
            AgentCatalogEntry(
                id=awx_troubleshooter.AGENT_NAME,
                display_name="AWX Troubleshooter",
                description=(
                    "Investigates recent failed AWX automation jobs and produces an "
                    "evidence-based summary, distinguishing AWX's own reported failures "
                    "from Mantis retrieval errors."
                ),
                read_only=True,
                build_runtime=awx_troubleshooter.build_runtime,
                default_prompt=awx_troubleshooter.DEFAULT_PROMPT,
            ),
            AgentCatalogEntry(
                id=system_troubleshooter.AGENT_NAME,
                display_name="System Troubleshooter",
                description=(
                    "Correlates historical AWX evidence, current-state TCP connectivity, "
                    "time-series Prometheus data, and Loki logs to investigate a "
                    "system-level issue."
                ),
                read_only=True,
                build_runtime=system_troubleshooter.build_runtime,
                default_prompt=system_troubleshooter.DEFAULT_PROMPT,
            ),
        ]
    )
