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
import re
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from mantis.routing import ModelCallFailureKind

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
    def from_env(cls, *, model_env: str | None = None) -> "LiteLLMConfig":
        """Resolve LiteLLM connection settings, with an optional
        agent-specific model override.

        ``model_env``, when given, names an environment variable an
        individual agent's ``build_runtime()`` can use to pin its own
        model alias (e.g. ``MANTIS_SYSTEM_TROUBLESHOOTER_MODEL`` for
        one agent, ``MANTIS_AWX_TROUBLESHOOTER_MODEL`` for another) —
        this is a minimal precursor to #16's full routing/escalation
        policy, not that policy itself: no fallback, retry, or
        escalation across models happens here, and this stays entirely
        server-side (never a caller-supplied field on the API/CLI).

        Resolution precedence: ``model_env`` (if given and set/non-empty)
        -> ``LITELLM_MODEL`` -> :data:`DEFAULT_LITELLM_MODEL`. With
        ``model_env`` omitted or unset, this is exactly the prior
        ``LITELLM_MODEL``-or-default behavior — existing deployments
        that only set ``LITELLM_MODEL`` are unaffected.
        """
        model = None
        if model_env is not None:
            model = os.environ.get(model_env) or None
        if model is None:
            model = os.environ.get("LITELLM_MODEL") or DEFAULT_LITELLM_MODEL
        return cls(
            url=_require("LITELLM_URL"),
            api_key=_require_secret("LITELLM_API_KEY"),
            model=model,
        )


MAX_ROUTING_ALIASES = 5
"""Bound on ``ModelRoutingPolicy``'s total configured routes (primary +
fallbacks) — a deliberately small, generous-enough cap that prevents
pathological configuration growth, not a tuning knob. See
``docs/model-routing.md``."""


