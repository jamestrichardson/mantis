"""Deterministic AWX job-event failure selection (#28).

AWX job-event streams can be very large. The design goal here is
"structured AWX job events first, bounded stdout only as supporting/
fallback context" — never make a model wade through a huge raw event
stream or stdout dump when deterministic code can select the useful
failure evidence first.

Everything in this module is plain, testable-without-a-model Python: no
HTTP (that's ``mantis.integrations.awx``), no LLM-facing shape decisions
(that's ``mantis.tools.awx``). It answers exactly one question —
"given AWX's job-event records, which ones matter, and what do we keep
from each" — under named, bounded limits.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mantis.contracts import ToolError, ToolErrorKind
from mantis.integrations.awx import AWXClient, AWXJobEventsError
from mantis.reliability import Deadline, DeadlineExceededError, IntegrationError, IntegrationErrorKind

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named, documented bounds. Deliberately conservative for smaller local
# models (see docs/awx-job-failure.md) and easy to change in one place.
# ---------------------------------------------------------------------------

MAX_EVENT_PAGES_INSPECTED = 5
"""Hard cap on how many job-event pages are fetched from AWX for one
tool call, regardless of how many more pages AWX reports exist."""

MAX_EVENTS_INSPECTED = 500
"""Hard cap on the total number of raw event records inspected across
all fetched pages. Protects against a huge job (tens of thousands of
events) even if AWX's server-side event filter isn't honored and every
page is mostly irrelevant ``runner_on_ok`` noise."""

MAX_RETURNED_FAILURE_EVENTS = 10
"""Cap on how many selected failure events are actually returned in a
tool result, independent of the inspection caps above — a job could
have far more failing events inspected than we want to hand a model."""

EVENT_PAGE_SIZE = 100
"""AWX ``page_size`` requested per job-events page."""

MAX_EVENT_CONTEXT_CHARS = 2_000
"""Bound on each selected event's ``context`` text (see
:func:`_bounded_context`) — generous enough to be useful, nowhere near
large enough to smuggle a huge nested AWX payload through one event."""

FAILURE_EVENT_TYPES: tuple[str, ...] = (
    "runner_on_failed",
    "runner_on_unreachable",
    "runner_on_async_failed",
    "runner_item_on_failed",
)
"""AWX/Ansible callback event-type strings that materially represent a
job failure. Deliberately small and explicit rather than inferring
failure from a generic ``failed`` flag, which also appears on summary
events (e.g. ``playbook_on_stats``) that aren't themselves a specific
failure to report."""


def _is_failure_event(event: dict[str, Any]) -> bool:
    return event.get("event") in FAILURE_EVENT_TYPES


def _categorize_event(event: dict[str, Any]) -> str:
    """A small, explicitly Mantis-derived normalization of AWX's own
    ``event`` type string — never returned as if AWX itself reported it
    (see ``derived_fields`` on the tool result). Deliberately just two
    categories plus a catch-all rather than a broader root-cause
    taxonomy, which is out of scope (see docs)."""
    event_type = event.get("event")
    if event_type == "runner_on_unreachable":
        return "network_reachability"
    if event_type in ("runner_on_failed", "runner_on_async_failed", "runner_item_on_failed"):
        return "task_failure"
    return "other"


def _bounded_context(text: str, *, max_chars: int = MAX_EVENT_CONTEXT_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "... [truncated]"


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Allowlist/normalize one raw AWX job-event record.

    Deliberately does not expose the full ``event_data`` object — that
    can be a large, deeply nested, version-varying payload. Only a
    small set of useful, AWX-reported fields is retained, plus a single
    bounded free-text ``context`` string (AWX's own per-event ``stdout``
    when present, since that is the most reliable human-readable field
    across event types; falls back to ``event_data.res.msg``).
    """
    event_data = event.get("event_data") or {}
    res = event_data.get("res") if isinstance(event_data.get("res"), dict) else {}
    context_source = event.get("stdout") or (res.get("msg") if res else None) or ""

    return {
        "id": event.get("id"),
        "counter": event.get("counter"),
        "event": event.get("event"),
        "task": event.get("task") or event_data.get("task"),
        "host": event.get("host_name") or event_data.get("host"),
        "created": event.get("created"),
        "failed": bool(event.get("failed")),
        "unreachable": event.get("event") == "runner_on_unreachable",
        # Mantis-derived, not AWX-reported — see derived_fields on the
        # tool result.
        "category": _categorize_event(event),
        "context": _bounded_context(context_source),
    }


