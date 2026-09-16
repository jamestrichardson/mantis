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
    return AWXClient(
        config=AWXConfig(
            url="https://awx.example.test", token=Secret("tok"), verify_ssl=True
        )
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
        def list_jobs(self, *, status, order_by, page_size):
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

        def get_job_stdout(self, job_id):
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

    result = awx_recent_failed_jobs(limit=1)

    job = result["jobs"][0]
    assert job["stdout_retrieval_error"] is not None
    assert job["failure_excerpt"] == ""
    # The AWX-reported failure fields must remain untouched by the stdout error.
    assert job["failed"] is True

    # stdout_retrieval_error is a typed ToolError (mantis.contracts), not a
    # bare string — its "kind" must never be confused with the job's own
    # failure reason above.
    assert job["stdout_retrieval_error"]["kind"] == "retrieval_error"
    assert "message" in job["stdout_retrieval_error"]


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
