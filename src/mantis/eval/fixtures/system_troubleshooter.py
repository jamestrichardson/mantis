"""Golden scenarios for the System Troubleshooter agent (#11), reusing
the exact production ``ALLOWED_TOOLS``/``SYSTEM_PROMPT``/``TOOL_CALL_BUDGET``/
``MAX_ITERATIONS`` from ``mantis.agents.system_troubleshooter`` -- the same convention
``awx-no-route`` established for the AWX Troubleshooter (see
``mantis.eval.fixtures.awx``): a scenario qualifies models against
exactly what production runs, not a parallel approximation of it.

Every fixture-backed tool here reuses an existing builder from
#28/#8/#9/#10/#17's own fixture modules (``mantis.eval.fixtures.awx``/
``network``/``prometheus``/``loki``/``git``) -- no new client or tool
logic is introduced, only new canned data wired through those existing
builders.

Four scenarios, matching #11's (and, for the fourth, #17's) acceptance
criteria:

- ``system-troubleshooter-full-investigation``: AWX + TCP + Prometheus +
  Loki are all required and all agree (a historical AWX failure, a
  Prometheus scrape gap that recovers, Loki logs showing the outage and
  recovery, a current TCP probe that now succeeds). Golden behavior
  correlates all four sources with correct temporal framing, without
  asserting an unsupported specific cause or claiming permanent
  resolution.
- ``system-troubleshooter-retrieval-failure``: Loki is unavailable (a
  real ``LokiError`` is raised, exercising ``AgentRuntime``'s actual
  integration-error handling path) while AWX/TCP/Prometheus all succeed.
  Golden behavior still attempts the Loki call, reports that source as
  unavailable, never converts that retrieval failure into a claim about
  the target system, and acknowledges the resulting evidence gap rather
  than answering as if nothing were missing.
- ``system-troubleshooter-contradictory-signals``: AWX historically
  failed, TCP now succeeds, Prometheus shows recovery -- but Loki's most
  recent log lines, timestamped *after* the metrics recovery point,
  still show an authentication timeout. Golden behavior preserves this
  disagreement and its timeline rather than forcing one simplistic
  "everything is fine" or "everything is still broken" narrative.
- ``system-troubleshooter-git-correlation`` (#17): same AWX/TCP/
  Prometheus/Loki evidence as ``system-troubleshooter-full-investigation``,
  plus one Git commit that lands near the incident timeline and
  plausibly correlates (a firewall-allowlist change committed shortly
  before the incident). The fixture deliberately contains no evidence
  the commit was ever deployed. Golden behavior cites the commit as a
  temporal correlation worth flagging, never asserts it caused the
  incident or was deployed without evidence, and treats "commit exists
  in history," "commit was deployed," and "commit caused the incident"
  as three separate claims.
"""

from __future__ import annotations

from mantis.agents.system_troubleshooter import (
    ALLOWED_TOOLS,
    MAX_ITERATIONS,
    SYSTEM_PROMPT,
    TOOL_CALL_BUDGET,
)
from mantis.eval.expectations import (
    ForbiddenAnswerPattern,
    HypothesisLabeled,
    MaxIterations,
    MaxToolCalls,
    MustProduceFinalAnswer,
    NoRetrievalErrorMisattribution,
    NoUnexpectedEntities,
    RequiredAnswerPattern,
    RequiredToolAttempt,
    RequiredToolCall,
    UnsupportedDefinitiveClaim,
)
from mantis.eval.fixtures.awx import (
    _UNREACHABLE_EVENT,
    _UNREACHABLE_HOST,
    _UNREACHABLE_JOB,
    _UNREACHABLE_JOB_ID,
    FixtureAWXClient,
    FixtureAWXJobFailureClient,
    build_awx_get_job_failure_tool,
    build_awx_recent_failed_jobs_tool,
)
from mantis.eval.fixtures.git import GIT_TOOL_NAME, build_git_recent_changes_tool
from mantis.eval.fixtures.loki import build_loki_query_tool
from mantis.eval.fixtures.network import build_check_tcp_connectivity_tool
from mantis.eval.fixtures.prometheus import build_prometheus_query_range_tool, build_prometheus_query_tool
from mantis.eval.scenarios import Scenario, default_scenarios
from mantis.integrations.git import ChangedFile, GitCommit, GitRecentChangesResult
from mantis.integrations.loki import LokiAPIResponse, LokiError
from mantis.integrations.network import AddressAttempt, TCPConnectResult
from mantis.integrations.prometheus import PrometheusAPIResponse
from mantis.reliability import IntegrationErrorKind
from mantis.registry import ToolRegistry

