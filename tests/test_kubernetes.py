"""Tests for the Kubernetes integration client (mantis.integrations.kubernetes):
auth/config construction, #15 reliability reuse, and error classification.

No live cluster/network access required anywhere in this file: every test
either exercises pure classification/config logic or injects a fake
``core_v1``/``apps_v1`` object satisfying the narrow structural shape
:class:`KubernetesClient` actually needs. No test sleeps in real time
(retries use an injected no-op sleep).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import urllib3
from kubernetes.client import V1ListMeta, V1NodeList, V1PodList
from kubernetes.client.exceptions import ApiException

from mantis.config import ConfigurationError, KubernetesConfig
from mantis.integrations.kubernetes import (
    KubernetesClient,
    KubernetesError,
    classify_k8s_exception,
)
from mantis.reliability import Deadline, DeadlineExceededError, IntegrationErrorKind


def _kubeconfig_config(**overrides: Any) -> KubernetesConfig:
    defaults: dict[str, Any] = dict(
        auth_mode="kubeconfig",
        kubeconfig_path="/tmp/kubeconfig",
        context="home",
        cluster_name="home-k3s",
        verify_ssl=True,
    )
    defaults.update(overrides)
    return KubernetesConfig(**defaults)


@dataclass
class _FakeCoreV1:
    """A fake satisfying exactly the structural shape KubernetesClient
    needs from CoreV1Api -- never the real SDK class, no network access."""

    pods_response: Any = None
    nodes_response: Any = None
    events_response: Any = None
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def list_namespaced_pod(self, namespace: str, **kwargs: Any) -> Any:
        self.calls.append("list_namespaced_pod")
        if self.error is not None:
            raise self.error
        return self.pods_response

    def list_node(self, **kwargs: Any) -> Any:
        self.calls.append("list_node")
        if self.error is not None:
            raise self.error
        return self.nodes_response

    def list_namespaced_event(self, namespace: str, **kwargs: Any) -> Any:
        self.calls.append("list_namespaced_event")
        if self.error is not None:
            raise self.error
        return self.events_response


@dataclass
class _FakeAppsV1:
    deployments_response: Any = None
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def list_namespaced_deployment(self, namespace: str, **kwargs: Any) -> Any:
        self.calls.append("list_namespaced_deployment")
        if self.error is not None:
            raise self.error
        return self.deployments_response


def _client(core_v1: _FakeCoreV1, apps_v1: _FakeAppsV1 | None = None, **kwargs: Any) -> KubernetesClient:
    return KubernetesClient(
        core_v1=core_v1,
        apps_v1=apps_v1 or _FakeAppsV1(),
        config=_kubeconfig_config(),
        sleep=lambda *_: None,
        **kwargs,
    )


def _api_exception(status: int, reason: str = "reason", headers: dict | None = None) -> ApiException:
    exc = ApiException(status=status, reason=reason)
    exc.headers = headers
    exc.body = None
    return exc


# ---------------------------------------------------------------------------
# KubernetesConfig (#18) -- construction/validation only, no networking
# ---------------------------------------------------------------------------


def test_kubeconfig_mode_requires_kubeconfig_path():
    with pytest.raises(ConfigurationError, match="MANTIS_KUBERNETES_KUBECONFIG"):
        KubernetesConfig(
            auth_mode="kubeconfig", kubeconfig_path=None, context=None, cluster_name="home-k3s"
        )


def test_kubeconfig_mode_allows_missing_context():
    cfg = KubernetesConfig(
        auth_mode="kubeconfig", kubeconfig_path="/tmp/kubeconfig", context=None, cluster_name="home-k3s"
    )
    assert cfg.context is None


def test_in_cluster_mode_rejects_kubeconfig_path():
    with pytest.raises(ConfigurationError, match="in_cluster"):
        KubernetesConfig(
            auth_mode="in_cluster", kubeconfig_path="/tmp/kubeconfig", context=None, cluster_name="home-k3s"
        )


def test_in_cluster_mode_rejects_context():
    with pytest.raises(ConfigurationError, match="in_cluster"):
        KubernetesConfig(auth_mode="in_cluster", kubeconfig_path=None, context="home", cluster_name="home-k3s")


def test_in_cluster_mode_with_no_kubeconfig_or_context_is_valid():
    cfg = KubernetesConfig(auth_mode="in_cluster", kubeconfig_path=None, context=None, cluster_name="home-k3s")
    assert cfg.auth_mode == "in_cluster"


def test_rejects_unknown_auth_mode():
    with pytest.raises(ConfigurationError, match="MANTIS_KUBERNETES_AUTH_MODE"):
        KubernetesConfig(auth_mode="token", kubeconfig_path=None, context=None, cluster_name="home-k3s")


def test_requires_cluster_name():
    with pytest.raises(ConfigurationError, match="MANTIS_KUBERNETES_CLUSTER_NAME"):
        KubernetesConfig(auth_mode="in_cluster", kubeconfig_path=None, context=None, cluster_name="")


def test_verify_ssl_defaults_true():
    cfg = KubernetesConfig(auth_mode="in_cluster", kubeconfig_path=None, context=None, cluster_name="x")
    assert cfg.verify_ssl is True


@pytest.mark.parametrize(
    "missing_var", ["MANTIS_KUBERNETES_AUTH_MODE", "MANTIS_KUBERNETES_CLUSTER_NAME"]
)
def test_from_env_raises_on_missing_required_variable(monkeypatch, missing_var):
    monkeypatch.setenv("MANTIS_KUBERNETES_AUTH_MODE", "in_cluster")
    monkeypatch.setenv("MANTIS_KUBERNETES_CLUSTER_NAME", "home-k3s")
    monkeypatch.delenv(missing_var, raising=False)
    with pytest.raises(ConfigurationError, match=missing_var):
        KubernetesConfig.from_env()


def test_from_env_kubeconfig_mode(monkeypatch):
    monkeypatch.setenv("MANTIS_KUBERNETES_AUTH_MODE", "kubeconfig")
    monkeypatch.setenv("MANTIS_KUBERNETES_KUBECONFIG", "/home/user/.kube/config")
    monkeypatch.setenv("MANTIS_KUBERNETES_CONTEXT", "home")
    monkeypatch.setenv("MANTIS_KUBERNETES_CLUSTER_NAME", "home-k3s")
    monkeypatch.delenv("MANTIS_KUBERNETES_VERIFY_SSL", raising=False)

    cfg = KubernetesConfig.from_env()

    assert cfg.auth_mode == "kubeconfig"
    assert cfg.kubeconfig_path == "/home/user/.kube/config"
    assert cfg.context == "home"
    assert cfg.cluster_name == "home-k3s"
    assert cfg.verify_ssl is True


def test_from_env_in_cluster_mode(monkeypatch):
    monkeypatch.setenv("MANTIS_KUBERNETES_AUTH_MODE", "in_cluster")
    monkeypatch.delenv("MANTIS_KUBERNETES_KUBECONFIG", raising=False)
    monkeypatch.delenv("MANTIS_KUBERNETES_CONTEXT", raising=False)
    monkeypatch.setenv("MANTIS_KUBERNETES_CLUSTER_NAME", "prod-eks")
    monkeypatch.setenv("MANTIS_KUBERNETES_VERIFY_SSL", "false")

    cfg = KubernetesConfig.from_env()

    assert cfg.auth_mode == "in_cluster"
    assert cfg.kubeconfig_path is None
    assert cfg.context is None
    assert cfg.verify_ssl is False


def test_from_env_never_opens_or_networks(monkeypatch, tmp_path):
    """from_env() must be pure config parsing -- pointing it at a
    kubeconfig path that doesn't exist must not raise, since actually
    opening/validating that file is a separate concern (client
    construction), not config parsing."""
    monkeypatch.setenv("MANTIS_KUBERNETES_AUTH_MODE", "kubeconfig")
    monkeypatch.setenv("MANTIS_KUBERNETES_KUBECONFIG", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("MANTIS_KUBERNETES_CLUSTER_NAME", "home-k3s")

    cfg = KubernetesConfig.from_env()  # must not raise

    assert cfg.kubeconfig_path == str(tmp_path / "does-not-exist")


def test_config_repr_and_str_never_crash_or_need_special_handling():
    # KubernetesConfig carries no Secret field at all -- credential
    # material lives inside the kubeconfig file/mounted service-account
    # token, never in this dataclass -- so there is nothing to redact,
    # but repr()/str() must still work normally.
    cfg = _kubeconfig_config()
    assert "home-k3s" in repr(cfg)


# ---------------------------------------------------------------------------
# classify_k8s_exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, IntegrationErrorKind.AUTHENTICATION),
        (403, IntegrationErrorKind.AUTHORIZATION),
        (404, IntegrationErrorKind.NOT_FOUND),
        (429, IntegrationErrorKind.RATE_LIMIT),
        (400, IntegrationErrorKind.BAD_REQUEST),
        (500, IntegrationErrorKind.SERVER_ERROR),
        (503, IntegrationErrorKind.SERVER_ERROR),
    ],
)
def test_classify_api_exception_status_codes(status, expected):
    assert classify_k8s_exception(_api_exception(status)) == expected


def test_classify_api_exception_zero_status_is_connection():
    assert classify_k8s_exception(_api_exception(0)) == IntegrationErrorKind.CONNECTION


def test_classify_connect_timeout_is_timeout():
    reason = urllib3.exceptions.ConnectTimeoutError("connect timed out")
    exc = urllib3.exceptions.MaxRetryError(pool=None, url="/api/v1/pods", reason=reason)
    assert classify_k8s_exception(exc) == IntegrationErrorKind.TIMEOUT


def test_classify_connection_refused_is_connection():
    reason = urllib3.exceptions.NewConnectionError(None, "Connection refused")
    exc = urllib3.exceptions.MaxRetryError(pool=None, url="/api/v1/pods", reason=reason)
    assert classify_k8s_exception(exc) == IntegrationErrorKind.CONNECTION


def test_classify_unknown_exception_is_unknown():
    assert classify_k8s_exception(ValueError("something else")) == IntegrationErrorKind.UNKNOWN


# ---------------------------------------------------------------------------
# KubernetesClient -- retry/deadline reuse (#15), zero API calls on an
# already-expired deadline, and every list method reaching the right
# fake API method.
# ---------------------------------------------------------------------------


def test_list_pods_success_returns_raw_typed_response():
    response = V1PodList(items=[], metadata=V1ListMeta())
    core_v1 = _FakeCoreV1(pods_response=response)
    client = _client(core_v1)

    result = client.list_pods("prod", label_selector=None, limit=51)

    assert result is response
    assert core_v1.calls == ["list_namespaced_pod"]


def test_list_deployments_reaches_apps_v1():
    apps_v1 = _FakeAppsV1(deployments_response="sentinel")
    client = _client(_FakeCoreV1(), apps_v1)

    result = client.list_deployments("prod", label_selector=None, limit=51)

    assert result == "sentinel"
    assert apps_v1.calls == ["list_namespaced_deployment"]


def test_list_nodes_reaches_core_v1_list_node():
    core_v1 = _FakeCoreV1(nodes_response=V1NodeList(items=[], metadata=V1ListMeta()))
    client = _client(core_v1)

    client.list_nodes(label_selector=None, limit=51)

    assert core_v1.calls == ["list_node"]


def test_list_events_reaches_core_v1_list_namespaced_event():
    core_v1 = _FakeCoreV1(events_response="sentinel")
    client = _client(core_v1)

    result = client.list_events("prod", field_selector="involvedObject.name=foo", limit=51)

    assert result == "sentinel"
    assert core_v1.calls == ["list_namespaced_event"]


@pytest.mark.parametrize(
    "status,expected_kind",
    [
        (401, IntegrationErrorKind.AUTHENTICATION),
        (403, IntegrationErrorKind.AUTHORIZATION),
        (404, IntegrationErrorKind.NOT_FOUND),
        (500, IntegrationErrorKind.SERVER_ERROR),
    ],
)
def test_list_pods_raises_classified_kubernetes_error(status, expected_kind):
    core_v1 = _FakeCoreV1(error=_api_exception(status))
    client = _client(core_v1)

    with pytest.raises(KubernetesError) as exc_info:
        client.list_pods("prod", label_selector=None, limit=51)

    assert exc_info.value.kind == expected_kind
    assert exc_info.value.source_system == "kubernetes"


def test_connection_failure_is_classified_connection():
    reason = urllib3.exceptions.NewConnectionError(None, "Connection refused")
    error = urllib3.exceptions.MaxRetryError(pool=None, url="/api/v1/namespaces/prod/pods", reason=reason)
    core_v1 = _FakeCoreV1(error=error)
    client = _client(core_v1)

    with pytest.raises(KubernetesError) as exc_info:
        client.list_pods("prod", label_selector=None, limit=51)

    assert exc_info.value.kind == IntegrationErrorKind.CONNECTION


def test_timeout_failure_is_classified_timeout():
    reason = urllib3.exceptions.ConnectTimeoutError("connect timed out")
    error = urllib3.exceptions.MaxRetryError(pool=None, url="/api/v1/namespaces/prod/pods", reason=reason)
    core_v1 = _FakeCoreV1(error=error)
    client = _client(core_v1)

    with pytest.raises(KubernetesError) as exc_info:
        client.list_pods("prod", label_selector=None, limit=51)

    assert exc_info.value.kind == IntegrationErrorKind.TIMEOUT


def test_not_found_is_never_retried():
    core_v1 = _FakeCoreV1(error=_api_exception(404))
    client = _client(core_v1)

    with pytest.raises(KubernetesError):
        client.list_pods("prod", label_selector=None, limit=51)

    assert core_v1.calls == ["list_namespaced_pod"]  # exactly one attempt, no retry


def test_auth_failure_is_never_retried():
    core_v1 = _FakeCoreV1(error=_api_exception(401))
    client = _client(core_v1)

    with pytest.raises(KubernetesError):
        client.list_pods("prod", label_selector=None, limit=51)

    assert core_v1.calls == ["list_namespaced_pod"]


def test_server_error_is_retried_up_to_policy_max_attempts():
    from mantis.config import ReliabilityConfig

    core_v1 = _FakeCoreV1(error=_api_exception(503))
    client = _client(core_v1, reliability=ReliabilityConfig(retry_max_attempts=3))

    with pytest.raises(KubernetesError):
        client.list_pods("prod", label_selector=None, limit=51)

    assert core_v1.calls == ["list_namespaced_pod"] * 3


def test_expired_deadline_makes_zero_api_calls():
    core_v1 = _FakeCoreV1(pods_response=V1PodList(items=[], metadata=V1ListMeta()))
    client = _client(core_v1)
    expired = Deadline.after(-1.0)

    with pytest.raises(DeadlineExceededError):
        client.list_pods("prod", label_selector=None, limit=51, deadline=expired)

    assert core_v1.calls == []


def test_expired_deadline_makes_zero_api_calls_for_events():
    core_v1 = _FakeCoreV1(events_response="sentinel")
    client = _client(core_v1)
    expired = Deadline.after(-1.0)

    with pytest.raises(DeadlineExceededError):
        client.list_events("prod", field_selector=None, limit=51, deadline=expired)

    assert core_v1.calls == []


def test_kubernetes_error_message_never_includes_api_response_body():
    exc = _api_exception(500, reason="Internal error", headers={"Some-Header": "value"})
    exc.body = "super secret internal detail that must never leak"
    core_v1 = _FakeCoreV1(error=exc)
    client = _client(core_v1)

    with pytest.raises(KubernetesError) as exc_info:
        client.list_pods("prod", label_selector=None, limit=51)

    assert "super secret internal detail" not in str(exc_info.value)


def test_retry_after_parsed_from_rate_limit_response():
    core_v1 = _FakeCoreV1(error=_api_exception(429, headers={"Retry-After": "7"}))
    client = _client(core_v1)

    with pytest.raises(KubernetesError) as exc_info:
        client.list_pods("prod", label_selector=None, limit=51)

    assert exc_info.value.retry_after == 7.0


# ---------------------------------------------------------------------------
# KubernetesClient.from_config -- client/auth construction failures must
# be translated into a classified KubernetesError, never left to escape
# as a raw SDK exception through AgentRuntime's generic catch-all. Every
# case here touches only local files/env vars -- no real cluster or
# network access anywhere.
# ---------------------------------------------------------------------------


def test_from_config_raises_kubernetes_error_for_nonexistent_kubeconfig(tmp_path):
    config = _kubeconfig_config(kubeconfig_path=str(tmp_path / "does-not-exist" / "config"))

    with pytest.raises(KubernetesError) as exc_info:
        KubernetesClient.from_config(config)

    assert exc_info.value.kind == IntegrationErrorKind.AUTHENTICATION
    assert exc_info.value.source_system == "kubernetes"
    assert not exc_info.value.retryable


def test_from_config_raises_kubernetes_error_for_unreadable_kubeconfig(tmp_path):
    bad_file = tmp_path / "kubeconfig"
    bad_file.write_text("not: [valid, yaml, at all: :::")
    config = _kubeconfig_config(kubeconfig_path=str(bad_file))

    with pytest.raises(KubernetesError) as exc_info:
        KubernetesClient.from_config(config)

    assert exc_info.value.kind == IntegrationErrorKind.AUTHENTICATION


def _valid_kubeconfig(path, *, secret_token: str = "super-secret-token-value") -> None:
    path.write_text(
        f"""
