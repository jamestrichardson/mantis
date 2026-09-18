"""Kubernetes API client (#18): auth/config/access/error translation only.

This module owns everything about *talking to* a Kubernetes API server —
building an authenticated client from :class:`~mantis.config.KubernetesConfig`
(kubeconfig-file or in-cluster service-account auth), issuing bounded list
reads, and classifying failures through the shared reliability contract
(``mantis.reliability`` — see ``docs/reliability.md``). It has no knowledge
of agents, LLMs, tool schemas, or which fields of a Pod/Deployment/Node/
Event actually matter to a model — see ``mantis.tools.kubernetes`` for the
semantic, LLM-facing layer built on top of this client.

Uses the official Kubernetes Python client library (``kubernetes`` on
PyPI) exclusively — never a ``kubectl`` subprocess, and never any
mutating call. Only four read-only list operations are ever issued:
pods, deployments, nodes, events (see :class:`KubernetesClient`).

Every list call goes through :func:`~mantis.reliability.retry_call` with
the shared retry policy — safe here because every call this client makes
is a read — using the exact same ``AWXClient``/``PrometheusClient``/
``LokiClient``-established pattern (see those modules). No second retry
helper, timeout config family, or breaker abstraction was introduced for
Kubernetes.

Client construction (:meth:`KubernetesClient.from_config`) is the one
narrow factory path every semantic tool goes through — a tool handler
never loads a kubeconfig or picks an auth mode itself, and never knows
which auth mode is actually in use (see ``mantis.config.KubernetesConfig``
and ``docs/kubernetes.md``'s "Configuration and authentication" section).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import urllib3
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from mantis.config import KubernetesConfig, ReliabilityConfig
from mantis.observability.logging import log_event
from mantis.reliability import (
    Deadline,
    IntegrationError,
    IntegrationErrorKind,
    RetryPolicy,
    classify_http_status,
    retry_call,
)

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "kubernetes"


class KubernetesError(IntegrationError):
    """Raised for a transport/API-level Kubernetes failure.

    Subclasses the shared :class:`~mantis.reliability.IntegrationError`
    so ``AgentRuntime`` can classify, retry-budget-account, and
    short-circuit on it generically — the runtime never needs to import
    this class. Never raised for the observed state of a Pod/Deployment/
    Node itself (an unhealthy pod is evidence a list call successfully
    retrieved, not a :class:`KubernetesError`) — see ``mantis.tools.kubernetes``.

    The message never includes an API response body, headers, or the
    configured API-server URL/kubeconfig path — only a short, fixed
    action description and the classified status/reason, matching every
    other Mantis integration's diagnostic-message convention.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: IntegrationErrorKind = IntegrationErrorKind.UNKNOWN,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message,
            kind=kind,
            source_system=SOURCE_SYSTEM,
            status_code=status_code,
            retry_after=retry_after,
        )


def _parse_retry_after(exc: ApiException) -> float | None:
    """Best-effort parse of a ``Retry-After`` response header's simple
    integer-seconds form, mirroring ``mantis.integrations.loki``'s
    convention. ``exc.headers`` is ``None`` unless the exception was
    built from a real HTTP response."""
    if not exc.headers:
        return None
    raw = exc.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def classify_k8s_exception(exc: Exception) -> IntegrationErrorKind:
    """Map a Kubernetes client-library exception onto
    :class:`~mantis.reliability.IntegrationErrorKind`.

    Two distinct exception families are possible:

    - :class:`~kubernetes.client.exceptions.ApiException` (and its
      status-specific subclasses, e.g. ``UnauthorizedException``,
      ``NotFoundException``) — the API server actually responded with a
      non-2xx status. Classified by reusing
      :func:`~mantis.reliability.classify_http_status` on ``exc.status``
      — the exact same status-code taxonomy every HTTP-based Mantis
      integration uses, never a second one invented for Kubernetes.
    - A ``urllib3`` transport-level exception, raised before any HTTP
      response was received at all (DNS failure, connection refused,
      TLS handshake failure, connect/read timeout) — the Kubernetes
      client library does not wrap these in :class:`ApiException`, they
      propagate as plain ``urllib3.exceptions.HTTPError`` subclasses.
    """
    if isinstance(exc, ApiException):
        if exc.status:
            return classify_http_status(exc.status)
        # status=0 covers the one case the client library itself
        # constructs an ApiException without a real HTTP response (a
        # local TLS/SSL error raised while sending the request) — see
        # kubernetes.client.rest.RESTClientObject.request.
        return IntegrationErrorKind.CONNECTION
    if isinstance(exc, urllib3.exceptions.MaxRetryError):
        return _classify_urllib3_reason(exc.reason)
    return _classify_urllib3_reason(exc) if isinstance(exc, urllib3.exceptions.HTTPError) else IntegrationErrorKind.UNKNOWN


