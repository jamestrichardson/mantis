# Standalone deployment

A single-host, Docker Compose–based deployment is intentionally manual
and explicit. GitHub Actions publishes PR, `dev`, SHA, and release
images to GHCR; an operator chooses exactly which tag to run, on
whichever host they've designated for it. Nothing under `deploy/standalone/`
or in this page encodes a specific hostname — host identity is your own
operator configuration/inventory, not Mantis's architecture (#87).

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
                    Docker Compose runs `mantis serve`
                                      |
                         health gate polls /readyz
```

A requested mutable tag such as `pr-123` is resolved to an immutable GHCR digest before the container is started. This matters when the same PR tag is rebuilt: each deployment runs the exact digest that was pulled, and rollback can return to the previous digest even when its tag has since moved.

## Service lifecycle (#21)

The deployed container runs `mantis serve` (#21/#83) as PID1 — the real,
persistent Mantis application, not a resident `sleep infinity` process.
It:

- owns startup/configuration validation, the FastAPI app (#83), and the
  persistent Prometheus registry/server (#66) for the process's entire
  lifetime;
- serves `/healthz`/`/readyz` and the versioned `/api/v1` surface on
  `MANTIS_API_PORT` (default `8080`);
- handles `SIGTERM`/`SIGINT` (delivered by `docker compose stop`/
  `restart`/`down`, and by `mantis-deploy` recreating the service)
  gracefully — see [Graceful shutdown](#graceful-shutdown) below.

There is exactly one process per container performing all of this —
no separate daemon for metrics, the API, or agent execution.

## Network exposure and TLS

`mantis serve` speaks plain HTTP; it does not terminate TLS. The
reference Compose file therefore publishes the API port bound to
`127.0.0.1` on the deployment host only:

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

so the bearer-token-protected API is never reachable directly from
outside the host by accident. For access beyond the host itself, put a
TLS-terminating reverse proxy in front of `127.0.0.1:8080` — see
[docs/api.md](api.md#transport-security-tls) for the supported pattern.
The metrics port (`9108`) remains published on all interfaces, matching
its pre-#21 exposure — it carries no credential.

## Initial setup

Requirements on the deployment host:

- Docker Engine
- Docker Compose v2 (`docker compose`)
- `flock` (normally provided by `util-linux`)
- network access to `ghcr.io`

From a Mantis checkout:

```bash
sudo bash deploy/standalone/install.sh
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

