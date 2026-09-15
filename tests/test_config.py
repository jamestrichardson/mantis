"""Tests for mantis.config: the local-dev .env.* file convention and
config defaulting behavior.
"""

from __future__ import annotations

import os
from pathlib import Path

import mantis.config as config
from mantis.config import DEFAULT_LITELLM_MODEL, LiteLLMConfig


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
