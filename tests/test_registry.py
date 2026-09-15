"""Tests for mantis.registry.ToolRegistry."""

from __future__ import annotations

import pytest

from mantis.registry import DuplicateToolError, Tool, ToolNotFoundError, ToolRegistry


def _make_tool(name: str, **overrides) -> Tool:
    schema = {
        "type": "function",
        "function": {"name": name, "description": "test tool", "parameters": {}},
    }
    defaults = dict(name=name, schema=schema, handler=lambda: None)
    defaults.update(overrides)
    return Tool(**defaults)


def test_register_and_get():
    registry = ToolRegistry()
    tool = _make_tool("ping")

    registry.register(tool)

    assert registry.get("ping") is tool
    assert "ping" in registry


def test_get_unknown_tool_raises():
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError):
        registry.get("does-not-exist")


def test_register_duplicate_name_raises():
    registry = ToolRegistry()
    registry.register(_make_tool("ping"))

    with pytest.raises(DuplicateToolError):
        registry.register(_make_tool("ping"))


def test_register_rejects_schema_name_mismatch():
    registry = ToolRegistry()
    schema = {
        "type": "function",
        "function": {"name": "wrong_name", "description": "", "parameters": {}},
    }
    bad_tool = Tool(name="ping", schema=schema, handler=lambda: None)

    with pytest.raises(ValueError):
        registry.register(bad_tool)


def test_subset_returns_requested_tools_in_order():
    registry = ToolRegistry()
    registry.register(_make_tool("a"))
    registry.register(_make_tool("b"))
    registry.register(_make_tool("c"))

    result = registry.subset(["c", "a"])

    assert [t.name for t in result] == ["c", "a"]


def test_subset_raises_on_unknown_name():
    registry = ToolRegistry()
    registry.register(_make_tool("a"))

    with pytest.raises(ToolNotFoundError):
        registry.subset(["a", "missing"])


def test_schemas_for_returns_schemas_only():
    registry = ToolRegistry()
    registry.register(_make_tool("a"))

    schemas = registry.schemas_for(["a"])

    assert schemas == [registry.get("a").schema]


def test_all_returns_every_registered_tool():
    registry = ToolRegistry()
    registry.register(_make_tool("a"))
    registry.register(_make_tool("b"))

    assert {t.name for t in registry.all()} == {"a", "b"}


def test_mutating_defaults_to_false():
    tool = _make_tool("ping")

    assert tool.mutating is False
