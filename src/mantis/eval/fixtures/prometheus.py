"""Fixture-backed Prometheus tools and golden scenarios combining all
three of Mantis's evidence sources: #28's historical AWX evidence, #8's
current-state TCP evidence, and #9's time-series Prometheus evidence.

:class:`FixturePrometheusClient` duck-types
``mantis.integrations.prometheus.PrometheusClient``'s public interface
(``query``, ``query_range``) but returns canned
:class:`~mantis.integrations.prometheus.PrometheusAPIResponse` data
instead of making HTTP calls. Passed to the real
``mantis.tools.prometheus.prometheus_query``/``prometheus_query_range``
via their ``_client`` override, so scenarios exercise the exact
production validation/normalization/contract logic against fixture
data.

Two golden scenarios reuse #28's exact ``ferros-c01``/job-7301/
``runner_on_unreachable`` fixture data (see
``mantis.eval.fixtures.awx``) and #8's ``check_tcp_connectivity``
fixture builder (see ``mantis.eval.fixtures.network``), adding a
Prometheus ``up{instance="ferros-c01:9100"}`` range query showing the
scrape target's health over a window spanning the AWX failure:

- ``multi-signal-recovery``: AWX historically failed, Prometheus shows
  the scrape degrading (``up`` dropping to 0) then recovering, and a
  current TCP probe now succeeds. Golden behavior correlates the
  timeline across all three sources without asserting an unsupported
  specific cause or claiming the incident is permanently resolved.
- ``multi-signal-still-down`` (inverse): AWX historically failed,
  Prometheus shows ``up`` still at 0 with no recovery, and a current TCP
  probe also still fails. Golden behavior still avoids unsupported
  causal certainty even though all three signals agree, and does not
  treat a zero/missing ``up`` sample as proof the host itself is
  completely down (see #9 item 25 — ``up==0`` means the *scrape*
  failed, not necessarily the target host/service).
"""

from __future__ import annotations

from typing import Any

from mantis.eval.expectations import (
    ForbiddenAnswerPattern,
    HypothesisLabeled,
    MaxIterations,
    MaxToolCalls,
    MustProduceFinalAnswer,
    NoUnexpectedEntities,
    RequiredAnswerPattern,
    RequiredToolCall,
    UnsupportedDefinitiveClaim,
)
from mantis.eval.fixtures.awx import (
    _UNREACHABLE_EVENT,
    _UNREACHABLE_HOST,
    _UNREACHABLE_JOB,
    _UNREACHABLE_JOB_ID,
    FixtureAWXJobFailureClient,
    build_awx_get_job_failure_tool,
)
from mantis.eval.fixtures.network import NETWORK_TOOL_NAME, build_check_tcp_connectivity_tool
from mantis.eval.scenarios import Scenario, default_scenarios
from mantis.integrations.network import AddressAttempt, TCPConnectResult
from mantis.integrations.prometheus import PrometheusAPIResponse
from mantis.registry import Tool, ToolRegistry
from mantis.tools.prometheus import (
    PROMETHEUS_QUERY_RANGE_SCHEMA,
    PROMETHEUS_QUERY_SCHEMA,
    prometheus_query,
    prometheus_query_range,
)

PROMETHEUS_TOOL_NAME = "prometheus_query_range"
PROMETHEUS_INSTANT_TOOL_NAME = "prometheus_query"
JOB_FAILURE_TOOL_NAME = "awx_get_job_failure"

_PORT = 22
_INSTANCE = f"{_UNREACHABLE_HOST}:9100"


class FixturePrometheusClient:
    """A canned stand-in for ``PrometheusClient``, scoped to one
    scenario's instant- and/or range-query response. Either can be
    omitted (``None``) for a scenario that only exercises one of the two
    -- calling the corresponding tool then raises ``NotImplementedError``
    rather than silently returning the other response, so a
    scenario/fixture bug (calling the wrong tool) fails loudly instead of
    returning misleading data."""

    def __init__(
        self,
        *,
        query_response: PrometheusAPIResponse | None = None,
        query_range_response: PrometheusAPIResponse | None = None,
    ) -> None:
        self._query_response = query_response
        self._query_range_response = query_range_response

    def query(
        self, promql: str, *, time_param: str | None = None, deadline: Any = None
    ) -> PrometheusAPIResponse:
        if self._query_response is None:
            raise NotImplementedError("this fixture was not given a query_response")
        return self._query_response

    def query_range(
        self, promql: str, *, start: str, end: str, step: str, deadline: Any = None
    ) -> PrometheusAPIResponse:
        if self._query_range_response is None:
            raise NotImplementedError("this fixture was not given a query_range_response")
        return self._query_range_response


