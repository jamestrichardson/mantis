"""Tests for mantis.config: the local-dev .env.* file convention and
config defaulting behavior.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mantis.config as config
from mantis.config import (
    DEFAULT_LITELLM_MODEL,
    ApiClientConfig,
    ApiServerConfig,
    AWXConfig,
    ConfigurationError,
    DNSConfig,
    LiteLLMConfig,
    ReliabilityConfig,
    Secret,
    get_metrics_enabled,
)


def test_litellm_model_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("LITELLM_MODEL", raising=False)

    cfg = LiteLLMConfig.from_env()

    assert cfg.model == DEFAULT_LITELLM_MODEL == "qwen3-opencode:latest"


def test_litellm_model_uses_explicit_value_when_set(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "some-other-model")

    cfg = LiteLLMConfig.from_env()

    assert cfg.model == "some-other-model"


# ---------------------------------------------------------------------------
# Per-agent model override (a minimal precursor to #16's full routing/
# escalation policy -- see LiteLLMConfig.from_env's model_env parameter).
# Precedence: model_env (if given and set/non-empty) -> LITELLM_MODEL ->
# DEFAULT_LITELLM_MODEL.
# ---------------------------------------------------------------------------


def test_model_env_override_wins_over_litellm_model(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "global-model")
    monkeypatch.setenv("MANTIS_SYSTEM_TROUBLESHOOTER_MODEL", "agent-specific-model")

    cfg = LiteLLMConfig.from_env(model_env="MANTIS_SYSTEM_TROUBLESHOOTER_MODEL")

    assert cfg.model == "agent-specific-model"


def test_model_env_falls_back_to_litellm_model_when_unset(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "global-model")
    monkeypatch.delenv("MANTIS_SYSTEM_TROUBLESHOOTER_MODEL", raising=False)

    cfg = LiteLLMConfig.from_env(model_env="MANTIS_SYSTEM_TROUBLESHOOTER_MODEL")

    assert cfg.model == "global-model"


def test_model_env_falls_back_to_litellm_model_when_set_but_empty(monkeypatch):
    # An empty string is treated the same as unset -- never resolved to
    # a literal empty model alias.
    monkeypatch.setenv("LITELLM_MODEL", "global-model")
    monkeypatch.setenv("MANTIS_SYSTEM_TROUBLESHOOTER_MODEL", "")

    cfg = LiteLLMConfig.from_env(model_env="MANTIS_SYSTEM_TROUBLESHOOTER_MODEL")

    assert cfg.model == "global-model"


def test_model_env_falls_back_to_built_in_default_when_both_absent(monkeypatch):
    monkeypatch.delenv("LITELLM_MODEL", raising=False)
    monkeypatch.delenv("MANTIS_SYSTEM_TROUBLESHOOTER_MODEL", raising=False)

    cfg = LiteLLMConfig.from_env(model_env="MANTIS_SYSTEM_TROUBLESHOOTER_MODEL")

    assert cfg.model == DEFAULT_LITELLM_MODEL


def test_model_env_omitted_preserves_prior_litellm_model_only_behavior(monkeypatch):
    # Existing deployments that only ever set LITELLM_MODEL (never any
    # agent-specific override) must resolve exactly as before -- calling
    # from_env() with no model_env at all is unaffected by this feature.
    monkeypatch.setenv("LITELLM_MODEL", "global-model")
    monkeypatch.setenv("MANTIS_SYSTEM_TROUBLESHOOTER_MODEL", "should-be-ignored")

    cfg = LiteLLMConfig.from_env()

    assert cfg.model == "global-model"


def test_model_env_does_not_affect_url_or_api_key(monkeypatch):
    monkeypatch.setenv("MANTIS_SYSTEM_TROUBLESHOOTER_MODEL", "agent-specific-model")

    cfg = LiteLLMConfig.from_env(model_env="MANTIS_SYSTEM_TROUBLESHOOTER_MODEL")

    assert cfg.url == "http://localhost:4000"
    assert cfg.api_key.get_secret_value() == "test-key"


def test_load_env_files_most_specific_file_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANTIS_ENV", "myenv")
    monkeypatch.delenv("MANTIS_TEST_VAR", raising=False)

    Path(".env").write_text("MANTIS_TEST_VAR=from_base\n")
    Path(".env.local").write_text("MANTIS_TEST_VAR=from_local\n")
    Path(".env.myenv").write_text("MANTIS_TEST_VAR=from_named_env\n")
    Path(".env.myenv.local").write_text("MANTIS_TEST_VAR=from_named_env_local\n")

    config._load_env_files()

    assert os.environ["MANTIS_TEST_VAR"] == "from_named_env_local"


def test_load_env_files_never_overrides_real_env_var(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANTIS_ENV", "myenv")
    monkeypatch.setenv("MANTIS_TEST_VAR", "from_real_environment")

    Path(".env.myenv").write_text("MANTIS_TEST_VAR=from_file\n")

    config._load_env_files()

    assert os.environ["MANTIS_TEST_VAR"] == "from_real_environment"


def test_load_env_files_defaults_to_local_env_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANTIS_ENV", raising=False)
    monkeypatch.delenv("MANTIS_TEST_VAR", raising=False)

    Path(".env.local").write_text("MANTIS_TEST_VAR=from_default_local\n")

    config._load_env_files()

    assert os.environ["MANTIS_TEST_VAR"] == "from_default_local"


def test_load_env_files_is_a_noop_when_no_files_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANTIS_TEST_VAR", raising=False)

    config._load_env_files()  # should not raise even with nothing to load

    assert "MANTIS_TEST_VAR" not in os.environ


# ---------------------------------------------------------------------------
# Missing required configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_var",
    ["LITELLM_URL", "LITELLM_API_KEY"],
)
def test_litellm_config_raises_on_missing_required_variable(monkeypatch, missing_var):
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(ConfigurationError, match=missing_var):
        LiteLLMConfig.from_env()


@pytest.mark.parametrize(
    "missing_var",
    ["AWX_URL", "AWX_TOKEN"],
)
def test_awx_config_raises_on_missing_required_variable(monkeypatch, missing_var):
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(ConfigurationError, match=missing_var):
        AWXConfig.from_env()


def test_configuration_error_never_leaks_an_already_set_secret(monkeypatch):
    # A secret that IS set (AWX_TOKEN, via the autouse fixture) must never
    # appear in a ConfigurationError raised over a *different* missing
    # variable — the error is about what's missing, not what's present.
    monkeypatch.delenv("LITELLM_URL", raising=False)
    monkeypatch.setenv("AWX_TOKEN", "super-secret-value")

    with pytest.raises(ConfigurationError) as exc_info:
        LiteLLMConfig.from_env()

    assert "super-secret-value" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


def test_secret_repr_and_str_never_expose_the_value():
    secret = Secret("super-secret-value")

    assert "super-secret-value" not in repr(secret)
    assert "super-secret-value" not in str(secret)


def test_secret_get_secret_value_returns_the_real_value():
    secret = Secret("super-secret-value")

    assert secret.get_secret_value() == "super-secret-value"


def test_litellm_config_repr_never_exposes_api_key(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "super-secret-litellm-key")

    cfg = LiteLLMConfig.from_env()

    assert "super-secret-litellm-key" not in repr(cfg)
    assert "super-secret-litellm-key" not in str(cfg)
    assert cfg.api_key.get_secret_value() == "super-secret-litellm-key"


def test_awx_config_repr_never_exposes_token(monkeypatch):
    monkeypatch.setenv("AWX_TOKEN", "super-secret-awx-token")

    cfg = AWXConfig.from_env()

    assert "super-secret-awx-token" not in repr(cfg)
    assert "super-secret-awx-token" not in str(cfg)
    assert cfg.token.get_secret_value() == "super-secret-awx-token"


# ---------------------------------------------------------------------------
# ReliabilityConfig (#15) — docs/reliability.md documents these defaults
# and env var names explicitly; this test is a regression guard against
# either silently drifting from the other.
# ---------------------------------------------------------------------------


def test_reliability_config_documented_defaults():
    cfg = ReliabilityConfig()
    assert cfg.http_connect_timeout_seconds == 5.0
    assert cfg.http_read_timeout_seconds == 25.0
    assert cfg.retry_max_attempts == 3
    assert cfg.retry_backoff_base_seconds == 0.5
    assert cfg.retry_backoff_cap_seconds == 8.0
    assert cfg.tool_timeout_seconds == 45.0
    assert cfg.run_timeout_seconds == 300.0
    assert cfg.short_circuit_threshold == 3


@pytest.mark.parametrize(
    "env_var,field,value",
    [
        ("MANTIS_HTTP_CONNECT_TIMEOUT_SECONDS", "http_connect_timeout_seconds", "1.5"),
        ("MANTIS_HTTP_READ_TIMEOUT_SECONDS", "http_read_timeout_seconds", "10.0"),
        ("MANTIS_RETRY_MAX_ATTEMPTS", "retry_max_attempts", "5"),
        ("MANTIS_RETRY_BACKOFF_BASE_SECONDS", "retry_backoff_base_seconds", "1.0"),
        ("MANTIS_RETRY_BACKOFF_CAP_SECONDS", "retry_backoff_cap_seconds", "20.0"),
        ("MANTIS_TOOL_TIMEOUT_SECONDS", "tool_timeout_seconds", "90.0"),
        ("MANTIS_RUN_TIMEOUT_SECONDS", "run_timeout_seconds", "600.0"),
        ("MANTIS_SHORT_CIRCUIT_THRESHOLD", "short_circuit_threshold", "5"),
    ],
)
def test_reliability_config_every_field_is_configurable_via_its_documented_env_var(
    monkeypatch, env_var, field, value
):
    monkeypatch.setenv(env_var, value)
    cfg = ReliabilityConfig.from_env()
    actual = getattr(cfg, field)
    assert actual == (float(value) if isinstance(actual, float) else int(value))


def test_reliability_config_rejects_a_non_numeric_env_var(monkeypatch):
    monkeypatch.setenv("MANTIS_RETRY_MAX_ATTEMPTS", "not-a-number")
    with pytest.raises(ConfigurationError):
        ReliabilityConfig.from_env()


# ---------------------------------------------------------------------------
# ReliabilityConfig range validation — a value can parse fine (0, -5, ...)
# but still be nonsensical. Type-checking alone let these through; without
# range validation, e.g. retry_max_attempts=0 doesn't fail at startup, it
# fails later as an internal AssertionError deep in
# mantis.reliability.retry_call. See PR #72 review discussion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("http_connect_timeout_seconds", 0.0),
        ("http_connect_timeout_seconds", -5.0),
        ("http_read_timeout_seconds", 0.0),
        ("http_read_timeout_seconds", -1.0),
        ("retry_max_attempts", 0),
        ("retry_max_attempts", -1),
        ("retry_backoff_base_seconds", -1.0),
        ("retry_backoff_cap_seconds", -1.0),
        ("tool_timeout_seconds", 0.0),
        ("tool_timeout_seconds", -5.0),
        ("run_timeout_seconds", 0.0),
        ("run_timeout_seconds", -5.0),
        ("short_circuit_threshold", 0),
        ("short_circuit_threshold", -1),
    ],
)
def test_reliability_config_rejects_invalid_field_values(field, value):
    with pytest.raises(ConfigurationError, match=field):
        ReliabilityConfig(**{field: value})


def test_reliability_config_rejects_backoff_cap_below_base():
    with pytest.raises(ConfigurationError, match="retry_backoff_cap_seconds"):
        ReliabilityConfig(retry_backoff_base_seconds=5.0, retry_backoff_cap_seconds=1.0)


def test_reliability_config_allows_backoff_cap_equal_to_base():
    cfg = ReliabilityConfig(retry_backoff_base_seconds=2.0, retry_backoff_cap_seconds=2.0)
    assert cfg.retry_backoff_cap_seconds == 2.0


@pytest.mark.parametrize(
    "env_var,value",
    [
        ("MANTIS_RETRY_MAX_ATTEMPTS", "0"),
        ("MANTIS_SHORT_CIRCUIT_THRESHOLD", "0"),
        ("MANTIS_HTTP_CONNECT_TIMEOUT_SECONDS", "-5"),
        ("MANTIS_RETRY_BACKOFF_BASE_SECONDS", "-1"),
    ],
)
def test_reliability_config_from_env_rejects_invalid_values(monkeypatch, env_var, value):
    monkeypatch.setenv(env_var, value)
    with pytest.raises(ConfigurationError):
        ReliabilityConfig.from_env()


# ---------------------------------------------------------------------------
# Non-finite float values (nan/inf) — float("nan")/float("inf") both parse
# successfully, so the zero/negative range checks above don't catch them on
# their own (NaN comparisons are always False; +inf > 0 is True). Left
# unvalidated, a NaN backoff produces confusing downstream behavior in
# random.uniform()/time.sleep()/httpx.Timeout rather than a clear error at
# config construction. See PR #72 review discussion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "http_connect_timeout_seconds",
        "http_read_timeout_seconds",
        "retry_backoff_base_seconds",
        "retry_backoff_cap_seconds",
        "tool_timeout_seconds",
        "run_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_reliability_config_rejects_non_finite_float_fields(field, value):
    with pytest.raises(ConfigurationError, match=field):
        ReliabilityConfig(**{field: value})


def test_reliability_config_from_env_rejects_nan(monkeypatch):
    monkeypatch.setenv("MANTIS_RETRY_BACKOFF_BASE_SECONDS", "nan")
    with pytest.raises(ConfigurationError):
        ReliabilityConfig.from_env()


def test_reliability_config_from_env_rejects_inf(monkeypatch):
    monkeypatch.setenv("MANTIS_RUN_TIMEOUT_SECONDS", "inf")
    with pytest.raises(ConfigurationError):
        ReliabilityConfig.from_env()


# ---------------------------------------------------------------------------
# ApiServerConfig (#21/#83) -- construction/validation only, no networking
# ---------------------------------------------------------------------------


def test_api_server_config_requires_token_in_default_bearer_token_mode():
    with pytest.raises(ConfigurationError, match="MANTIS_API_TOKEN"):
        ApiServerConfig(auth_mode="bearer_token", bearer_token=None)


def test_api_server_config_default_auth_mode_is_bearer_token():
    cfg = ApiServerConfig(bearer_token=Secret("x"))
    assert cfg.auth_mode == "bearer_token"


def test_api_server_config_disabled_mode_does_not_require_a_token():
    cfg = ApiServerConfig(auth_mode="disabled")
    assert cfg.bearer_token is None


def test_api_server_config_rejects_unknown_auth_mode():
    with pytest.raises(ConfigurationError, match="MANTIS_API_AUTH_MODE"):
        ApiServerConfig(auth_mode="none", bearer_token=Secret("x"))


@pytest.mark.parametrize("value", [0, -1])
def test_api_server_config_rejects_invalid_max_concurrent_runs(value):
    with pytest.raises(ConfigurationError, match="max_concurrent_runs"):
        ApiServerConfig(auth_mode="disabled", max_concurrent_runs=value)


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_api_server_config_rejects_invalid_shutdown_grace_period(value):
    with pytest.raises(ConfigurationError, match="shutdown_grace_period_seconds"):
        ApiServerConfig(auth_mode="disabled", shutdown_grace_period_seconds=value)


def test_api_server_config_rejects_invalid_port():
    with pytest.raises(ConfigurationError, match="port"):
        ApiServerConfig(auth_mode="disabled", port=-1)
    with pytest.raises(ConfigurationError, match="port"):
        ApiServerConfig(auth_mode="disabled", port=70000)


def test_api_server_config_allows_port_zero_for_os_assigned_ephemeral_binding():
    # A deliberate convention (see tests/test_api_server.py), never used
    # in production configuration -- not this dataclass's job to forbid.
    cfg = ApiServerConfig(auth_mode="disabled", port=0)
    assert cfg.port == 0


def test_api_server_config_from_env_defaults(monkeypatch):
    monkeypatch.setenv("MANTIS_API_TOKEN", "server-token")
    monkeypatch.delenv("MANTIS_API_AUTH_MODE", raising=False)
    monkeypatch.delenv("MANTIS_API_HOST", raising=False)
    monkeypatch.delenv("MANTIS_API_PORT", raising=False)

    cfg = ApiServerConfig.from_env()

    assert cfg.auth_mode == "bearer_token"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8080
    assert cfg.max_concurrent_runs == 4
    assert cfg.shutdown_grace_period_seconds == 30.0
    assert cfg.bearer_token.get_secret_value() == "server-token"


def test_api_server_config_from_env_missing_token_raises(monkeypatch):
    monkeypatch.delenv("MANTIS_API_TOKEN", raising=False)
    monkeypatch.delenv("MANTIS_API_AUTH_MODE", raising=False)

    with pytest.raises(ConfigurationError, match="MANTIS_API_TOKEN"):
        ApiServerConfig.from_env()


def test_api_server_config_from_env_disabled_mode(monkeypatch):
    monkeypatch.setenv("MANTIS_API_AUTH_MODE", "disabled")
    monkeypatch.delenv("MANTIS_API_TOKEN", raising=False)

    cfg = ApiServerConfig.from_env()

    assert cfg.auth_mode == "disabled"


def test_api_server_config_repr_never_exposes_token():
    cfg = ApiServerConfig(bearer_token=Secret("super-secret-api-token"))
    assert "super-secret-api-token" not in repr(cfg)
    assert "super-secret-api-token" not in str(cfg)


# ---------------------------------------------------------------------------
# ApiClientConfig (#83) -- client-side only, never a server credential
# ---------------------------------------------------------------------------


def test_api_client_config_from_env_defaults(monkeypatch):
    monkeypatch.delenv("MANTIS_API_URL", raising=False)
    monkeypatch.delenv("MANTIS_API_TOKEN", raising=False)

    cfg = ApiClientConfig.from_env()

    assert cfg.base_url == "http://localhost:8080"
    assert cfg.token is None
    assert cfg.connect_timeout_seconds == 5.0
    assert cfg.read_timeout_seconds == 340.0


def test_api_client_default_read_timeout_stays_above_the_server_run_deadline():
    # Regression guard: if the client's default read timeout ever
    # dropped to/below ReliabilityConfig's own run_timeout_seconds
    # default, the client could give up right as the server was about
    # to return its own classified run_timeout result -- see
    # docs/api.md's "Timeout semantics" section.
    client_default = ApiClientConfig.from_env().read_timeout_seconds
    server_default = ReliabilityConfig().run_timeout_seconds
    assert client_default > server_default + 30.0


def test_api_client_config_from_env_reads_url_and_token(monkeypatch):
    monkeypatch.setenv("MANTIS_API_URL", "https://mantis.example.test/")
    monkeypatch.setenv("MANTIS_API_TOKEN", "client-token")

    cfg = ApiClientConfig.from_env()

    assert cfg.base_url == "https://mantis.example.test"  # trailing slash stripped
    assert cfg.token.get_secret_value() == "client-token"


def test_api_client_config_rejects_empty_base_url():
    with pytest.raises(ConfigurationError, match="MANTIS_API_URL"):
        ApiClientConfig(base_url="")


@pytest.mark.parametrize("field", ["connect_timeout_seconds", "read_timeout_seconds"])
def test_api_client_config_rejects_non_positive_timeouts(field):
    with pytest.raises(ConfigurationError, match=field):
        ApiClientConfig(base_url="http://localhost:8080", **{field: 0.0})


def test_api_client_config_repr_never_exposes_token():
    cfg = ApiClientConfig(base_url="http://localhost:8080", token=Secret("super-secret-client-token"))
    assert "super-secret-client-token" not in repr(cfg)


# ---------------------------------------------------------------------------
# get_metrics_enabled -- the one centralized MANTIS_METRICS_ENABLED reader
# ---------------------------------------------------------------------------


def test_get_metrics_enabled_uses_the_given_default_when_unset(monkeypatch):
    monkeypatch.delenv("MANTIS_METRICS_ENABLED", raising=False)
    assert get_metrics_enabled(default=False) is False
    assert get_metrics_enabled(default=True) is True


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_get_metrics_enabled_true_values(monkeypatch, value):
    monkeypatch.setenv("MANTIS_METRICS_ENABLED", value)
    assert get_metrics_enabled(default=False) is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
def test_get_metrics_enabled_false_values(monkeypatch, value):
    monkeypatch.setenv("MANTIS_METRICS_ENABLED", value)
    assert get_metrics_enabled(default=True) is False


# ---------------------------------------------------------------------------
# DNSConfig (#109): server-side resolver profiles for dns_lookup.
# Parsing must never touch the network -- every test here is pure
# environment-variable/string handling.
# ---------------------------------------------------------------------------


def _clear_dns_resolver_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MANTIS_DNS_RESOLVER_"):
            monkeypatch.delenv(key, raising=False)


def test_dns_config_from_env_parses_a_single_profile(monkeypatch):
    _clear_dns_resolver_env(monkeypatch)
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_INTERNAL", "172.30.0.53,172.30.0.54")

    cfg = DNSConfig.from_env()

    assert cfg.resolve_profile("internal") == ("172.30.0.53", "172.30.0.54")


def test_dns_config_from_env_parses_multiple_profiles(monkeypatch):
    _clear_dns_resolver_env(monkeypatch)
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_INTERNAL", "172.30.0.53")
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_CLOUDFLARE", "1.1.1.1,1.0.0.1")
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_GOOGLE", "8.8.8.8,8.8.4.4")

    cfg = DNSConfig.from_env()

    assert cfg.resolve_profile("internal") == ("172.30.0.53",)
    assert cfg.resolve_profile("cloudflare") == ("1.1.1.1", "1.0.0.1")
    assert cfg.resolve_profile("google") == ("8.8.8.8", "8.8.4.4")


def test_dns_config_resolve_profile_is_case_insensitive(monkeypatch):
    _clear_dns_resolver_env(monkeypatch)
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_INTERNAL", "172.30.0.53")

    cfg = DNSConfig.from_env()

    assert cfg.resolve_profile("INTERNAL") == ("172.30.0.53",)
    assert cfg.resolve_profile("Internal") == ("172.30.0.53",)


def test_dns_config_resolve_profile_returns_none_for_unknown_alias(monkeypatch):
    _clear_dns_resolver_env(monkeypatch)
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_INTERNAL", "172.30.0.53")

    cfg = DNSConfig.from_env()

    assert cfg.resolve_profile("nonexistent") is None


@pytest.mark.parametrize("alias", [123, None, [], {}])
def test_dns_config_resolve_profile_returns_none_for_a_non_string_alias(monkeypatch, alias):
    _clear_dns_resolver_env(monkeypatch)
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_INTERNAL", "172.30.0.53")

    cfg = DNSConfig.from_env()

    assert cfg.resolve_profile(alias) is None


def test_dns_config_from_env_ignores_unrelated_variables(monkeypatch):
    _clear_dns_resolver_env(monkeypatch)
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_INTERNAL", "172.30.0.53")
    monkeypatch.setenv("LITELLM_MODEL", "some-model")

    cfg = DNSConfig.from_env()

    assert set(cfg.profiles.keys()) == {"internal"}


def test_dns_config_from_env_with_no_profiles_configured_is_empty(monkeypatch):
    _clear_dns_resolver_env(monkeypatch)

    cfg = DNSConfig.from_env()

    assert cfg.profiles == {}
    assert cfg.resolve_profile("internal") is None


def test_dns_config_rejects_a_hostname_server_not_an_ip_literal():
    # A resolver's own address must never itself require DNS resolution
    # to reach -- this is caught at config-construction time, not
    # discovered later when a query inexplicably fails.
    with pytest.raises(ConfigurationError, match="internal"):
        DNSConfig(profiles={"internal": ("resolver.example.com",)})


def test_dns_config_rejects_an_empty_server_list():
    with pytest.raises(ConfigurationError):
        DNSConfig(profiles={"internal": ()})


def test_dns_config_rejects_an_empty_alias():
    with pytest.raises(ConfigurationError):
        DNSConfig(profiles={"": ("172.30.0.53",)})


def test_dns_config_accepts_ipv6_servers():
    cfg = DNSConfig(profiles={"internal": ("2001:db8::1", "::1")})
    assert cfg.resolve_profile("internal") == ("2001:db8::1", "::1")


def test_dns_config_parsing_performs_no_network_access(monkeypatch):
    # Constructing/parsing DNSConfig must never itself attempt to
    # contact a resolver -- proven by making any socket-level UDP send
    # fail loudly if it's ever attempted during from_env()/__post_init__.
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("DNSConfig parsing must never touch the network")

    monkeypatch.setattr(socket.socket, "sendto", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    _clear_dns_resolver_env(monkeypatch)
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_INTERNAL", "172.30.0.53,172.30.0.54")
    monkeypatch.setenv("MANTIS_DNS_RESOLVER_CLOUDFLARE", "1.1.1.1")

    cfg = DNSConfig.from_env()

    assert cfg.resolve_profile("internal") == ("172.30.0.53", "172.30.0.54")
