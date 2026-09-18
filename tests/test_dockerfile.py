"""Regression guard for the Dockerfile's metrics-server behavior.

Not a build test (see .github/workflows/ci.yml's docker-build job for
that) — just a cheap, fast check that a future edit doesn't silently
reintroduce ENV MANTIS_METRICS_ENABLED=true, which would make every
short-lived `mantis` invocation in the image try to bind :9108 by
default. See docs/observability.md#current-status-of-metrics.
"""

from __future__ import annotations

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def _content() -> str:
    return DOCKERFILE.read_text()


def test_dockerfile_does_not_enable_metrics_by_default():
    # Check actual ENV instruction lines, not just any mention of the
    # variable name — the file legitimately references it in a comment
    # explaining the opt-in.
    env_lines = [line for line in _content().splitlines() if line.strip().startswith("ENV ")]
    assert not any("MANTIS_METRICS_ENABLED" in line for line in env_lines), env_lines


def test_dockerfile_still_documents_the_metrics_port():
    # EXPOSE stays even though a one-shot CLI invocation doesn't start
    # the metrics server by default — it's documentational (and correct
    # for `mantis serve`, which does), not a behavior switch.
    assert "EXPOSE 9108" in _content()


def test_dockerfile_documents_the_api_port():
    assert "EXPOSE 8080" in _content()


def test_dockerfile_default_command_starts_the_persistent_service():
    # #21: the production container must run the real Mantis service as
    # PID1 -- no resident `sleep infinity`, no CLI --help default.
    assert 'CMD ["serve"]' in _content()
    assert "sleep infinity" not in _content()


def test_dockerfile_bakes_in_the_lightweight_healthcheck_script():
    # #97: Docker's healthcheck (compose.yaml) runs this script *inside*
    # the container, so it must be copied into the image (not just
    # exist in the repo) and made executable.
    content = _content()
    assert "COPY deploy/standalone/healthcheck.sh /usr/local/bin/mantis-healthcheck" in content
    assert "chmod +x /usr/local/bin/mantis-healthcheck" in content


def test_dockerfile_does_not_reintroduce_the_slow_python_healthcheck_probe():
    # #97: a `python -c 'import urllib.request; ...'` probe's interpreter
    # startup/import overhead alone could exceed the healthcheck's
    # configured timeout on a slow host -- see deploy/standalone/healthcheck.sh.
    # Checks for the actual removed invocation, not just a mention of the
    # module name (this file's own comments legitimately reference it).
    assert "urllib.request.urlopen" not in _content()