def build_prometheus_query_tool(response: PrometheusAPIResponse) -> Tool:
    """Build a ``Tool`` for ``prometheus_query`` (instant) bound to a
    canned response — uses the real schema and real tool function, only
    the HTTP client is swapped out. See
    :func:`build_prometheus_query_range_tool` for the range-query
    equivalent."""
    client = FixturePrometheusClient(query_response=response)

    def _fixture_handler(query: str, time: Any = None) -> dict[str, Any]:
        return prometheus_query(query, time=time, _client=client)

    return Tool(
        name=PROMETHEUS_INSTANT_TOOL_NAME,
        schema=PROMETHEUS_QUERY_SCHEMA,
        handler=_fixture_handler,
        category="prometheus",
        mutating=False,
        description="Fixture-backed prometheus_query for evaluation scenarios.",
    )


def build_prometheus_query_range_tool(response: PrometheusAPIResponse) -> Tool:
    """Build a ``Tool`` for ``prometheus_query_range`` bound to a canned
    response — uses the real schema and real tool function, only the
    HTTP client is swapped out."""
    client = FixturePrometheusClient(query_range_response=response)

    def _fixture_handler(query: str, start: Any, end: Any, step: Any) -> dict[str, Any]:
        return prometheus_query_range(query, start, end, step, _client=client)

    return Tool(
        name=PROMETHEUS_TOOL_NAME,
        schema=PROMETHEUS_QUERY_RANGE_SCHEMA,
        handler=_fixture_handler,
        category="prometheus",
        mutating=False,
        description="Fixture-backed prometheus_query_range for evaluation scenarios.",
    )


def _combined_registry(
    *, tcp_result: TCPConnectResult, prometheus_response: PrometheusAPIResponse
) -> ToolRegistry:
    job_failure_client = FixtureAWXJobFailureClient(
        jobs_by_id={_UNREACHABLE_JOB_ID: _UNREACHABLE_JOB},
        events_by_id={_UNREACHABLE_JOB_ID: [_UNREACHABLE_EVENT]},
        stdout_by_job_id={},
    )
    registry = ToolRegistry()
    registry.register(build_awx_get_job_failure_tool(job_failure_client))
    registry.register(build_check_tcp_connectivity_tool(tcp_result))
    registry.register(build_prometheus_query_range_tool(prometheus_response))
    return registry


_SYSTEM_PROMPT = """\
You are a Mantis investigator correlating three distinct evidence sources.
Keep their semantics separate:

- `awx_get_job_failure(job_id)`: structured evidence AWX recorded at the
  time a job ran -- the past, at a specific point in time.
- `prometheus_query_range(query, start, end, step)`: how a monitored metric
  changed over a time window. A sample of `up == 0` means the Prometheus
  *scrape* of that target failed at that instant -- it does NOT by itself
  mean the host was powered off, all services were down, or the network
  was unreachable. A missing/absent series can mean the target
  disappeared, service discovery changed, retention expired, or other
  causes -- it is not proof of any single specific condition. Treat `up`
  as a scrape-health signal, not a host-health oracle.
- `check_tcp_connectivity(host, port)`: a live TCP connection attempt from
  Mantis's own network vantage point, right now -- the present, from one
  vantage point only.

Rules you must follow:

- Always keep historical (AWX), monitoring-window (Prometheus), and
  current-state (TCP) evidence clearly distinguished, with explicit
  temporal language for each.
- You may note that the AWX failure and the Prometheus scrape
  degradation occurred in roughly the same window -- that is a timeline
  correlation, not proof of a shared cause. Never assert a specific
  cause (e.g. "the firewall definitely blocked it", "the switch failed",
  "the host rebooted", "sshd crashed") unless the evidence actually
  identifies it. State such causes as possibilities, not conclusions.
- Do NOT claim an incident is permanently fixed or resolved just because
  one current TCP check succeeded -- you only checked one path from one
  vantage point at one instant.
- Only report information you actually retrieved via a tool call. Never
  invent hosts, job ids, timestamps, metric values, or error messages.

Keep your answer concise and organized: what AWX observed historically,
what Prometheus shows over the monitoring window, what Mantis observed
just now over TCP, and how (if at all) they relate.
"""

