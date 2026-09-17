"""Structured JSON logging: newline-delimited JSON events to stdout/stderr.

Mantis runs as a container whose stdout/stderr is collected by a
host-side agent (Grafana Alloy) and shipped to Loki — Mantis itself never
holds Loki credentials or talks to Loki directly (see
``docs/observability.md``). This module's only job is to make every
``mantis_*`` event a single well-formed JSON line, with credentials and
unbounded tool output kept out of it.

Usage: library code (``AgentRuntime``, ``mantis.eval.runner``) calls
:func:`log_event`; a process entry point (``mantis.cli.main``) calls
:func:`configure_logging` exactly once at startup. Nothing in between —
an agent module never configures logging itself, so log level/format is
controlled centrally without touching agent code.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

DEFAULT_LEVEL = "INFO"
MAX_LOGGED_VALUE_CHARS = 2000
"""Cap on a single bounded field's serialized size (see :func:`bound_for_log`).
Deliberately much smaller than tool output bounds like
``mantis.tools._text.STDOUT_TAIL_CHARS`` — a log line represents one
event among many, not the primary evidence surface an agent reads."""

_SENSITIVE_KEY_MARKERS = (
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "credential",
)

# Captured once, from a real LogRecord, so this always matches whatever
# attributes *this* Python version's logging module reserves (e.g. the
# 3.12+ addition of "taskName") without hardcoding a version-specific list.
_STANDARD_LOGRECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """A fresh correlation ID for one :meth:`AgentRuntime.run` invocation,
    to be attached as ``run_id`` on every event that run produces."""
    return uuid.uuid4().hex


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _redact(value: Any, _seen: frozenset[int] = frozenset()) -> Any:
    if isinstance(value, (dict, list)) and id(value) in _seen:
        return "<circular reference>"
    if isinstance(value, dict):
        seen = _seen | {id(value)}
        return {
            k: ("***" if _is_sensitive_key(k) else _redact(v, seen)) for k, v in value.items()
        }
    if isinstance(value, list):
        seen = _seen | {id(value)}
        return [_redact(v, seen) for v in value]
    return value


def bound_for_log(value: Any, max_chars: int = MAX_LOGGED_VALUE_CHARS) -> Any:
    """Redact sensitive keys and cap serialized size before a value (tool
    arguments/results) is attached to a log event.

    Returns the redacted value unchanged if it serializes within
    ``max_chars``; otherwise a truncated string with a notice of how much
    was cut, so a log consumer is never misled into thinking it has the
    complete value. Never raises — an unserializable value degrades to
    its ``repr()`` rather than breaking the event trying to describe it.
    """
    redacted = _redact(value)
    try:
        text = json.dumps(redacted, default=str)
    except (TypeError, ValueError):
        # ValueError covers json's circular-reference detection; TypeError
        # covers the (rare, since default=str) case where even the
        # fallback conversion itself fails.
        return repr(redacted)[:max_chars]
    if len(text) <= max_chars:
        return redacted
    return f"{text[:max_chars]}...<truncated {len(text) - max_chars} chars>"


class JSONFormatter(logging.Formatter):
    """Renders a :class:`logging.LogRecord` as one newline-delimited JSON
    object. Any field passed via ``extra=`` to :func:`log_event` becomes a
    top-level JSON key — this formatter doesn't hardcode the event
    vocabulary, so new event types never require a formatter change.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _utc_now_iso(),
            "level": record.levelname.lower(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_LOGRECORD_ATTRS:
                payload[key] = value
        payload.setdefault("event", record.getMessage())
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int | str | None = None) -> None:
    """Configure the ``mantis`` logger tree to emit newline-delimited JSON
    to stderr. Call once, at process startup — never from library/agent
    code.

    ``level`` defaults to the ``MANTIS_LOG_LEVEL`` environment variable
    (falling back to ``INFO``), so log verbosity is configurable without
    changing any agent implementation.
    """
    resolved_level = level if level is not None else os.environ.get("MANTIS_LOG_LEVEL", DEFAULT_LEVEL)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger("mantis")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved_level)
    root.propagate = False


def log_event(logger_: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured ``mantis_*`` event through ``logger_``.

    Every keyword in ``fields`` becomes a top-level key in the emitted
    JSON line (see :class:`JSONFormatter`) — pass the common fields
    (``run_id``, ``agent``, ``model_alias``, ``iteration``, ``tool``,
    ``duration_seconds``, ``outcome``, ``error_kind``, ``scenario``, ...)
    as plain keyword arguments, not nested under another key.
    """
    logger_.log(level, event, extra={"event": event, **fields})
