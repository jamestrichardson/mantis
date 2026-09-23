# Model-call routing and fallback (#16)

A deterministic, server-side model-*call* routing policy for
`AgentRuntime`: each agent requests a stable Mantis/LiteLLM alias
policy (one primary alias, zero or more ordered fallback aliases), and
the runtime may make a bounded fallback attempt within one logical
model call when that call fails for an explicitly eligible reason.

**This is model-call routing, not whole-agent replay.** A "logical
model call" is one model step in `AgentRuntime`'s existing conversation
loop — one iteration. The runtime never restarts an investigation from
scratch merely because one model call failed: it never replays an
already-executed tool call, and it never resets the run's iteration
number, tool-call budget, run deadline, or run-local breaker state.
That distinction is deliberate and is required before mutating tools
ever exist — a naive "just retry the whole run" fallback would
duplicate tool calls, which is fine for read-only evidence gathering
but would be actively dangerous once a mutating tool exists.

See `mantis/config.py` (`ModelRoutingPolicy`), `mantis/routing.py`
(the failure taxonomy and classification function), and
`mantis/runtime.py` (`AgentRuntime._call_model_with_routing`) for the
implementation.

## Configuration

```bash
# Global default: every agent with no per-agent override falls back to
# these ordered aliases, in order, when an eligible failure occurs.
LITELLM_MODEL=qwen3-opencode:latest
LITELLM_MODEL_FALLBACKS=qwen3-coder:30b-a3b-q8_0,devstral-small-2:latest
LITELLM_MODEL_MAX_ATTEMPTS=2   # optional; defaults to one attempt per configured route

# Per-agent override (same precedence pattern as MANTIS_<AGENT>_MODEL):
MANTIS_INCIDENT_TRIAGE_MODEL=qwen3-opencode:latest
MANTIS_INCIDENT_TRIAGE_MODEL_FALLBACKS=qwen3-coder:30b-a3b-q8_0
MANTIS_INCIDENT_TRIAGE_MODEL_MAX_ATTEMPTS=2
```

Leaving every `*_FALLBACKS` variable unset means exactly one route (the
primary alias, no fallback) for every agent — behaviorally identical to
Mantis before #16. No operator action is required to keep existing
deployments working, and no agent's `SYSTEM_PROMPT` needs to change to
gain (or to keep not using) routing support — the model is never told
which alias answered.

Programmatically:

```python
from mantis.config import ModelRoutingPolicy

# What every existing single-model agent gets automatically.
ModelRoutingPolicy.single("qwen3-opencode:latest")

# Explicit primary + fallbacks.
ModelRoutingPolicy(
    primary_alias="qwen3-opencode:latest",
    fallback_aliases=("qwen3-coder:30b-a3b-q8_0", "devstral-small-2:latest"),
    max_attempts=2,
)

# From the environment, mirroring LiteLLMConfig.from_env's precedence.
ModelRoutingPolicy.from_env(
    model_env="MANTIS_INCIDENT_TRIAGE_MODEL",
    fallback_env="MANTIS_INCIDENT_TRIAGE_MODEL_FALLBACKS",
    max_attempts_env="MANTIS_INCIDENT_TRIAGE_MODEL_MAX_ATTEMPTS",
)
```

### Validation

`ModelRoutingPolicy.__post_init__` rejects, at construction time (never
silently defaulting):

- an empty/whitespace-only primary alias;
- an empty/whitespace-only fallback alias;
- the primary alias duplicated in `fallback_aliases`;
- duplicate fallback aliases;
- more than `mantis.config.MAX_ROUTING_ALIASES` (5) total configured
  routes (primary + fallbacks);
- `max_attempts` below 1, or above the number of configured routes.

Every current agent's build_runtime() maps its existing
`LiteLLMConfig.from_env(model_env=MODEL_ENV)` call onto a matching
`ModelRoutingPolicy.from_env(model_env=MODEL_ENV, ...)` call — the
"agent-specific model configuration maps cleanly into routing policy"
requirement — see `mantis.config.ModelRoutingPolicy.from_litellm_config`
for the direct bridge when you already have a resolved `LiteLLMConfig`.

### API/CLI callers cannot override this