@dataclass(frozen=True)
class ModelRoutingPolicy:
    """A stable, server-side model-call routing policy for one agent
    (#16): one primary LiteLLM alias, zero or more ordered fallback
    aliases, and a bounded number of attempts per *logical model call*
    (one model step in ``AgentRuntime``'s conversation loop — never a
    whole-run/whole-investigation restart, see ``mantis.routing`` and
    ``docs/model-routing.md``).

    This is entirely a data/validation layer — no attempt/fallback
    *logic* lives here (that's ``AgentRuntime``, see
    ``mantis.routing.classify_model_call_exception``). Never
    caller-supplied through the API/CLI: ``mantis.api.schemas.RunRequest``
    has ``extra="forbid"`` and no model/alias field at all, and
    ``build_runtime()`` is the only place a ``ModelRoutingPolicy`` is
    ever constructed for a real agent.

    Existing single-model configuration keeps working unchanged as a
    one-route policy — see :meth:`single`.
    """

    primary_alias: str
    fallback_aliases: tuple[str, ...] = ()
    max_attempts: int = 1
    eligible_failure_kinds: frozenset[ModelCallFailureKind] | None = None

    def __post_init__(self) -> None:
        # Deferred import: mantis.routing has no dependency on
        # mantis.config, but importing it at module scope here would
        # still work fine -- kept as a local import purely so this
        # module's own import graph stays exactly as shallow as before
        # for every caller that never touches routing.
        from mantis.routing import DEFAULT_ELIGIBLE_FAILURE_KINDS

        if self.eligible_failure_kinds is None:
            object.__setattr__(self, "eligible_failure_kinds", DEFAULT_ELIGIBLE_FAILURE_KINDS)

        if not self.primary_alias or not self.primary_alias.strip():
            raise ConfigurationError("ModelRoutingPolicy requires a non-empty primary_alias")
        for alias in self.fallback_aliases:
            if not alias or not alias.strip():
                raise ConfigurationError("ModelRoutingPolicy's fallback_aliases must not contain an empty alias")
        if self.primary_alias in self.fallback_aliases:
            raise ConfigurationError(
                f"ModelRoutingPolicy's primary_alias {self.primary_alias!r} must not also "
                "appear in fallback_aliases"
            )
        if len(set(self.fallback_aliases)) != len(self.fallback_aliases):
            raise ConfigurationError("ModelRoutingPolicy's fallback_aliases must not contain duplicates")
        total_routes = 1 + len(self.fallback_aliases)
        if total_routes > MAX_ROUTING_ALIASES:
            raise ConfigurationError(
                f"ModelRoutingPolicy allows at most {MAX_ROUTING_ALIASES} total routes "
                f"(1 primary + fallbacks), got {total_routes}"
            )
        _check_at_least("max_attempts", self.max_attempts, 1)
        if self.max_attempts > total_routes:
            raise ConfigurationError(
                f"ModelRoutingPolicy's max_attempts ({self.max_attempts}) cannot exceed "
                f"the number of configured routes ({total_routes})"
            )

    @property
    def aliases(self) -> tuple[str, ...]:
        """Every configured route, primary first, in attempt order."""
        return (self.primary_alias,) + self.fallback_aliases

    @classmethod
    def single(cls, alias: str) -> "ModelRoutingPolicy":
        """A one-route policy wrapping a single alias — what every
        existing single-model agent gets automatically when it doesn't
        opt into real fallback routing. No fallback, no extra attempt;
        behaviorally identical to Mantis before #16."""
        return cls(primary_alias=alias, fallback_aliases=(), max_attempts=1)

    @classmethod
    def from_litellm_config(cls, config: "LiteLLMConfig", **kwargs: Any) -> "ModelRoutingPolicy":
        """Map an existing, already-resolved ``LiteLLMConfig`` cleanly
        into a one-route policy using its ``.model`` as the primary
        alias — the documented bridge from "agent-specific model
        configuration" to a routing policy (see the Configuration AC in
        #16). Pass ``fallback_aliases=(...)``/``max_attempts=``/
        ``eligible_failure_kinds=`` as keyword arguments to add real
        fallback routes on top of it."""
        return cls(primary_alias=config.model, **kwargs)

    @classmethod
    def from_env(
        cls,
        *,
        model_env: str | None = None,
        fallback_env: str | None = None,
        max_attempts_env: str | None = None,
    ) -> "ModelRoutingPolicy":
        """Resolve a routing policy from the environment, mirroring
        :meth:`LiteLLMConfig.from_env`'s precedence for the primary
        alias exactly (``model_env`` override -> ``LITELLM_MODEL`` ->
        :data:`DEFAULT_LITELLM_MODEL`).

        Fallback aliases: a comma-separated list from ``fallback_env``
        (if given and set) -> ``LITELLM_MODEL_FALLBACKS`` -> none.
        Unset/absent means exactly today's single-model behavior — no
        operator has to configure anything new to keep existing
        deployments working.

        ``max_attempts``: an integer from ``max_attempts_env`` (if given
        and set) -> ``LITELLM_MODEL_MAX_ATTEMPTS`` -> one attempt per
        configured route (primary + every fallback), the most permissive
        default that still respects :data:`MAX_ROUTING_ALIASES`.
        """
        primary = None
        if model_env is not None:
            primary = os.environ.get(model_env) or None
        if primary is None:
            primary = os.environ.get("LITELLM_MODEL") or DEFAULT_LITELLM_MODEL

        fallback_raw = None
        if fallback_env is not None:
            fallback_raw = os.environ.get(fallback_env) or None
        if fallback_raw is None:
            fallback_raw = os.environ.get("LITELLM_MODEL_FALLBACKS") or None
        # Every comma-separated segment is preserved (stripped, but never
        # dropped for being empty) -- a malformed value like "a,,b" or a
        # trailing comma must fail loudly via ModelRoutingPolicy's own
        # empty-alias validation below, not be silently normalized into
        # a shorter, seemingly-valid list.
        fallback_aliases = (
            tuple(a.strip() for a in fallback_raw.split(",")) if fallback_raw else ()
        )

        max_attempts_raw = None
        if max_attempts_env is not None:
            max_attempts_raw = os.environ.get(max_attempts_env) or None
        if max_attempts_raw is None:
            max_attempts_raw = os.environ.get("LITELLM_MODEL_MAX_ATTEMPTS") or None
        default_max_attempts = 1 + len(fallback_aliases)
        if max_attempts_raw is None:
            max_attempts = default_max_attempts
        else:
            try:
                max_attempts = int(max_attempts_raw)
            except ValueError as exc:
                name = max_attempts_env or "LITELLM_MODEL_MAX_ATTEMPTS"
                raise ConfigurationError(f"{name} must be an integer, got {max_attempts_raw!r}") from exc

        return cls(primary_alias=primary, fallback_aliases=fallback_aliases, max_attempts=max_attempts)


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


