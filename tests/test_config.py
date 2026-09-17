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
    AWXConfig,
    ConfigurationError,
    LiteLLMConfig,
    ReliabilityConfig,
    Secret,
)


def test_litellm_model_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("LITELLM_MODEL", raising=False)

    cfg = LiteLLMConfig.from_env()

    assert cfg.model == DEFAULT_LITELLM_MODEL == "qwen3-opencode:latest"


def test_litellm_model_uses_explicit_value_when_set(monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "some-other-model")

    cfg = LiteLLMConfig.from_env()

    assert cfg.model == "some-other-model"


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
