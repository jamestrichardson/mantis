import pytest


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tls_fixture_temp_files():
    """Remove every temporary CA/cert/key PEM file created by
    tests/_tls_fixtures.py (used by tests/test_tls.py,
    tests/test_tls_tools.py, and tests/test_http.py's TLS-wrapped
    scenarios) once, at the end of the whole test session -- never
    mid-session, since a path may still be in active use by a
    not-yet-run test. Addresses a Copilot review finding on PR #119:
    these files were previously created with ``delete=False`` and
    never cleaned up, leaking across full-suite runs."""
    yield
    import _tls_fixtures

    _tls_fixtures.cleanup_temp_files()


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
