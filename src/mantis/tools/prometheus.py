"""Semantic Prometheus tools exposed to agents.

Provides ``prometheus_query`` (instant PromQL) and
``prometheus_query_range`` (bounded range PromQL): Mantis's first
time-series evidence source (#9). See ``docs/prometheus.md`` for the
full design (result shapes, bounding constants, truncation semantics,
time-series ordering, and how this differs from #28's historical AWX
evidence and #8's current-state TCP evidence — three deliberately
distinct kinds of evidence).

All PromQL/time/range validation happens here, before any HTTP work
(see ``mantis.integrations.prometheus`` for the HTTP/auth/retry
mechanics this validation gates). A validation failure is returned as a
normal result (``query_error.type == "invalid_input"``), never raised —
the same reasoning ``mantis.tools.network`` documents: the invalid
PromQL/time text is untrusted, model-supplied data (#14) and must flow
through ``mantis.security.make_model_safe()`` like any other tool
result, not through the runtime's generic last-resort exception path.

Adopts the shared result contract from ``mantis.contracts`` (``meta`` /
``QueryMeta``), the same pattern ``mantis.tools.awx`` and
``mantis.tools.network`` established.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mantis.config import PrometheusConfig
from mantis.contracts import QueryMeta
from mantis.integrations.prometheus import PrometheusAPIResponse, PrometheusClient
from mantis.registry import Tool, default_registry
from mantis.reliability import Deadline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named, documented bounds (#9 item 11/12) -- see docs/prometheus.md.
# ---------------------------------------------------------------------------

MAX_PROMQL_CHARS = 1000
"""Hard cap on PromQL query text length. Generous for any realistic
query, nowhere near large enough to smuggle an oversized payload."""

MAX_SERIES_RETURNED = 50
"""Cap on the number of series returned for either an instant vector or
a range matrix result — small and named deliberately, sized for
interactive troubleshooting with local models, not bulk export."""

MAX_SAMPLES_PER_SERIES = 200
"""Cap on samples returned per series in a range (matrix) result."""

MAX_TOTAL_SAMPLES = 2_000
"""Global cap on samples across *all* returned series combined in a
range result — protects against a many-series, many-samples-each
response even when each individual series stays under
:data:`MAX_SAMPLES_PER_SERIES`."""

MAX_LABELS_PER_SERIES = 20
"""Cap on the number of labels preserved per series."""

MAX_LABEL_KEY_CHARS = 128
MAX_LABEL_VALUE_CHARS = 256
MAX_WARNING_CHARS = 500
"""Bounds on individual label keys/values and Prometheus warning
strings — never a raw, unbounded string reaching the model."""

MIN_RANGE_STEP_SECONDS = 1.0
"""Smallest allowed range-query step, in seconds."""

MAX_RANGE_SECONDS = 7 * 24 * 3600
"""Largest allowed range-query window (7 days) — bounded for
troubleshooting scope, not because Prometheus itself couldn't answer a
larger one."""

MAX_RANGE_POINTS_PER_SERIES = 1_000
"""Largest allowed ``(end - start) / step`` — the real defense against
a request like "30 days at 1-second resolution": Prometheus would
happily accept it, but Mantis rejects it before ever making the HTTP
call. See :func:`_validate_range`."""

# No field here is Mantis-computed interpretation of the underlying
# data (unlike, say, #28's derived event `category`) -- every value is
# Prometheus-reported, only bounded/reordered/reformatted (a raw Unix
# timestamp to an ISO 8601 string is a format change, not
# interpretation). See docs/prometheus.md.
DERIVED_RESULT_FIELDS: tuple[str, ...] = ()


class PromQLValidationError(ValueError):
    """Raised by :func:`validate_promql` for invalid query text — a
    caller/model mistake, not a query outcome. Never raised past this
    module's tool functions; always converted to a normal
    ``query_error``-shaped result (see the module docstring)."""


class TimeValidationError(ValueError):
    """Raised by :func:`_parse_time_input` for a time value that's
    neither an RFC3339 string nor a Unix timestamp."""


class RangeValidationError(ValueError):
    """Raised by :func:`_validate_step`/:func:`_validate_range` for an
    out-of-bounds range query (backwards window, non-positive step,
    window or point-density exceeding the named caps)."""


def validate_promql(query: Any) -> str:
    """Validate ``query`` is plain, bounded PromQL text.

    Deliberately does **not** parse PromQL (out of scope, see
    ``docs/prometheus.md``) — only mechanical checks: it's a string,
    non-empty, within :data:`MAX_PROMQL_CHARS`, and free of control
    characters. Normal PromQL syntax (operators, braces, regexes,
    quotes, ...) is never rejected on content grounds.
    """
    if not isinstance(query, str):
        raise PromQLValidationError(f"query must be a string, got {type(query).__name__}")
    if not query.strip():
        raise PromQLValidationError("query must not be empty")
    if len(query) > MAX_PROMQL_CHARS:
        raise PromQLValidationError(f"query must be at most {MAX_PROMQL_CHARS} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in query):
        raise PromQLValidationError("query must not contain control characters")
    return query


def _parse_time_input(value: Any) -> float:
    """Parse a model-facing time value into a Unix epoch float.

    Accepts either an RFC3339 string (``"2026-09-17T12:00:00Z"``) or a
    plain Unix timestamp (``int``/``float``) — see
    ``docs/prometheus.md``'s "Time input" section for why both are
    supported without a broader natural-language date parser.
    """
    if isinstance(value, bool):
        raise TimeValidationError("time value must be a string or number, not a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TimeValidationError(
                f"time value must be RFC3339 or a Unix timestamp, got {value!r}"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise TimeValidationError(f"time value must be a string or number, got {type(value).__name__}")


def _validate_step(step: Any) -> float:
    if isinstance(step, bool) or not isinstance(step, (int, float)):
        raise RangeValidationError(f"step must be a number of seconds, got {type(step).__name__}")
    if step < MIN_RANGE_STEP_SECONDS:
        raise RangeValidationError(f"step must be >= {MIN_RANGE_STEP_SECONDS} seconds, got {step}")
    return float(step)


def _validate_range(start: float, end: float, step: float) -> None:
    if start >= end:
        raise RangeValidationError(f"start ({start}) must be before end ({end})")
    window = end - start
    if window > MAX_RANGE_SECONDS:
        raise RangeValidationError(
            f"range window ({window:.0f}s) exceeds the maximum of {MAX_RANGE_SECONDS}s"
        )
    points = window / step
    if points > MAX_RANGE_POINTS_PER_SERIES:
        raise RangeValidationError(
            f"requested range implies {points:.0f} points per series, exceeding the "
            f"maximum of {MAX_RANGE_POINTS_PER_SERIES}"
        )


def _format_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _bounded_str(text: Any, max_chars: int) -> str:
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _label_sort_key(metric: dict[str, Any]) -> tuple:
    """A deterministic ordering key for one series, based on its
    normalized label set — never upstream response order or Python
    dict-insertion order. See ``docs/prometheus.md``'s "Ordering"
    section."""
    return tuple(sorted((str(k), str(v)) for k, v in metric.items()))


def _bounded_labels(metric: dict[str, Any]) -> tuple[dict[str, str], bool]:
    """Bound a series' label set: at most :data:`MAX_LABELS_PER_SERIES`
    labels, each key/value bounded to
    :data:`MAX_LABEL_KEY_CHARS`/:data:`MAX_LABEL_VALUE_CHARS`. Returns
    ``(bounded_labels, labels_were_truncated)``."""
    items = sorted(metric.items())
    labels_truncated = len(items) > MAX_LABELS_PER_SERIES
    bounded = {
        _bounded_str(k, MAX_LABEL_KEY_CHARS): _bounded_str(v, MAX_LABEL_VALUE_CHARS)
        for k, v in items[:MAX_LABELS_PER_SERIES]
    }
    return bounded, labels_truncated


def _normalize_sample(raw_sample: Any) -> dict[str, Any] | None:
    """Normalize one Prometheus ``[timestamp, value]`` pair. Returns
    ``None`` if malformed (missing/wrong-shaped) rather than raising —
    malformed sample data is handled safely, not fatally (#9 item 21).

    ``value`` is preserved exactly as Prometheus's own string
    representation (never coerced to ``float``) — Prometheus can
    legitimately report ``"NaN"``/``"+Inf"``/``"-Inf"``, and converting
    those to Python floats either loses that information or requires
    reinventing it later; the raw string is simply safer.
    """
    try:
        raw_timestamp, raw_value = raw_sample
        timestamp = _format_timestamp(float(raw_timestamp))
    except (TypeError, ValueError):
        return None
    return {"timestamp": timestamp, "value": str(raw_value)}


def _normalize_vector(raw_result: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Normalize an instant ``vector`` result's series list.

    Deterministically ordered by normalized label set (see
    :func:`_label_sort_key`), bounded to :data:`MAX_SERIES_RETURNED`.
    Returns ``(series, truncated)`` — ``truncated`` is computed by
    directly comparing the raw entry count to the final returned count
    (which is affected by both the series cap and any malformed/label
    truncation), never inferred merely from
    ``len(series) == MAX_SERIES_RETURNED`` (see #9 item 12 / #28's and
    #8's review history on this exact mistake).
    """
    entries = [e for e in raw_result if isinstance(e, dict)]
    entries.sort(key=lambda e: _label_sort_key(e.get("metric") or {}))
    raw_count = len(entries)

    series: list[dict[str, Any]] = []
    any_label_truncation = False
    for entry in entries[:MAX_SERIES_RETURNED]:
        metric, labels_truncated = _bounded_labels(entry.get("metric") or {})
        any_label_truncation = any_label_truncation or labels_truncated
        sample = _normalize_sample(entry.get("value"))
        if sample is None:
            continue
        series.append({"metric": metric, "sample": sample})

    truncated = (raw_count > len(series)) or any_label_truncation
    return series, truncated


def _normalize_matrix(raw_result: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Normalize a range ``matrix`` result's series list — same
    deterministic ordering as :func:`_normalize_vector`, plus a
    per-series sample cap (:data:`MAX_SAMPLES_PER_SERIES`) and a global
    total-sample budget (:data:`MAX_TOTAL_SAMPLES`) shared across every
    returned series. Sample order within a series is preserved exactly
    as Prometheus returned it (already chronological)."""
    entries = [e for e in raw_result if isinstance(e, dict)]
    entries.sort(key=lambda e: _label_sort_key(e.get("metric") or {}))
    raw_count = len(entries)

    series: list[dict[str, Any]] = []
    any_truncation = False
    total_samples_used = 0
    for entry in entries[:MAX_SERIES_RETURNED]:
        metric, labels_truncated = _bounded_labels(entry.get("metric") or {})
        raw_values = entry.get("values") or []
        raw_sample_count = len(raw_values)

        remaining_global_budget = max(0, MAX_TOTAL_SAMPLES - total_samples_used)
        this_series_cap = min(MAX_SAMPLES_PER_SERIES, remaining_global_budget)

        samples: list[dict[str, Any]] = []
        for raw_sample in raw_values[:this_series_cap]:
            normalized = _normalize_sample(raw_sample)
            if normalized is not None:
                samples.append(normalized)
        total_samples_used += len(samples)

        if labels_truncated or raw_sample_count > len(samples):
            any_truncation = True

        series.append({"metric": metric, "samples": samples})

    truncated = (raw_count > len(series)) or any_truncation
    return series, truncated


def _get_client() -> PrometheusClient:
    return PrometheusClient(config=PrometheusConfig.from_env())


def _invalid_input_result(
    *, mode: str, promql: Any, exc: Exception, extra_query_fields: dict[str, Any] | None = None
) -> dict[str, Any]:
    meta = QueryMeta(source_system="prometheus", derived_fields=list(DERIVED_RESULT_FIELDS))
    query_section: dict[str, Any] = {"promql": promql, "mode": mode}
    if extra_query_fields:
        query_section.update(extra_query_fields)
    return {
        "meta": meta.to_dict(),
        "query": query_section,
        "result_type": None,
        "series": [],
        "value": None,
        "warnings": [],
        "query_error": {"type": "invalid_input", "message": str(exc)},
    }


def _shape_result(response: PrometheusAPIResponse, *, query_section: dict[str, Any]) -> dict[str, Any]:
    warnings = [_bounded_str(w, MAX_WARNING_CHARS) for w in response.warnings]

    if response.status == "error":
        meta = QueryMeta(source_system="prometheus", derived_fields=list(DERIVED_RESULT_FIELDS))
        return {
            "meta": meta.to_dict(),
            "query": query_section,
            "result_type": None,
            "series": [],
            "value": None,
            "warnings": warnings,
            "query_error": {
                "type": _bounded_str(response.error_type or "unknown", MAX_LABEL_KEY_CHARS),
                "message": _bounded_str(response.error or "", MAX_WARNING_CHARS),
            },
        }

    result_type = response.result_type
    series: list[dict[str, Any]] = []
    value: dict[str, Any] | None = None
    truncated = False
    observation_time: str | None = None

    if result_type == "vector":
        series, truncated = _normalize_vector(response.result or [])
    elif result_type == "matrix":
        series, truncated = _normalize_matrix(response.result or [])
    elif result_type in ("scalar", "string"):
        value = _normalize_sample(response.result)
        if value is not None:
            observation_time = value["timestamp"]
    # Any other result_type is unexpected per Prometheus's own API
    # contract; normalized as empty evidence (no series/value) rather
    # than crashing -- this should not occur against a real Prometheus.

    meta = QueryMeta(
        source_system="prometheus",
        observation_time=observation_time,
        truncated=truncated,
        derived_fields=list(DERIVED_RESULT_FIELDS),
    )

    return {
        "meta": meta.to_dict(),
        "query": query_section,
        "result_type": result_type,
        "series": series,
        "value": value,
        "warnings": warnings,
        "query_error": None,
    }


def prometheus_query(
    query: Any,
    *,
    time: Any = None,
    _client: PrometheusClient | None = None,
    _deadline: Deadline | None = None,
) -> dict[str, Any]:
    """Execute an instant PromQL query against Prometheus.

    This describes **monitored state at one instant** (or "now", if
    ``time`` is omitted) — distinct from #28's historical AWX evidence
    (what automation observed when a job ran) and #8's current-state TCP
    evidence (can Mantis reach a host/port right now). Never conflate
    the three.

    Args:
        query: PromQL query text. Validated mechanically (type,
            non-empty, bounded length, no control characters) — never
            parsed. See :func:`validate_promql`.
        time: Optional evaluation instant, as an RFC3339 string or Unix
            timestamp. Omit to evaluate at Prometheus's own current
            time.
        _client: Test/evaluation-only client override — same convention
            as the AWX/network tools.
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime``. Same convention as the AWX/network tools.

    Returns:
        A dict with ``meta`` (provenance), ``query`` (the validated
        PromQL and, for a scalar/string result, no per-series timestamp
        applies so ``meta.observation_time`` is set to that single
        sample's timestamp; for a vector, each series carries its own
        sample timestamp instead — see ``docs/prometheus.md``),
        ``result_type``, ``series`` (bounded, ordered — see
        :func:`_normalize_vector`), ``value`` (only for scalar/string),
        ``warnings`` (bounded Prometheus-reported warnings), and
        ``query_error`` (set for either a Mantis-side validation
        rejection or a Prometheus-reported query/API error — never for
        a transport failure, which propagates as a classified
        :class:`~mantis.integrations.prometheus.PrometheusError`
        instead, handled by ``AgentRuntime`` generically like any other
        integration failure).

        An empty, successful result (``result_type="vector"``,
        ``series=[]``) is valid evidence, not a failure — see
        ``docs/prometheus.md``'s "Empty results" section.
    """
    try:
        safe_query = validate_promql(query)
        time_value = _parse_time_input(time) if time is not None else None
    except (PromQLValidationError, TimeValidationError) as exc:
        return _invalid_input_result(
            mode="instant", promql=query, exc=exc, extra_query_fields={"time": None}
        )

    client = _client or _get_client()
    time_param = str(time_value) if time_value is not None else None
    response = client.query(safe_query, time_param=time_param, deadline=_deadline)

    return _shape_result(
        response,
        query_section={
            "promql": safe_query,
            "mode": "instant",
            "time": _format_timestamp(time_value) if time_value is not None else None,
        },
    )


def prometheus_query_range(
    query: Any,
    start: Any,
    end: Any,
    step: Any,
    *,
    _client: PrometheusClient | None = None,
    _deadline: Deadline | None = None,
) -> dict[str, Any]:
    """Execute a bounded range PromQL query against Prometheus.

    This describes **how monitored state changed over a window** —
    distinct from an instant query's single point in time. Bounded
    before any HTTP work: see :data:`MAX_RANGE_SECONDS` and
    :data:`MAX_RANGE_POINTS_PER_SERIES` — a request implying, say, 30
    days at 1-second resolution is rejected here even though Prometheus
    itself would accept it.

    Args:
        query: PromQL query text — same validation as
            :func:`prometheus_query`.
        start: Range start, RFC3339 string or Unix timestamp.
        end: Range end, RFC3339 string or Unix timestamp. Must be after
            ``start``.
        step: Query resolution step, in seconds (a plain number, not a
            PromQL duration string like ``"15s"``).
        _client: Test/evaluation-only client override.
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime``.

    Returns:
        Same overall shape as :func:`prometheus_query`, with
        ``query.mode="range"`` and ``query.start``/``query.end``/
        ``query.step_seconds`` reporting the normalized requested
        window. ``meta.observation_time`` is always ``None`` for a
        range result — many samples across the window each carry their
        own timestamp (``series[].samples[].timestamp``); no single
        batch-level value could represent that without being
        misleading, the same reasoning ``awx_recent_failed_jobs``
        documents for its job list.
    """
    try:
        safe_query = validate_promql(query)
        start_ts = _parse_time_input(start)
        end_ts = _parse_time_input(end)
        step_seconds = _validate_step(step)
        _validate_range(start_ts, end_ts, step_seconds)
    except (PromQLValidationError, TimeValidationError, RangeValidationError) as exc:
        return _invalid_input_result(
            mode="range",
            promql=query,
            exc=exc,
            extra_query_fields={"start": None, "end": None, "step_seconds": None},
        )

    client = _client or _get_client()
    response = client.query_range(
        safe_query,
        start=str(start_ts),
        end=str(end_ts),
        step=str(step_seconds),
        deadline=_deadline,
    )

    return _shape_result(
        response,
        query_section={
            "promql": safe_query,
            "mode": "range",
            "start": _format_timestamp(start_ts),
            "end": _format_timestamp(end_ts),
            "step_seconds": step_seconds,
        },
    )


PROMETHEUS_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "prometheus_query",
        "description": (
            "Execute an instant PromQL query against Prometheus, "
            "describing monitored state at one instant (or now, if "
            "time is omitted). Distinct from historical AWX automation "
            "evidence and current-state TCP connectivity checks -- "
            "this is time-series monitoring data. An empty successful "
            "result is valid evidence, not a failure. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PromQL query text, e.g. 'up{instance=\"host:9100\"}'.",
                },
                "time": {
                    "type": "string",
                    "description": (
                        "Optional evaluation instant, as an RFC3339 "
                        "timestamp (e.g. '2026-09-17T12:00:00Z') or a "
                        "Unix timestamp. Omit to evaluate at the "
                        "current time."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

PROMETHEUS_QUERY_RANGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "prometheus_query_range",
        "description": (
            "Execute a bounded range PromQL query against Prometheus, "
            "describing how monitored state changed over a time "
            "window. Bounded before any request is made: the window "
            "and the implied points-per-series (roughly "
            "(end-start)/step) are both capped, so a request like "
            "'30 days at 1-second resolution' is rejected. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PromQL query text.",
                },
                "start": {
                    "type": "string",
                    "description": "Range start: an RFC3339 timestamp or Unix timestamp.",
                },
                "end": {
                    "type": "string",
                    "description": "Range end: an RFC3339 timestamp or Unix timestamp. Must be after start.",
                },
                "step": {
                    "type": "number",
                    "description": "Query resolution step, in seconds.",
                },
            },
            "required": ["query", "start", "end", "step"],
        },
    },
}


default_registry.register(
    Tool(
        name="prometheus_query",
        schema=PROMETHEUS_QUERY_SCHEMA,
        handler=prometheus_query,
        category="prometheus",
        mutating=False,
        # Metric names/label values/warnings are external,
        # Mantis-uncontrolled evidence -- same untrusted-output
        # treatment as every other evidence tool. See mantis.security
        # and docs/security.md.
        contains_untrusted_text=True,
        description="Execute an instant PromQL query (current monitored state).",
    )
)

default_registry.register(
    Tool(
        name="prometheus_query_range",
        schema=PROMETHEUS_QUERY_RANGE_SCHEMA,
        handler=prometheus_query_range,
        category="prometheus",
        mutating=False,
        contains_untrusted_text=True,
        description="Execute a bounded range PromQL query (monitored state over a time window).",
    )
)
