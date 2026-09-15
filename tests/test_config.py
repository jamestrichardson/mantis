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