_DNS_RESOLVER_PROFILE_ENV_PREFIX = "MANTIS_DNS_RESOLVER_"
"""Every environment variable ``MANTIS_DNS_RESOLVER_<ALIAS>=server1,server2,...``
defines one named resolver profile (#109) — ``<ALIAS>`` (case-insensitive)
becomes the ``resolver_alias`` a caller may request, e.g.
``MANTIS_DNS_RESOLVER_INTERNAL=...`` for ``resolver_alias="internal"``.
Unlike every other integration config in this module, the *set* of
profiles an operator configures is open-ended (there is no fixed list
of alias names to declare fields for), so :meth:`DNSConfig.from_env`
scans the environment for this prefix rather than reading a fixed set
of named variables — the only place this module does that. This is
still config *parsing*, not a network call: nothing here contacts a
resolver or validates that a configured server is actually reachable."""


@dataclass(frozen=True)
class DNSConfig:
    """Server-side DNS resolver profile configuration for ``dns_lookup``
    (#109) — see ``mantis.integrations.dns`` and ``docs/dns-lookup.md``.

    A caller (model or API client) may only ever select a profile by its
    ``resolver_alias`` *name* — never a resolver IP/hostname/port
    directly. This is the whole point of resolver profiles being
    server-side configuration: split-horizon diagnostics (comparing an
    ``internal`` perspective against a ``cloudflare``/``google``/etc.
    public one) requires the *set* of available perspectives to be
    fixed by the operator, not expandable by whatever a model decides
    to ask for.

    ``profiles`` maps a lowercase alias to an ordered tuple of one or
    more server IP literals (never hostnames — a resolver's own address
    must not itself require DNS resolution to reach). There is no
    special-cased "system"/"cloudflare"/"google" behavior anywhere in
    this class: those are just example alias *names* an operator is
    free to configure like any other (see ``deploy/standalone/runtime.env.example``)
    — Cloudflare/Google are never hardcoded as a mandatory default
    resolver.
    """

    profiles: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for alias, servers in self.profiles.items():
            if not alias:
                raise ConfigurationError("A DNS resolver profile alias must not be empty")
            if not servers:
                raise ConfigurationError(
                    f"DNS resolver profile '{alias}' must configure at least one server"
                )
            for server in servers:
                try:
                    ip_address(server)
                except ValueError as exc:
                    raise ConfigurationError(
                        f"DNS resolver profile '{alias}' has an invalid server address "
                        f"{server!r} -- resolver servers must be IP literals, never hostnames"
                    ) from exc

    @classmethod
    def from_env(cls) -> "DNSConfig":
        """Scan the environment for every ``MANTIS_DNS_RESOLVER_<ALIAS>``
        variable and build the corresponding profile map. Performs no
        network access — see :data:`_DNS_RESOLVER_PROFILE_ENV_PREFIX`.
        """
        profiles: dict[str, tuple[str, ...]] = {}
        for key, value in os.environ.items():
            if not key.startswith(_DNS_RESOLVER_PROFILE_ENV_PREFIX):
                continue
            alias = key[len(_DNS_RESOLVER_PROFILE_ENV_PREFIX) :].lower()
            if not alias:
                continue
            servers = tuple(s.strip() for s in value.split(",") if s.strip())
            if servers:
                profiles[alias] = servers
        return cls(profiles=profiles)

    def resolve_profile(self, alias: object) -> tuple[str, ...] | None:
        """Look up ``alias`` (case-insensitive), returning its
        configured server tuple or ``None`` if unknown. Never raises —
        an unrecognized alias is exactly as "not found" as a
        non-string/malformed value; the caller (``mantis.tools.dns``)
        turns either into the same safe, no-network-access
        ``invalid_input`` tool result (#109)."""
        if not isinstance(alias, str):
            return None
        return self.profiles.get(alias.lower())


_HOSTNAME_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,62})?"
_HOSTNAME_RE = re.compile(rf"^{_HOSTNAME_LABEL}(\.{_HOSTNAME_LABEL})*\.?$")
"""Same RFC-1123-ish allowlist shape used by
``mantis.integrations.network``/``.dns`` — defined locally rather than
imported, matching the established convention that config.py has no
dependency on any integration module (integrations depend on config,
never the reverse)."""


def _is_ip_literal(value: str) -> bool:
    try:
        ip_address(value)
        return True
    except ValueError:
        return False


def _is_valid_host(value: str) -> bool:
    """True if ``value`` is a plain IPv4/IPv6 literal or an RFC-1123-ish
    hostname — the same allowlist every Mantis network-facing config
    validates a host against. Never a URL, path, or anything containing
    credentials/whitespace (those are rejected by the callers that parse
    a full URL/host string before reaching this check)."""
    return _is_ip_literal(value) or bool(_HOSTNAME_RE.match(value))


