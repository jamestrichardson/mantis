# Deploying Mantis on degobah

The standalone deployment on `degobah.cosprings.teknofile.net` is intentionally manual and explicit. GitHub Actions publishes PR, `dev`, SHA, and release images to GHCR; an operator chooses exactly which tag to run.

`latest` is never a deployment source of truth. `mantis-deploy` refuses it.

## Model

```text
GitHub Actions -> ghcr.io/jamestrichardson/mantis:<tag>
                                      |
                                      v
                         mantis-deploy <tag>
                                      |
                              docker pull
                                      |
                         resolve tag -> digest
                                      |
                         Docker Compose on degobah
```

A requested mutable tag such as `pr-59` is resolved to an immutable GHCR digest before the container is started. This matters when the same PR tag is rebuilt: each deployment runs the exact digest that was pulled, and rollback can return to the previous digest even when its tag has since moved.

## Initial setup

Requirements on degobah:

- Docker Engine
- Docker Compose v2 (`docker compose`)
- `flock` (normally provided by `util-linux`)
- network access to `ghcr.io`

From a Mantis checkout:

```bash
sudo bash deploy/degobah/install.sh
```

This installs:

```text
/opt/mantis/
├── compose.yaml
├── deploy.env
├── runtime.env
└── state/

/usr/local/bin/mantis-deploy
```

The installer updates the checked-in Compose file and deploy script, but preserves an existing `deploy.env` and `runtime.env`.

Edit `/opt/mantis/runtime.env` and replace credential placeholders. Keep that file root-readable only (`0600`). Application secrets are injected into the container at runtime and are never written to deployment state.

If the GHCR package is private, authenticate Docker once with a credential that has package-read access. Do not put the token in `runtime.env` or `deploy.env`:

```bash
read -rsp 'GHCR token: ' GHCR_TOKEN; echo
printf '%s' "$GHCR_TOKEN" | sudo docker login ghcr.io -u james-t-richardson-ii --password-stdin
unset GHCR_TOKEN
```

If the package is public, no registry login is required.

## Deploy an image

Deploy a PR build:

```bash
sudo mantis-deploy pr-59
```

Deploy a release:

```bash
sudo mantis-deploy 1.0.0
```

Other explicit published tags such as `dev` or `sha-<short-sha>` also work. The only intentionally rejected valid Docker tag is `latest`.

A deploy performs these steps:

1. validates the tag;
2. pulls `ghcr.io/jamestrichardson/mantis:<tag>`;
3. resolves the pulled tag to an immutable `repo@sha256:...` reference;
4. records the previous known-good tag/digest;
5. recreates the `mantis` Compose service using the immutable digest;
6. waits for a bounded Docker health/readiness result;
7. records the new tag/digest only after the health check succeeds;
8. automatically restores the previous known-good digest if the new container fails health.

Deployments are serialized with a local `flock`, so two operators cannot update the container at the same time.

## Current CLI-oriented runtime

Mantis is currently a CLI-oriented image, not yet a long-running API/service. The default Compose command therefore keeps the selected image resident with `sleep infinity`, allowing quick testing of the exact deployed PR/release image:

```bash
cd /opt/mantis
sudo docker compose --env-file deploy.env exec mantis mantis --help
sudo docker compose --env-file deploy.env exec mantis \
  mantis awx-troubleshooter 'Show me the last 3 failed AWX jobs.'
```

When Mantis gains a long-running service command, set `MANTIS_CONTAINER_COMMAND` in `/opt/mantis/deploy.env`; the tag/digest deployment and rollback mechanism does not need to change.

## Health/readiness

Today the Compose health check verifies process liveness. When the Mantis service endpoint from #39 is available, configure:

```text
MANTIS_HEALTH_URL=http://127.0.0.1:9108/metrics
```

in `/opt/mantis/deploy.env`. The same Docker health check will then perform an in-container HTTP readiness probe with a bounded timeout.

`MANTIS_HEALTH_TIMEOUT_SECONDS` and `MANTIS_HEALTH_INTERVAL_SECONDS` control how long `mantis-deploy` waits for Docker health.

## Status

```bash
sudo mantis-deploy status
```

Status reports:

- configured tag and immutable image reference;
- current known-good tag/digest;
- previous known-good tag/digest;
- actual running image reference and image ID;
- Docker container state and health;
- container start timestamp.

## Rollback

```bash
sudo mantis-deploy rollback
```

Rollback deploys the exact immutable digest recorded for the previous known-good deployment. It does not re-resolve the old tag, so rollback remains deterministic even for mutable PR tags.

A successful rollback swaps current/previous state, allowing another `rollback` to move back if needed.

## Deployment events

Each deployment/rollback emits a compact JSON `mantis_deployment` event to the host journal. When the container is running, the script also writes the event into container stdout so Alloy's existing Docker log collection sends it to Loki.

Example Loki query:

```logql
{host="degobah", container="mantis"} |= "mantis_deployment"
```

## Files and state

`deploy.env` contains deployment configuration only. `runtime.env` contains application configuration/secrets. State files under `/opt/mantis/state/` contain only tags and immutable image references:

```text
current-tag
current-ref
previous-tag
previous-ref
```

Deleting state does not delete Docker images, but it removes the script's knowledge of the previous known-good deployment and therefore disables automatic/manual rollback until another successful deployment establishes state.

## Updating the deployment tooling

After pulling a newer Mantis checkout, rerun:

```bash
sudo bash deploy/degobah/install.sh
```

Existing runtime/deployment environment files are preserved while `compose.yaml` and `/usr/local/bin/mantis-deploy` are refreshed.

## Out of scope

The current deployment is deliberately on-demand. Automatic deployment on release, GitHub Actions SSH deployment, self-hosted runners, Kubernetes, and GitOps are deferred; any future automation should invoke the same explicit-tag/digest deployment primitive rather than creating a second path.