Edit `/opt/mantis/runtime.env` and replace credential placeholders — including `MANTIS_API_TOKEN` (the client-facing credential clients use to call the deployed service; see [docs/api.md](api.md#authentication) and [docs/configuration.md](configuration.md#api-server-mantis-serve)), entirely separate from the LiteLLM/AWX/integration credentials also in that file. Keep that file root-readable only (`0600`). Application secrets are injected into the container at runtime and are never written to deployment state.

If the GHCR package is private, authenticate Docker once with a credential that has package-read access. Do not put the token in `runtime.env` or `deploy.env`:

```bash
read -rsp 'GHCR token: ' GHCR_TOKEN; echo
printf '%s' "$GHCR_TOKEN" | sudo docker login ghcr.io -u <your-github-username> --password-stdin
unset GHCR_TOKEN
```

If the package is public, no registry login is required.

## Deploy an image

Deploy a PR build:

```bash
sudo mantis-deploy pr-123
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
4. records the previous known-good tag/digest/contract;
5. recreates the `mantis` Compose service using the immutable digest;
6. waits for a bounded Docker health/readiness result — the real `/readyz` contract (see below), not a synthetic substitute;
7. records the new tag/digest/contract only after the health check succeeds — the contract recorded is always the currently active `DEPLOYMENT_CONTRACT_VERSION`, since that's what this deployment was just proven against;
8. automatically restores the previous known-good digest if the new container fails health — **but only if that previous deployment's own recorded contract is known to match the currently active one** (#98); otherwise the failed candidate is stopped and left unresolved for manual recovery rather than risking an incompatible-lifecycle restart loop. See [Deployment contract compatibility](#deployment-contract-compatibility-98).

Deployments are serialized with a local `flock`, so two operators cannot update the container at the same time.

## Running commands against the deployed service

The CLI is an HTTP client (#83) — `mantis <command>` inside the container talks to the same `mantis serve` process over `localhost`, exactly as it would from anywhere else with `MANTIS_API_URL`/`MANTIS_API_TOKEN` configured:

```bash
cd /opt/mantis
sudo docker compose --env-file deploy.env exec mantis mantis agents
sudo docker compose --env-file deploy.env exec mantis \
  mantis awx-troubleshooter 'Show me the last 3 failed AWX jobs.'
```

`MANTIS_API_URL`/`MANTIS_API_TOKEN` are already present in the container's own environment (`runtime.env`), so no extra configuration is needed for this exec-based usage.

`MANTIS_CONTAINER_COMMAND` in `deploy.env` remains available to override the container's default command for deliberate one-off debugging; it should not be needed for normal operation.

## Health/readiness

The Compose health check performs a real, bounded HTTP probe of `/readyz` (see [docs/api.md](api.md#health-and-readiness)) from inside the container:

```text
MANTIS_HEALTH_URL=http://127.0.0.1:8080/readyz
```

This is the default in `deploy.env.example` — `/readyz` reports `not_ready` only while mandatory local startup is still in progress or shutdown has begun, never because of an AWX/LiteLLM/Kubernetes/Prometheus/Loki outage (see [docs/api.md](api.md#health-and-readiness) for the full contract). `mantis-deploy` polls the resulting Docker health status; `MANTIS_HEALTH_TIMEOUT_SECONDS`/`MANTIS_HEALTH_INTERVAL_SECONDS` control how long it waits. Only a Docker-reported `healthy` status promotes a deployment — a container with no healthcheck metadata at all (`docker inspect` reports `none`, e.g. from a missing or misconfigured `healthcheck:` stanza) fails closed: it simply runs out the poll timeout and the deployment is reported as failed, exactly like a container that never passes its healthcheck. It is never treated as an acceptable substitute for a proven `/readyz` result.

### The probe itself (#97)

`deploy/standalone/healthcheck.sh` is baked into the runtime image at `/usr/local/bin/mantis-healthcheck` and invoked directly by Compose's `healthcheck.test` — a raw HTTP/1.0 request over bash's own `/dev/tcp` pseudo-device (a builtin, not a new process or package): connect, send `GET <path> HTTP/1.0`, read one response line, and treat any `2xx` status as success. Anything else — a non-`2xx` status, a malformed/non-HTTP response line, a refused/failed connection, or no response within its own bounded read timeout (`MANTIS_HEALTHCHECK_PROBE_TIMEOUT_SECONDS`, default `3`) — is a failure, exiting non-zero. It never weakens the check to process liveness, port-open-only, or `/healthz` — it is always a real `/readyz` HTTP round-trip. On failure it prints one fixed diagnostic line to stderr (e.g. the HTTP status or "no response within Ns") — never response headers/body, and never a credential (`/readyz` itself is always unauthenticated, so none could leak here regardless).

This replaced an earlier `python -c 'import urllib.request; ...'` probe: on a slow/constrained host, Python interpreter startup and import machinery alone were measured taking 1.4s–5.8s, which could exceed the healthcheck's own `timeout` even though `/readyz` itself had already returned `200` — Docker would mark a genuinely ready container unhealthy purely from probe-process overhead, not application state. The bash-only probe adds no new package to the runtime image and, once the service is actually ready, completes in single-digit milliseconds even under heavy CPU throttling (verified with `docker run --cpus=0.1`).

Two independent timeouts are both intentional, not redundant:

- The probe's **own** `MANTIS_HEALTHCHECK_PROBE_TIMEOUT_SECONDS` (default `3`) bounds the read after a successful TCP connect — the realistic hang case (the connection is accepted, but the server is slow/blocked before writing a response). The target is always the loopback interface in the shipped `compose.yaml`, whose `connect()` is synchronous and never itself hangs.
- Compose's `healthcheck.timeout: 5s` bounds the *whole* probe process from Docker's side, leaving ~2s of margin beyond the probe's own budget for bash/exec startup — not because the probe is expensive, but so a slow host still has headroom. `start_period: 5s` gives `mantis serve`'s own startup the same kind of margin before failures start counting toward `retries` (`20`, at `interval: 2s`).

Both were chosen with realistic slow-host headroom in mind, not as a substitute for the probe being cheap — the workaround from the original incident (bumping the Docker healthcheck timeout to 10s while keeping the expensive probe) is no longer necessary and should not be reintroduced.

## Graceful shutdown

`docker compose stop` (used implicitly by `mantis-deploy` when recreating the service, and by `mantis-deploy rollback`) sends `SIGTERM` to the container's PID1 — `mantis serve` itself, not an intermediate shell, since the Compose command uses `exec mantis serve`. The exact sequence, precisely, because the ordering matters and is easy to get wrong:

1. **Synchronously, in the signal handler itself** (`mantis.api.server._DrainingAwareServer.handle_exit`) — before uvicorn does anything else — `/readyz` flips to `not_ready` (`reason: "shutting_down"`) and `POST /api/v1/runs` starts returning `503`. This is deliberately *not* tied to uvicorn's own ASGI lifespan-shutdown phase, which normally only runs *after* the steps below — that would make the readiness transition depend on however long draining in-flight connections happens to take, defeating "readiness transitions to not-ready before new work is rejected" as a real guarantee rather than a usual case.
2. uvicorn stops accepting new TCP connections immediately.
3. Any request/run already in progress gets up to `MANTIS_API_SHUTDOWN_GRACE_PERIOD_SECONDS` (default `30`) to finish. If it hasn't by then, uvicorn cancels the *waiting* task — which stops the HTTP response, but cannot stop a still-running `AgentRuntime.run()` call, since Python cannot preempt arbitrary synchronous code (see [docs/reliability.md](reliability.md)). That run keeps executing in an abandoned **daemon** thread (`mantis.api.invocation._run_in_daemon_thread`); being a daemon thread is what lets the next step happen on schedule instead of hanging.
4. The process exits — **on schedule, even if step 3's run is still running** in its abandoned thread. That run's result is simply lost; it does not extend the shutdown window.

Docker's own `stop_grace_period` (`deploy/standalone/compose.yaml`, `MANTIS_STOP_GRACE_PERIOD` in `deploy.env`, default `40s`) must stay comfortably above `MANTIS_API_SHUTDOWN_GRACE_PERIOD_SECONDS` (default `30s`) — if you raise the latter, raise the former to match (keep at least a 10s buffer for OS/Docker overhead), or Docker will `SIGKILL` the process before Mantis's own graceful window has had a chance to elapse.

## Status

```bash
sudo mantis-deploy status
```

Status reports:

- configured tag and immutable image reference;
- the currently active deployment contract version (#98);
- current known-good tag/digest/contract;
- previous known-good tag/digest/contract;
- actual running image reference and image ID;
- Docker container state and health;
- container start timestamp.

A `contract` value of `unknown` means that side (current or previous) either predates #98 or was never recorded — see [Deployment contract compatibility](#deployment-contract-compatibility-98).

## Deployment contract compatibility (#98)

A previous image and the currently installed `compose.yaml`/`mantis-deploy` are two independently-evolving things — nothing guarantees an older image actually supports whatever the *current* Compose `command`/`healthcheck` expects of it. Mantis 1.7 introduced `mantis serve` as PID1 (#21); a 1.5.0 image predates that entirely. During the real 1.7.0 rollout, an automatic rollback recreated a 1.5.0 container using the 1.7 Compose contract, and it entered a restart loop (`Unknown agent: 'serve'`) — the old image had no `serve` subcommand for the new `command:` to invoke.

`mantis-deploy` fixes this with an explicit **deployment contract version** — `DEPLOYMENT_CONTRACT_VERSION` in the script itself (currently `"1"`), bumped deliberately whenever a future change to `compose.yaml`'s `command`/`healthcheck` (or an equivalent lifecycle-shape change) means an older image can no longer be assumed to work under it. **This is never inferred from the image's own tag or SemVer** — a 1.5.0 vs. 1.7.0 tag doesn't itself tell you their lifecycle contracts differ; only a deliberate, reviewed bump of this constant does.

Every successful deployment now records which contract was active at the time, alongside its tag/ref (`current-contract`/`previous-contract` — see [Files and state](#files-and-state) below). Both automatic rollback (after a failed `mantis-deploy <tag>`) and manual `mantis-deploy rollback` apply the exact same rule, checked **before** any `docker pull`/`compose up` for the rollback target — never by starting the old image under the current contract "to find out," which is exactly the failure mode this exists to prevent:

- The target's recorded contract must be present and **exactly equal** to the currently active `DEPLOYMENT_CONTRACT_VERSION`. Anything else — a genuine mismatch, or an empty/missing value — is treated as **incompatible**, never as compatible-by-default.
- **Legacy state that predates this feature** (installed before #98, so `current-contract`/`previous-contract` were never written) reads back empty, which this rule treats identically to a known mismatch: unsafe, not compatible. There is no grace period or implicit migration that assumes an existing deployment is fine — an operator upgrading `mantis-deploy` (via `install.sh`) onto a pre-#98 install will have its first automatic/manual rollback attempt refused until a fresh, successful `mantis-deploy <tag>` records a real contract for the running deployment.
- On refusal, `mantis-deploy` **never** pulls or starts the incompatible target, **never** changes `current-*`/`previous-*` state to claim a rollback happened, and prints the exact tag/ref and a manual recovery command. For an automatic-rollback refusal (i.e. a brand-new deploy already failed its own health check), the failed candidate container is stopped (`compose stop`) so it doesn't sit restart-looping — `current-*` state is left describing the last known-good deployment throughout, even though nothing is currently running. For a manual `mantis-deploy rollback` refusal, the currently running container is never touched at all.
- `mantis-deploy` only ever reports rollback success once the restored target has actually passed the same `/readyz` health gate as a normal deploy (unchanged from before #98) — a refused rollback is always a distinct, explicit failure, never silently reported as success.

## Rollback

```bash
sudo mantis-deploy rollback
```

Rollback deploys the exact immutable digest recorded for the previous known-good deployment, validated against the same `/readyz` health gate as a normal deploy, and against the [deployment contract compatibility](#deployment-contract-compatibility-98) rule above. It does not re-resolve the old tag, so rollback remains deterministic even for mutable PR tags.

A successful rollback swaps current/previous state (tag, ref, and contract together), allowing another `rollback` to move back if needed.

## Deployment events

Each deployment/rollback emits a compact JSON `mantis_deployment` event to the host journal. When the container is running, the script also writes the event into container stdout so Alloy's existing Docker log collection sends it to Loki — no second logging backend was added for this.

Every event now also carries `deployment_contract`: the currently active `DEPLOYMENT_CONTRACT_VERSION` (a fixed, low-cardinality string, never a secret) — most useful on a refused rollback, where `outcome` is `refused_incompatible_contract`:

```json
{"event":"mantis_deployment","timestamp":"...","action":"rollback","outcome":"refused_incompatible_contract","requested_tag":"1.5.0","image_ref":"ghcr.io/jamestrichardson/mantis@sha256:...","deployment_contract":"1","host":"..."}
```

Example Loki queries:

```logql
{container="mantis"} |= "mantis_deployment"

# Refused rollbacks specifically
{container="mantis"} | json | outcome="refused_incompatible_contract"
```

## Files and state

`deploy.env` contains deployment configuration only. `runtime.env` contains application configuration/secrets. State files under `/opt/mantis/state/` contain tags, immutable image references, and (#98) the deployment contract each was recorded under:

```text
current-tag
current-ref
current-contract
previous-tag
previous-ref
previous-contract
```

Deleting state does not delete Docker images, but it removes the script's knowledge of the previous known-good deployment and therefore disables automatic/manual rollback until another successful deployment establishes state. A `-contract` file missing while its paired `-tag`/`-ref` files exist is exactly the legacy/pre-#98 shape described above — treated as an unknown, incompatible contract, never assumed safe.

## Updating the deployment tooling

After pulling a newer Mantis checkout, rerun:

```bash
sudo bash deploy/standalone/install.sh
```

Existing runtime/deployment environment files are preserved while `compose.yaml` and `/usr/local/bin/mantis-deploy` are refreshed. If the refreshed `mantis-deploy` bumped `DEPLOYMENT_CONTRACT_VERSION`, the previously-recorded `current-contract` (from before this refresh) no longer matches it by design — see [Deployment contract compatibility](#deployment-contract-compatibility-98). The currently running deployment keeps running untouched; only a *rollback* attempt would be affected, and only until the next successful `mantis-deploy <tag>` records a fresh, matching contract for whatever's running.

## Out of scope

The current deployment is deliberately on-demand and single-host. Automatic deployment on release, GitHub Actions SSH deployment, self-hosted runners, Kubernetes, GitOps, multi-node HA, and a distributed task queue are all deferred (see #21's non-goals); any future automation should invoke the same explicit-tag/digest deployment primitive rather than creating a second path.
