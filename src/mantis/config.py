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