`mantis.api.schemas.RunRequest` has no model/alias/routing field at all,
and `extra="forbid"` rejects any attempt to add one — see
[docs/api.md](api.md#invoking-an-agent) and
`tests/test_api_app.py::test_caller_cannot_override_server_side_routing_aliases`.
Routing is exclusively `build_runtime()`-time, server-side
configuration, exactly like every other agent-level setting.

## Attempt semantics

For one logical model call (one `AgentRuntime` iteration):

1. Attempt the primary alias.
2. If it fails with an **eligible** failure kind (see below), and
   another route is available within `max_attempts`, and the run
   deadline hasn't expired: record the failed attempt and try the next
   configured alias, reusing the *exact same* `messages`/tool-schema
   snapshot — nothing about the conversation state changes between
   attempts.
3. If it fails with a **non-eligible** kind: the original exception
   propagates unchanged, immediately — never silently rerouted.
4. If every permitted attempt fails with an eligible kind:
   `mantis.runtime.ModelRoutingExhaustedError` is raised, carrying the
   full attempt history for that logical call.
5. If the run deadline expires before a next attempt could start:
   `mantis.runtime.RunDeadlineExceededError` is raised instead — no new
   model attempt is ever started after the deadline, matching every
   other deadline check in `AgentRuntime`.

**Never reset by a fallback attempt**: run ID, conversation history,
iteration number, tool-call budget, run deadline, run-local breaker
state. **Never replayed**: an already-executed tool call — a fallback
only retries the *model's* half of one iteration, never re-dispatches
any tool the model already successfully called earlier in the run.

### Single-route policies behave exactly as before #16

If an agent's routing policy has only one configured route (the
default for every existing agent that hasn't set a `*_FALLBACKS`
variable), an eligible failure propagates the *original* exception
unchanged — it is never wrapped in `ModelRoutingExhaustedError`, since
that type specifically means "routing was actually attempted and every
route failed," which isn't a meaningful description of a single-route
policy's only attempt failing. This is what makes #16 fully backward
compatible: every agent, tool-permission set, and error-handling call
site (e.g. `mantis.eval.runner.run_scenario`'s
`except (OpenAIError, RuntimeError_)`) behaves identically to before
unless an operator actually configures a fallback alias.

## The model-call failure taxonomy

`mantis.routing.ModelCallFailureKind` — classified from the real
`openai` SDK exception type only, never by parsing exception message
text (`mantis.routing.classify_model_call_exception`):

| Kind | Eligible for fallback by default? | Example `openai` exception |
|---|---|---|
| `timeout` | Yes | `APITimeoutError` |
| `connection` | Yes | `APIConnectionError` |
| `rate_limit` | Yes | `RateLimitError` |
| `server_error` | Yes | `InternalServerError` |
| `authentication` | **No** | `AuthenticationError` |
| `authorization` | **No** | `PermissionDeniedError` |
| `bad_request` | **No** | `BadRequestError`, `UnprocessableEntityError`, `NotFoundError` |
| `invalid_response` | **No** | `APIResponseValidationError` |
| `unknown` | **No** | anything not recognized above |

### Why the default eligibility split is what it is

Fallback is only permitted by default for failures that plausibly
differ by route/model availability — a different configured alias
might genuinely not be rate-limited, timing out, or down even when the
primary is.

The following never silently fall back by default, because a
different model alias behind the *same* LiteLLM gateway would very
likely fail identically, or fail in a way that's actively misleading to
paper over:

- **authentication / authorization** — a credential/permission
  problem almost certainly applies to every route behind the same
  gateway.
- **bad_request** — the request itself was malformed (by Mantis, by
  the model's own tool-call arguments, or by an unsupported tool
  schema); a different alias receives the exact same malformed request.
- **invalid_response** — Mantis's runtime couldn't structurally
  interpret the response; this could be a Mantis-side bug as easily as
  a route-specific quirk, so it fails closed rather than being assumed
  safe to retry elsewhere.
- **unknown** — an unclassified failure fails closed by design. Never
  silently treated as eligible just because it wasn't recognized.

A caller may pass a different `eligible_failure_kinds` frozenset to
`ModelRoutingPolicy` to change this per agent, but the shipped default
is deliberately conservative. Also explicitly **never** eligible for
fallback, regardless of configuration, because none of them are
model-*call* failures at all: a target integration/tool failure
(`mantis.reliability.IntegrationError` — a completely separate failure
domain, see [docs/reliability.md](reliability.md)), a deterministic
safety/policy rejection, arbitrary free-form model "confidence," or
model prose asking for escalation. Routing only ever reacts to how the
*model gateway call itself* failed, never to what the model said in its
answer.

## Escalation semantics (v1 scope)

For v1, "escalation" means exactly one thing: moving to the next
explicitly configured model alias for the *same logical model call*,
under this deterministic routing policy. Explicitly **not**
implemented, and not planned for this issue:

- learned/adaptive routing;
- model self-selection;
- confidence-from-free-form-prose routing;
- severity inferred from generated natural language;
- whole-run restart on `MaxIterationsExceededError` (that failure means
  the *model's own behavior* — never converging — was disqualifying
  across however many iterations it took, not that one model call
  failed; #16 doesn't touch it).

A later issue may add additional deterministic escalation signals if
needed — this document and `ModelRoutingPolicy.eligible_failure_kinds`
are the extension points, not a natural-language classifier.

## Interaction with run/tool/iteration budgets

Routing adds no new budget mechanism — it operates strictly inside the
existing ones:

- **`max_iterations`** — unaffected. A fallback attempt happens
  *within* one iteration; it never counts as an extra iteration.
- **`tool_call_budget`** — unaffected. Routing only ever talks to the
  model gateway; it never touches tool dispatch, so a fallback can
  never inflate or reset the successful-tool-call count.
- **`ReliabilityConfig.run_timeout_seconds`** (the overall run
  deadline) — a fallback attempt consumes the *same* overall run
  wall-clock budget as everything else in the run. No new model attempt
  may start after this deadline expires, whether that attempt would
  have been the first of a fresh iteration or a fallback attempt within
  one. An already-in-flight call is not forcibly cancelled beyond what
  the underlying HTTP client/transport already supports — the same
  limitation every deadline in `mantis.reliability` has (see
  [docs/reliability.md](reliability.md)).
- **`max_attempts` / configured route count** — new, #16-specific
  bounds: `max_attempts` caps how many routes one logical call may try
  (`mantis.config.MAX_ROUTING_ALIASES` caps how many can be configured
  at all, 5 total). Both are validated at `ModelRoutingPolicy`
  construction time.

### LiteLLM virtual-key/cost/rate-limit budgets remain LiteLLM's job

Mantis does not reimplement a second billing/quota engine. Per-key
token/request/cost budgets and rate limits are a LiteLLM
virtual-key/service-key responsibility — configure them there. Mantis's
own `cost_usd` field (see `mantis.eval.qualification.QualificationRecord`)
is honestly reported as unavailable rather than approximated, precisely
because Mantis has no independent view into that accounting.

**Mantis currently uses one scoped LiteLLM virtual key per
service/process.** `LiteLLMConfig` resolves exactly one
`LITELLM_URL`/`LITELLM_API_KEY` pair (`LiteLLMConfig.from_env`), and
`AgentRuntime` builds exactly one `OpenAI` client from it in
`__post_init__` — every alias in a `ModelRoutingPolicy`, primary and
every fallback alike, is requested through that same client with that
same key; only the `model` field in the request body changes. This
holds across agents within one running process too: `model_env` only
selects an agent-specific model *alias*, never a separate credential.
**Per-route credentials are not supported** — there is no way to give
one alias in a routing policy a different virtual key than another
today. For independent per-agent-class budgets, run separate Mantis
service instances/configurations, each with its own
`LITELLM_API_KEY` — that's a deployment/process-topology decision, not
something `ModelRoutingPolicy` or any agent code can express.

**A LiteLLM rate/budget denial is already routing-eligible, but don't
expect a fallback to escape a key-level budget.** LiteLLM enforces a
rate/budget limit by returning an HTTP 429, which the OpenAI SDK raises
as `openai.RateLimitError`; `classify_model_call_exception` maps that
to `ModelCallFailureKind.RATE_LIMIT`, which is in
`DEFAULT_ELIGIBLE_FAILURE_KINDS` (see `mantis.routing`) — so a primary
alias hitting a rate/budget ceiling does trigger a fallback attempt at
the next configured alias, the same as a timeout or connection failure.
But since every alias shares the one virtual key above, a **key-level**
budget/rate exhaustion generally blocks the fallback attempt too — it's
the same key being denied again. A fallback alias only has a realistic
chance of succeeding where LiteLLM enforces the limit **per model or
per upstream provider** rather than only per key (e.g. a
model-specific rate ceiling on the primary's backend that the
fallback's different backend isn't subject to); it never escapes a
global per-key budget. Account for this in how you order
`fallback_aliases` — routing itself has no cost- or
budget-scope-awareness.

## Privacy / local-vs-cloud routing considerations

`fallback_aliases` is an ordered list — if a deployment mixes a
self-hosted/local model as primary with a cloud-hosted model as a
fallback (or vice versa), that ordering is a real data-residency
decision: which model actually sees a given prompt/tool-result payload
depends on which route succeeds. Order aliases so that a failure never
silently routes potentially sensitive evidence (tool results already
flow through `mantis.security`'s untrusted-content handling, but that's
a prompt-injection safeguard, not a data-residency one) to a less
trusted destination than the operator intended. When in doubt, keep
`fallback_aliases` restricted to routes with equivalent trust/residency
characteristics to the primary.

## Observability

Every attempt is recorded, bounded and safe (see
`mantis.routing.ModelCallAttempt`/`AgentRuntime.model_call_log`):
`iteration`, `attempt_number`, `requested_alias`, `routing_reason`
(`"primary"`/`"fallback"`), `outcome`, `failure_kind`, a bounded
`detail` (the exception's class name and HTTP status code **only** —
never `str(exc)`, which can carry an arbitrary, potentially large
provider/LiteLLM/upstream error body — a real example hit during #13
qualification was an nginx 504 Gateway Time-out HTML page as the
exception's own message), `latency_seconds`, `total_tokens`, and
`backend_model` (the resolved backend identity on success). Log events:
`mantis_model_call` (extended with `attempt_number`/`routing_reason`/
`failure_kind`), `mantis_model_routing_exhausted`.

Metrics (`mantis.observability.metrics`, all bounded-label — see
`tests/observability/test_metrics.py`'s cardinality guard):

- `mantis_model_calls_total` / `mantis_model_call_duration_seconds` /
  `mantis_model_tokens_total` — now emitted per *attempt*, not per
  iteration (identical counts to before #16 for any single-route
  policy, since there's exactly one attempt per iteration there).
- `mantis_model_call_failures_total{failure_kind}` — new; the
  classified failure taxonomy above as a bounded label.
- `mantis_model_routing_fallbacks_total` — new; incremented each time
  an eligible failure triggers a fallback attempt.
- `mantis_model_routing_exhausted_total` — new; incremented when every
  permitted attempt for one logical call failed.

Never a metric label, ever: a prompt, a full tool result, a
credential, a raw provider error body, or `backend_model` (an
unbounded, backend-reported string) — these live only in structured
logs/`ModelCallAttempt` records, never in Prometheus label space.

The run overall exposes (see `mantis.eval.results.EvalResult`, used by
both `mantis eval run` and #13's qualification harness):
`requested_primary_alias`, `final_alias` (which alias actually produced
the final response — `None` if none did), and `route_attempts` (the
full bounded attempt history).

### LiteLLM request attribution

Every request to the model gateway (every attempt, primary or
fallback) carries low-risk attribution metadata via the OpenAI SDK's
`extra_body` parameter — LiteLLM's proxy reads a top-level `metadata`
object in the request body for its own logging/attribution:

```json
{"metadata": {"mantis_agent": "incident-triage", "mantis_run_id": "<run_id>", "mantis_iteration": 3}}
```

`mantis_agent` (the agent name), `mantis_run_id` (this run's ID, shared
with every structured log event for it), and `mantis_iteration` (which
model-call iteration this attempt belongs to) are the only fields sent.
**Never** a prompt, tool output, credential, target hostname, or any
other user-derived or high-cardinality string — the same bounded-only
discipline as every other piece of #16 observability above. This lets
an operator correlate a request observed at the LiteLLM gateway back to
the exact Mantis run/agent/iteration that made it, satisfying the
gateway-attribution requirement without Mantis's own `run_id` logging
alone (which the gateway has no visibility into).

## Eval integration

`mantis.eval.runner.run_scenario` populates
`requested_primary_alias`/`final_alias`/`route_attempts` on every
`EvalResult` unconditionally — even a scenario that never exercises
routing (the overwhelming majority today) gets a length-1
`route_attempts` list reflecting its one, successful attempt. Tool-call
counts (`tool_calls`/`duplicate_call_count`/`malformed_call_count`)
come entirely from `AgentRuntime.call_log` and are never inflated by a
model-call fallback — a scenario with two failed model-call attempts
and one real tool call still reports exactly one tool call.

#13's qualification harness (`mantis.eval.qualification`) carries the
same facts on its bounded `QualificationRecord`: `requested_alias`
(primary), `final_alias`, and `failed_route_attempts` (a bounded
`"<alias>:<failure_kind>"` summary — never a raw exception message).
**How #13's qualification recommendations should inform alias
ordering**: qualify the aliases you intend to use as a `primary`/
`fallback_aliases` set *together*, under the real routing policy you
plan to ship, before trusting a fallback ordering in production — a
model that qualifies well alone may behave differently as a fallback
target if it has different tool-calling reliability characteristics
under the same prompts (see `docs/model-qualification.md`'s real
findings, e.g. a candidate that reliably mishandles multi-tool
scenarios specifically). Put the most-qualified `mantis-reasoning`/
`mantis-fast` candidate first; only add a fallback alias that has
*itself* been qualified against the same baseline suite.

Existing deterministic expectation scoring
(`mantis.eval.scoring.evaluate_result`) is completely unchanged by
#16 — it scores `final_answer`/`tool_calls`, neither of which routing
touches. No scenario's expectations needed updating for this issue, and
none is planned unless a future route-specific expectation type is
deliberately added.

## Non-goals

- Adaptive/learned routing.
- Autonomous provider discovery.
- Caller-selected backend models.
- Model-prose-driven escalation.
- Whole-agent/whole-run replay.
- Tool permission escalation (tool permissions are identical regardless
  of which configured alias answered — routing never changes
  `ALLOWED_TOOLS`).
- Infrastructure remediation policy.
- Replacing LiteLLM's cost/rate-limit enforcement.
