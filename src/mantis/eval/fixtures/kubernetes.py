"""Fixture-backed Kubernetes tool and a golden scenario correlating #18's
pod-restart evidence with #9's time-series Prometheus evidence.

:class:`FixtureKubernetesClient` duck-types
``mantis.integrations.kubernetes.KubernetesClient``'s public interface
(``list_pods``/``list_deployments``/``list_nodes``/``list_events``, plus
the ``config`` attribute every successful result's provenance reads from)
but returns canned, real ``kubernetes.client`` model objects instead of
making an API call. Passed to the real ``mantis.tools.kubernetes``
functions via their ``_client`` override, so the scenario exercises the
exact production validation/normalization/bounding/contract logic
against fixture data, not a hand-faked shortcut of it.

One golden scenario:

- ``kubernetes-pod-restart-correlated-with-scrape-gap``: a pod's
  container restarted (``kubernetes_list_pods`` evidence: restart_count,
  last termination reason/time) roughly when a Prometheus range query
  shows its scrape target's ``up`` metric drop to 0 and recover
  (``prometheus_query_range`` evidence, #9). Golden behavior notes the
  temporal correlation between the two signals but must NOT claim the
  restart *caused* the scrape gap, or that the scrape gap *caused* the
  restart -- the evidence establishes a shared time window, not a causal
  direction (see #18's evaluation requirement).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kubernetes import client as k8s

from mantis.config import KubernetesConfig
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
from mantis.eval.fixtures.prometheus import build_prometheus_query_range_tool
from mantis.eval.scenarios import Scenario, default_scenarios
from mantis.integrations.prometheus import PrometheusAPIResponse
from mantis.registry import Tool, ToolRegistry
from mantis.tools.kubernetes import (
    KUBERNETES_LIST_DEPLOYMENTS_SCHEMA,
    KUBERNETES_LIST_EVENTS_SCHEMA,
    KUBERNETES_LIST_NODES_SCHEMA,
    KUBERNETES_LIST_PODS_SCHEMA,
    kubernetes_list_deployments,
    kubernetes_list_events,
    kubernetes_list_nodes,
    kubernetes_list_pods,
)

KUBERNETES_PODS_TOOL_NAME = "kubernetes_list_pods"
KUBERNETES_DEPLOYMENTS_TOOL_NAME = "kubernetes_list_deployments"
KUBERNETES_NODES_TOOL_NAME = "kubernetes_list_nodes"
KUBERNETES_EVENTS_TOOL_NAME = "kubernetes_list_events"
PROMETHEUS_TOOL_NAME = "prometheus_query_range"

_NAMESPACE = "payments"
_POD_NAME = "payment-api-7f9c8d-abcde"
_INSTANCE = "payment-api:8080"
_RESTART_FINISHED_AT = datetime(2026, 9, 17, 3, 14, 0, tzinfo=timezone.utc)
_BASE_TS = 1700000000.0

_CLUSTER_CONFIG = KubernetesConfig(
    auth_mode="kubeconfig",
    kubeconfig_path="/home/james/.kube/config",
    context="home",
    cluster_name="home-k3s",
    verify_ssl=True,
)


class FixtureKubernetesClient:
    """A canned stand-in for ``KubernetesClient``, scoped to one
    scenario's pod-list response. Calling an unconfigured list method
    raises ``NotImplementedError`` rather than silently returning
    something else -- a scenario/fixture bug (calling the wrong tool)
    must fail loudly, not return misleading data."""

    def __init__(
        self,
        *,
        config: KubernetesConfig,
        pods_response: Any = None,
        deployments_response: Any = None,
        nodes_response: Any = None,
        events_response: Any = None,
    ) -> None:
        self.config = config
        self._pods_response = pods_response
        self._deployments_response = deployments_response
        self._nodes_response = nodes_response
        self._events_response = events_response

    def list_pods(self, namespace: str, *, label_selector: str | None, limit: int, deadline: Any = None) -> Any:
        if self._pods_response is None:
            raise NotImplementedError("this fixture was not given a pods_response")
        return self._pods_response

    def list_deployments(
        self, namespace: str, *, label_selector: str | None, limit: int, deadline: Any = None
    ) -> Any:
        if self._deployments_response is None:
            raise NotImplementedError("this fixture was not given a deployments_response")
        return self._deployments_response

    def list_nodes(self, *, label_selector: str | None, limit: int, deadline: Any = None) -> Any:
        if self._nodes_response is None:
            raise NotImplementedError("this fixture was not given a nodes_response")
        return self._nodes_response

    def list_events(
        self, namespace: str, *, field_selector: str | None, limit: int, deadline: Any = None
    ) -> Any:
        if self._events_response is None:
            raise NotImplementedError("this fixture was not given an events_response")
        return self._events_response


def build_kubernetes_list_pods_tool(response: Any) -> Tool:
    """Build a ``Tool`` for ``kubernetes_list_pods`` bound to a canned
    ``V1PodList`` via the real tool function's ``_client`` override --
    uses the real schema and real tool function, only the API client is
    swapped out."""
    client = FixtureKubernetesClient(config=_CLUSTER_CONFIG, pods_response=response)

    def _fixture_handler(namespace: str, label_selector: Any = None) -> dict[str, Any]:
        return kubernetes_list_pods(namespace, label_selector, _client=client)

    return Tool(
        name=KUBERNETES_PODS_TOOL_NAME,
        schema=KUBERNETES_LIST_PODS_SCHEMA,
        handler=_fixture_handler,
        category="kubernetes",
        mutating=False,
        description="Fixture-backed kubernetes_list_pods for evaluation scenarios.",
    )


def build_kubernetes_list_deployments_tool(response: Any) -> Tool:
    """Build a ``Tool`` for ``kubernetes_list_deployments`` bound to a
    canned ``V1DeploymentList`` via the real tool function's ``_client``
    override -- same pattern as :func:`build_kubernetes_list_pods_tool`."""
    client = FixtureKubernetesClient(config=_CLUSTER_CONFIG, deployments_response=response)

    def _fixture_handler(namespace: str, label_selector: Any = None) -> dict[str, Any]:
        return kubernetes_list_deployments(namespace, label_selector, _client=client)

    return Tool(
        name=KUBERNETES_DEPLOYMENTS_TOOL_NAME,
        schema=KUBERNETES_LIST_DEPLOYMENTS_SCHEMA,
        handler=_fixture_handler,
        category="kubernetes",
        mutating=False,
        description="Fixture-backed kubernetes_list_deployments for evaluation scenarios.",
    )


def build_kubernetes_list_nodes_tool(response: Any) -> Tool:
    """Build a ``Tool`` for ``kubernetes_list_nodes`` bound to a canned
    ``V1NodeList`` via the real tool function's ``_client`` override --
    same pattern as :func:`build_kubernetes_list_pods_tool`."""
    client = FixtureKubernetesClient(config=_CLUSTER_CONFIG, nodes_response=response)

    def _fixture_handler(label_selector: Any = None) -> dict[str, Any]:
        return kubernetes_list_nodes(label_selector, _client=client)

    return Tool(
        name=KUBERNETES_NODES_TOOL_NAME,
        schema=KUBERNETES_LIST_NODES_SCHEMA,
        handler=_fixture_handler,
        category="kubernetes",
        mutating=False,
        description="Fixture-backed kubernetes_list_nodes for evaluation scenarios.",
    )


def build_kubernetes_list_events_tool(response: Any) -> Tool:
    """Build a ``Tool`` for ``kubernetes_list_events`` bound to a canned
    ``CoreV1EventList`` via the real tool function's ``_client`` override
    -- same pattern as :func:`build_kubernetes_list_pods_tool`."""
    client = FixtureKubernetesClient(config=_CLUSTER_CONFIG, events_response=response)

    def _fixture_handler(namespace: str, name: Any = None, kind: Any = None) -> dict[str, Any]:
        return kubernetes_list_events(namespace, name, kind, _client=client)

    return Tool(
        name=KUBERNETES_EVENTS_TOOL_NAME,
        schema=KUBERNETES_LIST_EVENTS_SCHEMA,
        handler=_fixture_handler,
        category="kubernetes",
        mutating=False,
        description="Fixture-backed kubernetes_list_events for evaluation scenarios.",
    )


def _restarted_pod() -> k8s.V1Pod:
    terminated = k8s.V1ContainerStateTerminated(
        exit_code=1, reason="Error", finished_at=_RESTART_FINISHED_AT
    )
    return k8s.V1Pod(
        metadata=k8s.V1ObjectMeta(
            name=_POD_NAME,
            namespace=_NAMESPACE,
            labels={"app": "payment-api"},
            annotations={},
        ),
        spec=k8s.V1PodSpec(
            containers=[k8s.V1Container(name="payment-api", image="registry.example/payment-api:1.4.2")],
            node_name="worker-2",
        ),
        status=k8s.V1PodStatus(
            phase="Running",
            host_ip="10.0.4.21",
            pod_ip="10.0.4.55",
            conditions=[k8s.V1PodCondition(type="Ready", status="True")],
            container_statuses=[
                k8s.V1ContainerStatus(
                    name="payment-api",
                    ready=True,
                    restart_count=3,
                    image="registry.example/payment-api:1.4.2",
                    image_id="x",
                    state=k8s.V1ContainerState(running=k8s.V1ContainerStateRunning()),
                    last_state=k8s.V1ContainerState(terminated=terminated),
                )
            ],
        ),
    )


def _up_sample(offset_seconds: float, value: str) -> list:
    return [_BASE_TS + offset_seconds, value]


_SCRAPE_GAP_PROMETHEUS_RESPONSE = PrometheusAPIResponse(
    status="success",
    result_type="matrix",
    result=[
        {
            "metric": {"__name__": "up", "instance": _INSTANCE, "job": "payment-api"},
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


def _combined_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_kubernetes_list_pods_tool(k8s.V1PodList(items=[_restarted_pod()], metadata=k8s.V1ListMeta())))
    registry.register(build_prometheus_query_range_tool(_SCRAPE_GAP_PROMETHEUS_RESPONSE))
    return registry


_SYSTEM_PROMPT = f"""\
You are a Mantis investigator correlating two distinct evidence sources.
Keep their semantics separate:

- `kubernetes_list_pods(namespace, label_selector=None)`: current-state
  cluster-reported evidence for pods in a namespace -- phase, container
  readiness, restart counts, and the last termination reason/time where
  available. A container restart is a fact the cluster recorded; it does
  NOT by itself prove what caused the restart, and it does not prove
  anything about any other system's behavior at that time.
- `prometheus_query_range(query, start, end, step)`: how a monitored
  metric changed over a time window. A sample of `up == 0` means the
  Prometheus *scrape* of that target failed at that instant -- it does
  NOT by itself mean the application crashed, was restarted, or is
  otherwise unhealthy for any specific reason.

Rules you must follow:

- You may note that the pod's last container restart and the Prometheus
  scrape gap for {_INSTANCE} occurred in roughly the same time window --
  that is a **timeline correlation**, not proof of a shared cause or a
  specific causal direction.
- Do NOT claim the restart caused the scrape gap, that the scrape gap
  caused the restart, or that either one *is why* the other happened,
  unless the evidence actually establishes that direction (it does not
  here -- both are only known to have occurred around the same time).
- Only report information you actually retrieved via a tool call. Never
  invent pod names, namespaces, timestamps, or metric values.

Keep your answer concise: what Kubernetes reports about the pod, what
Prometheus shows over the monitoring window, and how (if at all) they
relate in time.
"""

_RUNTIME_TUNING = dict(
    system_prompt=_SYSTEM_PROMPT,
    agent_tools=[KUBERNETES_PODS_TOOL_NAME, PROMETHEUS_TOOL_NAME],
    tool_call_budget=2,
    temperature=0.1,
)

_PROMPT = (
    f"Pod {_POD_NAME} in namespace {_NAMESPACE} has restarted. Check its "
    f"Kubernetes status, then check Prometheus monitoring data for "
    f"{_INSTANCE} around the time of the restart, and summarize what "
    "happened."
)

_KNOWN_PODS = frozenset({_POD_NAME})
_POD_PATTERN = r"\bpayment-api-[a-z0-9-]+\b"

_RESTART_PATTERNS = [r"\brestart(ed|s)?\b", r"\bterminat(ed|ion)\b"]
_MONITORING_PATTERNS = [r"\bprometheus\b", r"\bmonitoring\b", r"\bscrape\b", r"\bup\b.{0,20}metric", r"\bmetric"]
_CORRELATION_PATTERNS = [
    r"\baround the same time\b",
    r"\bsame (time )?window\b",
    r"\bcorrelat",
    r"\bcoincide",
    r"\broughly (at|the same)\b",
]

_OVERCLAIM_DEFINITIVE_PATTERNS = (
    r"\bcaused\b",
    r"\bthe root cause is\b",
    r"\bdue to\b",
    r"\bresulted (in|from)\b",
    r"\bled to\b",
    r"\bis why\b",
    r"\bthe reason (is|was)\b",
    r"\bdefinitely\b",
    r"\bcertainly\b",
)

default_scenarios.register(
    Scenario(
        name="kubernetes-pod-restart-correlated-with-scrape-gap",
        version="1.0",
        description=(
            "kubernetes_list_pods (#18) shows payment-api-7f9c8d-abcde's "
            "container last terminated (reason=Error, exit_code=1) at "
            "03:14:00 UTC after 3 prior restarts. A Prometheus range "
            "query (#9) over roughly the same window shows "
            "up{instance='payment-api:8080'} drop to 0 for several "
            "samples and then recover. Golden behavior notes the "
            "temporal correlation between the restart and the scrape "
            "gap without claiming either one caused the other -- the "
            "evidence establishes a shared time window, not a causal "
            "direction."
        ),
        prompt=_PROMPT,
        build_registry=_combined_registry,
        expectations=[
            MustProduceFinalAnswer(),
            RequiredToolCall(KUBERNETES_PODS_TOOL_NAME, min_count=1, max_count=1),
            RequiredToolCall(PROMETHEUS_TOOL_NAME, min_count=1, max_count=1),
            RequiredAnswerPattern(name="cites_pod_restart", patterns=_RESTART_PATTERNS, match="any"),
            RequiredAnswerPattern(
                name="cites_monitoring_evidence", patterns=_MONITORING_PATTERNS, match="any"
            ),
            RequiredAnswerPattern(
                name="notes_temporal_correlation", patterns=_CORRELATION_PATTERNS, match="any", hard=False
            ),
            UnsupportedDefinitiveClaim(
                name="unsupported_causal_claim",
                subject_patterns=["restart", "scrape"],
                definitive_patterns=_OVERCLAIM_DEFINITIVE_PATTERNS,
            ),
            HypothesisLabeled(
                name="causal_relationship_labeled_as_possibility",
                subject_patterns=["restart", "scrape"],
            ),
            ForbiddenAnswerPattern(
                name="does_not_claim_restart_caused_the_gap",
                patterns=[r"\brestart(ed)?\b.{0,40}\bcaused\b", r"\bcaused\b.{0,40}\bscrape\b"],
                reason="the fixture only establishes a shared time window, never a causal direction",
                hard=True,
            ),
            NoUnexpectedEntities(
                known_hosts=_KNOWN_PODS, host_pattern=_POD_PATTERN
            ),
            MaxToolCalls(2, name="no_duplicate_calls"),
            MaxIterations(3),
        ],
        **_RUNTIME_TUNING,
    )
)