def _classify_urllib3_reason(reason: Exception) -> IntegrationErrorKind:
    """Classify the underlying transport exception (either a bare
    ``urllib3`` error, or the ``.reason`` wrapped inside a
    ``MaxRetryError``).

    ``urllib3.exceptions.NewConnectionError`` — "usually ECONNREFUSED"
    per its own docstring — is checked *before* the generic
    ``TimeoutError`` check below, even though it is (surprisingly)
    itself a ``ConnectTimeoutError``/``TimeoutError`` subclass in
    urllib3's own exception hierarchy. Without this ordering, an
    actively refused/unreachable connection would be misreported as
    ``timeout`` — a real urllib3 API quirk, not a Mantis classification
    bug, and deliberately worked around here rather than propagated.
    """
    if isinstance(reason, urllib3.exceptions.NewConnectionError):
        return IntegrationErrorKind.CONNECTION
    if isinstance(reason, urllib3.exceptions.TimeoutError):
        return IntegrationErrorKind.TIMEOUT
    return IntegrationErrorKind.CONNECTION


def _build_api_client(config: KubernetesConfig) -> k8s_client.ApiClient:
    """Build an authenticated :class:`~kubernetes.client.ApiClient` from
    ``config`` — the one place Mantis ever decides between kubeconfig
    and in-cluster auth. Never called from a semantic tool directly (see
    :meth:`KubernetesClient.from_config`).

    Uses ``kubernetes.config.new_client_from_config`` for the
    ``kubeconfig`` mode, which builds a fresh, self-contained
    :class:`~kubernetes.client.Configuration`/``ApiClient`` pair rather
    than mutating any process-global default configuration — so
    constructing one :class:`KubernetesClient` can never leak auth state
    into another (e.g. across concurrent evaluation scenarios).
    """
    if config.auth_mode == "in_cluster":
        configuration = k8s_client.Configuration()
        k8s_config.load_incluster_config(client_configuration=configuration)
        configuration.verify_ssl = config.verify_ssl
        return k8s_client.ApiClient(configuration=configuration)

    api_client = k8s_config.new_client_from_config(
        config_file=config.kubeconfig_path,
        context=config.context,
    )
    api_client.configuration.verify_ssl = config.verify_ssl
    return api_client


class _CoreV1Like(Protocol):
    """Structural shape :class:`KubernetesClient` needs from
    ``CoreV1Api`` — deliberately narrow (only the four read calls this
    integration ever issues), so evaluation fixtures can supply a
    lightweight stand-in without depending on the real SDK class."""

    def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> Any: ...

    def list_node(self, **kwargs: Any) -> Any: ...

    def list_namespaced_event(self, namespace: str, **kwargs: Any) -> Any: ...


class _AppsV1Like(Protocol):
    """Structural shape needed from ``AppsV1Api`` — see :class:`_CoreV1Like`."""

    def list_namespaced_deployment(self, namespace: str, **kwargs: Any) -> Any: ...


