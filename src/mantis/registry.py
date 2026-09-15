"""Central registry for Mantis tools.

A :class:`Tool` bundles together everything the agent runtime needs to
expose a semantic operation to a model and to actually execute it:

- an OpenAI-compatible function/tool JSON schema
- a Python handler that performs the work
- metadata describing category and mutation risk

Agents never implement tools themselves. They declare which *already
registered* tools they are allowed to use, by name, and the runtime looks
them up here. This keeps tool implementations reusable across every future
agent and keeps each agent's exposed surface narrow and explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

ToolHandler = Callable[..., Any]


class ToolNotFoundError(KeyError):
    """Raised when an agent requests a tool name that is not registered."""


class DuplicateToolError(ValueError):
    """Raised when attempting to register a tool name that already exists."""


@dataclass(frozen=True)
class Tool:
    """A single registered Mantis tool.

    Attributes:
        name: Unique tool name. Must match the ``name`` in ``schema``.
        schema: OpenAI-compatible tool schema, e.g.::

            {
                "type": "function",
                "function": {
                    "name": "...",
                    "description": "...",
                    "parameters": {...},
                },
            }
        handler: Callable that performs the operation and returns
            JSON-serializable data suitable for the model to read.
        category: Free-form grouping label (e.g. "awx", "network").
        mutating: True if the tool can change external system state.
            Mantis tools default to read-only (``mutating=False``); a
            mutating tool must set this explicitly so agents and policy
            layers can treat it with more caution.
        description: Short human-readable description, primarily for
            documentation/introspection (the model-facing description
            lives in ``schema``).
    """

    name: str
    schema: Mapping[str, Any]
    handler: ToolHandler
    category: str = "general"
    mutating: bool = False
    description: str = ""


class ToolRegistry:
    """In-memory registry mapping tool names to :class:`Tool` definitions."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Raises :class:`DuplicateToolError` on name clash."""
        if tool.name in self._tools:
            raise DuplicateToolError(f"Tool already registered: {tool.name}")
        if tool.schema.get("function", {}).get("name") != tool.name:
            raise ValueError(
                f"Tool schema name mismatch for '{tool.name}': "
                f"schema declares '{tool.schema.get('function', {}).get('name')}'"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """Look up a tool by name. Raises :class:`ToolNotFoundError` if missing."""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[Tool]:
        """Return every registered tool."""
        return list(self._tools.values())

    def subset(self, names: list[str]) -> list[Tool]:
        """Return the registered tools matching ``names``, in that order.

        Raises :class:`ToolNotFoundError` if any name is not registered.
        This is how an agent's narrow, declared toolset is resolved.
        """
        return [self.get(name) for name in names]

    def schemas_for(self, names: list[str]) -> list[Mapping[str, Any]]:
        """Return the OpenAI-compatible schemas for ``names``, in order."""
        return [tool.schema for tool in self.subset(names)]


# A single process-wide registry that integrations/tools register into at
# import time, and that agents pull their allowed toolset from.
default_registry = ToolRegistry()
