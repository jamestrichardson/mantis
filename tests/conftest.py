import pytest


@pytest.fixture(autouse=True)
def _mantis_env(monkeypatch):
    """Provide baseline env vars so config loading never hits real
    environment/`.env` state during tests."""
    monkeypatch.setenv("AWX_URL", "https://awx.example.test")
    monkeypatch.setenv("AWX_TOKEN", "test-token")
    monkeypatch.setenv("AWX_VERIFY_SSL", "true")
    monkeypatch.setenv("LITELLM_URL", "http://localhost:4000")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_MODEL", "test-model")