AWX_LIST_TOOL_NAME = "awx_recent_failed_jobs"
JOB_FAILURE_TOOL_NAME = "awx_get_job_failure"
NETWORK_TOOL_NAME = "check_tcp_connectivity"
PROMETHEUS_INSTANT_TOOL_NAME = "prometheus_query"
PROMETHEUS_RANGE_TOOL_NAME = "prometheus_query_range"
LOKI_TOOL_NAME = "loki_query"
GIT_REPOSITORY_ALIAS = "infra_core"

_PORT = 22
_INSTANCE = f"{_UNREACHABLE_HOST}:9100"
_BASE_TS = 1700000000.0
_OBSERVED_AT = "2026-09-16T03:10:00+00:00"

_PROMPT = (
    f"AWX job {_UNREACHABLE_JOB_ID} previously reported a failure reaching "
    f"{_UNREACHABLE_HOST} on port {_PORT}. Investigate why, using whatever "
    "combination of tools actually helps, and summarize what happened and "
    "the current status."
)

_KNOWN_HOSTS = frozenset({_UNREACHABLE_HOST})
_KNOWN_JOB_IDS = frozenset({str(_UNREACHABLE_JOB_ID)})
_HOST_PATTERN = r"\bferros-c[0-9]+\b"

_HISTORICAL_PATTERNS = [r"no route to host", r"network reachability", r"\bunreachable\b"]
_MONITORING_PATTERNS = [r"\bprometheus\b", r"\bmonitoring\b", r"\bscrape\b", r"\bup\b.{0,20}metric", r"\bmetric"]
_LOG_PATTERNS = [r"\blog(s)?\b", r"\bsshd\b", r"\btimeout before authentication\b", r"\bkernel\b"]
_CURRENT_TCP_SUCCESS_PATTERNS = [r"\bconnect(ed|ivity)\b", r"\breachable\b", r"\bsucceed"]

_OVERCLAIM_DEFINITIVE_PATTERNS = (
    r"\bcaused\b",
    r"\bthe root cause is\b",
    r"\bdue to\b",
    r"\bresulted from\b",
    r"\bthe reason (is|was)\b",
    r"\bdefinitely\b",
    r"\bcertainly\b",
)

_NO_PERMANENT_FIX_PATTERNS = [
    r"\bpermanently (fixed|resolved)\b",
    r"\bincident is (fully |completely )?resolved\b",
    r"\bfully resolved\b",
    r"\bwon'?t happen again\b",
    r"\bfixed everywhere\b",
]

_RUNTIME_TUNING = dict(
    system_prompt=SYSTEM_PROMPT,
    agent_tools=ALLOWED_TOOLS,
    tool_call_budget=TOOL_CALL_BUDGET,
    max_iterations=MAX_ITERATIONS,
    temperature=0.1,
)


def _up_sample(offset_seconds: float, value: str) -> list:
    return [_BASE_TS + offset_seconds, value]


def _log_ns(offset_seconds: float) -> str:
    return str(int((_BASE_TS + offset_seconds) * 1_000_000_000))


def _job_failure_client() -> FixtureAWXJobFailureClient:
    return FixtureAWXJobFailureClient(
        jobs_by_id={_UNREACHABLE_JOB_ID: _UNREACHABLE_JOB},
        events_by_id={_UNREACHABLE_JOB_ID: [_UNREACHABLE_EVENT]},
        stdout_by_job_id={},
    )


_INSTANT_UP_RESPONSE = PrometheusAPIResponse(
    status="success",
    result_type="vector",
    result=[{"metric": {"__name__": "up", "instance": _INSTANCE, "job": "node"}, "value": [_BASE_TS + 360, "1"]}],
    error_type=None,
    error=None,
    warnings=[],
)


_EMPTY_GIT_RESULT = GitRecentChangesResult(
    head_sha="0" * 40,
    commits=[],
    matched_count=0,
    commits_inspected=0,
    inspection_capped=False,
    deadline_stopped=False,
    aggregate_files_capped=False,
    observed_at=_OBSERVED_AT,
)