# ---------------------------------------------------------------------------
# HTTP target profiles (#110) -- see mantis.integrations.http and
# docs/http-probe.md. Mirrors DNSConfig's shape exactly: an open-ended,
# environment-scanned set of named profiles, since the set of aliases an
# operator configures has no fixed field list to declare. A caller (the
# model or an API client) may only ever select a target by its alias
# name -- never a host/port/scheme/proxy/TLS-verification-mode directly.
# ---------------------------------------------------------------------------

_HTTP_TARGET_ENV_PREFIX = "MANTIS_HTTP_TARGET_"
_HTTP_TARGET_URL_SUFFIX = "_URL"
_HTTP_TARGET_VERIFY_SSL_SUFFIX = "_VERIFY_SSL"

_HTTP_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class HTTPTargetConfig:
    """One server-side-configured HTTP probe target (#110).

    Establishes the *immutable* origin (``scheme``/``host``/``port``)
    and an optional ``base_path`` prefix a caller's own ``path`` is
    appended to (never replaces) -- see ``mantis.tools.http.http_probe``.
    None of these fields is ever caller-overridable; only ``alias`` is
    ever supplied by a caller (the model or an API client), and only to
    select *which* already-configured target to use.
    """

    alias: str
    scheme: str
    host: str
    port: int
    base_path: str = ""
    verify_ssl: bool = True


def _parse_http_target(alias: str, url: str, *, verify_ssl: bool) -> HTTPTargetConfig:
    try:
        # urlsplit() itself can raise ValueError for a structurally
        # malformed authority (e.g. an unbalanced "[" in an IPv6
        # literal) -- caught here so every parse failure for this
        # target becomes this module's normal ConfigurationError,
        # never a raw ValueError escaping from_env().
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ConfigurationError(f"HTTP target '{alias}' has a malformed URL {url!r}: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ConfigurationError(
            f"HTTP target '{alias}' must use http:// or https://, got scheme {parsed.scheme!r} in {url!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"HTTP target '{alias}' must not embed credentials in its configured URL")
    if not parsed.hostname:
        raise ConfigurationError(f"HTTP target '{alias}' is missing a host in {url!r}")
    if not _is_valid_host(parsed.hostname):
        raise ConfigurationError(f"HTTP target '{alias}' has an invalid host: {parsed.hostname!r}")
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            f"HTTP target '{alias}' URL must not include a query string or fragment -- "
            "configure a clean origin (and optional base path) only"
        )
    try:
        # SplitResult.port is a lazy property that raises ValueError
        # (not caught anywhere above) for a malformed port -- e.g.
        # ":99999" (out of range) or ":notaport" (not an integer at
        # all) -- rather than returning None the way a genuinely absent
        # port does. Converted to this module's normal
        # ConfigurationError so a bad MANTIS_HTTP_TARGET_*_URL fails
        # exactly like every other invalid-target case, never with a
        # raw ValueError escaping from_env().
        explicit_port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"HTTP target '{alias}' has an invalid port in {url!r}: {exc}") from exc
    port = explicit_port or _HTTP_DEFAULT_PORTS[parsed.scheme]
    base_path = parsed.path.rstrip("/")
    return HTTPTargetConfig(
        alias=alias, scheme=parsed.scheme, host=parsed.hostname, port=port, base_path=base_path, verify_ssl=verify_ssl
    )


@dataclass(frozen=True)
class HTTPProfilesConfig:
    """Every configured HTTP probe target, keyed by lowercase alias."""

    targets: dict[str, HTTPTargetConfig] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "HTTPProfilesConfig":
        """Scan the environment for every
        ``MANTIS_HTTP_TARGET_<ALIAS>_URL`` variable and build the
        corresponding target map (``..._VERIFY_SSL`` is optional,
        default ``true``). Performs no network access."""
        targets: dict[str, HTTPTargetConfig] = {}
        for key, value in os.environ.items():
            if not key.startswith(_HTTP_TARGET_ENV_PREFIX) or not key.endswith(_HTTP_TARGET_URL_SUFFIX):
                continue
            alias = key[len(_HTTP_TARGET_ENV_PREFIX) : -len(_HTTP_TARGET_URL_SUFFIX)].lower()
            if not alias:
                continue
            verify_env = f"{_HTTP_TARGET_ENV_PREFIX}{alias.upper()}{_HTTP_TARGET_VERIFY_SSL_SUFFIX}"
            verify_ssl = _getenv_bool(verify_env, True)
            targets[alias] = _parse_http_target(alias, value, verify_ssl=verify_ssl)
        return cls(targets=targets)

    def resolve_target(self, alias: object) -> HTTPTargetConfig | None:
        """Look up ``alias`` (case-insensitive), returning its
        configured target or ``None`` if unknown. Never raises — see
        ``DNSConfig.resolve_profile``'s identical convention."""
        if not isinstance(alias, str):
            return None
        return self.targets.get(alias.lower())


