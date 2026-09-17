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
from typing import Any, Callable

from mantis.config import AWXConfig
from mantis.contracts import QueryMeta, ToolError, ToolErrorKind
from mantis.integrations.awx import AWXClient, AWXError, AWXStdoutError
from mantis.registry import Tool, default_registry
from mantis.reliability import Deadline, IntegrationErrorKind
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


def _job_base_summary(job: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {field: job.get(field) for field in _JOB_FIELDS}
    result["inventory"] = _summary_name(job, "inventory")
    result["project"] = _summary_name(job, "project")
    result["job_template"] = _summary_name(job, "job_template")
    return result


def _skipped_job_summary(job: dict[str, Any]) -> dict[str, Any]:
    """A job summary for one whose stdout was never even requested because
    the run-local breaker opened earlier in this same logical tool call
    (see the loop in :func:`awx_recent_failed_jobs`). Distinct from a real
    ``AWXStdoutError`` — this job's retrieval was never attempted at all,
    not attempted-and-failed."""
    result = _job_base_summary(job)
    result["stdout_retrieval_error"] = ToolError(
        kind=ToolErrorKind.RETRIEVAL_ERROR,
        message=(
            "Stdout retrieval skipped: AWX has failed repeatedly during "
            "this call; short-circuiting further requests without "
            "retrieving this job's stdout. See "
            "mantis.reliability.RunLocalBreaker."
        ),
    ).to_dict()
    result["failure_excerpt"] = ""
    result["stdout_tail"] = ""
    return result


def _summarize_job(
    client: AWXClient,
    job: dict[str, Any],
    *,
    deadline: Deadline | None,
    reliability_report: Callable[[IntegrationErrorKind], bool] | None,
) -> tuple[dict[str, Any], bool]:
    """Returns ``(job_summary, breaker_now_open)`` — the latter is True
    only when this job's own stdout failure was the one that pushed the
    run-local breaker open (via ``reliability_report``'s return value),
    which tells the caller to stop enriching any further jobs in this
    same call. See ``docs/reliability.md``'s "Partial-success tools and
    the breaker" section.
    """
    result = _job_base_summary(job)

    job_id = job.get("id")
    try:
        stdout = client.get_job_stdout(job_id, deadline=deadline)
    except AWXStdoutError as exc:
        # Deliberately separate from the AWX job's own failure reason
        # (job_explanation / failed above): this represents our failure to
        # *retrieve* evidence, not evidence of an infrastructure failure.
        # kind comes from the real classification the integration layer
        # assigned (mantis.reliability) — never hardcoded — so, e.g., a
        # timeout is reported as ToolErrorKind.TIMEOUT, not a generic
        # retrieval_error indistinguishable from a connection failure.
        #
        # This is swallowed here, not raised — awx_recent_failed_jobs
        # still returns a successful result with the other jobs' evidence
        # intact. But an AgentRuntime tool call that "succeeds" resets its
        # run-local breaker (mantis.reliability.RunLocalBreaker), so a
        # degraded-but-swallowed failure like this one must still be
        # reported into that breaker explicitly, or a run could make many
        # failing stdout requests against an unhealthy AWX while the
        # breaker stays blind to it (see docs/reliability.md).
        logger.warning("Could not retrieve stdout for AWX job %s: %s", job_id, exc)
        breaker_now_open = False
        if reliability_report is not None:
            breaker_now_open = reliability_report(exc.kind)
        result["stdout_retrieval_error"] = ToolError(
            kind=exc.to_tool_error_kind(), message=str(exc)
        ).to_dict()
        result["failure_excerpt"] = ""
        result["stdout_tail"] = ""
        return result, breaker_now_open

    result["stdout_retrieval_error"] = None
    result["failure_excerpt"] = extract_excerpt(stdout, FAILURE_MARKERS)
    result["stdout_tail"] = tail(stdout)
    return result, False


def awx_recent_failed_jobs(
    limit: int = 5,
    *,
    _client: AWXClient | None = None,
    _deadline: Deadline | None = None,
    _reliability_report: Callable[[IntegrationErrorKind], bool] | None = None,
) -> dict[str, Any]:
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
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime`` — see ``mantis.reliability.Deadline`` and
            ``docs/reliability.md``. Same keyword-only/underscore
            convention as ``_client``, for the same reason. ``None``
            (the default for direct/manual calls) means no deadline is
            enforced beyond each individual request's own connect/read
            timeout.
        _reliability_report: Callback into the run-local breaker
            (``mantis.reliability.RunLocalBreaker``), set by
            ``AgentRuntime``. Same keyword-only/underscore convention as
            ``_client``/``_deadline``. Called once per job whose stdout
            retrieval fails, so a stdout failure that's swallowed into
            that job's own ``stdout_retrieval_error`` (rather than
            raised — see "Raises" below) still counts toward the breaker
            instead of being invisible to it, since a logically
            successful call like this one would otherwise reset it.
            Returns whether the breaker is now open for this call's
            category — once it is, this function stops requesting stdout
            for any remaining jobs in this same call (each gets a
            ``stdout_retrieval_error`` explaining it was skipped, not
            attempted-and-failed) instead of continuing to hammer an
            already-tripped integration until the *next* tool call. See
            ``docs/reliability.md``. ``None`` (the default for
            direct/manual calls) means no reporting happens and no
            within-call short-circuiting occurs.

    Raises:
        mantis.integrations.awx.AWXError: if the job list itself
            couldn't be retrieved (as opposed to one job's stdout, which
            degrades gracefully into that job's own
            ``stdout_retrieval_error`` instead of raising) — this is a
            ``mantis.reliability.IntegrationError`` subclass, which is
            what lets ``AgentRuntime`` classify, retry-account, and
            run-local-short-circuit on it generically.

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
        being misleading. If enough stdout failures occur within this
        same call to open the run-local breaker (only relevant when
        ``_reliability_report`` is given — see above), any remaining
        jobs are never requested at all; their ``stdout_retrieval_error``
        says so explicitly, distinct from one that was requested and
        failed.
    """
    clamped_limit = max(1, min(limit, MAX_FAILED_JOBS_LIMIT))

    client = _client or _get_client()
    try:
        page = client.list_jobs(
            status="failed",
            order_by="-finished",
            page_size=clamped_limit,
            deadline=_deadline,
        )
    except AWXError as exc:
        # Preserve the original classification/status/retry_after — a
        # wrap-and-reraise that dropped them would leave AgentRuntime
        # unable to tell a timeout from an auth failure here.
        raise AWXError(
            f"Could not list recent failed AWX jobs: {exc}",
            kind=exc.kind,
            status_code=exc.status_code,
            retry_after=exc.retry_after,
        ) from exc

    # An explicit loop, not a list comprehension: once the run-local
    # breaker opens (a job's stdout failure pushed the failure count to
    # threshold), every remaining job in this same call is skipped rather
    # than still attempted — a list comprehension has no way to stop
    # partway through. See _summarize_job's breaker_now_open return value
    # and docs/reliability.md's "Partial-success tools and the breaker".
    summarized: list[dict[str, Any]] = []
    breaker_open = False
    for job in page.jobs:
        if breaker_open:
            summarized.append(_skipped_job_summary(job))
            continue
        job_summary, breaker_open = _summarize_job(
            client, job, deadline=_deadline, reliability_report=_reliability_report
        )
        summarized.append(job_summary)

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
