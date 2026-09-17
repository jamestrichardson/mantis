"""Tests for the AWX integration client and the awx_recent_failed_jobs tool.

All HTTP interactions are mocked with respx; no live AWX instance required.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mantis.config import AWXConfig, Secret
from mantis.integrations.awx import AWXClient, AWXError, AWXStdoutError
from mantis.tools._text import extract_excerpt, tail
from mantis.tools.awx import FAILURE_MARKERS, awx_recent_failed_jobs


@pytest.fixture
def awx_client() -> AWXClient:
    # sleep=lambda *_: None: no test in this file should ever really wait
    # out a retry backoff (see #15 / mantis.reliability) — a real delay
    # here would make retry-triggering tests (a mocked 500/503/etc.)
    # slow without adding any coverage value.
    return AWXClient(
        config=AWXConfig(
            url="https://awx.example.test", token=Secret("tok"), verify_ssl=True
        ),
        sleep=lambda *_: None,
    )


# ---------------------------------------------------------------------------
# list_jobs: failed-job query parameters
# ---------------------------------------------------------------------------


@respx.mock
def test_list_jobs_sends_expected_query_params(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)

    assert route.called
    request = route.calls.last.request
    assert request.url.params["status"] == "failed"
    assert request.url.params["order_by"] == "-finished"
    assert request.url.params["page_size"] == "5"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_list_jobs_returns_total_count_for_truncation_detection(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(
            200, json={"count": 17, "results": [{"id": 1}, {"id": 2}]}
        )
    )

    page = awx_client.list_jobs(status="failed", order_by="-finished", page_size=2)

    assert page.total_count == 17
    assert len(page.jobs) == 2


@respx.mock
def test_list_jobs_raises_awx_error_on_http_failure(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with pytest.raises(AWXError):
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)


# ---------------------------------------------------------------------------
# get_job_stdout: normal retrieval and txt_download fallback
# ---------------------------------------------------------------------------


@respx.mock
def test_get_job_stdout_normal_retrieval(awx_client: AWXClient):
    respx.get(
        "https://awx.example.test/api/v2/jobs/42/stdout/",
        params={"format": "txt"},
    ).mock(return_value=httpx.Response(200, text="PLAY RECAP\nok=1 failed=0"))

    result = awx_client.get_job_stdout(42)

    assert "PLAY RECAP" in result


@respx.mock
def test_get_job_stdout_falls_back_to_txt_download(awx_client: AWXClient):
    too_large_notice = "This is too large to display. Use the download feature."
    respx.get(
        "https://awx.example.test/api/v2/jobs/42/stdout/",
        params={"format": "txt"},
    ).mock(return_value=httpx.Response(200, text=too_large_notice))

    full_route = respx.get(
        "https://awx.example.test/api/v2/jobs/42/stdout/",
        params={"format": "txt_download"},
    ).mock(return_value=httpx.Response(200, text="full stdout content" * 1000))

    result = awx_client.get_job_stdout(42)

    assert full_route.called
    assert "full stdout content" in result
    assert too_large_notice not in result


@respx.mock
def test_get_job_stdout_uses_text_plain_accept_header(awx_client: AWXClient):
    route = respx.get(
        "https://awx.example.test/api/v2/jobs/42/stdout/",
        params={"format": "txt"},
    ).mock(return_value=httpx.Response(200, text="ok"))

    awx_client.get_job_stdout(42)

    assert route.calls.last.request.headers["Accept"] == "text/plain"


@respx.mock
def test_get_job_stdout_raises_stdout_error_not_awx_error(awx_client: AWXClient):
    respx.get(
        "https://awx.example.test/api/v2/jobs/42/stdout/",
        params={"format": "txt"},
    ).mock(return_value=httpx.Response(503, text="unavailable"))

    with pytest.raises(AWXStdoutError):
        awx_client.get_job_stdout(42)


# ---------------------------------------------------------------------------
# text preprocessing: excerpt extraction and tail truncation
# ---------------------------------------------------------------------------


def test_extract_excerpt_finds_marker_lines():
    text = "\n".join(
        [
            "TASK [debug]",
            "ok: [host1]",
            'fatal: [host2]: UNREACHABLE! => {"msg": "No route to host"}',
            "PLAY RECAP",
            "host1 : ok=1 failed=0",
            "host2 : ok=0 failed=1 unreachable=1",
        ]
    )

    excerpt = extract_excerpt(text, FAILURE_MARKERS)

    assert "UNREACHABLE!" in excerpt
    assert "PLAY RECAP" in excerpt
    assert "failed=0" in excerpt or "unreachable=1" in excerpt


def test_extract_excerpt_returns_empty_when_no_markers_present():
    text = "TASK [debug]\nok: [host1]\nok: [host2]"

    assert extract_excerpt(text, FAILURE_MARKERS) == ""


def test_tail_truncates_and_marks_omission():
    text = "x" * 20_000

    result = tail(text, max_chars=12_000)

    assert len(result) < 20_000
    assert "omitted" in result
    assert result.endswith("x" * 100)


def test_tail_returns_full_text_when_under_limit():
    text = "short output"

    assert tail(text, max_chars=12_000) == text


# ---------------------------------------------------------------------------
# awx_recent_failed_jobs: end-to-end tool behavior
# ---------------------------------------------------------------------------


@respx.mock
def test_awx_recent_failed_jobs_returns_job_summaries(monkeypatch):
    jobs_payload = {
        "results": [
            {
                "id": 101,
                "name": "deploy-webservers",
                "status": "failed",
                "started": "2026-09-14T10:00:00Z",
                "finished": "2026-09-14T10:05:00Z",
                "elapsed": 300.0,
                "failed": True,
                "job_explanation": "",
                "inventory": 5,
                "project": 3,
                "job_template": 7,
                "summary_fields": {
                    "inventory": {"name": "production"},
                    "project": {"name": "site-ops"},
                    "job_template": {"name": "deploy-webservers"},
                },
            }
        ]
    }
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(200, json=jobs_payload)
    )
    respx.get(
        "https://awx.example.test/api/v2/jobs/101/stdout/",
        params={"format": "txt"},
    ).mock(
        return_value=httpx.Response(
            200,
            text=(
                "TASK [Gathering Facts]\n"
                "fatal: [host1]: UNREACHABLE! => "
                '{"msg": "ssh: connect to host host1 port 22: No route to host"}\n'
                "PLAY RECAP\nhost1 : ok=0 failed=0 unreachable=1"
            ),
        )
    )

    result = awx_recent_failed_jobs(limit=3)

    assert result["returned_count"] == 1
    job = result["jobs"][0]
    assert job["id"] == 101
    assert job["inventory"] == "production"
    assert job["project"] == "site-ops"
    assert job["job_template"] == "deploy-webservers"
    assert "No route to host" in job["failure_excerpt"]
    assert job["stdout_retrieval_error"] is None

    # Result contract (mantis.contracts.QueryMeta) adoption: every result
    # carries provenance, and with only 1 job in AWX matching (no "count"
    # in the mocked payload defaults to len(results)), nothing was
    # truncated.
    assert result["meta"]["source_system"] == "awx"
    assert result["meta"]["truncated"] is False
    assert "query_time" in result["meta"]

    # derived_fields makes "evidence vs. Mantis interpretation" machine
    # checkable: failure_excerpt/stdout_tail are Mantis-computed, every
    # other job field is AWX-reported verbatim.
    assert set(result["meta"]["derived_fields"]) == {"failure_excerpt", "stdout_tail"}

    # observation_time is deliberately unset at the meta level: this call
    # returns multiple jobs, each with its own natural observation time
    # (its own "finished" field) — no single batch timestamp applies.
    assert result["meta"]["observation_time"] is None


@respx.mock
def test_awx_recent_failed_jobs_reports_truncation_when_more_jobs_exist():
    jobs_payload = {
        "count": 17,
        "results": [
            {
                "id": 101,
                "name": "deploy-webservers",
                "status": "failed",
                "started": "2026-09-14T10:00:00Z",
                "finished": "2026-09-14T10:05:00Z",
                "elapsed": 300.0,
                "failed": True,
                "job_explanation": "",
            }
        ],
    }
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(200, json=jobs_payload)
    )
    respx.get(
        "https://awx.example.test/api/v2/jobs/101/stdout/",
        params={"format": "txt"},
    ).mock(return_value=httpx.Response(200, text="ok"))

    result = awx_recent_failed_jobs(limit=1)

    # AWX reports 17 total matching failed jobs but only 1 was returned
    # (limit=1) — that's truncation, distinct from "fewer jobs exist than
    # requested."
    assert result["meta"]["truncated"] is True


def test_awx_recent_failed_jobs_accepts_a_client_override():
    # This is the injection point mantis.eval uses to run this exact
    # production code path against fixture data instead of live AWX — no
    # real AWXConfig/env vars should be required when a client is given.
    class StubClient:
        def list_jobs(self, *, status, order_by, page_size, deadline=None):
            from mantis.integrations.awx import JobListPage

            return JobListPage(
                jobs=[
                    {
                        "id": 1,
                        "name": "stub-job",
                        "status": "failed",
                        "started": "2026-09-14T10:00:00Z",
                        "finished": "2026-09-14T10:05:00Z",
                        "elapsed": 1.0,
                        "failed": True,
                        "job_explanation": "",
                    }
                ],
                total_count=1,
            )

        def get_job_stdout(self, job_id, *, deadline=None):
            return "PLAY RECAP\nok=1 failed=0"

    result = awx_recent_failed_jobs(limit=1, _client=StubClient())

    assert result["returned_count"] == 1
    assert result["jobs"][0]["id"] == 1


@respx.mock
def test_awx_recent_failed_jobs_clamps_limit_above_max():
    route = respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    awx_recent_failed_jobs(limit=999)

    assert route.calls.last.request.url.params["page_size"] == "10"


@respx.mock
def test_awx_recent_failed_jobs_separates_stdout_error_from_job_failure():
    jobs_payload = {
        "results": [
            {
                "id": 202,
                "name": "broken-stdout-job",
                "status": "failed",
                "started": "2026-09-14T10:00:00Z",
                "finished": "2026-09-14T10:05:00Z",
                "elapsed": 300.0,
                "failed": True,
                "job_explanation": "",
            }
        ]
    }
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(200, json=jobs_payload)
    )
    respx.get(
        "https://awx.example.test/api/v2/jobs/202/stdout/",
        params={"format": "txt"},
    ).mock(return_value=httpx.Response(500, text="server error"))

    # 500 is a retryable classification (see mantis.reliability) — inject
    # a no-op sleep so this test doesn't really wait out the backoff
    # between the retry attempts it deliberately triggers.
    client = AWXClient(
        config=AWXConfig(url="https://awx.example.test", token=Secret("tok"), verify_ssl=True),
        sleep=lambda *_: None,
    )
    result = awx_recent_failed_jobs(limit=1, _client=client)

    job = result["jobs"][0]
    assert job["stdout_retrieval_error"] is not None
    assert job["failure_excerpt"] == ""
    # The AWX-reported failure fields must remain untouched by the stdout error.
    assert job["failed"] is True

    # stdout_retrieval_error is a typed ToolError (mantis.contracts), not a
    # bare string — its "kind" must never be confused with the job's own
    # failure reason above. A 500 from AWX's own stdout endpoint classifies
    # as upstream_error (see mantis.reliability.classify_http_status) —
    # distinct from e.g. a connection failure reaching AWX at all.
    assert job["stdout_retrieval_error"]["kind"] == "upstream_error"
    assert "message" in job["stdout_retrieval_error"]


@respx.mock
def test_awx_recent_failed_jobs_reports_swallowed_stdout_failures_via_reliability_report():
    # Regression test (PR #72 review): a per-job stdout failure never
    # raises out of awx_recent_failed_jobs — it degrades into that job's
    # own stdout_retrieval_error so the other jobs' evidence still comes
    # back. But AgentRuntime's run-local breaker (RunLocalBreaker) resets
    # on every "successful" tool call, so a swallowed failure like this
    # one must still be reported into the breaker via _reliability_report,
    # or an unhealthy AWX could be hit repeatedly across many jobs/calls
    # without the breaker ever noticing. See docs/reliability.md.
    jobs_payload = {
        "results": [
            {"id": 301, "name": "job-a", "status": "failed", "failed": True},
            {"id": 302, "name": "job-b", "status": "failed", "failed": True},
        ]
    }
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(200, json=jobs_payload)
    )
    respx.get(
        "https://awx.example.test/api/v2/jobs/301/stdout/", params={"format": "txt"}
    ).mock(return_value=httpx.Response(500, text="server error"))
    respx.get(
        "https://awx.example.test/api/v2/jobs/302/stdout/", params={"format": "txt"}
    ).mock(return_value=httpx.Response(200, text="ok stdout"))

    client = AWXClient(
        config=AWXConfig(url="https://awx.example.test", token=Secret("tok"), verify_ssl=True),
        sleep=lambda *_: None,
    )
    reported: list[IntegrationErrorKind] = []
    result = awx_recent_failed_jobs(
        limit=2, _client=client, _reliability_report=reported.append
    )

    # Only the job whose stdout retrieval actually failed is reported —
    # once, matching the single AWXStdoutError it swallowed.
    assert reported == [IntegrationErrorKind.SERVER_ERROR]
    assert result["jobs"][0]["stdout_retrieval_error"] is not None
    assert result["jobs"][1]["stdout_retrieval_error"] is None


@respx.mock
def test_awx_recent_failed_jobs_stops_requesting_stdout_once_the_breaker_opens_mid_call():
    # Regression test (PR #72 review, round 2): reporting a degraded
    # failure into the breaker fixes visibility for the *next* tool call,
    # but awx_recent_failed_jobs would still keep requesting stdout for
    # every remaining job in the SAME call after the breaker opens unless
    # it actually checks _reliability_report's return value and stops.
    # With short_circuit_threshold=3 and five jobs all failing stdout,
    # only the first three should ever be requested at all.
    jobs_payload = {
        "results": [
            {"id": 500 + i, "name": f"job-{i}", "status": "failed", "failed": True}
            for i in range(5)
        ]
    }
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(200, json=jobs_payload)
    )
    stdout_route = respx.route(
        method="GET", url__regex=r"https://awx\.example\.test/api/v2/jobs/\d+/stdout/"
    ).mock(return_value=httpx.Response(500, text="server error"))

    client = AWXClient(
        config=AWXConfig(url="https://awx.example.test", token=Secret("tok"), verify_ssl=True),
        sleep=lambda *_: None,
    )
    breaker = RunLocalBreaker(threshold=3)

    def reliability_report(kind: IntegrationErrorKind) -> bool:
        breaker.record_failure("awx", kind)
        return breaker.is_open("awx")

    result = awx_recent_failed_jobs(
        limit=5, _client=client, _reliability_report=reliability_report
    )

    # Three logical stdout retrievals, each exhausting the default
    # 3-attempt retry budget -> 9 real HTTP requests total. Jobs 4 and 5
    # must never reach the network at all.
    assert stdout_route.call_count == 3 * client.reliability.retry_max_attempts

    jobs = result["jobs"]
    assert len(jobs) == 5
    for job in jobs[:3]:
        # Attempted and failed -- a real AWXStdoutError-derived message.
        assert job["stdout_retrieval_error"] is not None
        assert "skip" not in job["stdout_retrieval_error"]["message"].lower()
    for job in jobs[3:]:
        # Never attempted at all -- distinct wording from an actual failure.
        assert job["stdout_retrieval_error"] is not None
        assert "skip" in job["stdout_retrieval_error"]["message"].lower()


@respx.mock
def test_awx_recent_failed_jobs_contract_adoption_keeps_every_prior_field():
    # Regression test for issue #23's acceptance criterion: adopting the
    # shared result contract must not drop any AWX-specific field that
    # existed before the contract was introduced.
    jobs_payload = {
        "results": [
            {
                "id": 303,
                "name": "job-with-everything",
                "status": "failed",
                "started": "2026-09-14T10:00:00Z",
                "finished": "2026-09-14T10:05:00Z",
                "elapsed": 300.0,
                "failed": True,
                "job_explanation": "some explanation",
                "inventory": 5,
                "project": 3,
                "job_template": 7,
                "summary_fields": {
                    "inventory": {"name": "production"},
                    "project": {"name": "site-ops"},
                    "job_template": {"name": "deploy-webservers"},
                },
            }
        ]
    }
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        return_value=httpx.Response(200, json=jobs_payload)
    )
    respx.get(
        "https://awx.example.test/api/v2/jobs/303/stdout/",
        params={"format": "txt"},
    ).mock(return_value=httpx.Response(200, text="ok"))

    result = awx_recent_failed_jobs(limit=1)

    assert set(result.keys()) >= {"meta", "requested_limit", "returned_count", "jobs"}
    job = result["jobs"][0]
    assert set(job.keys()) >= {
        "id",
        "name",
        "status",
        "started",
        "finished",
        "elapsed",
        "failed",
        "job_explanation",
        "inventory",
        "project",
        "job_template",
        "stdout_retrieval_error",
        "failure_excerpt",
        "stdout_tail",
    }


def test_awx_recent_failed_jobs_tool_is_registered_as_containing_untrusted_text():
    # AWX stdout/job-event text is external, Mantis-uncontrolled evidence
    # — the runtime's model-input safety pipeline (mantis.security) must
    # see this tool as untrusted so results get marked accordingly. See
    # docs/security.md.
    from mantis.registry import default_registry

    import mantis.tools  # noqa: F401 — registers built-in tools as a side effect

    tool = default_registry.get("awx_recent_failed_jobs")
    assert tool.contains_untrusted_text is True


# ---------------------------------------------------------------------------
# Reliability (#15): explicit timeouts, classification, retry behavior
# ---------------------------------------------------------------------------

from mantis.reliability import Deadline, IntegrationErrorKind, RunLocalBreaker  # noqa: E402


def test_awx_client_uses_explicit_connect_and_read_timeouts(awx_client: AWXClient):
    # No production request may rely on httpx's implicit default timeout
    # — every request must carry an explicit, named, configured timeout.
    with awx_client._client(accept="application/json") as client:
        timeout = client.timeout
    assert timeout.connect == awx_client.reliability.http_connect_timeout_seconds
    assert timeout.read == awx_client.reliability.http_read_timeout_seconds
    assert timeout.connect is not None
    assert timeout.read is not None


def test_awx_client_caps_effective_timeout_at_the_remaining_deadline(awx_client: AWXClient):
    # Regression test (PR #72 review): the configured connect/read
    # timeouts are a ceiling, not a promise. With only 2s left of tool
    # budget, a request must not still be configured with the full
    # (much larger) default read timeout — that would leave the
    # advertised per-tool-call budget far looser than what's documented.
    deadline = Deadline.after(2.0)

    with awx_client._client(accept="application/json", deadline=deadline) as client:
        timeout = client.timeout

    assert timeout.connect <= 2.0
    assert timeout.read <= 2.0
    assert timeout.connect < awx_client.reliability.http_connect_timeout_seconds
    assert timeout.read < awx_client.reliability.http_read_timeout_seconds


def test_awx_client_does_not_shrink_timeout_below_configured_value_when_deadline_is_generous(
    awx_client: AWXClient,
):
    # The cap only ever narrows the effective timeout — a deadline with
    # more time remaining than the configured timeout must not stretch
    # it beyond what was actually configured.
    deadline = Deadline.after(10_000.0)

    with awx_client._client(accept="application/json", deadline=deadline) as client:
        timeout = client.timeout

    assert timeout.connect == awx_client.reliability.http_connect_timeout_seconds
    assert timeout.read == awx_client.reliability.http_read_timeout_seconds


def test_awx_client_uses_full_configured_timeout_when_no_deadline_given(awx_client: AWXClient):
    with awx_client._client(accept="application/json", deadline=None) as client:
        timeout = client.timeout

    assert timeout.connect == awx_client.reliability.http_connect_timeout_seconds
    assert timeout.read == awx_client.reliability.http_read_timeout_seconds


def test_reliability_config_defaults_are_conservative_but_bounded():
    from mantis.config import ReliabilityConfig

    config = ReliabilityConfig()
    assert 0 < config.http_connect_timeout_seconds <= 10
    assert config.http_connect_timeout_seconds < config.http_read_timeout_seconds
    assert config.retry_max_attempts >= 1


@respx.mock
def test_connect_timeout_classifies_as_timeout_kind(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/").mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(AWXError) as excinfo:
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=1)

    assert excinfo.value.kind == IntegrationErrorKind.TIMEOUT


@respx.mock
def test_read_timeout_classifies_as_timeout_kind(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/").mock(side_effect=httpx.ReadTimeout("timed out"))

    with pytest.raises(AWXError) as excinfo:
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=1)

    assert excinfo.value.kind == IntegrationErrorKind.TIMEOUT


@respx.mock
def test_connection_error_classifies_as_connection_kind(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(AWXError) as excinfo:
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=1)

    assert excinfo.value.kind == IntegrationErrorKind.CONNECTION


@respx.mock
def test_transient_failure_then_success_consumes_two_attempts_one_logical_call(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/")
    route.side_effect = [
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json={"results": [], "count": 0}),
    ]

    page = awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)

    assert page.total_count == 0
    assert route.call_count == 2


@respx.mock
def test_retry_exhaustion_raises_after_named_max_attempts(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/")
    route.side_effect = [httpx.Response(503)] * awx_client.reliability.retry_max_attempts

    with pytest.raises(AWXError) as excinfo:
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)

    assert route.call_count == awx_client.reliability.retry_max_attempts
    assert excinfo.value.kind == IntegrationErrorKind.SERVER_ERROR


@respx.mock
def test_429_is_retried(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"results": [], "count": 0}),
    ]

    awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)

    assert route.call_count == 2


@pytest.mark.parametrize("status", [502, 503, 504])
@respx.mock
def test_representative_5xx_statuses_are_retried(awx_client: AWXClient, status):
    route = respx.get("https://awx.example.test/api/v2/jobs/")
    route.side_effect = [
        httpx.Response(status),
        httpx.Response(200, json={"results": [], "count": 0}),
    ]

    awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)

    assert route.call_count == 2


@respx.mock
def test_authentication_failure_is_not_retried(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/").mock(return_value=httpx.Response(401))

    with pytest.raises(AWXError) as excinfo:
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)

    assert route.calls.call_count == 1
    assert excinfo.value.kind == IntegrationErrorKind.AUTHENTICATION


@respx.mock
def test_authorization_failure_is_not_retried(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/").mock(return_value=httpx.Response(403))

    with pytest.raises(AWXError) as excinfo:
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)

    assert route.calls.call_count == 1
    assert excinfo.value.kind == IntegrationErrorKind.AUTHORIZATION


@respx.mock
def test_bad_request_is_not_retried(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/").mock(return_value=httpx.Response(400))

    with pytest.raises(AWXError) as excinfo:
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=5)

    assert route.calls.call_count == 1
    assert excinfo.value.kind == IntegrationErrorKind.BAD_REQUEST


@respx.mock
def test_not_found_is_not_retried_by_default(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/999/").mock(return_value=httpx.Response(404))

    with pytest.raises(AWXError) as excinfo:
        awx_client.get_job(999)

    assert route.calls.call_count == 1
    assert excinfo.value.kind == IntegrationErrorKind.NOT_FOUND


@respx.mock
def test_retry_stops_respecting_remaining_tool_deadline(awx_client: AWXClient):
    from mantis.reliability import Deadline

    route = respx.get("https://awx.example.test/api/v2/jobs/").mock(return_value=httpx.Response(503))
    # Expired before the call even starts -> zero HTTP requests, not a
    # multi-attempt retry sequence.
    deadline = Deadline.after(0.0)

    with pytest.raises(Exception):
        awx_client.list_jobs(status="failed", order_by="-finished", page_size=5, deadline=deadline)

    assert route.calls.call_count == 0


# ---------------------------------------------------------------------------
# AWXClient.list_job_events (#28) -- reuses the exact same _get()/retry_call
# path as list_jobs/get_job/get_job_stdout, so most of the generic timeout/
# retry/classification behavior is already covered above against those
# methods. These tests confirm list_job_events itself is wired into that
# same shared path correctly, plus its own pagination-metadata parsing.
# ---------------------------------------------------------------------------

from mantis.integrations.awx import AWXJobEventsError, JobEventPage  # noqa: E402


@respx.mock
def test_list_job_events_returns_events_and_pagination_metadata(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 3,
                "next": "http://awx/api/v2/jobs/42/job_events/?page=2",
                "results": [{"id": 1, "event": "runner_on_failed"}],
            },
        )
    )

    page = awx_client.list_job_events(42, page=1, page_size=50)

    assert isinstance(page, JobEventPage)
    assert page.total_count == 3
    assert page.next_page == 2
    assert page.events == [{"id": 1, "event": "runner_on_failed"}]


@respx.mock
def test_list_job_events_next_page_is_none_when_awx_reports_no_further_page(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(200, json={"count": 1, "next": None, "results": [{"id": 1}]})
    )

    page = awx_client.list_job_events(42)

    assert page.next_page is None


@respx.mock
def test_list_job_events_sends_expected_query_params(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(200, json={"count": 0, "next": None, "results": []})
    )

    awx_client.list_job_events(
        42, page=2, page_size=25, event_types=("runner_on_failed", "runner_on_unreachable")
    )

    request = route.calls.last.request
    assert request.url.params["page"] == "2"
    assert request.url.params["page_size"] == "25"
    assert request.url.params["order_by"] == "counter"
    assert request.url.params["event__in"] == "runner_on_failed,runner_on_unreachable"


@respx.mock
def test_list_job_events_omits_event_filter_when_not_given(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(200, json={"count": 0, "next": None, "results": []})
    )

    awx_client.list_job_events(42)

    assert "event__in" not in route.calls.last.request.url.params


@pytest.mark.parametrize("status,attr", [(401, "AUTHENTICATION"), (403, "AUTHORIZATION"), (404, "NOT_FOUND")])
@respx.mock
def test_list_job_events_classifies_client_errors_and_does_not_retry(awx_client: AWXClient, status, attr):
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(status)
    )

    with pytest.raises(AWXJobEventsError) as excinfo:
        awx_client.list_job_events(42)

    assert excinfo.value.kind == getattr(IntegrationErrorKind, attr)
    assert route.calls.call_count == 1


@respx.mock
def test_list_job_events_transient_5xx_is_retried(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/")
    route.side_effect = [
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json={"count": 0, "next": None, "results": []}),
    ]

    page = awx_client.list_job_events(42)

    assert page.total_count == 0
    assert route.call_count == 2


@respx.mock
def test_list_job_events_connect_timeout_classifies_as_timeout(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )

    with pytest.raises(AWXJobEventsError) as excinfo:
        awx_client.list_job_events(42)

    assert excinfo.value.kind == IntegrationErrorKind.TIMEOUT


@respx.mock
def test_list_job_events_connection_error_classifies_as_connection(awx_client: AWXClient):
    respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        side_effect=httpx.ConnectError("refused")
    )

    with pytest.raises(AWXJobEventsError) as excinfo:
        awx_client.list_job_events(42)

    assert excinfo.value.kind == IntegrationErrorKind.CONNECTION


@respx.mock
def test_list_job_events_deadline_already_expired_means_zero_requests(awx_client: AWXClient):
    route = respx.get("https://awx.example.test/api/v2/jobs/42/job_events/").mock(
        return_value=httpx.Response(503)
    )
    deadline = Deadline.after(0.0)

    with pytest.raises(Exception):
        awx_client.list_job_events(42, deadline=deadline)

    assert route.calls.call_count == 0
