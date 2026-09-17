"""Tests for mantis.tools._awx_events: deterministic AWX job-event
failure selection (#28).

These are pure, model-free unit tests of the selection/categorization/
normalization/bounding logic — no HTTP, no AgentRuntime. Integration-
level (mocked HTTP) and full tool-result tests live in
tests/test_awx_job_failure.py.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mantis.config import AWXConfig, Secret
from mantis.contracts import ToolErrorKind
from mantis.integrations.awx import AWXClient
from mantis.reliability import Deadline, IntegrationErrorKind
from mantis.tools._awx_events import (
    FAILURE_EVENT_TYPES,
    MAX_EVENT_CONTEXT_CHARS,
    MAX_EVENT_PAGES_INSPECTED,
    MAX_EVENTS_INSPECTED,
    MAX_RETURNED_FAILURE_EVENTS,
    _bounded_context,
    _categorize_event,
    _is_failure_event,
    _normalize_event,
    collect_failure_events,
)


@pytest.fixture
def awx_client() -> AWXClient:
    return AWXClient(
        config=AWXConfig(url="https://awx.example.test", token=Secret("tok"), verify_ssl=True),
        sleep=lambda *_: None,
    )


def _event(event_type: str, **overrides) -> dict:
    base = {
        "id": 1,
        "counter": 1,
        "event": event_type,
        "created": "2026-09-10T08:01:00Z",
        "failed": False,
        "host_name": None,
        "task": None,
        "stdout": "",
        "event_data": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Selection: which event types count as failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", FAILURE_EVENT_TYPES)
def test_failure_event_types_are_selected(event_type):
    assert _is_failure_event(_event(event_type)) is True


@pytest.mark.parametrize(
    "event_type",
    ["runner_on_ok", "runner_on_skipped", "playbook_on_stats", "playbook_on_task_start", "runner_on_start"],
)
def test_successful_or_informational_events_are_not_selected(event_type):
    assert _is_failure_event(_event(event_type)) is False


def test_unreachable_event_selected():
    assert _is_failure_event(_event("runner_on_unreachable")) is True


def test_failed_task_event_selected():
    assert _is_failure_event(_event("runner_on_failed")) is True


# ---------------------------------------------------------------------------
# Categorization: explicit, small, Mantis-derived
# ---------------------------------------------------------------------------


def test_unreachable_categorized_as_network_reachability():
    assert _categorize_event(_event("runner_on_unreachable")) == "network_reachability"


@pytest.mark.parametrize(
    "event_type", ["runner_on_failed", "runner_on_async_failed", "runner_item_on_failed"]
)
def test_failed_variants_categorized_as_task_failure(event_type):
    assert _categorize_event(_event(event_type)) == "task_failure"


def test_unrecognized_event_type_categorized_as_other():
    assert _categorize_event(_event("playbook_on_stats")) == "other"


# ---------------------------------------------------------------------------
# Normalization: bounded, allowlisted, no raw event_data dump
# ---------------------------------------------------------------------------


def test_normalize_event_uses_top_level_stdout_as_context():
    event = _event(
        "runner_on_unreachable",
        host_name="ferros-c01",
        stdout="ssh: connect to host ferros-c01 port 22: No route to host",
    )
    normalized = _normalize_event(event)
    assert normalized["host"] == "ferros-c01"
    assert normalized["context"] == "ssh: connect to host ferros-c01 port 22: No route to host"
    assert normalized["category"] == "network_reachability"
    assert normalized["unreachable"] is True


def test_normalize_event_falls_back_to_event_data_res_msg_when_no_top_level_stdout():
    event = _event(
        "runner_on_failed",
        stdout="",
        event_data={"task": "Apply migration", "host": "db01", "res": {"msg": "exit code 1"}},
    )
    normalized = _normalize_event(event)
    assert normalized["context"] == "exit code 1"
    assert normalized["task"] == "Apply migration"
    assert normalized["host"] == "db01"


def test_normalize_event_never_exposes_the_full_raw_event_data_object():
    event = _event(
        "runner_on_failed",
        event_data={
            "task": "x",
            "host": "y",
            "res": {"msg": "boom"},
            "some_huge_nested_field": {"a": {"b": {"c": list(range(1000))}}},
        },
    )
    normalized = _normalize_event(event)
    assert "event_data" not in normalized
    assert "some_huge_nested_field" not in str(normalized)


def test_huge_event_stdout_is_bounded():
    huge = "x" * (MAX_EVENT_CONTEXT_CHARS * 5)
    event = _event("runner_on_failed", stdout=huge)
    normalized = _normalize_event(event)
    assert len(normalized["context"]) <= MAX_EVENT_CONTEXT_CHARS + len("... [truncated]")
    assert normalized["context"].endswith("... [truncated]")


def test_bounded_context_leaves_short_text_untouched():
    assert _bounded_context("short") == "short"


def test_bounded_context_handles_empty_text():
    assert _bounded_context("") == ""


def test_normalize_event_preserves_useful_awx_fields():
    event = _event(
        "runner_on_failed",
        id=99,
        counter=42,
        created="2026-09-10T08:01:00Z",
        failed=True,
        host_name="db01",
        task="Apply migration 042",
    )
    normalized = _normalize_event(event)
    assert normalized["id"] == 99
    assert normalized["counter"] == 42
    assert normalized["created"] == "2026-09-10T08:01:00Z"
    assert normalized["failed"] is True
    assert normalized["task"] == "Apply migration 042"


# ---------------------------------------------------------------------------
# collect_failure_events: pagination, caps, retrieval-failure handling
# ---------------------------------------------------------------------------


@respx.mock
def test_collect_failure_events_normal_single_page(awx_client):
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 2,
                "next": None,
                "results": [
                    _event("runner_on_unreachable", id=1, host_name="host03"),
                    _event("runner_on_ok", id=2),
                ],
            },
        )
    )

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=None
    )

    assert len(selected) == 1
    assert selected[0]["host"] == "host03"
    assert inspection == {
        "events_inspected": 2,
        "pages_inspected": 1,
        "inspection_capped": False,
        "more_failures_than_returned": False,
    }
    assert error is None
    assert breaker_open is False


@respx.mock
def test_collect_failure_events_paginates_across_pages(awx_client):
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "count": 2,
                "next": "http://awx/api/v2/jobs/42/job_events/?page=2",
                "results": [_event("runner_on_failed", id=1, counter=1)],
            },
        ),
        httpx.Response(
            200,
            json={
                "count": 2,
                "next": None,
                "results": [_event("runner_on_unreachable", id=2, counter=2)],
            },
        ),
    ]

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=None
    )

    assert len(selected) == 2
    assert inspection["pages_inspected"] == 2
    assert inspection["inspection_capped"] is False
    assert route.call_count == 2


@respx.mock
def test_collect_failure_events_requests_second_page_number(awx_client):
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/")
    route.side_effect = [
        httpx.Response(200, json={"count": 1, "next": "x", "results": []}),
        httpx.Response(200, json={"count": 1, "next": None, "results": []}),
    ]

    collect_failure_events(awx_client, 42, deadline=None, reliability_report=None)

    first_request, second_request = route.calls[0].request, route.calls[1].request
    assert first_request.url.params["page"] == "1"
    assert second_request.url.params["page"] == "2"


@respx.mock
def test_collect_failure_events_stops_at_page_inspection_cap(awx_client):
    # More pages exist ("next" always set) than MAX_EVENT_PAGES_INSPECTED
    # allows -- inspection must stop at the hard cap, never fetch forever.
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/")
    route.side_effect = [
        httpx.Response(
            200,
            json={"count": 1000, "next": "x", "results": [_event("runner_on_ok", id=i)]},
        )
        for i in range(MAX_EVENT_PAGES_INSPECTED + 5)
    ]

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=None
    )

    assert route.call_count == MAX_EVENT_PAGES_INSPECTED
    assert inspection["pages_inspected"] == MAX_EVENT_PAGES_INSPECTED
    assert inspection["inspection_capped"] is True
    assert error is None


@respx.mock
def test_collect_failure_events_stops_at_event_inspection_cap_even_with_large_page_size(awx_client):
    # A single page can itself contain more events than
    # MAX_EVENTS_INSPECTED -- large event streams must never be fetched
    # or inspected unboundedly even within one page's response.
    huge_page = [_event("runner_on_ok", id=i) for i in range(MAX_EVENTS_INSPECTED + 200)]
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(
            200, json={"count": len(huge_page), "next": "more", "results": huge_page}
        )
    )

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=None
    )

    assert inspection["events_inspected"] == len(huge_page)
    assert inspection["inspection_capped"] is True
    # Only one page was ever requested -- the cap stopped further pagination.
    assert inspection["pages_inspected"] == 1


@respx.mock
def test_collect_failure_events_caps_returned_failures_but_keeps_inspecting_within_bounds(awx_client):
    many_failures = [
        _event("runner_on_failed", id=i, counter=i) for i in range(MAX_RETURNED_FAILURE_EVENTS + 5)
    ]
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(
            200, json={"count": len(many_failures), "next": None, "results": many_failures}
        )
    )

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=None
    )

    assert len(selected) == MAX_RETURNED_FAILURE_EVENTS
    # The stream itself was fully inspected (no "next"); only the
    # *returned* failure list was capped -- these are different signals.
    assert inspection["inspection_capped"] is False
    assert inspection["more_failures_than_returned"] is True


@respx.mock
def test_collect_failure_events_exact_page_count_matching_cap_is_not_marked_inspection_capped(awx_client):
    # Regression test: a job with *exactly* MAX_EVENT_PAGES_INSPECTED
    # pages, whose last page naturally reports no further page, must not
    # be misreported as capped just because the inspected-page count
    # happens to equal the cap by coincidence.
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "count": MAX_EVENT_PAGES_INSPECTED,
                "next": "x" if i < MAX_EVENT_PAGES_INSPECTED - 1 else None,
                "results": [_event("runner_on_ok", id=i)],
            },
        )
        for i in range(MAX_EVENT_PAGES_INSPECTED)
    ]

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=None
    )

    assert route.call_count == MAX_EVENT_PAGES_INSPECTED
    assert inspection["pages_inspected"] == MAX_EVENT_PAGES_INSPECTED
    assert inspection["inspection_capped"] is False


@respx.mock
def test_collect_failure_events_exact_relevant_failure_count_matching_cap_is_not_truncated(awx_client):
    # Regression test: a job with *exactly* MAX_RETURNED_FAILURE_EVENTS
    # relevant failures and nothing more must not report
    # more_failures_than_returned=True -- nothing was actually omitted.
    exact = [
        _event("runner_on_failed", id=i, counter=i) for i in range(MAX_RETURNED_FAILURE_EVENTS)
    ]
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(200, json={"count": len(exact), "next": None, "results": exact})
    )

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=None
    )

    assert len(selected) == MAX_RETURNED_FAILURE_EVENTS
    assert inspection["inspection_capped"] is False
    assert inspection["more_failures_than_returned"] is False


@respx.mock
def test_collect_failure_events_deterministic_ordering(awx_client):
    events = [
        _event("runner_on_failed", id=3, counter=3),
        _event("runner_on_unreachable", id=1, counter=1),
        _event("runner_on_failed", id=2, counter=2),
    ]
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(200, json={"count": 3, "next": None, "results": events})
    )

    selected, *_ = collect_failure_events(awx_client, 42, deadline=None, reliability_report=None)

    # Selection preserves whatever order AWX returned (requested via
    # order_by=counter) -- it does not re-sort.
    assert [e["counter"] for e in selected] == [3, 1, 2]


@respx.mock
def test_collect_failure_events_reports_classified_retrieval_failure(awx_client):
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(500, text="server error")
    )

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=None
    )

    assert selected == []
    assert error is not None
    assert error.kind == "upstream_error"
    assert inspection["inspection_capped"] is False


@respx.mock
def test_collect_failure_events_deadline_already_expired_means_zero_requests(awx_client):
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(200, json={"count": 0, "next": None, "results": []})
    )
    deadline = Deadline.after(0.0)

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=deadline, reliability_report=None
    )

    assert route.call_count == 0
    assert selected == []
    assert error is not None
    assert error.kind == ToolErrorKind.TIMEOUT.value


@respx.mock
def test_collect_failure_events_deadline_exhaustion_does_not_report_via_reliability_report(awx_client):
    # DeadlineExceededError is a budget/scheduling failure, not an
    # integration-health signal -- it must never be reported to the
    # run-local breaker the way a classified AWXJobEventsError is.
    deadline = Deadline.after(0.0)
    reported = []

    collect_failure_events(
        awx_client, 42, deadline=deadline, reliability_report=reported.append
    )

    assert reported == []


@respx.mock
def test_collect_failure_events_reports_via_reliability_report(awx_client):
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(500, text="server error")
    )
    reported = []

    def reliability_report(kind):
        reported.append(kind)
        return False

    collect_failure_events(awx_client, 42, deadline=None, reliability_report=reliability_report)

    assert reported == [IntegrationErrorKind.SERVER_ERROR]


@respx.mock
def test_collect_failure_events_stops_when_reliability_report_says_breaker_open(awx_client):
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(500, text="server error")
    )

    selected, inspection, error, breaker_open = collect_failure_events(
        awx_client, 42, deadline=None, reliability_report=lambda kind: True
    )

    assert breaker_open is True
    assert error is not None