def collect_failure_events(
    client: AWXClient,
    job_id: int,
    *,
    deadline: Deadline | None,
    reliability_report: Callable[[IntegrationErrorKind], bool] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], ToolError | None, bool]:
    """Paginate ``job_id``'s AWX job events, selecting failure-bearing
    ones under the named caps above.

    Requests AWX's server-side ``event__in`` filter as an optimization,
    but never trusts it was honored — :func:`_is_failure_event` is
    applied to every returned record regardless, so correctness never
    depends on AWX's filtering behavior.

    Returns:
        A 4-tuple ``(selected_events, inspection, retrieval_error,
        breaker_now_open)``:

        - ``selected_events``: normalized, bounded failure events (see
          :func:`_normalize_event`), capped at
          :data:`MAX_RETURNED_FAILURE_EVENTS`.
        - ``inspection``: ``{"events_inspected", "pages_inspected",
          "inspection_capped", "more_failures_than_returned"}``.
          ``inspection_capped`` is True only when a hard cap (pages or
          events) actually prevented following a real further page —
          never inferred from the inspected counts alone, since those
          can legitimately equal a cap by coincidence on the exact page
          where AWX's own stream also ends (e.g. a job with exactly
          :data:`MAX_EVENT_PAGES_INSPECTED` pages). Tracked instead by
          whether the loop exited because AWX reported no further page
          (``next_page is None``) versus because a cap stopped it before
          that could be checked. ``more_failures_than_returned`` is True
          only if a failure event was actually observed and dropped
          after :data:`MAX_RETURNED_FAILURE_EVENTS` was already reached
          — not inferred from ``len(selected_events)`` alone, since a
          job with *exactly* that many relevant failures and nothing
          more would otherwise be misreported as truncated. Callers
          (see ``mantis.tools.awx.awx_get_job_failure``) OR these two
          fields together into the result's ``meta.truncated`` — see
          ``docs/awx-job-failure.md``.
        - ``retrieval_error``: set only if a page fetch itself failed
          (classified AWX failure, or the tool-call deadline was
          already exhausted) — distinct from "we inspected everything
          and found nothing," which returns an empty list and no error.
        - ``breaker_now_open``: True if this failure was the one that
          pushed the run-local breaker open (see #72's
          ``_reliability_report`` pattern) — callers should stop making
          further AWX requests in this same tool call when this is
          True, not just on the *next* call.
    """
    selected: list[dict[str, Any]] = []
    events_inspected = 0
    pages_inspected = 0
    page = 1
    stream_exhausted = False
    more_failures_than_returned = False

    while True:
        if pages_inspected >= MAX_EVENT_PAGES_INSPECTED or events_inspected >= MAX_EVENTS_INSPECTED:
            # A cap stopped us from even checking whether a further page
            # exists -- genuinely unknown, so the stream is not
            # considered exhausted.
            break

        try:
            page_result = client.list_job_events(
                job_id,
                page=page,
                page_size=EVENT_PAGE_SIZE,
                event_types=FAILURE_EVENT_TYPES,
                deadline=deadline,
            )
        except (AWXJobEventsError, DeadlineExceededError) as exc:
            logger.warning("Could not retrieve job events for AWX job %s: %s", job_id, exc)
            breaker_now_open = False
            if isinstance(exc, IntegrationError) and reliability_report is not None:
                breaker_now_open = reliability_report(exc.kind)
            tool_error = (
                ToolError(kind=ToolErrorKind.TIMEOUT, message=str(exc))
                if isinstance(exc, DeadlineExceededError)
                else ToolError(kind=exc.to_tool_error_kind(), message=str(exc))
            )
            inspection = {
                "events_inspected": events_inspected,
                "pages_inspected": pages_inspected,
                # Retrieval itself failed -- not a bound we hit, so this
                # is not "capped" in the sense the other branch means.
                "inspection_capped": False,
                "more_failures_than_returned": more_failures_than_returned,
            }
            return selected, inspection, tool_error, breaker_now_open

        pages_inspected += 1
        events_inspected += len(page_result.events)

        for event in page_result.events:
            if not _is_failure_event(event):
                continue
            if len(selected) >= MAX_RETURNED_FAILURE_EVENTS:
                more_failures_than_returned = True
                continue
            selected.append(_normalize_event(event))

        if page_result.next_page is None:
            stream_exhausted = True
            break
        page = page_result.next_page

    inspection = {
        "events_inspected": events_inspected,
        "pages_inspected": pages_inspected,
        "inspection_capped": not stream_exhausted,
        "more_failures_than_returned": more_failures_than_returned,
    }
    return selected, inspection, None, False
