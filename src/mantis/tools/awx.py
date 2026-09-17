"""Semantic AWX tools exposed to agents.

Provides two tools:

- ``awx_recent_failed_jobs`` fetches the most recently finished failed
  jobs, retrieves their stdout, and preprocesses that stdout into a
  small, high-signal excerpt plus a bounded tail — instead of handing
  multi-megabyte Ansible output to the model.
- ``awx_get_job_failure`` (#28) fetches *structured* failure evidence
  for one job — deterministically selected job-event records (task
  failures, unreachable hosts) — with bounded stdout used only as
  supporting/fallback context, not the primary evidence source. See
  ``mantis.tools._awx_events`` for the selection logic and
  ``docs/awx-job-failure.md`` for the full design rationale.

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
from mantis.reliability import Deadline, DeadlineExceededError, IntegrationError, IntegrationErrorKind
from mantis.tools._awx_events import collect_failure_events
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


# ---------------------------------------------------------------------------
# awx_get_job_failure (#28): structured job-event failure evidence for one
# job, with bounded stdout as supporting/fallback context — never the
# primary evidence source. See mantis.tools._awx_events for the
# deterministic event-selection logic this orchestrates.
# ---------------------------------------------------------------------------

_JOB_CONTEXT_FIELDS = (
    "id",
    "name",
    "status",
    "started",
    "finished",
    "job_explanation",
)


def _job_context(job: dict[str, Any]) -> dict[str, Any]:
    """Just enough job metadata to interpret the evidence — never the
    full AWX job record. Reuses the same inventory/project/job_template
    name-resolution helper as ``awx_recent_failed_jobs`` (see
    ``_summary_name``); AWX's job-detail response (``get_job``) carries
    the same ``summary_fields`` shape as a job-list entry, so the same
    lookup applies unchanged."""
    result: dict[str, Any] = {field: job.get(field) for field in _JOB_CONTEXT_FIELDS}
    result["job_template"] = _summary_name(job, "job_template")
    result["project"] = _summary_name(job, "project")
    result["inventory"] = _summary_name(job, "inventory")
    return result


def _classify_sub_read_failure(exc: "AWXError | DeadlineExceededError") -> ToolError:
    if isinstance(exc, DeadlineExceededError):
        # Budget exhaustion, not an integration-health classification --
        # TIMEOUT is the closest existing ToolErrorKind without inventing
        # a new one (out of scope here; see docs/reliability.md).
        return ToolError(kind=ToolErrorKind.TIMEOUT, message=str(exc))
    return ToolError(kind=exc.to_tool_error_kind(), message=str(exc))


def _fetch_stdout_context(
    client: AWXClient,
    job_id: int,
    *,
    role: str,
    deadline: Deadline | None,
    reliability_report: Callable[[IntegrationErrorKind], bool] | None,
) -> tuple[dict[str, Any] | None, ToolError | None, bool]:
    """Fetch and bound stdout for ``job_id``, reusing the exact same
    stdout-retrieval and excerpt/tail helpers as ``awx_recent_failed_jobs``
    — no second stdout parser. ``role`` labels how this evidence is being
    used: ``"fallback"`` when no structured failure event was found (this
    is now the primary evidence), or ``"supporting"`` when structured
    failures already exist and this is just extra context.

    Returns ``(stdout_context, retrieval_error, breaker_now_open)``.
    """
    try:
        stdout = client.get_job_stdout(job_id, deadline=deadline)
    except (AWXStdoutError, DeadlineExceededError) as exc:
        logger.warning("Could not retrieve stdout for AWX job %s: %s", job_id, exc)
        breaker_now_open = False
        if isinstance(exc, IntegrationError) and reliability_report is not None:
            breaker_now_open = reliability_report(exc.kind)
        return None, _classify_sub_read_failure(exc), breaker_now_open

    stdout_context = {
        "role": role,
        "excerpt": extract_excerpt(stdout, FAILURE_MARKERS),
        "tail": tail(stdout),
    }
    return stdout_context, None, False


def awx_get_job_failure(
    job_id: int,
    *,
    _client: AWXClient | None = None,
    _deadline: Deadline | None = None,
    _reliability_report: Callable[[IntegrationErrorKind], bool] | None = None,
) -> dict[str, Any]:
    """Fetch structured failure evidence for one AWX job.

    Structured job-event records (task failures, unreachable hosts) are
    the preferred evidence source — deterministically selected and
    bounded, never a raw event-stream dump. Bounded stdout is fetched as
    supporting context when structured failures were found, or as
    fallback evidence when none were.

    Args:
        job_id: The AWX job to inspect (e.g. an ``id`` from
            ``awx_recent_failed_jobs``).
        _client: Test/evaluation-only client override — same convention
            as ``awx_recent_failed_jobs``.
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime``. Same convention as ``awx_recent_failed_jobs``.
        _reliability_report: Run-local breaker callback, set by
            ``AgentRuntime``. Same convention as
            ``awx_recent_failed_jobs`` — called for a degraded (not
            raised) sub-read failure, and its return value (whether the
            breaker is now open) is checked before attempting any
            further AWX request in this same call. See
            ``docs/reliability.md``.

    Raises:
        mantis.integrations.awx.AWXError: if the job's own context
            couldn't be retrieved at all — without it, nothing coherent
            can be returned, mirroring ``awx_recent_failed_jobs``'s
            treatment of a ``list_jobs`` failure. A failure to retrieve
            *structured events* or *stdout* is handled differently (see
            below): degraded into the result rather than raised, since
            job context plus one of the two remaining evidence sources
            can still be useful on their own.

    Returns:
        A dict with ``meta`` (provenance), ``job`` (context — id, name,
        status, template, project, inventory, timing), ``structured_failures``
        (bounded, normalized failure events — empty list if none were
        found or retrievable), ``structured_failures_error`` (a typed
        ``ToolError`` if event retrieval itself failed, else ``None``),
        ``event_inspection`` (``events_inspected``/``pages_inspected``/
        ``inspection_capped`` — whether the full event stream was
        inspected or a hard cap stopped it early), ``stdout_context``
        (bounded excerpt/tail plus a ``role`` of ``"fallback"`` or
        ``"supporting"``, or ``None`` if stdout retrieval failed), and
        ``stdout_retrieval_error`` (a typed ``ToolError`` if so).
        ``meta.truncated`` is true when more relevant failure events
        likely exist than were returned — either because the
        :data:`~mantis.tools._awx_events.MAX_RETURNED_FAILURE_EVENTS` cap
        was reached, or because ``event_inspection.inspection_capped`` is
        true (the full stream wasn't scanned, so completeness can't be
        claimed). These are deliberately two different signals; see
        ``docs/awx-job-failure.md``.
    """
    client = _client or _get_client()

    try:
        job = client.get_job(job_id, deadline=_deadline)
    except AWXError as exc:
        raise AWXError(
            f"Could not fetch AWX job {job_id}: {exc}",
            kind=exc.kind,
            status_code=exc.status_code,
            retry_after=exc.retry_after,
        ) from exc

    job_context = _job_context(job)

    selected_events, inspection, events_error, events_breaker_open = collect_failure_events(
        client, job_id, deadline=_deadline, reliability_report=_reliability_report
    )

    if events_breaker_open:
        # The events sub-read is what tripped the breaker open -- do not
        # immediately turn around and hammer the same integration again
        # for stdout within this same call (see #72's within-call
        # short-circuit fix for awx_recent_failed_jobs; the same
        # reasoning applies here).
        stdout_context = None
        stdout_error = ToolError(
            kind=ToolErrorKind.RETRIEVAL_ERROR,
            message=(
                "Stdout retrieval skipped: AWX has failed repeatedly during "
                "this call; short-circuiting further requests without "
                "retrieving stdout. See mantis.reliability.RunLocalBreaker."
            ),
        )
    else:
        role = "supporting" if selected_events else "fallback"
        stdout_context, stdout_error, _stdout_breaker_open = _fetch_stdout_context(
            client,
            job_id,
            role=role,
            deadline=_deadline,
            reliability_report=_reliability_report,
        )

    # Both signals are tracked precisely in collect_failure_events, not
    # inferred here from counts/lengths alone -- a job with exactly
    # MAX_RETURNED_FAILURE_EVENTS relevant failures and nothing more
    # must not be reported as truncated just because the count matches
    # the cap. See mantis.tools._awx_events.collect_failure_events.
    truncated = inspection["inspection_capped"] or inspection["more_failures_than_returned"]

    meta = QueryMeta(
        source_system="awx",
        truncated=truncated,
        # Per-event fields, not top-level result keys: category is pure
        # Mantis interpretation of AWX's event type; context is a
        # bounded/selected excerpt of AWX-reported text (same treatment
        # as awx_recent_failed_jobs's failure_excerpt/stdout_tail).
        derived_fields=["category", "context"],
    )

    return {
        "meta": meta.to_dict(),
        "job": job_context,
        "structured_failures": selected_events,
        "structured_failures_error": events_error.to_dict() if events_error else None,
        "event_inspection": inspection,
        "stdout_context": stdout_context,
        "stdout_retrieval_error": stdout_error.to_dict() if stdout_error else None,
    }


AWX_GET_JOB_FAILURE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "awx_get_job_failure",
        "description": (
            "Fetch structured failure evidence for one AWX job: "
            "deterministically selected job-event records (task_failure, "
            "network_reachability) with bounded per-event context — the "
            "preferred evidence source over raw stdout parsing. Falls "
            "back to a bounded stdout excerpt when no structured failure "
            "event was found (stdout_context.role='fallback'), or keeps "
            "stdout only as supporting context when one was "
            "(role='supporting'). event_inspection reports whether the "
            "full event stream was inspected or a hard cap stopped it "
            "early. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "integer",
                    "description": (
                        "The AWX job ID to inspect, e.g. an id returned "
                        "by awx_recent_failed_jobs."
                    ),
                }
            },
            "required": ["job_id"],
        },
    },
}


default_registry.register(
    Tool(
        name="awx_get_job_failure",
        schema=AWX_GET_JOB_FAILURE_SCHEMA,
        handler=awx_get_job_failure,
        category="awx",
        mutating=False,
        # Event stdout/messages/task names/host strings are external,
        # Mantis-uncontrolled evidence -- same untrusted-output treatment
        # as awx_recent_failed_jobs. See mantis.security and docs/security.md.
        contains_untrusted_text=True,
        description=(
            "Fetch structured AWX job-event failure evidence for one job, "
            "with bounded stdout as supporting/fallback context."
        ),
    )
)
