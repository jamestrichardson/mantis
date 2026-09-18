"""Environment-driven configuration for Mantis.

Configuration is read entirely from process environment variables. Nothing
here should hardcode credentials, and no module outside this file should
call ``os.environ`` directly for Mantis settings.

Local development environment files
------------------------------------

In a real deployment (EKS/GKE/ECS/plain Docker, ...) environment variables
are injected by the platform itself — there are no ``.env`` files, and
none are needed. For local development, Mantis supports a
``.env.<name>``-style file convention (see ``docs/configuration.md``):

- ``MANTIS_ENV`` selects which local environment to load (default:
  ``"local"``).
- Files are loaded, most specific first, with ``override=False`` — so a
  real environment variable already set by the platform/shell always wins
  over anything in a file, and a more specific file wins over a less
  specific one:

  1. ``.env.<MANTIS_ENV>.local`` — personal, machine-specific overrides.
  2. ``.env.<MANTIS_ENV>`` — the environment's own settings (e.g.
     ``.env.local`` for day-to-day local dev).
  3. ``.env.local`` — legacy/catch-all personal overrides.
  4. ``.env`` — shared, environment-agnostic defaults.

Only ``*.example`` files are committed to git; real ``.env*`` files are
git-ignored and must never contain checked-in credentials.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MANTIS_ENV = "local"
DEFAULT_LITELLM_MODEL = "qwen3-opencode:latest"


def _load_env_files() -> None:
    env_name = os.environ.get("MANTIS_ENV", DEFAULT_MANTIS_ENV)
    candidates = [
        f".env.{env_name}.local",
        f".env.{env_name}",
        ".env.local",
        ".env",
    ]
    # De-duplicate while preserving precedence order (e.g. MANTIS_ENV=local
    # would otherwise load ".env.local" twice).
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate)
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)


_load_env_files()


def _getenv_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _getenv_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc


def _getenv_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc


def _check_positive(name: str, value: float) -> None:
    # math.isfinite() rejects both NaN and +/-inf explicitly — without it,
    # float("inf") silently passes a bare "value > 0" check (inf > 0 is
    # True), and float("nan") produces confusing downstream behavior in
    # random.uniform(), time.sleep(), and httpx.Timeout construction
    # rather than a clear startup error.
    if not math.isfinite(value) or not value > 0:
        raise ConfigurationError(f"{name} must be a finite number > 0, got {value!r}")


def _check_non_negative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ConfigurationError(f"{name} must be a finite number >= 0, got {value!r}")


def _check_at_least(name: str, value: int, minimum: int) -> None:
    if value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value!r}")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        # Deliberately names only the variable, never a value — this is
        # the only piece of information ConfigurationError ever carries,
        # so it can never leak a partially-set secret in exception text.
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _require_secret(name: str) -> "Secret":
    return Secret(_require(name))


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Secret:
    """Wraps a credential so it can't be accidentally logged or leaked.

    ``repr()``/``str()`` (and therefore f-strings, ``logging`` calls, and a
    dataclass's default ``__repr__``) never expose the wrapped value — only
    :meth:`get_secret_value` does, and callers should reach for it only at
    the point the raw credential is actually needed (e.g. building an
    ``Authorization`` header), never store or pass around the unwrapped
    string beyond that.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class LiteLLMConfig:
    """Connection settings for the LiteLLM (OpenAI-compatible) gateway."""

    url: str
    api_key: Secret
    model: str

    @classmethod
    def from_env(cls) -> "LiteLLMConfig":
        return cls(
            url=_require("LITELLM_URL"),
            api_key=_require_secret("LITELLM_API_KEY"),
            model=os.environ.get("LITELLM_MODEL") or DEFAULT_LITELLM_MODEL,
        )


@dataclass(frozen=True)
class AWXConfig:
    """Connection settings for AWX."""

    url: str
    token: Secret
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> "AWXConfig":
        return cls(
            url=_require("AWX_URL").rstrip("/"),
            token=_require_secret("AWX_TOKEN"),
            verify_ssl=_getenv_bool("AWX_VERIFY_SSL", True),
        )


@dataclass(frozen=True)
class PrometheusConfig:
    """Connection settings for Prometheus (#9).

    Authentication is optional: an unauthenticated Prometheus endpoint
    works with no further configuration. When both a bearer token and
    basic auth credentials are set, the bearer token takes priority
    (mirrors the more common Prometheus deployment pattern of a reverse
    proxy adding one or the other, not both). ``verify_ssl`` defaults to
    ``True`` — disabling TLS verification is possible (for a local/dev
    endpoint with a self-signed certificate) but must be opted into
    explicitly via ``MANTIS_PROMETHEUS_VERIFY_SSL=false``; doing so
    removes protection against a machine-in-the-middle intercepting or
    tampering with monitoring data in transit.
    """

    url: str
    bearer_token: Secret | None = None
    basic_auth_username: str | None = None
    basic_auth_password: Secret | None = None
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> "PrometheusConfig":
        bearer_token = os.environ.get("MANTIS_PROMETHEUS_BEARER_TOKEN")
        basic_auth_username = os.environ.get("MANTIS_PROMETHEUS_BASIC_AUTH_USERNAME")
        basic_auth_password = os.environ.get("MANTIS_PROMETHEUS_BASIC_AUTH_PASSWORD")
        return cls(
            url=_require("MANTIS_PROMETHEUS_URL").rstrip("/"),
            bearer_token=Secret(bearer_token) if bearer_token else None,
            basic_auth_username=basic_auth_username or None,
            basic_auth_password=Secret(basic_auth_password) if basic_auth_password else None,
            verify_ssl=_getenv_bool("MANTIS_PROMETHEUS_VERIFY_SSL", True),
        )


@dataclass(frozen=True)
class LokiConfig:
    """Connection settings for Loki (#10).

    Mirrors :class:`PrometheusConfig` exactly: authentication is
    optional (an unauthenticated Loki endpoint works with no further
    configuration), a bearer token takes priority over basic auth when
    both are set, and ``verify_ssl`` defaults to ``True`` (disabling TLS
    verification requires the explicit ``MANTIS_LOKI_VERIFY_SSL=false``
    opt-out, for the same machine-in-the-middle reasons documented on
    :class:`PrometheusConfig`).

    ``tenant_id``, when set, is sent as a static ``X-Scope-OrgID`` header
    on every request (Loki's multi-tenancy convention) — this is
    deployment configuration, never something the model selects or
    supplies per call (see ``docs/loki.md``'s "Tenant handling" section
    and #10's security requirements).
    """

    url: str
    bearer_token: Secret | None = None
    basic_auth_username: str | None = None
    basic_auth_password: Secret | None = None
    tenant_id: str | None = None
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> "LokiConfig":
        bearer_token = os.environ.get("MANTIS_LOKI_BEARER_TOKEN")
        basic_auth_username = os.environ.get("MANTIS_LOKI_BASIC_AUTH_USERNAME")
        basic_auth_password = os.environ.get("MANTIS_LOKI_BASIC_AUTH_PASSWORD")
        tenant_id = os.environ.get("MANTIS_LOKI_TENANT_ID")
        return cls(
            url=_require("MANTIS_LOKI_URL").rstrip("/"),
            bearer_token=Secret(bearer_token) if bearer_token else None,
            basic_auth_username=basic_auth_username or None,
            basic_auth_password=Secret(basic_auth_password) if basic_auth_password else None,
            tenant_id=tenant_id or None,
            verify_ssl=_getenv_bool("MANTIS_LOKI_VERIFY_SSL", True),
        )


_KUBERNETES_AUTH_MODES = ("kubeconfig", "in_cluster")


@dataclass(frozen=True)
class KubernetesConfig:
    """Connection settings for Kubernetes (#18).

    Deliberately mirrors every other Mantis integration config: a frozen
    dataclass with ``from_env()``, no eager networking/file access here
    (parsing configuration and actually connecting to a cluster are
    separate concerns — see ``mantis.integrations.kubernetes`` for the
    latter), and a single explicit ``auth_mode`` rather than guessing
    between several credentials based on whichever happens to be set.

    Unlike AWX/Prometheus/Loki, there is no ``Secret``-wrapped field
    here: in ``kubeconfig`` mode, credential material lives inside the
    kubeconfig file itself and is read directly by the Kubernetes client
    library, never by Mantis; in ``in_cluster`` mode, the client library
    reads the mounted service-account token/CA directly from the
    filesystem. Mantis never holds a raw Kubernetes credential value in
    memory on its own, so there is nothing here for ``Secret`` to wrap.

    ``kubeconfig_path``/``context`` are deployment configuration, never
    model/tool input — see ``mantis.tools.kubernetes`` and
    ``docs/kubernetes.md``'s "Configuration and authentication" section.
    """

    auth_mode: str
    kubeconfig_path: str | None
    context: str | None
    cluster_name: str
    verify_ssl: bool = True

    def __post_init__(self) -> None:
        if self.auth_mode not in _KUBERNETES_AUTH_MODES:
            raise ConfigurationError(
                f"MANTIS_KUBERNETES_AUTH_MODE must be one of {_KUBERNETES_AUTH_MODES}, "
                f"got {self.auth_mode!r}"
            )
        if self.auth_mode == "kubeconfig":
            if not self.kubeconfig_path:
                raise ConfigurationError(
                    "MANTIS_KUBERNETES_KUBECONFIG is required when "
                    "MANTIS_KUBERNETES_AUTH_MODE=kubeconfig"
                )
        else:
            # in_cluster: rejecting kubeconfig-only settings outright,
            # rather than silently ignoring them, avoids ambiguous
            # precedence between "which auth material actually wins" —
            # see #18's configuration requirements.
            if self.kubeconfig_path is not None or self.context is not None:
                raise ConfigurationError(
                    "MANTIS_KUBERNETES_KUBECONFIG/MANTIS_KUBERNETES_CONTEXT must not be set "
                    "when MANTIS_KUBERNETES_AUTH_MODE=in_cluster (ambiguous precedence)"
                )
        if not self.cluster_name:
            raise ConfigurationError("MANTIS_KUBERNETES_CLUSTER_NAME is required")

    @classmethod
    def from_env(cls) -> "KubernetesConfig":
        return cls(
            auth_mode=_require("MANTIS_KUBERNETES_AUTH_MODE"),
            kubeconfig_path=os.environ.get("MANTIS_KUBERNETES_KUBECONFIG") or None,
            context=os.environ.get("MANTIS_KUBERNETES_CONTEXT") or None,
            cluster_name=_require("MANTIS_KUBERNETES_CLUSTER_NAME"),
            verify_ssl=_getenv_bool("MANTIS_KUBERNETES_VERIFY_SSL", True),
        )


_API_AUTH_MODES = ("bearer_token", "disabled")
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8080
DEFAULT_API_MAX_CONCURRENT_RUNS = 4
DEFAULT_API_SHUTDOWN_GRACE_PERIOD_SECONDS = 30.0


@dataclass(frozen=True)
class ApiServerConfig:
    """Server-side configuration for the Mantis FastAPI service (#21/#83)
    — see ``mantis.api`` and ``docs/api.md``.

    This is deliberately the *only* place ``MANTIS_API_*`` server
    settings are read from the environment; ``mantis.api`` modules
    accept this config object rather than reading ``os.environ``
    themselves.

    ``auth_mode`` defaults to ``"bearer_token"`` — the secure mode is
    what you get by doing nothing, never the reverse. Disabling auth
    (``auth_mode="disabled"``) requires an explicit, logged opt-in (see
    ``mantis.api.auth``); it is never silently selected just because
    ``MANTIS_API_TOKEN`` happens to be unset.
    """

    host: str = DEFAULT_API_HOST
    port: int = DEFAULT_API_PORT
    auth_mode: str = "bearer_token"
    bearer_token: Secret | None = None
    max_concurrent_runs: int = DEFAULT_API_MAX_CONCURRENT_RUNS
    shutdown_grace_period_seconds: float = DEFAULT_API_SHUTDOWN_GRACE_PERIOD_SECONDS

    def __post_init__(self) -> None:
        if self.auth_mode not in _API_AUTH_MODES:
            raise ConfigurationError(
                f"MANTIS_API_AUTH_MODE must be one of {_API_AUTH_MODES}, got {self.auth_mode!r}"
            )
        if self.auth_mode == "bearer_token" and self.bearer_token is None:
            raise ConfigurationError(
                "MANTIS_API_TOKEN is required when MANTIS_API_AUTH_MODE=bearer_token "
                "(the default) -- set MANTIS_API_AUTH_MODE=disabled explicitly for an "
                "unauthenticated local-development mode instead of leaving the token unset"
            )
        # 0 is deliberately valid: "bind an OS-assigned ephemeral port,"
        # the standard convention tests use to avoid a fixed-port clash
        # (see tests/test_api_server.py) -- never used in production
        # configuration, but not this dataclass's job to forbid a
        # legitimate socket-binding convention.
        _check_at_least("port", self.port, 0)
        if self.port > 65535:
            raise ConfigurationError(f"port must be <= 65535, got {self.port!r}")
        _check_at_least("max_concurrent_runs", self.max_concurrent_runs, 1)
        _check_positive("shutdown_grace_period_seconds", self.shutdown_grace_period_seconds)

    @classmethod
    def from_env(cls) -> "ApiServerConfig":
        token = os.environ.get("MANTIS_API_TOKEN")
        return cls(
            host=os.environ.get("MANTIS_API_HOST", DEFAULT_API_HOST),
            port=_getenv_int("MANTIS_API_PORT", DEFAULT_API_PORT),
            auth_mode=os.environ.get("MANTIS_API_AUTH_MODE", "bearer_token"),
            bearer_token=Secret(token) if token else None,
            max_concurrent_runs=_getenv_int(
                "MANTIS_API_MAX_CONCURRENT_RUNS", DEFAULT_API_MAX_CONCURRENT_RUNS
            ),
            shutdown_grace_period_seconds=_getenv_float(
                "MANTIS_API_SHUTDOWN_GRACE_PERIOD_SECONDS", DEFAULT_API_SHUTDOWN_GRACE_PERIOD_SECONDS
            ),
        )


@dataclass(frozen=True)
class ApiClientConfig:
    """Client-side configuration for talking to the Mantis API (#83) —
    used by the ``mantis`` CLI, never by server-side code.

    Deliberately holds only the Mantis API's own base URL/token/timeouts
    — never LiteLLM/AWX/Kubernetes/Prometheus/Loki credentials. A CLI
    invoking Mantis through the API needs none of those (see
    ``docs/api.md``'s security-boundary section).
    """

    base_url: str
    token: Secret | None = None
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ConfigurationError("MANTIS_API_URL must not be empty")
        _check_positive("connect_timeout_seconds", self.connect_timeout_seconds)
        _check_positive("read_timeout_seconds", self.read_timeout_seconds)

    @classmethod
    def from_env(cls) -> "ApiClientConfig":
        base_url = os.environ.get("MANTIS_API_URL", "http://localhost:8080")
        token = os.environ.get("MANTIS_API_TOKEN")
        return cls(
            base_url=base_url.rstrip("/"),
            token=Secret(token) if token else None,
            connect_timeout_seconds=_getenv_float("MANTIS_API_CLIENT_CONNECT_TIMEOUT_SECONDS", 5.0),
            read_timeout_seconds=_getenv_float("MANTIS_API_CLIENT_READ_TIMEOUT_SECONDS", 300.0),
        )


@dataclass(frozen=True)
class ReliabilityConfig:
    """The shared reliability contract's configurable knobs (see
    ``mantis.reliability`` and ``docs/reliability.md``) — one shared
    config for every integration/tool, not one set of magic numbers per
    integration. Every value has a named default; nothing here needs to
    be set to get sensible, documented behavior.
    """

    http_connect_timeout_seconds: float = 5.0
    http_read_timeout_seconds: float = 25.0
    retry_max_attempts: int = 3
    retry_backoff_base_seconds: float = 0.5
    retry_backoff_cap_seconds: float = 8.0
    tool_timeout_seconds: float = 45.0
    run_timeout_seconds: float = 300.0
    short_circuit_threshold: int = 3

    def __post_init__(self) -> None:
        # Range validation, not just type validation — a value that
        # parses fine (0, -5, ...) but is nonsensical must still fail
        # loudly at startup/construction rather than surface later as an
        # internal assertion failure deep in mantis.reliability.retry_call
        # (e.g. retry_max_attempts=0 skips its loop entirely with no
        # error ever raised). Applies to direct construction too, not
        # only .from_env(), since __post_init__ runs either way.
        _check_positive("http_connect_timeout_seconds", self.http_connect_timeout_seconds)
        _check_positive("http_read_timeout_seconds", self.http_read_timeout_seconds)
        _check_at_least("retry_max_attempts", self.retry_max_attempts, 1)
        _check_non_negative("retry_backoff_base_seconds", self.retry_backoff_base_seconds)
        _check_non_negative("retry_backoff_cap_seconds", self.retry_backoff_cap_seconds)
        if self.retry_backoff_cap_seconds < self.retry_backoff_base_seconds:
            raise ConfigurationError(
                "retry_backoff_cap_seconds must be >= retry_backoff_base_seconds "
                f"(got cap={self.retry_backoff_cap_seconds!r}, "
                f"base={self.retry_backoff_base_seconds!r})"
            )
        _check_positive("tool_timeout_seconds", self.tool_timeout_seconds)
        _check_positive("run_timeout_seconds", self.run_timeout_seconds)
        _check_at_least("short_circuit_threshold", self.short_circuit_threshold, 1)

    @classmethod
    def from_env(cls) -> "ReliabilityConfig":
        defaults = cls()
        return cls(
            http_connect_timeout_seconds=_getenv_float(
                "MANTIS_HTTP_CONNECT_TIMEOUT_SECONDS", defaults.http_connect_timeout_seconds
            ),
            http_read_timeout_seconds=_getenv_float(
                "MANTIS_HTTP_READ_TIMEOUT_SECONDS", defaults.http_read_timeout_seconds
            ),
            retry_max_attempts=_getenv_int("MANTIS_RETRY_MAX_ATTEMPTS", defaults.retry_max_attempts),
            retry_backoff_base_seconds=_getenv_float(
                "MANTIS_RETRY_BACKOFF_BASE_SECONDS", defaults.retry_backoff_base_seconds
            ),
            retry_backoff_cap_seconds=_getenv_float(
                "MANTIS_RETRY_BACKOFF_CAP_SECONDS", defaults.retry_backoff_cap_seconds
            ),
            tool_timeout_seconds=_getenv_float(
                "MANTIS_TOOL_TIMEOUT_SECONDS", defaults.tool_timeout_seconds
            ),
            run_timeout_seconds=_getenv_float(
                "MANTIS_RUN_TIMEOUT_SECONDS", defaults.run_timeout_seconds
            ),
            short_circuit_threshold=_getenv_int(
                "MANTIS_SHORT_CIRCUIT_THRESHOLD", defaults.short_circuit_threshold
            ),
        )


def get_metrics_enabled(default: bool) -> bool:
    """Whether ``MANTIS_METRICS_ENABLED`` is set — the one place this
    variable is read from the environment. Both ``mantis.cli`` (agent/
    eval invocations, default ``False`` — see ``docs/observability.md``
    for why a one-shot process shouldn't default to binding the metrics
    port) and ``mantis.api.server`` (the persistent ``mantis serve``
    process, default ``True`` — it's the process #66 gives metrics a
    real home in) call this instead of reading ``os.environ`` directly,
    each supplying the default appropriate to its own process model.
    """
    return _getenv_bool("MANTIS_METRICS_ENABLED", default)