_RUNTIME_TUNING = dict(
    system_prompt=_SYSTEM_PROMPT,
    agent_tools=[JOB_FAILURE_TOOL_NAME, NETWORK_TOOL_NAME, PROMETHEUS_TOOL_NAME],
    tool_call_budget=3,
    temperature=0.1,
)

_PROMPT = (
    f"AWX job {_UNREACHABLE_JOB_ID} previously reported a failure reaching "
    f"{_UNREACHABLE_HOST} on port {_PORT}. Check recent Prometheus monitoring "
    f"data for {_INSTANCE} around that time, check current TCP connectivity, "
    "and summarize what happened and the current status."
)

_KNOWN_HOSTS = frozenset({_UNREACHABLE_HOST})
_KNOWN_JOB_IDS = frozenset({str(_UNREACHABLE_JOB_ID)})
_HOST_PATTERN = r"\bferros-c[0-9]+\b"

_HISTORICAL_PATTERNS = [r"no route to host", r"network reachability", r"\bunreachable\b"]
_HISTORICAL_TIME_PATTERNS = [r"\bpreviously\b", r"\bhistorically\b", r"\bat that time\b", r"\bat the time\b"]
_CURRENT_TIME_PATTERNS = [r"\bnow\b", r"\bcurrently\b", r"\bat this time\b", r"\bright now\b"]
_MONITORING_PATTERNS = [r"\bprometheus\b", r"\bmonitoring\b", r"\bscrape\b", r"\bup\b.{0,20}metric", r"\bmetric"]

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

_NO_METRIC_OVERINTERPRETATION_PATTERNS = [
    r"\bhost was (completely |totally )?(down|off|powered off)\b",
    r"\ball services were down\b",
    r"\b(the )?(server|host) crashed\b",
    r"\bhost (definitely |certainly )?rebooted\b",
    r"\bsshd (definitely |certainly )?crashed\b",
    r"\bswitch (definitely |certainly )?failed\b",
]


def _up_sample(offset_seconds: float, value: str) -> list:
    return [1700000000.0 + offset_seconds, value]


# ---------------------------------------------------------------------------
# multi-signal-recovery
# ---------------------------------------------------------------------------

_RECOVERY_TCP_RESULT = TCPConnectResult(
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
    observed_at="2026-09-16T03:10:00+00:00",
)

