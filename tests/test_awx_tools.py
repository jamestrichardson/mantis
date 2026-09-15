"""Tests for the AWX integration client and the awx_recent_failed_jobs tool.

All HTTP interactions are mocked with respx; no live AWX instance required.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mantis.config import AWXConfig
from mantis.integrations.awx import AWXClient, AWXError, AWXStdoutError
from mantis.tools._text import extract_excerpt, tail
from mantis.tools.awx import FAILURE_MARKERS, awx_recent_failed_jobs


@pytest.fixture
def awx_client() -> AWXClient:
    return AWXClient(
        config=AWXConfig(url="https://awx.example.test", token="tok", verify_ssl=True)
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
