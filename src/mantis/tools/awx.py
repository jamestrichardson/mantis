"""Semantic AWX tools exposed to agents.

Currently provides ``awx_recent_failed_jobs``, which fetches the most
recently finished failed jobs, retrieves their stdout, and preprocesses
that stdout into a small, high-signal excerpt plus a bounded tail —
instead of handing multi-megabyte Ansible output to the model.

Adopts the shared result contract from ``mantis.contracts`` (see that
module for the design rationale): a ``meta`` key carries provenance
(source system, query time, truncation), and any tool-level retrieval
failure is a typed :class:`~mantis.contracts.ToolError` instead of a bare
string. This is additive — every previously existing field stays exactly
where it was.
"""

from __future__ import annotations

import logging
from typing import Any

from mantis.config import AWXConfig
from mantis.contracts import QueryMeta, ToolError, ToolErrorKind
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

# Job-summary fields that are Mantis-generated interpretation of raw AWX
# stdout, not data AWX itself reported. Published via
# QueryMeta.derived_fields so this distinction is machine-checkable, not
# just a naming convention — see mantis.contracts.QueryMeta.
DERIVED_JOB_FIELDS = ("failure_excerpt", "stdout_tail")

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
        # Tagged with the shared ToolErrorKind vocabulary (mantis.contracts)
        # rather than left as a bare string.
        logger.warning("Could not retrieve stdout for AWX job %s: %s", job_id, exc)
        result["stdout_retrieval_error"] = ToolError(
            kind=ToolErrorKind.RETRIEVAL_ERROR, message=str(exc)
        ).to_dict()
        result["failure_excerpt"] = ""
        result["stdout_tail"] = ""
        return result

    result["stdout_retrieval_error"] = None
    result["failure_excerpt"] = extract_excerpt(stdout, FAILURE_MARKERS)
    result["stdout_tail"] = tail(stdout)
    return result


def awx_recent_failed_jobs(limit: int = 5, *, _client: AWXClient | None = None) -> dict[str, Any]:
    """Fetch the most recently finished failed AWX jobs, with preprocessed
    stdout evidence for each.

    Args:
        limit: Number of jobs to return. Clamped to
            ``[1, MAX_FAILED_JOBS_LIMIT]``.
        _client: Test/evaluation-only override for the AWX client.
            Keyword-only and leading-underscore so it's never something a
            model's tool-call arguments could accidentally set (the
            runtime only ever passes keys the model supplied in its JSON
            arguments). Defaults to a real client built from
            ``AWXConfig.from_env()``. Used by ``mantis.eval`` to run this
            exact production code path against fixture data instead of
            live AWX — see ``mantis/eval/fixtures/awx.py``.

    Returns:
        A dict with ``meta`` (provenance — see ``mantis.contracts.QueryMeta``),
        ``requested_limit``, ``returned_count``, and ``jobs`` (a list of
        job summaries, most recently finished first). Each job summary
        separates AWX-reported failure information from any error Mantis
        encountered while retrieving stdout evidence
        (``stdout_retrieval_error``, a typed
        ``mantis.contracts.ToolError`` when present). ``meta.truncated``
        is true when AWX has more matching failed jobs than were
        returned — distinct from simply fewer jobs existing than
        ``limit`` requested. ``meta.derived_fields`` names which job
        fields (``failure_excerpt``, ``stdout_tail``) are Mantis-computed
        interpretation rather than AWX-reported data.  ``meta.observation_time``
        is deliberately left unset: this call returns multiple jobs, each
        with its own natural observation time (its ``finished`` field) —
        no single batch-level timestamp could represent that without
        being misleading.
    """
    clamped_limit = max(1, min(limit, MAX_FAILED_JOBS_LIMIT))

    client = _client or _get_client()
    try:
        page = client.list_jobs(
            status="failed",
            order_by="-finished",
            page_size=clamped_limit,
        )
    except AWXError as exc:
        raise AWXError(f"Could not list recent failed AWX jobs: {exc}") from exc

    summarized = [_summarize_job(client, job) for job in page.jobs]
    meta = QueryMeta(
        source_system="awx",
        truncated=page.total_count > len(summarized),
        derived_fields=list(DERIVED_JOB_FIELDS),
    )

    return {
        "meta": meta.to_dict(),
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
            "reported separately as stdout_retrieval_error (an object with "
            "kind/message) and must not be treated as the job's own "
            "failure reason. A top-level meta field reports when this was "
            "queried and whether more matching failed jobs exist than were "
            "returned (meta.truncated). Read-only."
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
        # Explicit even though it's also Tool's default: stdout/job-event
        # text is external, Mantis-uncontrolled evidence and must be
        # treated as untrusted by the runtime's model-input safety
        # pipeline — see mantis.security and docs/security.md.
        contains_untrusted_text=True,
        description=(
            "List the most recently finished failed AWX jobs with "
            "preprocessed stdout evidence."
        ),
    )
)