# ---------------------------------------------------------------------------
# TLS target profiles (#111) -- see mantis.integrations.tls and
# docs/tls-certificate-inspection.md. Deliberately a separate config
# class/alias namespace from HTTPProfilesConfig above, not a shared
# "endpoint" abstraction: TLS inspection is meaningful for any direct
# TLS endpoint (not only HTTPS), and forcing it to be HTTP-specific
# would be exactly the premature generalization #110/#111 warn against.
# ---------------------------------------------------------------------------

_TLS_TARGET_ENV_PREFIX = "MANTIS_TLS_TARGET_"
_TLS_TARGET_HOST_SUFFIX = "_HOST"


@dataclass(frozen=True)
class TLSTargetConfig:
    """One server-side-configured TLS inspection target (#111).

    ``server_name`` is the SNI/hostname-verification value -- always
    resolved deterministically at config time, never guessed at request
    time: it defaults to ``host`` when ``host`` is itself a hostname,
    and must be set explicitly (``..._SERVER_NAME``) when ``host`` is an
    IP literal, since there is no hostname to default it from. See
    :func:`_build_tls_target`. ``ca_file`` is deployment-only trust
    configuration (never exposed in any tool result) — see
    ``docs/tls-certificate-inspection.md``'s "Trust store" section.
    """

    alias: str
    host: str
    port: int
    server_name: str
    ca_file: str | None = None


def _build_tls_target(
    alias: str, *, host: str, port: int, server_name: str | None, ca_file: str | None
) -> TLSTargetConfig:
    if not host:
        raise ConfigurationError(f"TLS target '{alias}' is missing a host")
    if not _is_valid_host(host):
        raise ConfigurationError(f"TLS target '{alias}' has an invalid host: {host!r}")
    if not (1 <= port <= 65535):
        raise ConfigurationError(f"TLS target '{alias}' port must be between 1 and 65535, got {port!r}")
    if server_name is None:
        if _is_ip_literal(host):
            raise ConfigurationError(
                f"TLS target '{alias}': server_name (SNI) must be set explicitly "
                f"(MANTIS_TLS_TARGET_{alias.upper()}_SERVER_NAME) when host is an IP literal -- "
                "there is no hostname to default it from, and SNI must never be guessed"
            )
        server_name = host
    elif not _is_valid_host(server_name):
        raise ConfigurationError(f"TLS target '{alias}' has an invalid server_name (SNI): {server_name!r}")
    return TLSTargetConfig(alias=alias, host=host, port=port, server_name=server_name, ca_file=ca_file or None)


@dataclass(frozen=True)
class TLSProfilesConfig:
    """Every configured TLS inspection target, keyed by lowercase alias."""

    targets: dict[str, TLSTargetConfig] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "TLSProfilesConfig":
        """Scan the environment for every
        ``MANTIS_TLS_TARGET_<ALIAS>_HOST`` variable and build the
        corresponding target map (``..._PORT`` defaults to ``443``;
        ``..._SERVER_NAME``/``..._CA_FILE`` are optional). Performs no
        network access -- ``ca_file``'s existence is never checked here,
        only when a tool actually runs (see
        ``mantis.integrations.tls``)."""
        targets: dict[str, TLSTargetConfig] = {}
        for key, value in os.environ.items():
            if not key.startswith(_TLS_TARGET_ENV_PREFIX) or not key.endswith(_TLS_TARGET_HOST_SUFFIX):
                continue
            alias = key[len(_TLS_TARGET_ENV_PREFIX) : -len(_TLS_TARGET_HOST_SUFFIX)].lower()
            if not alias:
                continue
            upper = alias.upper()
            port = _getenv_int(f"{_TLS_TARGET_ENV_PREFIX}{upper}_PORT", 443)
            server_name = os.environ.get(f"{_TLS_TARGET_ENV_PREFIX}{upper}_SERVER_NAME") or None
            ca_file = os.environ.get(f"{_TLS_TARGET_ENV_PREFIX}{upper}_CA_FILE") or None
            targets[alias] = _build_tls_target(alias, host=value, port=port, server_name=server_name, ca_file=ca_file)
        return cls(targets=targets)

    def resolve_target(self, alias: object) -> TLSTargetConfig | None:
        """Look up ``alias`` (case-insensitive), returning its
        configured target or ``None`` if unknown. Never raises — see
        ``DNSConfig.resolve_profile``'s identical convention."""
        if not isinstance(alias, str):
            return None
        return self.targets.get(alias.lower())


