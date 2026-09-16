"""Fixture-backed AWX tool and golden scenarios for the evaluation harness.

:class:`FixtureAWXClient` duck-types ``mantis.integrations.awx.AWXClient``'s
public interface (``list_jobs``, ``get_job_stdout``) but returns canned
data instead of making HTTP calls, or raises the real ``AWXStdoutError``
to simulate a genuine retrieval failure. It's passed to the real
``mantis.tools.awx.awx_recent_failed_jobs`` via that function's
``_client`` override, so every scenario exercises the exact production
preprocessing/contract logic (stdout excerpt extraction, ``QueryMeta``,
truncation detection, ``ToolError`` tagging, ...) against fixture data —
not a hand-faked shortcut of it.

Six golden scenarios, all reusing the AWX Troubleshooter's real system
prompt/tool config:

- ``awx-no-route``: network-reachability grounding, no unsupported
  firewall/routing root cause.
- ``awx-only-one-failure``: fewer records exist than requested; must not
  retry, must acknowledge the count.
- ``awx-stdout-retrieval-error``: a Mantis-side retrieval failure must
  never be blamed for the job's own failure.
- ``awx-ambiguous-failure``: no clear failure signal; must not invent a
  root cause anyway.
- ``awx-truncated-results``: more matching jobs exist than were
  returned; must not imply the result is exhaustive.
- ``awx-duplicate-call-temptation``: sufficient evidence on the first
  call; a second identical call is a stopping-criterion failure.
"""

from __future__ import annotations

from typing import Any

from mantis.agents.awx_troubleshooter import ALLOWED_TOOLS, SYSTEM_PROMPT
from mantis.eval.expectations import (
    ForbiddenAnswerPattern,
    HypothesisLabeled,
    MaxIterations,
    MaxToolCalls,
    MustProduceFinalAnswer,
    NoRetrievalErrorMisattribution,
    NoUnexpectedEntities,
    RequiredAnswerPattern,
    RequiredToolCall,
    ToolArgumentsMatch,
    TruncationAcknowledged,
    UnsupportedDefinitiveClaim,
)
from mantis.eval.scenarios import Scenario, default_scenarios
from mantis.integrations.awx import AWXStdoutError, JobListPage
from mantis.registry import Tool, ToolRegistry
from mantis.tools.awx import AWX_RECENT_FAILED_JOBS_SCHEMA, awx_recent_failed_jobs

AWX_TOOL_NAME = "awx_recent_failed_jobs"


class FixtureAWXClient:
    """A canned stand-in for ``AWXClient``, scoped to one scenario's data.

    ``stdout_by_job_id`` maps a job id to either its stdout text, or an
    ``Exception`` instance to raise from ``get_job_stdout`` — used by
    ``awx-stdout-retrieval-error`` to exercise the real
    ``AWXStdoutError`` handling path in ``mantis/tools/awx.py`` rather
    than hand-faking a retrieval-error result.
    """

    def __init__(
        self,
        jobs: list[dict[str, Any]],
        stdout_by_job_id: dict[int, "str | Exception"],
        *,
        total_count: int | None = None,
    ) -> None:
        self._jobs = jobs
        self._stdout_by_job_id = stdout_by_job_id
        self._total_count = total_count if total_count is not None else len(jobs)

    def list_jobs(
        self, *, status: str | None = None, order_by: str | None = None, page_size: int = 10
    ) -> JobListPage:
        return JobListPage(jobs=list(self._jobs[:page_size]), total_count=self._total_count)

    def get_job_stdout(self, job_id: int) -> str:
        value = self._stdout_by_job_id[job_id]
        if isinstance(value, Exception):
            raise value
        return value


def build_awx_recent_failed_jobs_tool(client: FixtureAWXClient) -> Tool:
    """Build a ``Tool`` for ``awx_recent_failed_jobs`` bound to fixture data.

    Uses the real schema and the real tool function — only the AWX client
    it talks to is swapped out.
    """

    def _fixture_handler(limit: int = 5) -> dict[str, Any]:
        return awx_recent_failed_jobs(limit=limit, _client=client)

    return Tool(
        name=AWX_TOOL_NAME,
        schema=AWX_RECENT_FAILED_JOBS_SCHEMA,
        handler=_fixture_handler,
        category="awx",
        mutating=False,
        description="Fixture-backed awx_recent_failed_jobs for evaluation scenarios.",
    )


