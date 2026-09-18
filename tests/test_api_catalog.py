"""Tests for mantis.api.catalog: the shared agent catalog (#83).

Asserts the catalog is built from the real agent modules (never
duplicated prompt/tool-list metadata), and that unknown/unavailable
agents are classified distinctly before any model/tool execution.
"""

from __future__ import annotations

import pytest

from mantis.api.catalog import (
    AgentCatalog,
    AgentCatalogEntry,
    AgentUnavailableError,
    UnknownAgentError,
    build_default_catalog,
)
from mantis.config import ConfigurationError
from mantis.runtime import AgentRuntime


def test_default_catalog_includes_both_shipped_agents():
    catalog = build_default_catalog()

    ids = {entry.id for entry in catalog.list()}

    assert ids == {"awx-troubleshooter", "system-troubleshooter"}


def test_default_catalog_entries_use_the_real_agent_modules():
    import mantis.agents.awx_troubleshooter as awx_troubleshooter
    import mantis.agents.system_troubleshooter as system_troubleshooter

    catalog = build_default_catalog()

    assert catalog.get("awx-troubleshooter").build_runtime is awx_troubleshooter.build_runtime
    assert catalog.get("awx-troubleshooter").default_prompt == awx_troubleshooter.DEFAULT_PROMPT
    assert catalog.get("system-troubleshooter").build_runtime is system_troubleshooter.build_runtime


def test_default_catalog_entries_are_read_only():
    catalog = build_default_catalog()

    assert all(entry.read_only for entry in catalog.list())


def test_get_unknown_agent_raises_unknown_agent_error():
    catalog = build_default_catalog()

    with pytest.raises(UnknownAgentError) as exc_info:
        catalog.get("does-not-exist")

    assert exc_info.value.http_status == 404
    assert exc_info.value.kind == "unknown_agent"
    assert exc_info.value.agent_id == "does-not-exist"


def test_get_known_agent_returns_the_entry():
    catalog = build_default_catalog()

    entry = catalog.get("system-troubleshooter")

    assert entry.id == "system-troubleshooter"


def test_available_agent_probes_true_with_no_reason():
    catalog = build_default_catalog()  # baseline env vars from conftest satisfy LiteLLM/AWX

    available, reason = catalog.get("awx-troubleshooter").probe_availability()

    assert available is True
    assert reason is None


def test_unavailable_agent_probes_false_with_a_safe_reason(monkeypatch):
    def _broken_build_runtime() -> AgentRuntime:
        raise ConfigurationError("Missing required environment variable: LITELLM_URL")

    entry = AgentCatalogEntry(
        id="broken",
        display_name="Broken",
        description="d",
        read_only=True,
        build_runtime=_broken_build_runtime,
        default_prompt="p",
    )

    available, reason = entry.probe_availability()

    assert available is False
    assert reason == "misconfigured"
    # The safe reason must never be (or contain) the raw ConfigurationError text.
    assert "LITELLM_URL" not in reason


def test_catalog_construction_rejects_duplicate_ids_by_keeping_the_last_one():
    # AgentCatalog is a plain dict-backed registry (like ToolRegistry) --
    # document the actual last-one-wins behavior explicitly rather than
    # leaving duplicate-id handling implicit/untested.
    entry_a = AgentCatalogEntry(
        id="dup", display_name="A", description="a", read_only=True,
        build_runtime=lambda: None, default_prompt="p",
    )
    entry_b = AgentCatalogEntry(
        id="dup", display_name="B", description="b", read_only=True,
        build_runtime=lambda: None, default_prompt="p",
    )
    catalog = AgentCatalog([entry_a, entry_b])

    assert catalog.get("dup").display_name == "B"


def test_agent_unavailable_error_has_stable_shape():
    exc = AgentUnavailableError("system-troubleshooter", reason="misconfigured")

    assert exc.http_status == 409
    assert exc.kind == "agent_unavailable"
    assert exc.agent_id == "system-troubleshooter"
    assert "misconfigured" in str(exc)