_RECOVERY_PROMETHEUS_RESPONSE = PrometheusAPIResponse(
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

default_scenarios.register(
    Scenario(
        name="multi-signal-recovery",
        version="1.0",
        description=(
            "AWX historically observed a runner_on_unreachable failure "
            "reaching ferros-c01:22 (#28). A Prometheus range query over "
            "roughly the same window shows the node_exporter scrape "
            "(up{instance='ferros-c01:9100'}) drop to 0 and then recover "
            "(#9). A current TCP probe to the same host/port now "
            "succeeds (#8). Golden behavior: correlate the timeline "
            "across all three sources without asserting an unsupported "
            "specific cause (firewall, switch, reboot, sshd) or "
            "claiming the incident is permanently fixed."
        ),
        prompt=_PROMPT,
        build_registry=lambda: _combined_registry(
            tcp_result=_RECOVERY_TCP_RESULT, prometheus_response=_RECOVERY_PROMETHEUS_RESPONSE
        ),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(JOB_FAILURE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(NETWORK_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(PROMETHEUS_TOOL_NAME, min_count=1, max_count=1),
            RequiredAnswerPattern(
                name="cites_historical_awx_evidence", patterns=_HISTORICAL_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_monitoring_evidence", patterns=_MONITORING_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_current_tcp_success",
                patterns=[r"\bconnect(ed|ivity)\b", r"\breachable\b", r"\bsucceed"],
                match="any",
            ),
            RequiredAnswerPattern(
                name="uses_historical_time_language", patterns=_HISTORICAL_TIME_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="uses_current_time_language", patterns=_CURRENT_TIME_PATTERNS, match="any"
            ),
            UnsupportedDefinitiveClaim(
                name="unsupported_root_cause",
                subject_patterns=["firewall", "routing", "switch", "reboot", "sshd"],
                definitive_patterns=_OVERCLAIM_DEFINITIVE_PATTERNS,
            ),
            HypothesisLabeled(
                name="deeper_causes_labeled_as_possibilities",
                subject_patterns=["firewall", "routing", "switch"],
            ),
            ForbiddenAnswerPattern(
                name="does_not_claim_permanently_fixed",
                patterns=_NO_PERMANENT_FIX_PATTERNS,
                reason="one current check from one vantage point does not prove the incident is permanently resolved",
                hard=True,
            ),
            ForbiddenAnswerPattern(
                name="does_not_overinterpret_the_up_metric",
                patterns=_NO_METRIC_OVERINTERPRETATION_PATTERNS,
                reason="up==0 means the Prometheus scrape failed, not that the host/services were definitively down",
                hard=True,
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_HOSTS, known_job_ids=_KNOWN_JOB_IDS, host_pattern=_HOST_PATTERN
            ),
            MaxToolCalls(3, name="no_duplicate_calls"),
            MaxIterations(4),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# multi-signal-still-down (inverse)
# ---------------------------------------------------------------------------

_STILL_DOWN_TCP_RESULT = TCPConnectResult(
    host=_UNREACHABLE_HOST,
    port=_PORT,
    status="host_unreachable",
    connected=False,
    resolved_address=None,
    address_family=None,
    latency_ms=None,
    attempts=[
        AddressAttempt(
            address="10.0.4.12", address_family="ipv4", status="host_unreachable", errno=113,
            latency_ms=8.1, message="[Errno 113] No route to host",
        )
    ],
    truncated=False,
    observed_at="2026-09-16T03:10:00+00:00",
)

_STILL_DOWN_PROMETHEUS_RESPONSE = PrometheusAPIResponse(
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
                _up_sample(300, "0"),
                _up_sample(360, "0"),
            ],
        }
    ],
    error_type=None,
    error=None,
    warnings=[],
)

default_scenarios.register(
    Scenario(
        name="multi-signal-still-down",
        version="1.0",
        description=(
            "The inverse of multi-signal-recovery: AWX historically "
            "failed reaching ferros-c01:22, Prometheus shows the "
            "node_exporter scrape still at up=0 with no recovery, and a "
            "current TCP probe also still fails (host_unreachable). "
            "Golden behavior: even with all three signals agreeing, "
            "avoid unsupported causal certainty and do not treat the "
            "zero/absent up metric as proof the host itself is "
            "completely down."
        ),
        prompt=_PROMPT,
        build_registry=lambda: _combined_registry(
            tcp_result=_STILL_DOWN_TCP_RESULT, prometheus_response=_STILL_DOWN_PROMETHEUS_RESPONSE
        ),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(JOB_FAILURE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(NETWORK_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(PROMETHEUS_TOOL_NAME, min_count=1, max_count=1),
            RequiredAnswerPattern(
                name="cites_historical_awx_evidence", patterns=_HISTORICAL_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_monitoring_evidence", patterns=_MONITORING_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_current_tcp_failure",
                patterns=[r"\bunreachable\b", r"\bstill\b.{0,20}\bfail", r"\bcould not connect\b", r"\bnot reachable\b"],
                match="any",
            ),
            UnsupportedDefinitiveClaim(
                name="unsupported_root_cause",
                subject_patterns=["firewall", "routing", "switch", "reboot", "sshd"],
                definitive_patterns=_OVERCLAIM_DEFINITIVE_PATTERNS,
            ),
            HypothesisLabeled(
                name="deeper_causes_labeled_as_possibilities",
                subject_patterns=["firewall", "routing", "switch"],
            ),
            ForbiddenAnswerPattern(
                name="does_not_overinterpret_the_up_metric",
                patterns=_NO_METRIC_OVERINTERPRETATION_PATTERNS,
                reason="up==0 means the Prometheus scrape failed, not that the host/services were definitively down",
                hard=True,
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_HOSTS, known_job_ids=_KNOWN_JOB_IDS, host_pattern=_HOST_PATTERN
            ),
            MaxToolCalls(3, name="no_duplicate_calls"),
            MaxIterations(4),
        ],
        **_RUNTIME_TUNING,
    )
)