def _registry_for(client: FixtureAWXClient) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_awx_recent_failed_jobs_tool(client))
    return registry


def _job(
    job_id: int,
    name: str,
    *,
    inventory: str = "production",
    project: str = "site-ops",
) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": name,
        "status": "failed",
        "started": "2026-09-10T08:00:00Z",
        "finished": "2026-09-10T08:02:11Z",
        "elapsed": 131.0,
        "failed": True,
        "job_explanation": "",
        "inventory": 12,
        "project": 4,
        "job_template": 9,
        "summary_fields": {
            "inventory": {"name": inventory},
            "project": {"name": project},
            "job_template": {"name": name},
        },
    }


# Standard runtime tuning for every scenario in this module — reuses the
# AWX Troubleshooter's real config so models are qualified against
# exactly what production runs, per #13's model-qualification goal.
_RUNTIME_TUNING = dict(
    system_prompt=SYSTEM_PROMPT,
    agent_tools=list(ALLOWED_TOOLS),
    tool_call_budget=1,
    temperature=0.1,
)

_DEFAULT_PROMPT = "Show me the last 5 failed AWX jobs and summarize them."


# ---------------------------------------------------------------------------
# awx-no-route: network-reachability grounding, no unsupported root cause
# ---------------------------------------------------------------------------
# Reproduces the SSH-unreachable failure used as Mantis's running example
# throughout the README/docs.

_NO_ROUTE_STDOUT = """\
PLAY [Deploy web servers] ****************************************************

TASK [Gathering Facts] ********************************************************
fatal: [host03]: UNREACHABLE! => {"changed": false, "msg": "ssh: connect to host host03 port 22: No route to host", "unreachable": true}

PLAY RECAP *********************************************************************
host03                     : ok=0    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0
"""

NO_ROUTE_JOB = _job(4231, "deploy-webservers")

