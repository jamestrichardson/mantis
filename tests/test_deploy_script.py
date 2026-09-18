from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "standalone" / "mantis-deploy"


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
      *no-healthcheck) digest=nohealthcheck ;;
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
        elif [[ "$ref" == *sha256:nohealthcheck ]]; then
          # Simulates a container with no Docker healthcheck metadata
          # at all (a missing/misconfigured `healthcheck:` stanza) --
          # `docker inspect` reports "none" for this, forever, never
          # "healthy" or "unhealthy".
          rm -f "$S/health"
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


def test_missing_healthcheck_cannot_promote_a_deployment(tmp_path: Path) -> None:
    # A container reporting Docker health="none" (no healthcheck
    # metadata at all -- e.g. a misconfigured or missing
    # `healthcheck:` stanza) must fail closed, not be silently promoted
    # as if it had proven readiness. See mantis-deploy's
    # wait_for_health() -- only "healthy" may return success.
    env, root = _deployment(tmp_path)

    assert _run(env, "pr-59").returncode == 0
    failed = _run(env, "no-healthcheck")

    assert failed.returncode == 1
    assert "Rolling back automatically to pr-59" in failed.stdout
    assert (root / "state/current-tag").read_text().strip() == "pr-59"
    assert (root / "state/current-ref").read_text().strip().endswith("@sha256:aaa")


def test_latest_is_refused(tmp_path: Path) -> None:
    env, _ = _deployment(tmp_path)
    result = _run(env, "latest")
    assert result.returncode == 1
    assert "Refusing to deploy 'latest'" in result.stderr


# ---------------------------------------------------------------------------
# #98: deployment-contract compatibility. A previous image must only be
# combined with the currently active Compose/lifecycle contract when
# that image's own recorded contract is explicitly known to match --
# never inferred from its tag/SemVer, never assumed for legacy state
# that predates contract tracking.
# ---------------------------------------------------------------------------


def test_successful_deploy_rotates_tag_ref_and_contract_together(tmp_path: Path) -> None:
    env, root = _deployment(tmp_path)

    assert _run(env, "pr-59").returncode == 0
    assert (root / "state/current-contract").read_text().strip() == "1"
    assert not (root / "state/previous-contract").exists()

    assert _run(env, "pr-60").returncode == 0
    assert (root / "state/current-tag").read_text().strip() == "pr-60"
    assert (root / "state/current-contract").read_text().strip() == "1"
    assert (root / "state/previous-tag").read_text().strip() == "pr-59"
    assert (root / "state/previous-contract").read_text().strip() == "1"


def test_compatible_rollback_succeeds_and_rotates_contract(tmp_path: Path) -> None:
    env, root = _deployment(tmp_path)
    assert _run(env, "pr-59").returncode == 0
    assert _run(env, "pr-60").returncode == 0

    result = _run(env, "rollback")

    assert result.returncode == 0, result.stderr
    assert (root / "state/current-tag").read_text().strip() == "pr-59"
    assert (root / "state/current-contract").read_text().strip() == "1"
    assert (root / "state/previous-tag").read_text().strip() == "pr-60"
    assert (root / "state/previous-contract").read_text().strip() == "1"


def test_incompatible_contract_refuses_automatic_rollback_before_apply_image(tmp_path: Path) -> None:
    # Simulates the exact #98 incident: the currently-running "good"
    # deployment's own recorded contract doesn't match what's currently
    # active (e.g. it predates a lifecycle-breaking compose.yaml change).
    # A failed new deploy must refuse to automatically roll back to it
    # rather than starting it under the incompatible current contract.
    env, root = _deployment(tmp_path)
    assert _run(env, "pr-59").returncode == 0
    assert _run(env, "pr-60").returncode == 0
    (root / "state/current-contract").write_text("0\n")  # pr-60 now "recorded" as contract 0

    failed = _run(env, "bad")

    assert failed.returncode == 1
    assert "Refusing automatic rollback to pr-60" in failed.stderr
    assert "does not match the currently active contract" in failed.stderr
    # Never combined the previous image with the current contract to
    # "find out" -- and never silently promoted a rollback.
    assert "Rolling back automatically" not in failed.stdout
    # State is exactly as it was before this failed attempt -- not
    # mutated to claim any rollback happened.
    assert (root / "state/current-tag").read_text().strip() == "pr-60"
    assert (root / "state/current-contract").read_text().strip() == "0"
    # The failed, incompatible-rollback-target candidate is stopped, not
    # left running/restart-looping.
    assert (root / "state/container-state").read_text().strip() == "exited"


