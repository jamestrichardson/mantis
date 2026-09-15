"""Tests for mantis.contracts: the shared tool-result provenance/error
vocabulary (QueryMeta, ToolError, ToolErrorKind).
"""

from __future__ import annotations

import dataclasses
import json

from mantis.contracts import CONTRACT_VERSION, QueryMeta, ToolError, ToolErrorKind


# ---------------------------------------------------------------------------
# ToolError / ToolErrorKind
# ---------------------------------------------------------------------------


def test_tool_error_to_dict_is_json_serializable():
    error = ToolError(kind=ToolErrorKind.TIMEOUT, message="took too long")

    payload = error.to_dict()

    assert payload == {"kind": "timeout", "message": "took too long"}
    json.dumps(payload)  # must not raise


def test_tool_error_kind_values_are_stable_strings():
    # These strings are a wire contract other code (and prompts) may
    # match against — renaming one silently would be a breaking change
    # this test is meant to catch.
    assert ToolErrorKind.RETRIEVAL_ERROR.value == "retrieval_error"
    assert ToolErrorKind.TIMEOUT.value == "timeout"
    assert ToolErrorKind.AUTH_ERROR.value == "auth_error"
    assert ToolErrorKind.NOT_FOUND.value == "not_found"
    assert ToolErrorKind.RATE_LIMITED.value == "rate_limited"
    assert ToolErrorKind.UPSTREAM_ERROR.value == "upstream_error"
    assert ToolErrorKind.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# QueryMeta
# ---------------------------------------------------------------------------


def test_query_meta_to_dict_is_json_serializable():
    meta = QueryMeta(source_system="awx")

    payload = meta.to_dict()

    json.dumps(payload)  # must not raise
    assert payload["source_system"] == "awx"
    assert payload["contract_version"] == CONTRACT_VERSION


def test_query_meta_query_time_defaults_to_now_iso8601():
    meta = QueryMeta(source_system="awx")

    # A parseable ISO 8601 timestamp with no extra args required.
    from datetime import datetime

    datetime.fromisoformat(meta.query_time)


def test_query_meta_truncated_and_window_default_to_falsy():
    meta = QueryMeta(source_system="awx")

    assert meta.truncated is False
    assert meta.query_window is None
    assert meta.observation_time is None
    assert meta.derived_fields == []


def test_query_meta_observation_time_for_point_in_time_tools():
    # e.g. a Prometheus instant query: one observation, one timestamp.
    meta = QueryMeta(
        source_system="prometheus",
        observation_time="2026-09-14T12:00:00+00:00",
    )

    payload = meta.to_dict()

    assert payload["observation_time"] == "2026-09-14T12:00:00+00:00"


def test_query_meta_derived_fields_names_mantis_computed_fields():
    meta = QueryMeta(
        source_system="awx",
        derived_fields=["failure_excerpt", "stdout_tail"],
    )

    payload = meta.to_dict()

    assert payload["derived_fields"] == ["failure_excerpt", "stdout_tail"]


def test_query_meta_supports_a_query_window_for_range_queries():
    meta = QueryMeta(
        source_system="prometheus",
        query_window={"start": "2026-09-14T00:00:00Z", "end": "2026-09-15T00:00:00Z"},
    )

    payload = meta.to_dict()

    assert payload["query_window"] == {
        "start": "2026-09-14T00:00:00Z",
        "end": "2026-09-15T00:00:00Z",
    }


def test_query_meta_only_source_system_is_required():
    # Every other field must default, so adding a new optional field to
    # this contract later never breaks an existing construction call site.
    required = [
        f.name
        for f in dataclasses.fields(QueryMeta)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    ]

    assert required == ["source_system"]