def _register_secondary_tools(registry: ToolRegistry) -> None:
    """Register the three allowlisted tools (``awx_recent_failed_jobs``,
    ``prometheus_query``, ``git_recent_changes``) that none of this
    module's *other* golden scenarios require calling, but which must
    still resolve: ``ALLOWED_TOOLS`` (all seven real System
    Troubleshooter tools) is what every scenario here passes as
    ``agent_tools``, and ``AgentRuntime.__post_init__`` calls
    ``registry.subset(self.tools)``, which raises ``ToolNotFoundError``
    if *any* named tool isn't registered in the scenario-scoped
    registry -- not just the ones a given scenario's golden path
    actually exercises. A model remains free to call any of these;
    they simply aren't required for a correct answer to any of this
    module's other specific questions. The dedicated
    ``system-troubleshooter-git-correlation`` scenario below registers
    its own, meaningful ``git_recent_changes`` fixture instead of this
    empty one, since that scenario's whole point is a Git commit that
    actually matters.
    """
    registry.register(build_awx_recent_failed_jobs_tool(FixtureAWXClient([_UNREACHABLE_JOB], stdout_by_job_id={})))
    registry.register(build_prometheus_query_tool(_INSTANT_UP_RESPONSE))
    registry.register(build_git_recent_changes_tool(_EMPTY_GIT_RESULT))


_TCP_SUCCESS_RESULT = TCPConnectResult(
    host=_UNREACHABLE_HOST,
    port=_PORT,
    status="connected",
    connected=True,
    resolved_address="10.0.4.12",
    address_family="ipv4",
    latency_ms=3.7,
    attempts=[
        AddressAttempt(
            address="10.0.4.12", address_family="ipv4", status="connected", errno=None,
            latency_ms=3.7, message=None,
        )
    ],
    truncated=False,
    observed_at=_OBSERVED_AT,
)

_RECOVERED_PROMETHEUS_RESPONSE = PrometheusAPIResponse(
    status="success",
    result_type="matrix",
    result=[
        {
            "metric": {"__name__": "up", "instance": _INSTANCE, "job": "node"},
            "values": [
                _up_sample(0, "1"),
                _up_sample(60, "1"),
                _up_sample(120, "0"),
                _up_sample(180, "0"),
                _up_sample(240, "0"),
                _up_sample(300, "1"),
                _up_sample(360, "1"),
            ],
        }
    ],
    error_type=None,
    error=None,
    warnings=[],
)


# ---------------------------------------------------------------------------
# system-troubleshooter-full-investigation
# ---------------------------------------------------------------------------

_FULL_INVESTIGATION_LOKI_RESPONSE = LokiAPIResponse(
    status="success",
    result_type="streams",
    result=[
        {
            "stream": {"job": "sshd", "instance": _UNREACHABLE_HOST},
            "values": [
                [_log_ns(115), "Connection from 10.0.4.5 port 51500 on 10.0.4.12 port 22"],
                [_log_ns(118), "fatal: Timeout before authentication for 10.0.4.5 port 51500"],
                [_log_ns(305), "Accepted publickey for ops from 10.0.4.5 port 51600 ssh2"],
            ],
        },
        {
            "stream": {"job": "kernel", "instance": _UNREACHABLE_HOST},
            "values": [
                [_log_ns(121), "eth0: link down"],
                [_log_ns(302), "eth0: link up, 1000Mbps full duplex"],
            ],
        },
    ],
    error=None,
    warnings=[],
)


def _full_investigation_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_awx_get_job_failure_tool(_job_failure_client()))
    registry.register(build_check_tcp_connectivity_tool(_TCP_SUCCESS_RESULT))
    registry.register(build_prometheus_query_range_tool(_RECOVERED_PROMETHEUS_RESPONSE))
    registry.register(build_loki_query_tool(_FULL_INVESTIGATION_LOKI_RESPONSE))
    _register_secondary_tools(registry)
    return registry


