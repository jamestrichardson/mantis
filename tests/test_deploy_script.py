from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "degobah" / "mantis-deploy"


def _write_fake_docker(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        r'''#!/usr/bin/env bash
set -euo pipefail
S="${FAKE_DOCKER_STATE:?}"
cmd="$1"; shift
case "$cmd" in
  pull)
    echo "$1" > "$S/last-pull"
    ;;
  image)
    [[ "$1" == "inspect" ]]
    ref="${@: -1}"
    if [[ "$ref" == *@sha256:* ]]; then
      echo "$ref"
      exit 0
    fi
    repo="${ref%:*}"
    case "$ref" in
      *pr-59) digest=aaa ;;
      *pr-60) digest=bbb ;;
      *bad) digest=bad ;;
      *) digest=ccc ;;
    esac
    echo "${repo}@sha256:${digest}"
    ;;
  compose)
    if [[ "${1:-}" == "version" ]]; then exit 0; fi
    args=("$@")
    envfile=""
    action=""
    for ((i=0; i<${#args[@]}; i++)); do
      case "${args[$i]}" in
        --env-file) ((i+=1)); envfile="${args[$i]}" ;;
        up) action=up ;;
        stop) action=stop ;;
        logs) action=logs ;;
      esac
    done
    case "$action" in
      up)
        ref="$(grep '^MANTIS_IMAGE_REF=' "$envfile" | cut -d= -f2-)"
        echo "$ref" > "$S/image"
        echo running > "$S/container-state"
        if [[ "$ref" == *sha256:bad ]]; then
          echo unhealthy > "$S/health"
        else
          echo healthy > "$S/health"
        fi
        ;;
      stop) echo exited > "$S/container-state" ;;
      logs) echo fake-container-logs ;;
    esac
    ;;
  inspect)
    format="${2:-}"
    case "$format" in
      *State.Status*) cat "$S/container-state" 2>/dev/null || exit 1 ;;
      *State.Health*) cat "$S/health" 2>/dev/null || echo none ;;
      *Config.Image*) cat "$S/image" 2>/dev/null || exit 1 ;;
      *'.Image'*) echo sha256:local-image-id ;;
      *State.StartedAt*) echo 2026-09-16T00:00:00Z ;;
    esac
    ;;
  exec)
    cat >> "$S/events"
    ;;
  *)
    echo "unexpected docker invocation: $cmd $*" >&2
    exit 2
    ;;
esac
'''
    )
    docker.chmod(0o755)


def _deployment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    root = tmp_path / "deploy"
    state = root / "state"
    fake_bin = tmp_path / "bin"
    root.mkdir()
    state.mkdir()
    fake_bin.mkdir()

    (root / "compose.yaml").write_text("services:\n  mantis:\n    image: test\n")
    (root / "deploy.env").write_text(
        "\n".join(
            [
                "MANTIS_IMAGE_REPOSITORY=ghcr.io/jamestrichardson/mantis",
                "MANTIS_IMAGE_REF=ghcr.io/jamestrichardson/mantis:__not_deployed__",
                "MANTIS_DEPLOYED_TAG=__not_deployed__",
                "MANTIS_HEALTH_TIMEOUT_SECONDS=2",
                "MANTIS_HEALTH_INTERVAL_SECONDS=1",
                "",
            ]
        )
    )
    _write_fake_docker(fake_bin)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_DOCKER_STATE"] = str(state)
    env["MANTIS_DEPLOY_ROOT"] = str(root)
    return env, root


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_deploy_resolves_tag_to_digest_and_rolls_back_by_digest(tmp_path: Path) -> None:
    env, root = _deployment(tmp_path)

    first = _run(env, "pr-59")
    assert first.returncode == 0, first.stderr
    assert (root / "state/current-tag").read_text().strip() == "pr-59"
    assert (root / "state/current-ref").read_text().strip().endswith("@sha256:aaa")
    assert "MANTIS_IMAGE_REF=ghcr.io/jamestrichardson/mantis@sha256:aaa" in (root / "deploy.env").read_text()

    second = _run(env, "pr-60")
    assert second.returncode == 0, second.stderr
    assert (root / "state/current-tag").read_text().strip() == "pr-60"
    assert (root / "state/previous-tag").read_text().strip() == "pr-59"

    rollback = _run(env, "rollback")
    assert rollback.returncode == 0, rollback.stderr
    assert (root / "state/current-tag").read_text().strip() == "pr-59"
    assert (root / "state/current-ref").read_text().strip().endswith("@sha256:aaa")
    assert (root / "state/previous-tag").read_text().strip() == "pr-60"


def test_failed_health_check_restores_previous_known_good_digest(tmp_path: Path) -> None:
    env, root = _deployment(tmp_path)

    assert _run(env, "pr-59").returncode == 0
    failed = _run(env, "bad")

    assert failed.returncode == 1
    assert "Rolling back automatically to pr-59" in failed.stdout
    assert (root / "state/current-tag").read_text().strip() == "pr-59"
    assert (root / "state/current-ref").read_text().strip().endswith("@sha256:aaa")
    assert "MANTIS_IMAGE_REF=ghcr.io/jamestrichardson/mantis@sha256:aaa" in (root / "deploy.env").read_text()


def test_latest_is_refused(tmp_path: Path) -> None:
    env, _ = _deployment(tmp_path)
    result = _run(env, "latest")
    assert result.returncode == 1
    assert "Refusing to deploy 'latest'" in result.stderr
