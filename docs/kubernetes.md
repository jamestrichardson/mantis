# Kubernetes evidence tools

Mantis's first Kubernetes evidence layer (#18): four read-only,
bounded, provenance-tagged tools that answer narrow questions about a
configured cluster's current state:

> What does the Kubernetes API server currently report about this
> namespace's pods, this namespace's Deployments, cluster nodes, or
> recent events?

This is not a general cluster-management or `kubectl` replacement. See
[Non-goals](#non-goals) below for what it deliberately does not do.

## Layering

Following the existing Mantis architecture (see
[docs/architecture.md](architecture.md)):

```
src/mantis/integrations/kubernetes.py
    Auth/config/API-client construction, the four bounded list calls,
    error classification (#15) -- no knowledge of tool schemas or which
    fields matter to a model.

src/mantis/tools/kubernetes.py
    Semantic result shaping, provenance (QueryMeta + cluster identity),
    bounding/truncation, registry registration, untrusted-output
    handling (#14).
```

No `kubectl` subprocess, no mutation, no `exec`/`attach`/`port-forward`,
anywhere in either module — see [Non-goals](#non-goals).

## Configuration and authentication

`mantis.config.KubernetesConfig` follows the exact same pattern as every
other Mantis integration config: a frozen dataclass, `from_env()`, no
eager networking/file access at config-construction time, and a single
explicit `auth_mode` rather than guessing between credential sources.
See [docs/configuration.md](configuration.md#kubernetes) for the full
environment-variable table.

Two auth modes:

- **`in_cluster`** — uses the Kubernetes service-account token/CA the
  runtime mounts automatically (`kubernetes.config.load_incluster_config`).
  Recommended when Mantis itself runs inside the cluster it inspects.
  Mantis's own configuration holds no credential material at all in this
  mode — the client library reads the mounted token/CA directly from the
  filesystem. Rejects `MANTIS_KUBERNETES_KUBECONFIG`/
  `MANTIS_KUBERNETES_CONTEXT` outright if either is set, so precedence
  between "in-cluster" and "kubeconfig" auth is never ambiguous.
- **`kubeconfig`** — uses a configured kubeconfig file path and
  (recommended) an explicit context, via
  `kubernetes.config.new_client_from_config()`, which builds a
  self-contained client rather than mutating any process-global default
  configuration. The path and context are **deployment configuration**,
  read once from `mantis.config.KubernetesConfig` — never a model/tool
  argument, and never silently picked from whichever context happens to
  be `current-context` in a user's kubeconfig if explicit configuration
  can avoid it.

Client construction (`mantis.integrations.kubernetes.KubernetesClient.from_config`)
is the **one** narrow factory path every semantic tool goes through — a
tool handler never loads a kubeconfig, decides an auth mode, or knows
which mode is actually in use. That choice lives entirely below the
tool/agent layer.

Credential material never appears in a tool result, a log line, or an
error message: `KubernetesConfig` carries no `Secret`-wrapped field at
all (there's nothing to wrap — the kubeconfig file or mounted
service-account token is read directly by the Kubernetes client
library, never by Mantis), and every `KubernetesError` diagnostic
message is built from a fixed action description plus a classified
status/reason, never a response body, header, or the configured
API-server URL.

### RBAC

Grant Mantis's service account (in-cluster) or kubeconfig user (local) a
narrowly scoped **read-only** role covering only the four resource kinds
these tools ever read:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: mantis-read-only
rules:
  - apiGroups: [""]
    resources: ["pods", "nodes", "events"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: mantis-read-only
subjects:
  - kind: ServiceAccount
    name: mantis
    namespace: mantis
roleRef:
  kind: ClusterRole
  name: mantis-read-only
  apiGroup: rbac.authorization.k8s.io
```

Never grant `create`/`update`/`patch`/`delete`, `exec`/`attach`/
`portforward` subresources, or access to `secrets` — Mantis has no code
path that would use any of it, and granting it anyway only widens blast
radius if Mantis's own credentials or a future bug were ever
compromised. If Mantis inspects more than one namespace, prefer a `Role`
+ `RoleBinding` per namespace over a cluster-wide `ClusterRole` when the
deployment's threat model calls for it — the tools themselves already
require an explicit `namespace` argument for every namespaced call, so a
namespace-scoped `Role` does not need any code change to work.

## Supported evidence

Four semantic tools, registered read-only (`mutating=False`) in the
shared `default_registry`, reusable by any future agent without
agent-specific wrappers:

| Tool | Question it answers |
|---|---|
| `kubernetes_list_pods(namespace, label_selector=None)` | Pod/workload status in a namespace: phase, container readiness, restart counts, last termination reason. |
| `kubernetes_list_deployments(namespace, label_selector=None)` | Deployment rollout status: desired vs. current/updated/available/ready replicas, conditions. |
| `kubernetes_list_nodes(label_selector=None)` | Cluster-scoped node summary: Ready/pressure conditions, schedulability. |
| `kubernetes_list_events(namespace, name=None, kind=None)` | Recent events for a namespace, or one resource within it: type, reason, message, count, timestamp. |

There is deliberately no fifth, generic "run an arbitrary Kubernetes API
call" tool — see [Non-goals](#non-goals).

## Input validation

- `namespace` and the events tool's optional `name` are validated
  (`mantis.tools.kubernetes.validate_k8s_name`) as bounded, RFC-1123-ish
  Kubernetes resource names — never used to construct a shell command or
  arbitrary filesystem path.
- `label_selector`, when supported, is bounded (length, no control
  characters — `MAX_LABEL_SELECTOR_CHARS`) and passed directly to the
  Kubernetes API as a query parameter, never to a shell. Its selector
  *grammar* is deliberately not parsed here — a syntactically invalid
  selector is rejected by the Kubernetes API server itself (a 400,
  classified like any other retrieval failure), not by this validation.
- `kind` (the events tool's optional resource-kind filter) is restricted
  to an explicit allowlist (`Pod`, `Deployment`, `Node`) — never a
  free-form string, and never arbitrary CRD browsing.
- `kubernetes_list_events`'s `field_selector` is **always built by
  Mantis** from the validated `name`/`kind` parameters
  (`involvedObject.name=...`, `involvedObject.kind=...`) — the model
  never supplies a raw field selector string.
- Cluster/context selection comes from `MANTIS_KUBERNETES_*`
  configuration only — never a tool argument, never an arbitrary
  filesystem path supplied by the model.

A validation rejection returns a normal tool result (`validation_error`
set, the resource list empty, `cluster: null`) rather than raising — the
rejected input is untrusted, model-supplied data (#14) that must flow
through the standard model-input safety pipeline like any other tool
result, and no Kubernetes API call is made.

## Bounds and truncation

Named, documented caps (`mantis.tools.kubernetes`), chosen for
interactive troubleshooting with local models, not cluster inventory
export:

| Constant | Value | Bounds |
|---|---|---|
| `MAX_OBJECTS_RETURNED` | 50 | Pods/Deployments/Nodes returned per call. |
| `MAX_EVENTS_RETURNED` | 50 | Events returned per call. |
| `MAX_CONTAINERS_PER_POD` | 20 | Containers reported per pod. |
| `MAX_CONDITIONS_PER_OBJECT` | 20 | Conditions reported per Deployment/Node. |
| `MAX_NAME_CHARS` | 253 | Name/image string length (matches Kubernetes' own RFC-1123 limit). |
| `MAX_LABEL_KEY_CHARS` / `MAX_LABEL_VALUE_CHARS` | 128 / 256 | Label key/value length — same values as #9/#10's Prometheus/Loki label bounds. |
| `MAX_ANNOTATION_VALUE_CHARS` | 500 | Annotation value length (larger than a label value — annotations are conventionally longer free-form text). |
| `MAX_LABELS_PER_OBJECT` / `MAX_ANNOTATIONS_PER_OBJECT` | 20 / 20 | How many labels/annotations are returned per object. |
| `MAX_MESSAGE_CHARS` | 500 | Event/condition/container-state message length. |
| `MAX_LABEL_SELECTOR_CHARS` | 256 | Model-supplied `label_selector` length. |
| `MAX_TOTAL_RESULT_CHARS` | 20,000 | Global character budget across every item admitted into one result — the primary size control, well under #14's 64,000-char final backstop (`MODEL_TOOL_RESULT_MAX_CHARS`). |

Full raw Kubernetes API objects are never dumped to the model — every
field above is either bounded source-reported data or a small, named
Mantis convenience derived from it (see [Provenance](#provenance)).

`meta.truncated` is `true` whenever any of the following happened, and
is never falsely reported as `false` when they did:

- More objects/events existed than `MAX_OBJECTS_RETURNED`/
  `MAX_EVENTS_RETURNED` — detected via a Loki-style sentinel: the actual
  API request asks for one more than the exposed cap (see
  `PODS_REQUEST_LIMIT` and siblings), so an exact-at-cap response can be
  told apart from "there were exactly this many."
- More containers/conditions existed for one object than
  `MAX_CONTAINERS_PER_POD`/`MAX_CONDITIONS_PER_OBJECT`.
- Any individual name, label, annotation, image, or message string was
  itself shortened.
- The global `MAX_TOTAL_RESULT_CHARS` budget was reached before every
  otherwise-admissible item could be included.

An empty result (`pods: []`, `events: []`, ...) on a successful call is
valid evidence — "nothing matched" — and is always distinct from a
retrieval failure (see [Reliability and error semantics](#reliability-and-error-semantics)).

## Reliability and error semantics

Reuses #15's shared reliability contract exactly — no second retry
helper, timeout family, or breaker abstraction was introduced for
Kubernetes:

- Every list call goes through `mantis.reliability.retry_call` with the
  shared `RetryPolicy`, safe here because every call this integration
  makes is a read.
- `mantis.integrations.kubernetes.classify_k8s_exception` maps a
  Kubernetes client-library failure onto the shared
  `IntegrationErrorKind` taxonomy: an `ApiException`'s status code
  through the same `classify_http_status` every HTTP-based Mantis
  integration uses (401 → `authentication`, 403 → `authorization`, 404 →
  `not_found`, 429 → `rate_limit`, 400 → `bad_request`, 5xx →
  `server_error`); a `urllib3` transport-level failure (no HTTP response
  received at all) into `timeout` or `connection`.
- Only `timeout`/`connection`/`rate_limit`/`server_error` are retried,
  and only up to the shared retry budget — `authentication`/
  `authorization`/`not_found`/`bad_request` are never retried.
- A per-call `Deadline` is checked before every attempt: if it's already
  expired before the first attempt can start, **zero Kubernetes API
  calls are made** — the call raises `DeadlineExceededError` instead,
  handled by `AgentRuntime` exactly like any other tool's exhausted
  budget.
- **A retrieval failure is never evidence that a workload/node is
  unhealthy.** A classified `KubernetesError` propagates as a normal
  integration failure — `AgentRuntime` reports it to the model as "Mantis
  could not retrieve evidence," never as a claim about the target
  cluster's state. This is architectural, the same guarantee #8/#9/#10
  already give for network/Prometheus/Loki failures.

## Security

Labels, annotations, event messages, image names, reasons, and resource
names are all external, Mantis-uncontrolled text and may legitimately
contain strings that look like instructions. They are preserved exactly
as the API reported them (subject only to the bounds above) and reach
the model marked as untrusted evidence (`contains_untrusted_text=True`
on every tool's registration) — reusing #14's `mantis.security` pipeline
exactly, with no second prompt-injection/redaction layer introduced.
Secret resources are never fetched by any tool here — there is no code
path that lists or reads `Secret` objects.

## Provenance

Every successful result carries two provenance blocks:

- `meta` (`mantis.contracts.QueryMeta`) — `source_system: "kubernetes"`,
  `query_time`, `truncated`, and `derived_fields` (the handful of
  Mantis-computed convenience booleans — `pod_ready` on pods, `ready` on
  nodes — everything else is Kubernetes-reported data, only
  bounded/reshaped, never interpreted).
- `cluster` — the configured logical cluster identity: `cluster_name`,
  `context`, `auth_mode`. Never a raw API-server URL (which could be
  unsafe to expose), never a kubeconfig path, never credential material.
  `null` when no cluster was actually queried (a validation rejection).

`meta.observation_time` is always `null` for these tools: every call
returns multiple records, each with its own natural state at query
time — no single batch-level timestamp could represent that without
being misleading (the same reasoning as AWX's job list and Loki's
stream results).

## What this evidence does and does not prove

- **A pod's `phase`/readiness/restart count is current-state,
  cluster-reported fact** — it does not explain *why* a container
  restarted (application bug, OOM-kill, node pressure, a rolling
  deploy, ...) unless the container's own termination `reason`
  (`OOMKilled`, `Error`, ...) or a correlated event already says so.
- **A Deployment's replica counts are a snapshot**, not a trend — a
  `desired_replicas=3, available_replicas=1` result does not by itself
  say whether availability is recovering, degrading, or has been stable
  at that level; correlate with events or a Prometheus range query for
  a trend.
- **A node's `Ready`/pressure conditions are the node's own
  self-reported kubelet state** at query time — they do not diagnose
  *why* a node is under memory/disk/PID pressure, and a `Ready=True`
  node can still host unhealthy pods for reasons the node's own
  conditions never surface.
- **An event is what the control plane or a kubelet chose to record**,
  with its own `count`/timestamps — the absence of an expected event is
  not proof nothing happened (event retention is time-limited and
  best-effort, not a durable audit log).
- **A retrieval failure of any kind is never evidence about the target
  cluster's health** — see [Reliability and error semantics](#reliability-and-error-semantics).

## Result shape

```json
{
  "meta": {
    "source_system": "kubernetes",
    "query_time": "2026-09-17T22:27:35.019539+00:00",
    "observation_time": null,
    "query_window": null,
    "truncated": false,
    "derived_fields": ["pod_ready"],
    "contract_version": "1.0"
  },
  "cluster": {
    "cluster_name": "home-k3s",
    "context": "home",
    "auth_mode": "kubeconfig"
  },
  "query": {
    "namespace": "payments",
    "label_selector": null
  },
  "pods": [
    {
      "namespace": "payments",
      "name": "payment-api-7f9c8d-abcde",
      "kind": "Pod",
      "phase": "Running",
      "pod_ready": true,
      "host_ip": "10.0.4.21",
      "pod_ip": "10.0.4.55",
      "node_name": "worker-2",
      "containers": [
        {
          "name": "payment-api",
          "ready": true,
          "restart_count": 3,
          "image": "registry.example/payment-api:1.4.2",
          "state": {
            "phase": "running",
            "reason": null,
            "message": null,
            "exit_code": null,
            "finished_at": null
          },
          "last_termination": {
            "phase": "terminated",
            "reason": "Error",
            "message": null,
            "exit_code": 1,
            "finished_at": "2026-09-17T03:14:00+00:00"
          }
        }
      ],
      "labels": {"app": "payment-api"},
      "annotations": {},
      "created_at": null
    }
  ]
}
```

(Real, verified output — generated by running `kubernetes_list_pods`
against a fixture-backed client returning the shapes above; not a
hand-written mockup.)

## Non-goals

Deliberately excluded (see issue #18's guardrails):

- **`kubectl` passthrough or any subprocess execution.** Everything here
  goes through the official Kubernetes Python client library; nothing is
  ever shelled out.
- **`exec`, `attach`, `port-forward`, log streaming.** Use Loki
  (`loki_query`, #10) for log evidence instead.
- **Mutation of any kind** — no `create`/`update`/`patch`/`delete`/
  `scale`/`restart`/`apply`. Every tool here has `mutating=False`, and
  there is no code path that could change cluster state.
- **A generic "run an arbitrary Kubernetes API call" tool.** Four
  purpose-built, narrow tools instead — see [Supported evidence](#supported-evidence).
- **Arbitrary CRD browsing.** Only the four built-in resource kinds
  (Pod, Deployment, Node, Event) are supported in this first
  implementation.
- **Arbitrary JSONPath, shell flags, or command strings** as tool input.
- **Secret resources.** Never fetched by any tool here.

## Worked troubleshooting example

A pod's container has restarted several times, and Prometheus shows a
scrape gap for the same target around the same time:

```
kubernetes_list_pods(namespace="payments")
  -> payment-api-7f9c8d-abcde: restart_count=3, last_termination
     reason="Error", finished_at="2026-09-17T03:14:00+00:00"

prometheus_query_range(query="up{instance='payment-api:8080'}", ...)
  -> up drops to 0 from ~03:12 to ~03:16, then recovers
```

Good behavior: "The pod's container last terminated (reason: Error) at
03:14:00 UTC, and Prometheus shows the scrape for payment-api:8080
dropping to 0 in roughly the same window before recovering. These two
signals occurred around the same time; the available evidence doesn't
establish which one, if either, caused the other." Bad behavior: "The
restart caused the outage" or "the outage caused the restart" — neither
claim is supported by evidence that only establishes a shared time
window. See `mantis.eval.fixtures.kubernetes`'s
`kubernetes-pod-restart-correlated-with-scrape-gap` golden scenario
(#18) for the deterministic version of this exact case, and
[docs/evaluation.md](evaluation.md) for how to run it against a live
model.