def test_legacy_state_with_no_recorded_contract_refuses_automatic_rollback(tmp_path: Path) -> None:
    # Legacy state from before #98 existed has no current-contract file
    # at all -- this must be treated as unknown/unsafe, never as
    # implicitly compatible.
    env, root = _deployment(tmp_path)
    assert _run(env, "pr-59").returncode == 0
    assert _run(env, "pr-60").returncode == 0
    (root / "state/current-contract").unlink()

    failed = _run(env, "bad")

    assert failed.returncode == 1
    assert "Refusing automatic rollback to pr-60" in failed.stderr
    assert "(unknown)" in failed.stderr
    assert (root / "state/current-tag").read_text().strip() == "pr-60"
    assert not (root / "state/current-contract").exists()
    assert (root / "state/container-state").read_text().strip() == "exited"


def test_manual_rollback_refuses_incompatible_contract(tmp_path: Path) -> None:
    env, root = _deployment(tmp_path)
    assert _run(env, "pr-59").returncode == 0
    assert _run(env, "pr-60").returncode == 0
    (root / "state/previous-contract").write_text("0\n")  # pr-59 now "recorded" as contract 0

    result = _run(env, "rollback")

    assert result.returncode == 1
    assert "Refusing rollback to pr-59" in result.stderr
    assert "does not match the currently active contract" in result.stderr
    assert "has NOT been touched" in result.stderr
    # The currently running deployment (pr-60) is untouched.
    assert (root / "state/current-tag").read_text().strip() == "pr-60"
    assert (root / "state/previous-tag").read_text().strip() == "pr-59"
    assert (root / "state/previous-contract").read_text().strip() == "0"


def test_manual_rollback_refuses_legacy_state_with_no_recorded_contract(tmp_path: Path) -> None:
    env, root = _deployment(tmp_path)
    assert _run(env, "pr-59").returncode == 0
    assert _run(env, "pr-60").returncode == 0
    (root / "state/previous-contract").unlink()

    result = _run(env, "rollback")

    assert result.returncode == 1
    assert "Refusing rollback to pr-59" in result.stderr
    assert "(unknown)" in result.stderr
    assert (root / "state/current-tag").read_text().strip() == "pr-60"


def test_automatic_and_manual_rollback_share_the_same_refusal_wording(tmp_path: Path) -> None:
    # Both compatibility gates must reject an incompatible/unknown
    # contract identically -- proving they're the same check, not two
    # independently-maintained ones that could silently drift apart.
    shared_phrase = "does not match the currently active contract"

    env, root = _deployment(tmp_path)
    assert _run(env, "pr-59").returncode == 0
    assert _run(env, "pr-60").returncode == 0
    (root / "state/current-contract").write_text("0\n")
    automatic = _run(env, "bad")
    assert shared_phrase in automatic.stderr

    second_tmp_path = tmp_path / "second"
    second_tmp_path.mkdir()
    env2, root2 = _deployment(second_tmp_path)
    assert _run(env2, "pr-59").returncode == 0
    assert _run(env2, "pr-60").returncode == 0
    (root2 / "state/previous-contract").write_text("0\n")
    manual = _run(env2, "rollback")
    assert shared_phrase in manual.stderr


def test_failed_rollback_target_does_not_mutate_state(tmp_path: Path) -> None:
    # The rollback *target* itself failing health/readiness (distinct
    # from a contract refusal) must also never touch current/previous
    # state -- die() happens before any write_state call.
    env, root = _deployment(tmp_path)
    assert _run(env, "pr-59").returncode == 0
    assert _run(env, "pr-60").returncode == 0
    # Contrive a previous target that is contract-compatible but fails
    # its own health check once redeployed.
    (root / "state/previous-ref").write_text(
        "ghcr.io/jamestrichardson/mantis@sha256:bad\n"
    )

    result = _run(env, "rollback")

    assert result.returncode == 1
    assert "Rollback target failed health/readiness check; current state files were not changed" in result.stderr
    assert (root / "state/current-tag").read_text().strip() == "pr-60"
    assert (root / "state/previous-tag").read_text().strip() == "pr-59"
    assert (
        root / "state/previous-ref"
    ).read_text().strip() == "ghcr.io/jamestrichardson/mantis@sha256:bad"