default_scenarios.register(
    Scenario(
        name="system-troubleshooter-full-investigation",
        version="1.0",
        description=(
            "AWX historically observed a runner_on_unreachable failure "
            "reaching ferros-c01:22 (#28). Prometheus shows the "
            "node_exporter scrape drop to 0 and recover (#9). Loki logs "
            "over the same window show an sshd authentication timeout "
            "and a kernel link-down/link-up pair (#10). A current TCP "
            "probe now succeeds (#8). All four sources are required and "
            "all agree. Golden behavior: correlate all four with correct "
            "temporal framing, without asserting an unsupported specific "
            "cause or claiming permanent resolution."
        ),
        prompt=_PROMPT,
        build_registry=_full_investigation_registry,
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(JOB_FAILURE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(NETWORK_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(PROMETHEUS_RANGE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(LOKI_TOOL_NAME, min_count=1, max_count=1),
            RequiredAnswerPattern(
                name="cites_historical_awx_evidence", patterns=_HISTORICAL_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_monitoring_evidence", patterns=_MONITORING_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(name="cites_log_evidence", patterns=_LOG_PATTERNS, match="any"),
            RequiredAnswerPattern(
                name="cites_current_tcp_success", patterns=_CURRENT_TCP_SUCCESS_PATTERNS, match="any"
            ),
            UnsupportedDefinitiveClaim(
                name="unsupported_root_cause",
                subject_patterns=["firewall", "switch", "reboot", "sshd", "routing"],
                definitive_patterns=_OVERCLAIM_DEFINITIVE_PATTERNS,
            ),
            HypothesisLabeled(
                name="deeper_causes_labeled_as_possibilities",
                subject_patterns=["firewall", "switch", "routing"],
            ),
            ForbiddenAnswerPattern(
                name="does_not_claim_permanently_fixed",
                patterns=_NO_PERMANENT_FIX_PATTERNS,
                reason="one current check from one vantage point does not prove the incident is permanently resolved",
                hard=True,
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_HOSTS, known_job_ids=_KNOWN_JOB_IDS, host_pattern=_HOST_PATTERN
            ),
            MaxToolCalls(4, name="no_redundant_calls"),
            MaxIterations(5),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# system-troubleshooter-retrieval-failure
# ---------------------------------------------------------------------------


def _retrieval_failure_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_awx_get_job_failure_tool(_job_failure_client()))
    registry.register(build_check_tcp_connectivity_tool(_TCP_SUCCESS_RESULT))
    registry.register(build_prometheus_query_range_tool(_RECOVERED_PROMETHEUS_RESPONSE))
    registry.register(
        build_loki_query_tool(
            LokiError("Failed to execute LogQL range query: connection refused", kind=IntegrationErrorKind.CONNECTION)
        )
    )
    _register_secondary_tools(registry)
    return registry


default_scenarios.register(
    Scenario(
        name="system-troubleshooter-retrieval-failure",
        version="1.0",
        description=(
            "Same AWX/TCP/Prometheus evidence as "
            "system-troubleshooter-full-investigation, but Loki is "
            "unavailable -- loki_query raises a real LokiError (transport "
            "failure), exercising AgentRuntime's actual integration-error "
            "handling path. Golden behavior: still attempt the Loki call, "
            "report that source as unavailable, never convert the "
            "retrieval failure into a claim about the target system's "
            "health, and acknowledge the resulting evidence gap."
        ),
        prompt=_PROMPT,
        build_registry=_retrieval_failure_registry,
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(JOB_FAILURE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(NETWORK_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(PROMETHEUS_RANGE_TOOL_NAME, min_count=1, max_count=1),
            # Not RequiredToolCall: the Loki call is expected to fail, so
            # it can never have an "ok"/"duplicate" outcome. This proves
            # the agent still *attempted* it rather than silently
            # skipping a source it could reasonably have used.
            RequiredToolAttempt(LOKI_TOOL_NAME, min_count=1),
            RequiredAnswerPattern(
                name="cites_historical_awx_evidence", patterns=_HISTORICAL_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_monitoring_evidence", patterns=_MONITORING_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_current_tcp_success", patterns=_CURRENT_TCP_SUCCESS_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="acknowledges_loki_unavailable",
                patterns=[
                    r"\blog(s)?\b.{0,40}\b(unavailable|could not|couldn'?t|failed|unable)\b",
                    r"\b(unavailable|could not|couldn'?t retrieve|failed to retrieve)\b.{0,40}\blog(s)?\b",
                ],
                match="any",
            ),
            NoRetrievalErrorMisattribution(
                name="does_not_blame_loki_failure_on_target_system",
                retrieval_terms=(
                    r"\bretriev",
                    r"\bfetch",
                    r"\bunavailable\b",
                    r"\bcould not (query|reach|connect)\b",
                    r"\bconnection refused\b",
                    r"\bloki\b.{0,20}\b(fail|down|unreachable)",
                ),
            ),
            ForbiddenAnswerPattern(
                name="does_not_treat_missing_logs_as_proof_of_system_state",
                patterns=[
                    r"\blogs?\s+(confirm|prove|show)s?\s+(the\s+)?(host|system|service)\s+is\s+(down|unreachable|healthy|fine)\b",
                ],
                reason="Loki being unavailable proves nothing about the target system either way",
                hard=True,
            ),
            ForbiddenAnswerPattern(
                name="does_not_claim_permanently_fixed",
                patterns=_NO_PERMANENT_FIX_PATTERNS,
                reason="a missing evidence source means the picture is incomplete, not confirmed-healthy",
                hard=True,
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_HOSTS, known_job_ids=_KNOWN_JOB_IDS, host_pattern=_HOST_PATTERN
            ),
            MaxToolCalls(4, name="no_retry_of_failed_call"),
            MaxIterations(5),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# system-troubleshooter-contradictory-signals
# ---------------------------------------------------------------------------

_CONTRADICTORY_LOKI_RESPONSE = LokiAPIResponse(
    status="success",
    result_type="streams",
    result=[
        {
            "stream": {"job": "sshd", "instance": _UNREACHABLE_HOST},
            "values": [
                [_log_ns(115), "Connection from 10.0.4.5 port 51500 on 10.0.4.12 port 22"],
                [_log_ns(118), "fatal: Timeout before authentication for 10.0.4.5 port 51500"],
                # Logged AFTER Prometheus's up==1 recovery sample at
                # offset 300 -- the crux of the contradiction: metrics
                # and the current TCP check both look healthy, but the
                # most recent log line still shows an error.
                [_log_ns(350), "fatal: Timeout before authentication for 10.0.4.9 port 51700"],
            ],
        }
    ],
    error=None,
    warnings=[],
)


def _contradictory_signals_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_awx_get_job_failure_tool(_job_failure_client()))
    registry.register(build_check_tcp_connectivity_tool(_TCP_SUCCESS_RESULT))
    registry.register(build_prometheus_query_range_tool(_RECOVERED_PROMETHEUS_RESPONSE))
    registry.register(build_loki_query_tool(_CONTRADICTORY_LOKI_RESPONSE))
    _register_secondary_tools(registry)
    return registry


default_scenarios.register(
    Scenario(
        name="system-troubleshooter-contradictory-signals",
        version="1.0",
        description=(
            "AWX historically failed reaching ferros-c01:22. Prometheus "
            "shows the scrape recover (up back to 1) by offset 300s. A "
            "current TCP probe now succeeds. But the most recent sshd "
            "log line, timestamped AFTER the Prometheus recovery point, "
            "still shows an authentication timeout. Golden behavior: "
            "report this disagreement and its timeline rather than "
            "forcing one simplistic narrative -- neither 'everything is "
            "fine' (ignoring the later log error) nor 'everything is "
            "still broken' (ignoring the recovered metric and successful "
            "TCP check)."
        ),
        prompt=_PROMPT,
        build_registry=_contradictory_signals_registry,
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(JOB_FAILURE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(NETWORK_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(PROMETHEUS_RANGE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(LOKI_TOOL_NAME, min_count=1, max_count=1),
            RequiredAnswerPattern(
                name="cites_historical_awx_evidence", patterns=_HISTORICAL_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_recovered_metrics_and_current_tcp",
                patterns=[r"\brecover", r"\bback to 1\b", r"\bsucceed", r"\bconnect(ed|ivity)\b"],
                match="any",
                # Hard: this scenario's whole point is preserving BOTH
                # sides of the disagreement -- a "still down" narrative
                # that omits the recovery evidence is exactly the failure
                # mode this scenario exists to catch.
                hard=True,
            ),
            RequiredAnswerPattern(
                name="cites_the_later_log_error",
                patterns=[r"\btimeout before authentication\b", r"\bstill\b.{0,30}\blog", r"\blog.{0,30}\bstill\b"],
                match="any",
                # Hard, symmetrically: a "fully resolved" narrative that
                # omits the later log error is the other half of the same
                # failure mode.
                hard=True,
            ),
            RequiredAnswerPattern(
                name="acknowledges_the_discrepancy",
                patterns=[
                    r"\bhowever\b",
                    r"\bdespite\b",
                    r"\balthough\b",
                    r"\bstill show",
                    r"\bdiscrepanc",
                    r"\binconsisten",
                    r"\bmixed signal",
                    r"\bconflicting\b",
                ],
                match="any",
            ),
            UnsupportedDefinitiveClaim(
                name="unsupported_root_cause",
                subject_patterns=["firewall", "switch", "reboot", "sshd", "routing"],
                definitive_patterns=_OVERCLAIM_DEFINITIVE_PATTERNS,
            ),
            HypothesisLabeled(
                name="deeper_causes_labeled_as_possibilities",
                subject_patterns=["firewall", "switch", "routing"],
            ),
            ForbiddenAnswerPattern(
                name="does_not_claim_permanently_fixed",
                patterns=_NO_PERMANENT_FIX_PATTERNS,
                reason="a later log error after the metrics recovery means the picture is not fully resolved",
                hard=True,
            ),
            ForbiddenAnswerPattern(
                name="does_not_claim_still_completely_down",
                patterns=[
                    r"\bstill (completely |totally )?(down|unreachable|broken)\b",
                    r"\bhas not recovered\b",
                    r"\bremains (down|unreachable)\b",
                ],
                reason="Prometheus and the current TCP check both show recovery -- ignoring them to force a still-down narrative is just as wrong as ignoring the later log error",
                hard=True,
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_HOSTS, known_job_ids=_KNOWN_JOB_IDS, host_pattern=_HOST_PATTERN
            ),
            MaxToolCalls(4, name="no_redundant_calls"),
            MaxIterations(5),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# system-troubleshooter-git-correlation
# ---------------------------------------------------------------------------

_GIT_CORRELATION_COMMIT_TIME = "2026-09-16T01:00:00+00:00"
_GIT_CORRELATION_WINDOW_START = "2026-09-14T00:00:00+00:00"
_GIT_CORRELATION_WINDOW_END = "2026-09-16T04:00:00+00:00"

_GIT_CORRELATION_RESULT = GitRecentChangesResult(
    head_sha="c1a2" * 10,
    commits=[
        GitCommit(
            sha="c1a2" * 10,
            authored_at=_GIT_CORRELATION_COMMIT_TIME,
            committed_at=_GIT_CORRELATION_COMMIT_TIME,
            author_name="Priya Patel",
            subject="Adjust firewall allowlist for ferros network segment",
            parent_count=1,
            changed_file_count=1,
            changed_files=[ChangedFile(path="network/firewall_rules.yaml", change_type="modified")],
            files_truncated=False,
        )
    ],
    matched_count=1,
    commits_inspected=1,
    inspection_capped=False,
    deadline_stopped=False,
    aggregate_files_capped=False,
    observed_at=_OBSERVED_AT,
)

_GIT_CORRELATION_PROMPT = (
    f"AWX job {_UNREACHABLE_JOB_ID} previously reported a failure reaching "
    f"{_UNREACHABLE_HOST} on port {_PORT}. Investigate why, including whether "
    f"any recent source/configuration changes to the '{GIT_REPOSITORY_ALIAS}' "
    "repository around that time might be related, and summarize what "
    "happened and the current status."
)


def _git_correlation_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_awx_get_job_failure_tool(_job_failure_client()))
    registry.register(build_check_tcp_connectivity_tool(_TCP_SUCCESS_RESULT))
    registry.register(build_prometheus_query_range_tool(_RECOVERED_PROMETHEUS_RESPONSE))
    registry.register(build_loki_query_tool(_FULL_INVESTIGATION_LOKI_RESPONSE))
    registry.register(build_git_recent_changes_tool(_GIT_CORRELATION_RESULT, repository_alias=GIT_REPOSITORY_ALIAS))
    registry.register(build_awx_recent_failed_jobs_tool(FixtureAWXClient([_UNREACHABLE_JOB], stdout_by_job_id={})))
    registry.register(build_prometheus_query_tool(_INSTANT_UP_RESPONSE))
    return registry


default_scenarios.register(
    Scenario(
        name="system-troubleshooter-git-correlation",
        version="1.0",
        description=(
            "Same AWX/TCP/Prometheus/Loki evidence as "
            "system-troubleshooter-full-investigation, plus one Git commit "
            "(#17) that lands near the incident timeline and plausibly "
            "correlates: a firewall-allowlist change committed about two "
            "hours before the incident's reference time. The fixture "
            "deliberately contains no evidence the commit was ever "
            "deployed. Golden behavior: cite the commit as a temporal "
            "correlation worth flagging, never assert it caused the "
            "incident or was deployed without evidence, and recommend "
            "checking deployment-state evidence as a next step."
        ),
        prompt=_GIT_CORRELATION_PROMPT,
        build_registry=_git_correlation_registry,
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(JOB_FAILURE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(NETWORK_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(PROMETHEUS_RANGE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(LOKI_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(GIT_TOOL_NAME, min_count=1, max_count=1),
            RequiredAnswerPattern(
                name="cites_historical_awx_evidence", patterns=_HISTORICAL_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_monitoring_evidence", patterns=_MONITORING_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(name="cites_log_evidence", patterns=_LOG_PATTERNS, match="any"),
            RequiredAnswerPattern(
                name="cites_current_tcp_success", patterns=_CURRENT_TCP_SUCCESS_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_the_git_commit",
                patterns=[r"\bfirewall\b", r"\ballowlist\b", r"\bfirewall_rules\b", r"\bcommit\b"],
                match="any",
                hard=True,
            ),
            RequiredAnswerPattern(
                name="describes_temporal_correlation_not_causation",
                patterns=[
                    r"\bcorrelat",
                    r"\baround the same time\b",
                    r"\bnear(ly)? the same time\b",
                    r"\bshortly before\b",
                    r"\bcoincide",
                    r"\btemporal\b",
                ],
                match="any",
            ),
            # Not ForbiddenAnswerPattern: a genuinely good answer is
            # expected to *raise* deployment as an open question ("check
            # whether this was rolled out") -- a bare phrase match alone
            # can't tell that apart from actually asserting it happened.
            # HypothesisLabeled (with hard=True and hedge patterns that
            # cover "checking"/"whether" phrasing, not just "possible"/
            # "unclear") only fails when a deployment-related phrase
            # appears with NO hedge/question language in the same
            # sentence -- i.e. asserted as settled fact.
            HypothesisLabeled(
                name="does_not_assert_deployment_without_hedging",
                subject_patterns=[
                    r"\bdeploy",
                    r"\brolled out\b",
                    r"\bpushed to production\b",
                    r"\btook effect\b",
                    r"\bwas applied\b",
                ],
                hedge_patterns=(
                    r"\bpossible\b",
                    r"\bpossibly\b",
                    r"\bmight\b",
                    r"\bmay have\b",
                    r"\bcould be\b",
                    r"\bcould have\b",
                    r"\bunclear\b",
                    r"\bunconfirmed\b",
                    r"\bhypothes",
                    r"\bpotential\b",
                    r"\bwhether\b",
                    r"\bcheck\b",
                    r"\bverify\b",
                    r"\bconfirm\b",
                    r"\bno evidence\b",
                    r"\bunknown\b",
                ),
                hard=True,
            ),
            UnsupportedDefinitiveClaim(
                name="unsupported_root_cause",
                subject_patterns=["firewall", "commit", "allowlist", "switch", "routing"],
                definitive_patterns=_OVERCLAIM_DEFINITIVE_PATTERNS,
            ),
            HypothesisLabeled(
                name="deeper_causes_labeled_as_possibilities",
                subject_patterns=["firewall", "commit", "allowlist"],
            ),
            ForbiddenAnswerPattern(
                name="does_not_claim_permanently_fixed",
                patterns=_NO_PERMANENT_FIX_PATTERNS,
                reason="one current check from one vantage point does not prove the incident is permanently resolved",
                hard=True,
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_HOSTS, known_job_ids=_KNOWN_JOB_IDS, host_pattern=_HOST_PATTERN
            ),
            MaxToolCalls(5, name="no_redundant_calls"),
            MaxIterations(6),
        ],
        **_RUNTIME_TUNING,
    )
)
