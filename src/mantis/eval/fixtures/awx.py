"""Fixture-backed AWX tool for evaluation scenarios.

:class:`FixtureAWXClient` duck-types ``mantis.integrations.awx.AWXClient``'s
public interface (``list_jobs``, ``get_job_stdout``) but returns canned
data instead of making HTTP calls. It's passed to the real
``mantis.tools.awx.awx_recent_failed_jobs`` via that function's
``_client`` override, so a scenario exercises the exact production
preprocessing/contract logic (stdout excerpt extraction, ``QueryMeta``,
truncation detection, ...) against fixture data — not a hand-faked
shortcut of it.
"""

from __future__ import annotations

from typing import Any

from mantis.agents.awx_troubleshooter import ALLOWED_TOOLS, SYSTEM_PROMPT
from mantis.eval.expectations import (
    ForbiddenClaim,
    MaxToolCalls,
    MustProduceFinalAnswer,
    RequiredEvidence,
    RequiredToolCall,
)
from mantis.eval.scenarios import Scenario, default_scenarios
from mantis.integrations.awx import JobListPage
from mantis.registry import Tool, ToolRegistry
from mantis.tools.awx import AWX_RECENT_FAILED_JOBS_SCHEMA, awx_recent_failed_jobs


class FixtureAWXClient:
    """A canned stand-in for ``AWXClient``, scoped to one scenario's data."""

    def __init__(
        self,
        jobs: list[dict[str, Any]],
        stdout_by_job_id: dict[int, str],
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
        return self._stdout_by_job_id[job_id]


def build_awx_recent_failed_jobs_tool(client: FixtureAWXClient) -> Tool:
    """Build a ``Tool`` for ``awx_recent_failed_jobs`` bound to fixture data.

    Uses the real schema and the real tool function — only the AWX client
    it talks to is swapped out.
    """

    def _fixture_handler(limit: int = 5) -> dict[str, Any]:
        return awx_recent_failed_jobs(limit=limit, _client=client)

    return Tool(
        name="awx_recent_failed_jobs",
        schema=AWX_RECENT_FAILED_JOBS_SCHEMA,
        handler=_fixture_handler,
        category="awx",
        mutating=False,
        description="Fixture-backed awx_recent_failed_jobs for evaluation scenarios.",
    )


# ---------------------------------------------------------------------------
# "No route to host" scenario fixture
# ---------------------------------------------------------------------------
# Reproduces the SSH-unreachable failure used as Mantis's running example
# throughout the README/docs, so this scenario's expected behavior (network
# reachability evidence, not an asserted firewall root cause) is exactly
# what a human reviewer would already recognize.

_NO_ROUTE_STDOUT = """\
PLAY [Deploy web servers] ****************************************************

TASK [Gathering Facts] ********************************************************
fatal: [host03]: UNREACHABLE! => {"changed": false, "msg": "ssh: connect to host host03 port 22: No route to host", "unreachable": true}

PLAY RECAP *********************************************************************
host03                     : ok=0    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0
"""

NO_ROUTE_JOB = {
    "id": 4231,
    "name": "deploy-webservers",
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
        "inventory": {"name": "production"},
        "project": {"name": "site-ops"},
        "job_template": {"name": "deploy-webservers"},
    },
}


def build_no_route_registry() -> ToolRegistry:
    """The fixture ``ToolRegistry`` for the ``awx-no-route`` scenario."""
    client = FixtureAWXClient(
        jobs=[NO_ROUTE_JOB],
        stdout_by_job_id={NO_ROUTE_JOB["id"]: _NO_ROUTE_STDOUT},
    )
    registry = ToolRegistry()
    registry.register(build_awx_recent_failed_jobs_tool(client))
    return registry


# Reuses the real AWX Troubleshooter's system prompt and runtime tuning
# (ALLOWED_TOOLS, tool_call_budget=1, temperature=0.1) rather than an
# eval-only prompt — this scenario qualifies models against exactly what
# production actually runs, per #13's model-qualification goal.
#
# expectations encode the "golden behavior" from the description below as
# deterministic, non-LLM-judged checks (#36): call the tool exactly once,
# cite the actual evidence (host03, a network-reachability
# classification, the fact that only one job exists), never overclaim an
# unproven specific root cause, and actually produce a final answer.
default_scenarios.register(
    Scenario(
        name="awx-no-route",
        version="1.1",
        description=(
            "A single failed AWX job whose stdout shows an SSH "
            '"No route to host" UNREACHABLE! failure. Golden behavior: '
            "identify this as a network reachability problem supported "
            "directly by the evidence, without asserting an unproven "
            "specific root cause (e.g. a firewall rule change)."
        ),
        prompt="Show me the last 5 failed AWX jobs and summarize them.",
        system_prompt=SYSTEM_PROMPT,
        agent_tools=list(ALLOWED_TOOLS),
        build_registry=build_no_route_registry,
        tool_call_budget=1,
        temperature=0.1,
        expectations=[
            RequiredToolCall(
                "awx_recent_failed_jobs",
                min_count=1,
                max_count=1,
                label="called AWX exactly once",
            ),
            RequiredEvidence("host03", label="cited host03"),
            RequiredEvidence(
                ["network reachability", "network issue", "reachability problem", "unreachable"],
                label="classified the evidence as a network reachability problem",
            ),
            RequiredEvidence(
                ["1 failed job", "one failed job", "only 1", "single failed job"],
                label="correctly reported only one failed job exists",
            ),
            ForbiddenClaim(
                [
                    "firewall caused",
                    "due to a firewall",
                    "firewall rule",
                    "firewall issue",
                    "firewall misconfiguration",
                ],
                label="did not assert firewall was the root cause",
            ),
            MaxToolCalls(1, label="stopped after receiving sufficient evidence"),
            MustProduceFinalAnswer(),
        ],
    )
)
