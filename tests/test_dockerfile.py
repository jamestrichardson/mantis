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
    # EXPOSE stays even though the server isn't started by default — it's
    # documentational (and correct for anyone who does set
    # MANTIS_METRICS_ENABLED=true), not a behavior switch.
    assert "EXPOSE 9108" in _content()
