"""Semantic AWX tools exposed to agents.

Currently provides ``awx_recent_failed_jobs``, which fetches the most
recently finished failed jobs, retrieves their stdout, and preprocesses
that stdout into a small, high-signal excerpt plus a bounded tail —
instead of handing multi-megabyte Ansible output to the model.
"""

from __future__ import annotations

import logging
from typing import Any

from mantis.config import AWXConfig
from mantis.integrations.awx import AWXClient, AWXError, AWXStdoutError
from mantis.registry import Tool, default_registry
from mantis.tools._text import extract_excerpt, tail

logger = logging.getLogger(__name__)

MAX_FAILED_JOBS_LIMIT = 10

# Heuristic markers for locating high-value lines in Ansible/AWX output.
# Order doesn't matter; matching is substring-based.
FAILURE_MARKERS = [
    "FAILED!",
    "fatal:",
    "UNREACHABLE!",
    "ERROR!",
    "Traceback",
    "exception",
    "PLAY RECAP",
    "failed=",
    "unreachable=",
    "rescued=",
    "ignored=",
]

_JOB_FIELDS = (
    "id",
    "name",
    "status",
    "started",
    "finished",
    "elapsed",
    "failed",
    "job_explanation",
)


def _get_client() -> AWXClient:
    return AWXClient(config=AWXConfig.from_env())


def _summary_name(job: dict[str, Any], key: str) -> Any:
    """Best-effort human-readable name for a related object (inventory,
    project, job_template), falling back to the raw id field AWX always
    includes.
    """
    summary_fields = job.get("summary_fields") or {}
    related = summary_fields.get(key) or {}
    return related.get("name", job.get(key))


def _summarize_job(client: AWXClient, job: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {field: job.get(field) for field in _JOB_FIELDS}
    result["inventory"] = _summary_name(job, "inventory")
    result["project"] = _summary_name(job, "project")
    result["job_template"] = _summary_name(job, "job_template")

    job_id = job.get("id")
    try:
        stdout = client.get_job_stdout(job_id)
    except AWXStdoutError as exc:
        # Deliberately separate from the AWX job's own failure reason
        # (job_explanation / failed above): this represents our failure to
        # *retrieve* evidence, not evidence of an infrastructure failure.
        logger.warning("Could not retrieve stdout for AWX job %s: %s", job_id, exc)
        result["stdout_retrieval_error"] = str(exc)
        result["failure_excerpt"] = ""
        result["stdout_tail"] = ""
        return result

    result["stdout_retrieval_error"] = None
    result["failure_excerpt"] = extract_excerpt(stdout, FAILURE_MARKERS)
    result["stdout_tail"] = tail(stdout)
    return result


def awx_recent_failed_jobs(limit: int = 5) -> dict[str, Any]:
    """Fetch the most recently finished failed AWX jobs, with preprocessed
    stdout evidence for each.

    Args:
        limit: Number of jobs to return. Clamped to
            ``[1, MAX_FAILED_JOBS_LIMIT]``.

    Returns:
        A dict with ``requested_limit``, ``returned_count``, and ``jobs``
        (a list of job summaries, most recently finished first). Each job
        summary separates AWX-reported failure information from any error
        Mantis encountered while retrieving stdout evidence
        (``stdout_retrieval_error``).
    """
    clamped_limit = max(1, min(limit, MAX_FAILED_JOBS_LIMIT))

    client = _get_client()
    try:
        jobs = client.list_jobs(
            status="failed",
            order_by="-finished",
            page_size=clamped_limit,
        )
    except AWXError as exc:
        raise AWXError(f"Could not list recent failed AWX jobs: {exc}") from exc

    summarized = [_summarize_job(client, job) for job in jobs]

    return {
        "requested_limit": limit,
        "returned_count": len(summarized),
        "jobs": summarized,
    }


AWX_RECENT_FAILED_JOBS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "awx_recent_failed_jobs",
        "description": (
            "Fetch the most recently finished failed AWX jobs (up to 10). "
            "For each job, returns identifying metadata (id, name, "
            "template, project, inventory, timing) plus preprocessed "
            "stdout evidence: a failure_excerpt of high-value lines and a "
            "bounded stdout_tail. Any error retrieving a job's stdout is "
            "reported separately as stdout_retrieval_error and must not "
            "be treated as the job's own failure reason. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": (
                        "Number of failed jobs to return. Maximum 10."
                    ),
                    "default": 5,
                    "minimum": 1,
                    "maximum": MAX_FAILED_JOBS_LIMIT,
                }
            },
            "required": [],
        },
    },
}


default_registry.register(
    Tool(
        name="awx_recent_failed_jobs",
        schema=AWX_RECENT_FAILED_JOBS_SCHEMA,
        handler=awx_recent_failed_jobs,
        category="awx",
        mutating=False,
        description=(
            "List the most recently finished failed AWX jobs with "
            "preprocessed stdout evidence."
        ),
    )
)