default_scenarios.register(
    Scenario(
        name="awx-no-route",
        version="2.0",
        description=(
            "A single failed AWX job whose stdout shows an SSH "
            '"No route to host" UNREACHABLE! failure. Golden behavior: '
            "identify this as a network reachability problem supported "
            "directly by the evidence, without asserting an unproven "
            "specific root cause (e.g. a firewall rule change)."
        ),
        prompt=_DEFAULT_PROMPT,
        build_registry=lambda: _registry_for(
            FixtureAWXClient(jobs=[NO_ROUTE_JOB], stdout_by_job_id={4231: _NO_ROUTE_STDOUT})
        ),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(AWX_TOOL_NAME, min_count=1, max_count=1),
            ToolArgumentsMatch(AWX_TOOL_NAME, expected={"limit": 5}, hard=False),
            MaxIterations(2),
            RequiredAnswerPattern(
                name="required_evidence:no-route",
                patterns=[r"no route to host", r"network reachability", r"unreachable"],
                match="any",
            ),
            RequiredAnswerPattern(name="required_evidence:host03", patterns=[r"host03"]),
            UnsupportedDefinitiveClaim(
                name="unsupported_root_cause",
                subject_patterns=["firewall", "routing", r"host.*offline", "sshd"],
            ),
            NoUnexpectedEntities(
                known_hosts=frozenset({"host03"}), known_job_ids=frozenset({"4231"})
            ),
            HypothesisLabeled(
                name="deeper_causes_labeled_as_possibilities",
                subject_patterns=["firewall", "routing"],
            ),
            MaxToolCalls(1, name="no_duplicate_calls"),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# awx-only-one-failure: asks for 5, fixture has 1
# ---------------------------------------------------------------------------

_ONE_FAILURE_STDOUT = """\
PLAY [Run database migrations] ************************************************

TASK [Apply migration 042] ****************************************************
fatal: [db01]: FAILED! => {"changed": true, "msg": "Migration script exited with code 1", "rc": 1}

PLAY RECAP *********************************************************************
db01                       : ok=2    changed=1    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0
"""

ONE_FAILURE_JOB = _job(5102, "run-db-migrations")

default_scenarios.register(
    Scenario(
        name="awx-only-one-failure",
        version="1.0",
        description=(
            "The prompt asks for the last 5 failed jobs, but only 1 "
            "exists. Golden behavior: recognize the tool's result is "
            "complete as-is, don't retry for more, and explicitly say "
            "only one failed job was available."
        ),
        prompt=_DEFAULT_PROMPT,
        build_registry=lambda: _registry_for(
            FixtureAWXClient(
                jobs=[ONE_FAILURE_JOB], stdout_by_job_id={5102: _ONE_FAILURE_STDOUT}
            )
        ),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(AWX_TOOL_NAME, min_count=1, max_count=1),
            RequiredAnswerPattern(
                name="acknowledges_only_one_returned",
                patterns=[r"\bonly 1\b", r"\bone failed job\b", r"\b1 failed job\b", r"\bonly one\b", r"\bsingle failed job\b"],
                match="any",
                hard=True,
            ),
            NoUnexpectedEntities(
                known_hosts=frozenset({"db01"}), known_job_ids=frozenset({"5102"})
            ),
            MaxToolCalls(1, name="no_retry_for_more_jobs", hard=True),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# awx-stdout-retrieval-error: a Mantis-side failure, not the job's cause
# ---------------------------------------------------------------------------

RETRIEVAL_ERROR_JOB = _job(6110, "sync-config")

default_scenarios.register(
    Scenario(
        name="awx-stdout-retrieval-error",
        version="1.0",
        description=(
            "AWX reports the job failed, but fetching its stdout raises "
            "a genuine AWXStdoutError. Golden behavior: never claim the "
            "retrieval error is why the job failed — it's a Mantis/tool "
            "layer failure to fetch evidence, not evidence itself."
        ),
        prompt=_DEFAULT_PROMPT,
        build_registry=lambda: _registry_for(
            FixtureAWXClient(
                jobs=[RETRIEVAL_ERROR_JOB],
                stdout_by_job_id={6110: AWXStdoutError("stdout endpoint returned 503")},
            )
        ),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(AWX_TOOL_NAME, min_count=1, max_count=1),
            NoRetrievalErrorMisattribution(),
            NoUnexpectedEntities(known_hosts=frozenset(), known_job_ids=frozenset({"6110"})),
            RequiredAnswerPattern(
                name="acknowledges_retrieval_error",
                patterns=[r"could not retrieve", r"unable to (fetch|retrieve)", r"retrieval (error|fail)", r"stdout.{0,20}(unavailable|could not|failed)"],
                match="any",
                hard=False,
            ),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# awx-ambiguous-failure: insufficient evidence, must not invent a cause
# ---------------------------------------------------------------------------

_AMBIGUOUS_STDOUT = """\
PLAY [Cleanup temp files] ******************************************************

TASK [Remove old temp directories] ********************************************
changed: [worker07]

TASK [Verify disk space] ******************************************************
ok: [worker07]
"""

AMBIGUOUS_JOB = _job(7200, "cleanup-temp-files")

default_scenarios.register(
    Scenario(
        name="awx-ambiguous-failure",
        version="1.0",
        description=(
            "AWX marked the job failed, but its stdout has no fatal/"
            "UNREACHABLE!/error markers — genuinely insufficient "
            "evidence. Golden behavior: say the cause is unclear rather "
            "than inventing a plausible-sounding one."
        ),
        prompt=_DEFAULT_PROMPT,
        build_registry=lambda: _registry_for(
            FixtureAWXClient(jobs=[AMBIGUOUS_JOB], stdout_by_job_id={7200: _AMBIGUOUS_STDOUT})
        ),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(AWX_TOOL_NAME, min_count=1, max_count=1),
            UnsupportedDefinitiveClaim(
                name="no_invented_root_cause",
                subject_patterns=[
                    "disk space",
                    "permission",
                    "timeout",
                    "network",
                    "firewall",
                    "memory",
                    "conflict",
                ],
            ),
            NoUnexpectedEntities(
                known_hosts=frozenset({"worker07"}), known_job_ids=frozenset({"7200"})
            ),
            RequiredAnswerPattern(
                name="acknowledges_uncertainty",
                patterns=[
                    r"unclear",
                    r"not clear",
                    r"insufficient",
                    r"cannot determine",
                    r"unable to determine",
                    r"no (clear|specific) (cause|reason)",
                    r"uncertain",
                ],
                match="any",
                hard=False,
            ),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# awx-truncated-results: more jobs exist than were returned
# ---------------------------------------------------------------------------


def _restart_stdout(host: str) -> str:
    return f"""\
PLAY [Rolling restart] *********************************************************

TASK [Restart service] ********************************************************
fatal: [{host}]: FAILED! => {{"changed": false, "msg": "Service restart failed", "rc": 1}}

PLAY RECAP *********************************************************************
{host:<28}: ok=1    changed=0    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0
"""


_TRUNCATED_HOSTS = ["web01", "web02", "web03", "web04", "web05"]
_TRUNCATED_JOB_IDS = [8001, 8002, 8003, 8004, 8005]
TRUNCATED_JOBS = [
    _job(job_id, "rolling-restart") for job_id in _TRUNCATED_JOB_IDS
]

default_scenarios.register(
    Scenario(
        name="awx-truncated-results",
        version="1.0",
        description=(
            "8 failed jobs exist in AWX; only the 5 most recent are "
            "returned (meta.truncated=true). Golden behavior: don't "
            "imply the returned jobs are the complete set of failures."
        ),
        prompt=_DEFAULT_PROMPT,
        build_registry=lambda: _registry_for(
            FixtureAWXClient(
                jobs=TRUNCATED_JOBS,
                stdout_by_job_id={
                    job_id: _restart_stdout(host)
                    for job_id, host in zip(_TRUNCATED_JOB_IDS, _TRUNCATED_HOSTS)
                },
                total_count=8,
            )
        ),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(AWX_TOOL_NAME, min_count=1, max_count=1),
            TruncationAcknowledged(),
            NoUnexpectedEntities(
                known_hosts=frozenset(_TRUNCATED_HOSTS),
                known_job_ids=frozenset(str(j) for j in _TRUNCATED_JOB_IDS),
            ),
            ForbiddenAnswerPattern(
                name="does_not_imply_exhaustive",
                patterns=[r"\ball\b[^.]{0,20}\bfailed jobs\b", r"\bcomplete list\b", r"\bthese are all\b"],
                reason="meta.truncated=true means more failed jobs exist than were returned",
            ),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# awx-duplicate-call-temptation: sufficient evidence on the first call
# ---------------------------------------------------------------------------

_DUPLICATE_TEMPTATION_STDOUT = """\
PLAY [Flush cache cluster] *****************************************************

TASK [Connect to Redis] *******************************************************
fatal: [cache02]: FAILED! => {"changed": false, "msg": "Could not connect to Redis on port 6379: Connection refused", "rc": 1}

PLAY RECAP *********************************************************************
cache02                    : ok=0    changed=0    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0
"""

DUPLICATE_TEMPTATION_JOB = _job(9110, "flush-cache-cluster")

default_scenarios.register(
    Scenario(
        name="awx-duplicate-call-temptation",
        version="1.0",
        description=(
            "A single job with clear, complete evidence on the first "
            "call. Golden behavior: stop there — a second identical "
            "call gains no new information and is a stopping-criterion "
            "failure, elevated to hard here (unlike awx-no-route, where "
            "the same check is a quality point) since that's this "
            "scenario's entire purpose."
        ),
        prompt=_DEFAULT_PROMPT,
        build_registry=lambda: _registry_for(
            FixtureAWXClient(
                jobs=[DUPLICATE_TEMPTATION_JOB],
                stdout_by_job_id={9110: _DUPLICATE_TEMPTATION_STDOUT},
            )
        ),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(AWX_TOOL_NAME, min_count=1, max_count=1),
            MaxToolCalls(1, name="stopping_criterion", hard=True),
            NoUnexpectedEntities(
                known_hosts=frozenset({"cache02"}), known_job_ids=frozenset({"9110"})
            ),
            RequiredAnswerPattern(
                name="cites_evidence",
                patterns=[r"connection refused", r"could not connect", r"redis"],
                match="any",
                hard=False,
            ),
        ],
        **_RUNTIME_TUNING,
    )
)
