"""Shared contracts for tool results: evidence, provenance, and errors.

Every Mantis tool eventually needs to answer the same questions for an
agent — and, later, for cross-signal correlation across tools — without
each one inventing its own shape: which system did this evidence come
from, when was it queried, was the result truncated, and did anything go
wrong *retrieving* it that is separate from what the evidence itself
shows about the system being investigated.

This module defines that shared, small vocabulary:

- :class:`QueryMeta` — provenance for a single tool call (source system,
  when it was queried, the query window if any, whether the result was
  truncated), attached as a ``meta`` key on a tool's result.
- :class:`ToolErrorKind` / :class:`ToolError` — a consistent way to
  represent a *tool-level* failure (couldn't reach/parse the source),
  never to be confused with the observed state of the system under
  investigation. A failed AWX job is evidence a tool successfully
  retrieved; a network timeout fetching that job's stdout is a
  :class:`ToolError`.

Design choice — additive, not a rigid envelope
------------------------------------------------

This intentionally does **not** force every tool into one rigid
``{meta, records, errors}`` wrapper shape. With only one real tool
(AWX) implemented so far, guessing the fully-normalized shape that will
actually fit Prometheus/Loki/network tools too would be premature — see
``docs/tools.md`` for the adoption guidance. Instead, tools attach a
``meta: QueryMeta.to_dict()`` key onto their existing, tool-specific
result shape, and represent any tool-level failure as a
``ToolError.to_dict()`` instead of a bare string. Nothing here changes
how a tool result reaches the model: :class:`mantis.runtime.AgentRuntime`
still does ``json.dumps(handler_result)``, so every dataclass here must
be turned into a plain dict via ``.to_dict()`` before a tool returns it.

Classifying *every* possible integration failure (auth vs. timeout vs.
rate-limit vs. server error) is out of scope here — that's timeout/retry/
circuit-breaking behavior (see the Reliability work tracked separately).
This module only defines the shared vocabulary those classifications will
eventually be expressed in.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

CONTRACT_VERSION = "1.0"
"""Bumped on a breaking change to this module's shape (a field removed or
renamed). Adding a new optional field does not require a bump — every
field besides ``source_system``/``kind``/``message`` has a default, so
existing construction call sites keep working."""


class ToolErrorKind(str, Enum):
    """Shared vocabulary for *tool-level* failures.

    These represent Mantis failing to retrieve or parse evidence from a
    source system — never a fact about that system's own observed state.
    A failed AWX job is evidence a tool successfully retrieved, not a
    :class:`ToolError`; a network timeout fetching that job's stdout is.
    """

    RETRIEVAL_ERROR = "retrieval_error"
    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    UNKNOWN = "unknown"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ToolError:
    """A single tool-level failure, tagged with a shared
    :class:`ToolErrorKind` instead of being an untyped, ad hoc string.
    """

    kind: ToolErrorKind
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "message": self.message}


@dataclass(frozen=True)
class QueryMeta:
    """Provenance for a single tool call.

    Attach one of these (via :meth:`to_dict`) as a ``meta`` key on every
    tool result so an agent — or, later, cross-signal correlation logic —
    can reason about evidence gathered from different systems at
    different times without each tool inventing its own timestamp/
    provenance fields.

    Args:
        source_system: Short identifier for the system this evidence came
            from, e.g. ``"awx"``, ``"prometheus"``, ``"loki"``.
        query_time: ISO 8601 UTC timestamp of when Mantis made this
            query. Defaults to now.
        query_window: For range-style queries (e.g. Prometheus/Loki),
            the ``{"start": ..., "end": ...}`` window that was queried.
            ``None`` for point-in-time queries (e.g. AWX's job list).
        truncated: True if more matching evidence exists than was
            returned (e.g. more failed AWX jobs exist than the requested
            limit covered). This is distinct from "fewer records exist
            than were requested" — that is not truncation, just a
            smaller true result.
        contract_version: Version of this shared contract shape. See
            :data:`CONTRACT_VERSION`.
    """

    source_system: str
    query_time: str = field(default_factory=_utc_now_iso)
    query_window: dict[str, str] | None = None
    truncated: bool = False
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