@dataclass
class KubernetesClient:
    """Thin client for the four read-only Kubernetes list operations
    this integration supports: pods, deployments, nodes, events.

    Never issues a write/patch/delete/exec/attach/port-forward call —
    there is no method on this class that could (see #18's non-goals).
    Every request uses an explicit connect/read timeout
    (``reliability.http_connect_timeout_seconds`` /
    ``.http_read_timeout_seconds``), further capped at whatever remains
    of a caller-supplied :class:`~mantis.reliability.Deadline`, and goes
    through :func:`~mantis.reliability.retry_call` with ``reliability``'s
    retry policy.
    """

    core_v1: _CoreV1Like
    apps_v1: _AppsV1Like
    config: KubernetesConfig
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig.from_env)
    sleep: Callable[[float], None] = time.sleep
    """Injectable backoff-sleep hook, matching every other Mantis
    integration client's test-injection convention."""

    @classmethod
    def from_config(
        cls,
        config: KubernetesConfig,
        *,
        reliability_config: ReliabilityConfig | None = None,
    ) -> "KubernetesClient":
        """The one narrow construction path every semantic tool uses —
        see ``mantis.tools.kubernetes._get_client``. Never called more
        than once per tool invocation's worth of work; a tool handler
        never loads a kubeconfig or decides an auth mode itself.

        Client/auth construction (an unreadable or invalid kubeconfig, a
        named context that doesn't exist, in-cluster service-account
        files not mounted, ...) is just as much a classified integration
        failure as a failed API call, not a bug that should escape to
        ``AgentRuntime``'s generic last-resort exception handling. Any
        failure here is translated into a :class:`KubernetesError`
        (``kind=IntegrationErrorKind.AUTHENTICATION`` — a broken/missing
        credential source, not a transient network condition, so it is
        never retried) carrying only a fixed diagnostic plus the
        underlying exception's *type* name — never ``str(exc)``, which
        for the Kubernetes config-loading library can itself contain the
        kubeconfig path or other configuration detail (e.g. "Invalid
        kube-config file. Expected object with name X in /path/to/file
        list").
        """
        try:
            api_client = _build_api_client(config)
        except Exception as exc:
            raise KubernetesError(
                f"Failed to construct an authenticated Kubernetes client "
                f"({type(exc).__name__}); check the configured kubeconfig "
                "path/context, or that in-cluster service-account files are "
                "mounted correctly.",
                kind=IntegrationErrorKind.AUTHENTICATION,
            ) from exc
        return cls(
            core_v1=k8s_client.CoreV1Api(api_client),
            apps_v1=k8s_client.AppsV1Api(api_client),
            config=config,
            reliability=reliability_config or ReliabilityConfig.from_env(),
        )

    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self.reliability.retry_max_attempts,
            backoff_base_seconds=self.reliability.retry_backoff_base_seconds,
            backoff_cap_seconds=self.reliability.retry_backoff_cap_seconds,
        )

    def _request_timeout(self, *, deadline: Deadline | None) -> tuple[float, float]:
        connect = self.reliability.http_connect_timeout_seconds
        read = self.reliability.http_read_timeout_seconds
        if deadline is not None:
            remaining = deadline.remaining()
            connect = min(connect, remaining)
            read = min(read, remaining)
        return (connect, read)

    def _call(
        self,
        fn: Callable[[], Any],
        *,
        action: str,
        deadline: Deadline | None,
    ) -> Any:
        """Shared retry-wrapped call path for every list operation.

        ``action`` is a short, fixed, human-readable description used
        only in the bounded diagnostic message — never the namespace,
        selector, or any other model-supplied value, and never the
        response body/headers/API-server URL (see :class:`KubernetesError`).
        """

        def attempt() -> Any:
            try:
                return fn()
            except ApiException as exc:
                kind = classify_k8s_exception(exc)
                raise KubernetesError(
                    f"Failed to {action}: HTTP {exc.status} {exc.reason}",
                    kind=kind,
                    status_code=exc.status or None,
                    retry_after=(_parse_retry_after(exc) if kind == IntegrationErrorKind.RATE_LIMIT else None),
                ) from exc
            except urllib3.exceptions.HTTPError as exc:
                kind = classify_k8s_exception(exc)
                raise KubernetesError(f"Failed to {action}: {type(exc).__name__}", kind=kind) from exc

        def on_attempt(attempt_number: int, error: IntegrationError | None) -> None:
            if error is None:
                return
            will_retry = error.retryable and attempt_number < self.reliability.retry_max_attempts
            log_event(
                logger,
                "mantis_integration_retry",
                level=logging.INFO if will_retry else logging.WARNING,
                source_system=SOURCE_SYSTEM,
                action=action,
                attempt=attempt_number,
                max_attempts=self.reliability.retry_max_attempts,
                error_kind=error.kind.value,
                will_retry=will_retry,
            )

        return retry_call(
            attempt,
            policy=self._retry_policy(),
            deadline=deadline,
            source_system=SOURCE_SYSTEM,
            sleep=self.sleep,
            on_attempt=on_attempt,
        )

    def list_pods(
        self,
        namespace: str,
        *,
        label_selector: str | None,
        limit: int,
        deadline: Deadline | None = None,
    ) -> Any:
        """List pods in ``namespace`` (``GET /api/v1/namespaces/{namespace}/pods``).

        ``namespace``/``label_selector`` must already be validated (see
        ``mantis.tools.kubernetes``) — this client trusts its arguments
        and performs no shell/subprocess work of any kind. ``limit`` is
        the raw request-limit sent to the API server (typically one more
        than the tool-facing cap — see ``mantis.tools.kubernetes``'s
        sentinel-limit convention, mirroring #10's ``LOKI_REQUEST_LIMIT``).
        """
        connect, read = self._request_timeout(deadline=deadline)
        return self._call(
            lambda: self.core_v1.list_namespaced_pod(
                namespace,
                label_selector=label_selector,
                limit=limit,
                watch=False,
                _request_timeout=(connect, read),
            ),
            action="list pods",
            deadline=deadline,
        )

    def list_deployments(
        self,
        namespace: str,
        *,
        label_selector: str | None,
        limit: int,
        deadline: Deadline | None = None,
    ) -> Any:
        """List Deployments in ``namespace``
        (``GET /apis/apps/v1/namespaces/{namespace}/deployments``)."""
        connect, read = self._request_timeout(deadline=deadline)
        return self._call(
            lambda: self.apps_v1.list_namespaced_deployment(
                namespace,
                label_selector=label_selector,
                limit=limit,
                watch=False,
                _request_timeout=(connect, read),
            ),
            action="list deployments",
            deadline=deadline,
        )

    def list_nodes(
        self,
        *,
        label_selector: str | None,
        limit: int,
        deadline: Deadline | None = None,
    ) -> Any:
        """List cluster nodes (``GET /api/v1/nodes``) — cluster-scoped,
        no namespace."""
        connect, read = self._request_timeout(deadline=deadline)
        return self._call(
            lambda: self.core_v1.list_node(
                label_selector=label_selector,
                limit=limit,
                watch=False,
                _request_timeout=(connect, read),
            ),
            action="list nodes",
            deadline=deadline,
        )

    def list_events(
        self,
        namespace: str,
        *,
        field_selector: str | None,
        limit: int,
        deadline: Deadline | None = None,
    ) -> Any:
        """List Events in ``namespace``
        (``GET /api/v1/namespaces/{namespace}/events``).

        ``field_selector``, when given, is always built by
        ``mantis.tools.kubernetes`` from validated ``name``/``kind``
        parameters (e.g. ``involvedObject.name=...``) — this client
        never accepts a raw, model-supplied field selector string; see
        #18's input-safety requirements.
        """
        connect, read = self._request_timeout(deadline=deadline)
        return self._call(
            lambda: self.core_v1.list_namespaced_event(
                namespace,
                field_selector=field_selector,
                limit=limit,
                watch=False,
                _request_timeout=(connect, read),
            ),
            action="list events",
            deadline=deadline,
        )
