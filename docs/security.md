# Security

Mantis operates against real infrastructure (AWX today; Prometheus, Loki,
Kubernetes, and others planned). These principles govern how it's built,
and are enforced architecturally, not just by convention.

## Least privilege

- Each agent declares an explicit, narrow `ALLOWED_TOOLS` list.
  `AgentRuntime` only ever sends the model schemas for that resolved set
  — an agent cannot invoke a tool it wasn't given, regardless of what the
  model is prompted or tricked into requesting.
- The AWX Troubleshooter's toolset is `["awx_recent_failed_jobs"]`: one
  read-only operation. It has no path to launching, cancelling, or
  modifying anything in AWX.
- Credentials used by integrations (AWX token, LiteLLM key) should
  themselves be scoped as narrowly as the underlying system allows — see
  below.

## Read-only-first design

Every tool implemented in this milestone has `mutating=False` (the
default on `mantis.registry.Tool`). This is a real field checked at
registration/introspection time, not just documentation: a future
approval/policy layer (see "Future approval gates" below) will gate on
it before allowing a mutating tool's handler to execute.

Do not add a mutating tool casually. If you're implementing one (launching
a job, restarting a service, modifying a Kubernetes resource, changing
DNS, applying Terraform), it must:

- Set `mutating=True` explicitly on its `Tool` registration.
- Be excluded from any purely investigative agent's `ALLOWED_TOOLS`.
- Go through the approval mechanism described below once it exists —
  it should not ship as directly agent-callable ahead of that.

Unrestricted shell execution is intentionally out of scope for Mantis.
A general-purpose shell tool would make every least-privilege boundary
above meaningless — it collapses "which tools can this agent call" back
into "can this agent do anything on the host." Any operational action
Mantis needs should be exposed as its own narrow, reviewable tool instead.

## Credential handling

- Credentials are read only from process environment variables through
  `mantis.config`. They are never hardcoded, logged, or embedded in
  prompts/tool schemas.
- Locally, those variables may come from a `.env.*` file (see
  [docs/configuration.md](configuration.md) for the full convention).
  Every real `.env*` file is git-ignored (`.gitignore` blanket-ignores
  `.env*` and explicitly un-ignores only `*.example` templates) — a
  hosted deployment (EKS/GKE/ECS/Docker) doesn't use these files at all
  and injects environment variables directly instead, so no credential
  ever needs to live in a file that could be committed.
- `mantis.config` raises `ConfigurationError` immediately if a required
  credential is missing, rather than letting integration code fail later
  with a less obvious error. The error names only the missing *variable*
  (e.g. `"Missing required environment variable: AWX_TOKEN"`), never a
  value — including a value belonging to some other, already-set
  variable.
- Every credential field (`LiteLLMConfig.api_key`, `AWXConfig.token`) is
  typed as `mantis.config.Secret`, not a plain `str`. `Secret` redacts
  itself on `repr()`/`str()` — including through a dataclass's default
  `__repr__`, an f-string, or a `logging` call — so accidentally logging
  or printing a config object (e.g. `logger.debug(config)`, an unguarded
  `print`) cannot leak the raw value. The real value is reachable only
  via `.get_secret_value()`, called at exactly the two points that
  legitimately need it (building the AWX `Authorization` header and
  constructing the LiteLLM client) — grep for `get_secret_value` before
  adding a new credential field to make sure a new one stays this narrow.

### LiteLLM virtual keys

Use a LiteLLM **virtual key** (`LITELLM_API_KEY`) scoped to Mantis, not a
raw upstream provider API key. This gives you:

- The ability to revoke or rotate Mantis's model access without touching
  the underlying provider credential.
- Separate rate limiting, budget, and usage tracking per virtual key —
  useful for noticing a runaway agent loop before it becomes an incident.
- A clean boundary between "who can call this model at all" (LiteLLM's
  concern) and "what can Mantis do once it has a model response" (this
  project's concern) — see "Separation of model authorization from
  operational authorization" below.

### AWX service-account / token recommendations

- Use a dedicated AWX service-account token for Mantis, not a personal
  user's token.
- Scope that account's AWX RBAC permissions to read-only access on the
  organizations/inventories/projects Mantis needs to observe. AWX's own
  RBAC is the actual enforcement point here — Mantis's tool-level
  `mutating=False` marker is a second, independent layer, not a
  substitute for correctly scoped AWX permissions.
- Rotate the token periodically and whenever it may have been exposed.

## Separation of model authorization from operational authorization

Two distinct trust boundaries exist and should never be conflated:

1. **Model authorization** (LiteLLM virtual key): who/what may send
   prompts to a model and consume tokens.
2. **Operational authorization** (AWX token / future integrations'
   credentials, plus each agent's `ALLOWED_TOOLS` and each tool's
   `mutating` flag): what the *agent* may actually observe or change in
   real infrastructure.

A compromised or misbehaving model (hallucinating, or manipulated via
prompt injection in tool output) should never be able to expand its own
operational authorization. This is why tool access is enforced at the
runtime/registry level in Python — not by asking the model nicely in the
system prompt — and why mutating tools will require an explicit approval
step outside the model's control.

## Future approval gates for mutations

Mantis is designed to grow into this shape, even though only the
"Investigation" stage is implemented today:

```
Investigation agent  (read-only tools, e.g. AWX Troubleshooter today)
        │
        ▼
Recommendation        (agent's evidence-based output to a human)
        │
        ▼
Human / policy approval   (not yet implemented)
        │
        ▼
Remediation tool or agent   (not yet implemented; mutating=True tools)
```

Concretely, when mutating tools are introduced, expect:

- A policy/approval layer that intercepts any call to a `mutating=True`
  tool before its handler executes, independent of what the model
  "decided."
- Investigation and remediation likely staying as separate agents (or at
  least separately gated toolsets) rather than one agent holding both
  read and write tools.
- Audit logging of who/what approved a mutating action, building on the
  `AgentRuntime` tool-call logging already in place for read-only tools.

None of this is implemented yet — this section documents intent so the
architecture (registry `mutating` flag, narrow per-agent tool lists,
separated integration credentials) is already shaped to support it.