apiVersion: v1
kind: Config
clusters:
  - cluster: {{server: https://example.test}}
    name: home
contexts:
  - context: {{cluster: home, user: home}}
    name: home
current-context: home
users:
  - name: home
    user: {{token: {secret_token}}}
"""
    )


def test_from_config_raises_kubernetes_error_for_nonexistent_context(tmp_path):
    kubeconfig_path = tmp_path / "kubeconfig"
    _valid_kubeconfig(kubeconfig_path)
    config = _kubeconfig_config(kubeconfig_path=str(kubeconfig_path), context="does-not-exist")

    with pytest.raises(KubernetesError) as exc_info:
        KubernetesClient.from_config(config)

    assert exc_info.value.kind == IntegrationErrorKind.AUTHENTICATION


def test_from_config_never_leaks_kubeconfig_path_or_secret_material(tmp_path):
    kubeconfig_path = tmp_path / "some-sensitive-directory" / "kubeconfig"
    kubeconfig_path.parent.mkdir()
    _valid_kubeconfig(kubeconfig_path, secret_token="definitely-a-secret-token-12345")
    config = _kubeconfig_config(kubeconfig_path=str(kubeconfig_path), context="does-not-exist")

    with pytest.raises(KubernetesError) as exc_info:
        KubernetesClient.from_config(config)

    message = str(exc_info.value)
    assert str(kubeconfig_path) not in message
    assert "some-sensitive-directory" not in message
    assert "definitely-a-secret-token-12345" not in message
    assert "does-not-exist" not in message  # the raw ConfigException text names the context


def test_from_config_raises_kubernetes_error_for_failed_in_cluster_load(monkeypatch):
    # No mounted service-account files/env vars in this test process --
    # load_incluster_config() fails exactly like it would on a real host
    # that isn't actually running inside a cluster.
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)
    config = KubernetesConfig(auth_mode="in_cluster", kubeconfig_path=None, context=None, cluster_name="prod-eks")

    with pytest.raises(KubernetesError) as exc_info:
        KubernetesClient.from_config(config)

    assert exc_info.value.kind == IntegrationErrorKind.AUTHENTICATION
    assert not exc_info.value.retryable


def test_from_config_failure_is_a_classified_integration_error_not_generic():
    from mantis.reliability import IntegrationError

    config = _kubeconfig_config(kubeconfig_path="/nonexistent/kubeconfig")

    try:
        KubernetesClient.from_config(config)
    except Exception as exc:
        assert isinstance(exc, IntegrationError)
        assert isinstance(exc, KubernetesError)
    else:
        pytest.fail("expected KubernetesClient.from_config to raise")