# ---------------------------------------------------------------------------
# Git repository aliases (#17) -- see mantis.integrations.git and
# docs/git.md. v1 is local-repository-only: an operator configures a
# fixed set of named aliases, each mapped to a server-side local
# filesystem path. A caller (the model or an API client) may only ever
# select a repository by its alias name -- never a raw path, remote URL,
# branch, tag, SHA, revision expression, or Git option/command directly.
# Mirrors DNSConfig's open-ended, environment-scanned profile shape
# exactly, for the same reason: the *set* of repositories Mantis is
# willing to inspect must be fixed by the operator, not expandable by
# whatever path a model decides to ask for.
# ---------------------------------------------------------------------------

_GIT_REPOSITORY_ENV_PREFIX = "MANTIS_GIT_REPOSITORY_"


@dataclass(frozen=True)
class GitRepositoriesConfig:
    """Every configured Git repository alias, keyed by lowercase alias,
    mapped to a server-side local filesystem path.

    Deliberately minimal validation: unlike an IP literal or a URL, a
    filesystem path has no comparable "is this syntactically valid"
    check worth enforcing beyond non-emptiness -- whether the path
    actually exists and is a usable Git repository is discovered only
    when the integration opens it (see ``mantis.integrations.git`` and
    #17's "repository validation/opening occurs only when the
    integration is used" requirement). This class never touches the
    filesystem.
    """

    repositories: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "GitRepositoriesConfig":
        """Scan the environment for every
        ``MANTIS_GIT_REPOSITORY_<ALIAS>`` variable and build the
        corresponding alias -> path map. Performs no filesystem, Git,
        or network access."""
        repositories: dict[str, str] = {}
        for key, value in os.environ.items():
            if not key.startswith(_GIT_REPOSITORY_ENV_PREFIX):
                continue
            alias = key[len(_GIT_REPOSITORY_ENV_PREFIX) :].lower()
            if not alias or not value:
                continue
            repositories[alias] = value
        return cls(repositories=repositories)

    def resolve_repository(self, alias: object) -> str | None:
        """Look up ``alias`` (case-insensitive), returning its
        configured repository path or ``None`` if unknown. Never
        raises — an unrecognized alias is exactly as "not found" as a
        non-string/malformed value; the caller
        (``mantis.tools.git.git_recent_changes``) turns either into the
        same safe, no-repository-access ``invalid_input`` tool result,
        mirroring ``DNSConfig.resolve_profile``'s identical
        convention."""
        if not isinstance(alias, str):
            return None
        return self.repositories.get(alias.lower())


_API_AUTH_MODES = ("bearer_token", "disabled")
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8080
DEFAULT_API_MAX_CONCURRENT_RUNS = 4
DEFAULT_API_SHUTDOWN_GRACE_PERIOD_SECONDS = 30.0

DEFAULT_API_CLIENT_READ_TIMEOUT_SECONDS = 340.0
"""Deliberately *above* :class:`ReliabilityConfig`'s own
``run_timeout_seconds`` default (300.0s, the server-side bound on one
agent run) by a comfortable ~40s margin. If the client's own read
timeout instead equalled (or fell below) the server's run deadline, the
client could give up and report a bare timeout at almost exactly the
moment the server was about to return a proper, classified
``outcome="error"``/``error.kind="run_timeout"`` result (see
``docs/api.md``'s "Timeout semantics" section) -- the client should
virtually always see the server's real answer first."""


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
    read_timeout_seconds: float = DEFAULT_API_CLIENT_READ_TIMEOUT_SECONDS

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
            read_timeout_seconds=_getenv_float(
                "MANTIS_API_CLIENT_READ_TIMEOUT_SECONDS", DEFAULT_API_CLIENT_READ_TIMEOUT_SECONDS
            ),
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
