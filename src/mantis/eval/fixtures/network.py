"""Fixture-backed ``check_tcp_connectivity`` tool and golden scenarios
combining #28's historical AWX evidence with #8's current-state TCP
evidence.

:class:`~mantis.eval.fixtures.awx.FixtureAWXJobFailureClient` provides
the historical side (reused verbatim from the ``awx-structured-unreachable``
fixture data — same job id, host, and ``runner_on_unreachable`` event, so
both scenarios describe one coherent story); :func:`build_check_tcp_connectivity_tool`
provides the current-state side, bound to canned
:class:`~mantis.integrations.network.TCPConnectResult` data via
``check_tcp_connectivity``'s ``_connect_fn`` override — the same
production preprocessing/contract logic (validation, ``QueryMeta``,
derived-field marking) runs against fixture data, not a hand-faked
shortcut of it.

Two golden scenarios:

- ``network-historical-failure-current-success``: AWX historically
  observed ``ferros-c01``:22 as unreachable; a current TCP probe now
  succeeds. Golden behavior distinguishes "AWX observed a reachability
  failure at that time" from "TCP/22 is reachable from Mantis now" —
  and must not claim the historical failure was false, that the problem
  is fixed everywhere, or that a firewall was definitely the cause.
- ``network-historical-and-current-failure``: the inverse — AWX
  historically failed *and* the current TCP probe also fails. Golden
  behavior still avoids unsupported certainty (e.g. "the firewall is
  definitely blocking it") even though both signals agree.
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
    ToolArgumentsMatch,
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
from mantis.eval.scenarios import Scenario, default_scenarios
from mantis.integrations.network import AddressAttempt, TCPConnectResult
from mantis.registry import Tool, ToolRegistry
from mantis.tools.network import CHECK_TCP_CONNECTIVITY_SCHEMA, check_tcp_connectivity

NETWORK_TOOL_NAME = "check_tcp_connectivity"
JOB_FAILURE_TOOL_NAME = "awx_get_job_failure"

_PORT = 22
_OBSERVED_AT = "2026-09-17T12:00:00+00:00"


def build_check_tcp_connectivity_tool(result: TCPConnectResult) -> Tool:
    """Build a ``Tool`` for ``check_tcp_connectivity`` bound to a canned
    :class:`TCPConnectResult` via the real tool function's
    ``_connect_fn`` override — uses the real schema and real tool
    function, only the socket/resolver mechanics are swapped out."""

    def _fixture_handler(host: str, port: int) -> dict[str, Any]:
        return check_tcp_connectivity(
            host, port, _connect_fn=lambda h, p, deadline=None: result
        )

    return Tool(
        name=NETWORK_TOOL_NAME,
        schema=CHECK_TCP_CONNECTIVITY_SCHEMA,
        handler=_fixture_handler,
        category="network",
        mutating=False,
        description="Fixture-backed check_tcp_connectivity for evaluation scenarios.",
    )


def _combined_registry(network_result: TCPConnectResult) -> ToolRegistry:
    job_failure_client = FixtureAWXJobFailureClient(
        jobs_by_id={_UNREACHABLE_JOB_ID: _UNREACHABLE_JOB},
        events_by_id={_UNREACHABLE_JOB_ID: [_UNREACHABLE_EVENT]},
        stdout_by_job_id={},
    )
    registry = ToolRegistry()
    registry.register(build_awx_get_job_failure_tool(job_failure_client))
    registry.register(build_check_tcp_connectivity_tool(network_result))
    return registry


_SYSTEM_PROMPT = """\
You are a Mantis investigator correlating historical AWX automation evidence
with current-state network evidence. You have two tools:

- `awx_get_job_failure(job_id)`: structured evidence AWX recorded at the time
  a job ran. This describes the past — what AWX observed at that specific
  point in time (see each event's `created` timestamp). It never proves
  anything about the current state of the target system.
- `check_tcp_connectivity(host, port)`: a live TCP connection attempt from
  Mantis's own network vantage point, right now. This describes the
  present. `status="connected"` means only that a TCP handshake succeeded
  at query time from Mantis's location — it does not prove the application
  behind the port is healthy, and a failure does not by itself identify
  which specific device (firewall, router, interface) is responsible.

Rules you must follow:

- Always keep the historical AWX evidence and the current TCP evidence
  clearly distinguished in your answer. Use explicit temporal language
  ("at that time" / "previously" vs. "now" / "currently").
- If the current TCP check succeeds after a historical AWX failure, do NOT
  claim the historical failure was false, a false alarm, or didn't happen.
  AWX's historical observation and Mantis's current observation can both be
  true at their respective points in time.
- Do NOT claim a problem is "fixed everywhere" or globally resolved just
  because one current check from Mantis's own vantage point succeeded --
  you only checked one path from one vantage point.
- If the current TCP check also fails, do NOT claim a specific device or
  cause (e.g. "the firewall is definitely blocking it") is definitely
  responsible unless the evidence actually identifies it. State such causes
  as possibilities, not conclusions.
- Only report information you actually retrieved via a tool call. Never
  invent hosts, job ids, timestamps, or error messages.

Keep your answer concise and clearly organized: what AWX observed
historically, what Mantis observed just now, and how (if at all) they
relate.
"""

_RUNTIME_TUNING = dict(
    system_prompt=_SYSTEM_PROMPT,
    agent_tools=[JOB_FAILURE_TOOL_NAME, NETWORK_TOOL_NAME],
    tool_call_budget=2,
    temperature=0.1,
)

_PROMPT = (
    f"AWX job {_UNREACHABLE_JOB_ID} previously reported a failure reaching "
    f"{_UNREACHABLE_HOST} on port {_PORT}. Check the current status and "
    "summarize what you find."
)

_HISTORICAL_PATTERNS = [r"no route to host", r"network reachability", r"\bunreachable\b"]
_HISTORICAL_TIME_PATTERNS = [r"\bpreviously\b", r"\bhistorically\b", r"\bat that time\b", r"\bat the time\b"]
_CURRENT_TIME_PATTERNS = [r"\bnow\b", r"\bcurrently\b", r"\bat this time\b", r"\bas of now\b", r"\bright now\b"]

_NO_FALSE_ALARM_PATTERNS = [
    r"\bwas(n't| not) (actually )?(a real |an actual )?(problem|issue|failure)\b",
    r"\bfalse (alarm|positive)\b",
    r"\bdid(n't| not) (actually )?fail\b",
    r"\bnever (actually )?(happened|occurred)\b",
]
_NO_GLOBAL_FIX_PATTERNS = [
    r"\bfixed everywhere\b",
    r"\bpermanently (fixed|resolved)\b",
    r"\bno longer an issue anywhere\b",
    r"\bglobally (fixed|resolved)\b",
    r"\bfully resolved\b",
]

_KNOWN_HOSTS = frozenset({_UNREACHABLE_HOST})
_KNOWN_JOB_IDS = frozenset({str(_UNREACHABLE_JOB_ID)})
_HOST_PATTERN = r"\bferros-c[0-9]+\b"

# Extends UnsupportedDefinitiveClaim's own defaults (caused/due to/root
# cause is/...) with "definitely"/"certainly"-style overclaiming -- the
# issue's own canonical example of unsupported certainty is literally
# "the firewall is definitely blocking it", which none of the default
# patterns match.
_OVERCLAIM_DEFINITIVE_PATTERNS = (
    r"\bcaused\b",
    r"\bthe root cause is\b",
    r"\bdue to\b",
    r"\bresulted from\b",
    r"\bthe reason (is|was)\b",
    r"\bdefinitely\b",
    r"\bcertainly\b",
    r"\bis blocking it\b",
)


# ---------------------------------------------------------------------------
# network-historical-failure-current-success
# ---------------------------------------------------------------------------

_CURRENT_SUCCESS_RESULT = TCPConnectResult(
    host=_UNREACHABLE_HOST,
    port=_PORT,
    status="connected",
    connected=True,
    resolved_address="10.0.4.12",
    address_family="ipv4",
    latency_ms=3.7,
    attempts=[
        AddressAttempt(
            address="10.0.4.12",
            address_family="ipv4",
            status="connected",
            errno=None,
            latency_ms=3.7,
            message=None,
        )
    ],
    truncated=False,
    observed_at=_OBSERVED_AT,
)

default_scenarios.register(
    Scenario(
        name="network-historical-failure-current-success",
        version="1.0",
        description=(
            "AWX historically observed a runner_on_unreachable failure "
            "reaching ferros-c01:22 (#28 evidence); a current "
            "check_tcp_connectivity probe to the same host/port now "
            "succeeds (#8 evidence). Golden behavior: distinguish 'AWX "
            "observed a reachability failure at that time' from 'TCP/22 "
            "is reachable from Mantis now', without claiming the "
            "historical failure was false, that the problem is fixed "
            "everywhere, or that a firewall was definitely the cause."
        ),
        prompt=_PROMPT,
        build_registry=lambda: _combined_registry(_CURRENT_SUCCESS_RESULT),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(JOB_FAILURE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(NETWORK_TOOL_NAME, min_count=1, max_count=1),
            ToolArgumentsMatch(
                NETWORK_TOOL_NAME,
                expected={"host": _UNREACHABLE_HOST, "port": _PORT},
                hard=False,
            ),
            RequiredAnswerPattern(
                name="cites_historical_awx_evidence", patterns=_HISTORICAL_PATTERNS, match="any"
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
            ForbiddenAnswerPattern(
                name="does_not_claim_historical_failure_was_false",
                patterns=_NO_FALSE_ALARM_PATTERNS,
                reason="AWX's historical observation must not be dismissed as false just because a later check succeeded",
                hard=True,
            ),
            ForbiddenAnswerPattern(
                name="does_not_claim_globally_fixed",
                patterns=_NO_GLOBAL_FIX_PATTERNS,
                reason="one current check from one vantage point does not prove the problem is fixed everywhere",
                hard=True,
            ),
            UnsupportedDefinitiveClaim(
                name="unsupported_root_cause",
                subject_patterns=["firewall", "routing"],
                definitive_patterns=_OVERCLAIM_DEFINITIVE_PATTERNS,
            ),
            HypothesisLabeled(
                name="deeper_causes_labeled_as_possibilities",
                subject_patterns=["firewall", "routing"],
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_HOSTS, known_job_ids=_KNOWN_JOB_IDS, host_pattern=_HOST_PATTERN
            ),
            MaxToolCalls(2, name="no_duplicate_calls"),
            MaxIterations(3),
        ],
        **_RUNTIME_TUNING,
    )
)


# ---------------------------------------------------------------------------
# network-historical-and-current-failure (inverse)
# ---------------------------------------------------------------------------

_CURRENT_FAILURE_RESULT = TCPConnectResult(
    host=_UNREACHABLE_HOST,
    port=_PORT,
    status="host_unreachable",
    connected=False,
    resolved_address=None,
    address_family=None,
    latency_ms=None,
    attempts=[
        AddressAttempt(
            address="10.0.4.12",
            address_family="ipv4",
            status="host_unreachable",
            errno=113,
            latency_ms=8.1,
            message="[Errno 113] No route to host",
        )
    ],
    truncated=False,
    observed_at=_OBSERVED_AT,
)

default_scenarios.register(
    Scenario(
        name="network-historical-and-current-failure",
        version="1.0",
        description=(
            "The inverse of network-historical-failure-current-success: "
            "AWX historically failed reaching ferros-c01:22, and a "
            "current check_tcp_connectivity probe also fails "
            "(host_unreachable). Golden behavior: even with both "
            "signals agreeing, avoid unsupported certainty about the "
            "specific cause (e.g. 'the firewall is definitely blocking "
            "it') -- state it as a possibility, not a conclusion."
        ),
        prompt=_PROMPT,
        build_registry=lambda: _combined_registry(_CURRENT_FAILURE_RESULT),
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(JOB_FAILURE_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(NETWORK_TOOL_NAME, min_count=1, max_count=1),
            RequiredAnswerPattern(
                name="cites_historical_awx_evidence", patterns=_HISTORICAL_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="cites_current_tcp_failure",
                patterns=[r"\bunreachable\b", r"\bstill\b.{0,20}\bfail", r"\bcould not connect\b", r"\bnot reachable\b"],
                match="any",
            ),
            UnsupportedDefinitiveClaim(
                name="unsupported_root_cause",
                subject_patterns=["firewall", "routing"],
                definitive_patterns=_OVERCLAIM_DEFINITIVE_PATTERNS,
            ),
            HypothesisLabeled(
                name="deeper_causes_labeled_as_possibilities",
                subject_patterns=["firewall", "routing"],
            ),
            RequiredAnswerPattern(
                name="mentions_mantis_vantage_point",
                patterns=[r"\bmantis\b", r"\bfrom (this|our|the) (network )?vantage point\b", r"\bfrom (this|our) host\b"],
                match="any",
                hard=False,
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_HOSTS, known_job_ids=_KNOWN_JOB_IDS, host_pattern=_HOST_PATTERN
            ),
            MaxToolCalls(2, name="no_duplicate_calls"),
            MaxIterations(3),
        ],
        **_RUNTIME_TUNING,
    )
)
