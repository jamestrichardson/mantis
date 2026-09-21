# Git source-history evidence (`git_recent_changes`)

Mantis's first Git evidence tool (#17). The core question it answers
is deliberately narrow:

> What commits landed in *this specific, server-side-configured local
> repository's* history, reachable from `HEAD`, within this bounded
> time window?

Not:

> ~~Was any of this deployed?~~

and not:

> ~~Did one of these commits cause the incident?~~

`git_recent_changes` proves only that a commit exists in source
history. It never proves a commit was deployed, and a commit landing
near an incident's timeline is only a temporal correlation — never a
proven cause. See "The three separate claims" below, which is the
entire reason this tool's result carries an explicit `limitations`
field on every call.

## Layering

Following the existing Mantis architecture (see
[docs/architecture.md](architecture.md)):

```
src/mantis/config.py
    GitRepositoriesConfig -- server-side repository alias configuration
    (alias -> local filesystem path). Parsing only; no filesystem, Git,
    or network access.

src/mantis/integrations/git.py
    Repository-open/traversal/diff mechanics: time-window and limit
    validation, in-process HEAD walking (via `dulwich`), deterministic
    ordering, first-parent changed-file diffing, per-commit/aggregate
    bounding.

src/mantis/tools/git.py
    Semantic result shaping, provenance (QueryMeta), repository-alias
    resolution against GitRepositoriesConfig, registry registration,
    untrusted-output handling, the tool's own result-size bound.
```

No Git mechanics live in an agent; no new reliability abstraction was
created — this reuses #15's `IntegrationError`/`IntegrationErrorKind`
directly, the same way `check_tcp_connectivity`/`dns_lookup`/
`http_probe`/`tls_certificate_inspect` do (see "Reliability posture"
below for how the *deadline* side differs, since this is a local read,
not a network call).

## Why `dulwich`, not the `git` CLI

#17 requires an **in-process** Git implementation so the Mantis
runtime never needs an OS `git` executable, and never shells out or
constructs a Git command line. `dulwich` is a pure-Python Git
implementation — reading repository objects (commits, trees, the
`HEAD` ref) happens entirely through Python calls into `dulwich`,
never a subprocess. There is no `subprocess` import anywhere in
`mantis.integrations.git`/`mantis.tools.git` — see
`tests/test_git_tools.py::test_tool_never_imports_subprocess`, which
checks this structurally by inspecting the modules' own imports, not
just by convention.

(Tests are a different matter: `tests/_git_fixtures.py` uses the real
`git` CLI to *build* small, temporary, throwaway repositories with
fully-controlled commit timestamps — a test-only convenience exactly
like `tests/_tls_fixtures.py`'s use of the `cryptography` library to
build certificates. Production code never does this.)

## v1 scope: local repositories only

The first implementation is **local configured Git repositories
only**:

- A repository is selected exclusively by a server-side-configured
  **alias** — never a filesystem path, remote URL, branch, tag, SHA,
  revision expression, or Git option/command. See "Repository
  aliases" below.
- v1 inspects commits reachable from the configured repository's
  `HEAD` only — never an arbitrary caller-supplied ref.
- GitHub/GitLab/API-backed history is explicitly deferred (see
  "Non-goals"). A later provider must live behind
  `mantis.integrations.git`'s boundary without changing
  `git_recent_changes`'s semantic contract — the tool layer never
  needs to know whether the evidence came from a local repository or
  a future remote-API provider.

## Repository aliases

A caller (model or API client) selects a repository only by its
**configured alias name** — `repository_alias` is the only
repository-selecting input `git_recent_changes` accepts. It can never
supply a filesystem path, remote URL, branch, tag, SHA, revision
expression, or Git option/command directly.

Configure one or more repositories via environment variables:

```bash
MANTIS_GIT_REPOSITORY_INFRA_CORE=/repos/infra-core
MANTIS_GIT_REPOSITORY_APP=/repos/app
```

`<ALIAS>` (case-insensitive) becomes the `repository_alias` a caller
may request. See [docs/configuration.md](configuration.md#git) for the
full reference, and `deploy/standalone/runtime.env.example`/
`deploy/standalone/compose.yaml` for a worked containerized example
(mounting the host repository **read-only**).

`GitRepositoriesConfig.from_env()` performs **no filesystem, Git, or
network access** — it only parses and validates these variables
(non-empty alias, non-empty path). Whether the configured path
actually exists, and whether it's actually a usable Git repository, is
discovered only when the integration opens it — never at
configuration-parse time (see "Repository validation/opening occurs
only when the integration is used" below).

An unrecognized `repository_alias` is rejected as invalid input
(`error.type="invalid_input"`) *before* `git_recent_changes` ever
attempts to open a repository — see "Input validation" below, and
`tests/test_git_tools.py::test_unknown_repository_alias_causes_no_repository_access`
for the proof.

## Repository validation/opening occurs only when the integration is used

Configuration parsing (`GitRepositoriesConfig.from_env()`) never
touches the filesystem, and never opens a repository. A repository is
opened, and its history walked, **only** inside
`mantis.integrations.git.collect_recent_commits()` — which runs only
when a validated `git_recent_changes` call actually needs it (i.e.
never for an invalid/unknown alias). This means a misconfigured path
(missing, not a repository, unreadable) is discovered as a normal,
classified retrieval failure at call time, not as a startup crash —
see "Repository-access failures" below.

## Time window and limit

- `start`/`end` must be timezone-aware RFC3339 timestamps, normalized
  to UTC. `start` must be strictly before `end`.
- The requested window (`end - start`) must not exceed
  `MAX_WINDOW_SECONDS` (**30 days**).
- `limit` defaults to `DEFAULT_COMMIT_LIMIT` (**20**) and is rejected
  — never silently clamped — if outside `[1, MAX_COMMITS_RETURNED]`
  (**25**).

All three are validated (`mantis.integrations.git.validate_time_window`/
`validate_commit_limit`) **before** any repository access is
attempted — an invalid window or limit returns `error.type=
"invalid_input"` with no filesystem access at all, the same posture
`dns_lookup`/`http_probe`/`tls_certificate_inspect` establish for their
own inputs.

## Committed timestamp, not authored timestamp

Filtering and ordering use each commit's **committed** timestamp, not
its authored timestamp. Both are returned (`authored_at`/
`committed_at`) so a rebase or cherry-pick — where a commit's authored
time is much earlier than when it actually landed on the branch being
inspected — stays visible rather than silently collapsed into one
timestamp. A window that covers the authored time but not the
committed time does **not** match; a window that covers the committed
time but not the authored time **does** match. See
`tests/test_git.py::test_authored_and_committed_timestamps_can_differ_and_filtering_follows_committed`.

## Deterministic ordering

Commits are ordered by `committed_at` **descending**, with the full
commit SHA (also descending) as the deterministic tie-breaker for
identical timestamps — never left to whatever order `dulwich`'s own
`HEAD` walk happens to visit commits in, which is a reasonable-looking
but not contractually guaranteed order once merges are involved. The
integration layer collects every matching commit first, then sorts
this way explicitly, then slices to `limit`. See
`tests/test_git.py::test_identical_committed_timestamps_break_ties_by_sha_descending`.

## First-parent changed-file semantics

Changed-file evidence is always **first-parent**: a commit is diffed
against its first parent's tree only. A root commit (no parents) is
diffed against an empty tree — every file it introduces shows as
`"added"`. A merge commit (two or more parents) is diffed against its
*first* parent only — files that already existed on the first-parent
side (e.g. everything already on the target branch before the merge)
are never reported, even though they're part of what the merge commit
"contains" in a broader sense. Combined/parent-by-parent merge
analysis is explicitly out of scope for v1. See
`tests/test_git.py::test_merge_commit_uses_first_parent_semantics_only`
for the deterministic proof, and "Non-goals" below.

Git rename detection is **not** enabled for v1 — a rename appears as a
`"deleted"`/`"added"` pair, never a distinct `"renamed"` change type.

## Failure semantics

**Repository-access failures are never a raw exception** — they're
mapped into #15's shared `IntegrationErrorKind` taxonomy via
`mantis.integrations.git.GitError`, handled by `AgentRuntime`
generically, exactly like every other Mantis integration's failures:

| Situation | Classification |
|---|---|
| Configured path doesn't exist | `NOT_FOUND` |
| Configured path exists but isn't a Git repository | `NOT_FOUND` |
| Configured path/repository is unreadable (permission denied) | `NOT_FOUND` |
| Repository has no commits reachable from `HEAD` (freshly initialized, empty) | `NOT_FOUND` |
| Unexpected error reading repository objects (corrupt object, I/O error mid-read) | `SERVER_ERROR` |

The first three collapse to the same classification deliberately:
`dulwich` itself raises the identical `NotGitRepository` for a missing
path, a path that exists but isn't a repository, and (in practice) a
path whose permissions prevent even detecting a repository there — it
cannot reliably tell these apart, so neither can this integration. See
`mantis.integrations.git.collect_recent_commits`'s docstring and
`tests/test_git.py`'s "Repository-access failures" section for the
deterministic proofs (including a real permission-revoked repository).

**An empty matching window is a successful result, never a failure.**
`commits=[]` with `error=None` is exactly what a correctly-behaving
call returns when nothing landed in the requested window — this is
evidence (the window is empty), not a retrieval problem. See
`tests/test_git.py::test_empty_matching_window_is_a_successful_result`.

## Deadline semantics (#15) — a local read, not a network call

This is local, synchronous, CPU-bound filesystem I/O — there is no
connect/read timeout to configure, and no transient failure that
retrying would fix (retrying a local read that failed because the path
isn't a repository would just fail again, identically, every time).
`collect_recent_commits` is therefore **never** wrapped in
`mantis.reliability.retry_call()` — the same reasoning
`check_tcp_connectivity`/`dns_lookup`/`http_probe`/`tls_certificate_inspect`
apply to their own current-state reads, generalized here to "current
source-history state" rather than "current network state". See
[docs/reliability.md](reliability.md#git-a-local-synchronous-read-not-an-http-call)
for the fuller comparison against HTTP-based integrations' retry
posture.

The caller's remaining `Deadline` (set by `AgentRuntime` per tool
call) is still respected: the `HEAD` walk checks it between commits
and stops early — marking the result `deadline_stopped`/inspection-
capped — rather than running unbounded against a pathologically large
history. See `tests/test_git.py::test_deadline_already_expired_stops_the_walk_before_any_commit_is_inspected`.

## Result shape

```json
{
  "meta": {
    "source_system": "git",
    "query_time": "2026-09-19T00:00:00+00:00",
    "observation_time": null,
    "query_window": {"start": "2026-09-14T00:00:00+00:00", "end": "2026-09-16T04:00:00+00:00"},
    "truncated": false,
    "derived_fields": [],
    "contract_version": "1.0"
  },
  "repository_alias": "infra_core",
  "ref": "HEAD",
  "head_sha": "c1a2c1a2c1a2c1a2c1a2c1a2c1a2c1a2c1a2c1a2",
  "start": "2026-09-14T00:00:00+00:00",
  "end": "2026-09-16T04:00:00+00:00",
  "requested_limit": 20,
  "returned_count": 1,
  "commits": [
    {
      "sha": "c1a2c1a2c1a2c1a2c1a2c1a2c1a2c1a2c1a2c1a2",
      "authored_at": "2026-09-16T01:00:00+00:00",
      "committed_at": "2026-09-16T01:00:00+00:00",
      "author_name": "Priya Patel",
      "subject": "Adjust firewall allowlist for ferros network segment",
      "parent_count": 1,
      "changed_file_count": 1,
      "changed_files": [
        {"path": "network/firewall_rules.yaml", "change_type": "modified"}
      ],
      "files_truncated": false
    }
  ],
  "truncation_reasons": [],
  "limitations": [
    "Repository history does not prove a commit was deployed.",
    "Temporal correlation between a commit and an incident does not establish causation."
  ],
  "error": null
}
```

(Real, verified output — generated by running `git_recent_changes`
against the System Troubleshooter's `system-troubleshooter-git-correlation`
evaluation fixture; not a hand-written mockup.)

Adopts the same `mantis.contracts.QueryMeta` contract as every other
Mantis tool: `meta.source_system` is always `"git"`;
`meta.observation_time` is deliberately left `None` — this call
returns multiple commits, each with its own natural timestamps
(`authored_at`/`committed_at`), so no single batch-level observation
time could represent that without being misleading, the same reasoning
`awx_recent_failed_jobs` documents for its own job list;
`meta.query_window` records the actual normalized UTC window
inspected; `meta.derived_fields` is empty — every field in a commit
record is either the original request or a direct, structural
transformation of the repository's own object data (timestamps,
author name, subject, changed-file paths/types), never heuristic
interpretation the way AWX's `failure_excerpt` is.

`head_sha`/`repository_alias`/`ref` are the only endpoint-identifying
fields — the configured filesystem path itself **never** appears in
any result, success or failure. See "Security and bounds" below.

## The three separate claims

This is #17's central design requirement, and the reason the
`limitations` field is present on *every* result, success or failure:

1. **"This commit exists in the repository's history."** What
   `git_recent_changes` actually proves.
2. **"This commit was deployed."** Never provable by this tool alone —
   `git_recent_changes` has no visibility into what's actually
   running anywhere. A separate, future deployment-state evidence
   source would be needed to establish this.
3. **"This commit caused the incident."** Never provable by temporal
   proximity alone — a commit landing near an incident's timeline is,
   at most, a correlation worth flagging as something to check
   further.

Code and documentation must keep these three claims distinct
everywhere they're discussed — see
`mantis.agents.system_troubleshooter.SYSTEM_PROMPT`'s explicit
Git-specific rule, and the
`system-troubleshooter-git-correlation` evaluation scenario (below),
whose entire purpose is a deterministic check that a model's answer
respects this separation.

## Input validation

`repository_alias`, `start`, `end`, and `limit` are all validated
before any repository access is attempted:

- `repository_alias` must name a configured repository — an alias
  with no matching repository is rejected the same way, and this check
  happens without ever touching the filesystem
  (`GitRepositoriesConfig.resolve_repository` performs no I/O).
- `start`/`end` must satisfy every check in "Time window and limit"
  above.
- `limit` must satisfy the same section's bound.

Invalid input returns `"error": {"type": "invalid_input", "message":
...}` as a **normal tool result**, not a raised exception —
deliberately, because the rejected text is untrusted, model-supplied
data (#14) and must flow through the standard model-input safety
pipeline (`mantis.security.make_model_safe()`) like any other tool
result. Every repository-observation field (`head_sha`, `commits`,
...) is `None`/empty in this case; `start`/`end`/`limit` echo back the
*raw* request (whatever was actually supplied, even if malformed) so
the rejection is inspectable. The runtime's generic last-resort
exception path does not apply that pipeline, which is why validation
failures are returned, never raised.

`git_recent_changes`'s own logging never writes the raw rejected
`repository_alias`/`start`/`end`/`limit` to a log line — only a fixed
string (`"git_recent_changes rejected invalid input"`). See
`tests/test_git_tools.py::test_invalid_input_never_writes_the_raw_untrusted_value_to_the_log`.

## Named bounds

Every bound is named and deliberate:

| Bound | Value | Protects against |
|---|---|---|
| `MAX_WINDOW_SECONDS` | 30 days | An unbounded time-window scan. |
| `DEFAULT_COMMIT_LIMIT` | 20 | The default when a caller doesn't specify `limit`. |
| `MAX_COMMITS_RETURNED` | 25 | The most commits any one call can ever return. |
| `MAX_COMMITS_INSPECTED` | 500 | Unbounded traversal of a pathologically large `HEAD` history. |
| `MAX_FILES_PER_COMMIT` | 25 | A single commit flooding the model with changed-file entries. |
| `MAX_TOTAL_FILES_RETURNED` | 200 | Several large-but-individually-under-cap commits still ballooning the aggregate result. |
| `MAX_AUTHOR_NAME_CHARS` | 128 | An oversized author name. |
| `MAX_SUBJECT_CHARS` | 256 | An oversized commit subject. |
| `MAX_PATH_CHARS` | 512 | An oversized changed-file path. |
| `MAX_RESULT_JSON_BYTES` | 64 KiB | This tool's *own* result-size ceiling — enforced before #14's separate, generic runtime backstop (`mantis.security.MODEL_TOOL_RESULT_MAX_CHARS`) ever applies; see below. |

`meta.truncated` is `true` whenever any bound above actually omitted
matching evidence — never merely because a bound exists.
`truncation_reasons` names which one(s) specifically applied:

| Reason | Meaning |
|---|---|
| `commit_limit` | More commits matched the window than `limit` (or `MAX_COMMITS_RETURNED`) allowed returning. |
| `inspection_limit` | `MAX_COMMITS_INSPECTED` was reached before the `HEAD` walk naturally exhausted reachable history. |
| `deadline_exceeded` | The caller's remaining `Deadline` expired before the walk naturally exhausted reachable history. |
| `per_commit_file_limit` | At least one returned commit's changed files exceeded `MAX_FILES_PER_COMMIT`. |
| `aggregate_file_limit` | `MAX_TOTAL_FILES_RETURNED` was reached before every returned commit's files could be fully represented. |
| `result_size_limit` | `mantis.tools.git._bound_result_size` had to trim whole commits from the tail to fit `MAX_RESULT_JSON_BYTES`. |

Truncation metadata never claims an exact omitted count unless the
implementation actually knows it (it doesn't, for most of these — e.g.
`inspection_limit` doesn't imply exactly how many further commits
exist beyond the inspected 500). A naturally small result (a window
that only ever contained a handful of commits) never reports
truncation merely because the caller requested a larger `limit` — see
`tests/test_git.py::test_files_under_the_per_commit_cap_are_not_flagged_truncated`
and `tests/test_git_tools.py::test_small_result_is_never_trimmed`.

### This tool's own result-size bound, before #14's backstop

`mantis.tools.git._bound_result_size` measures the fully-assembled
result's serialized size and, only if it exceeds
`MAX_RESULT_JSON_BYTES`, trims whole commits from the tail (the
least-recent end of the already `committed_at`-descending-sorted list)
until it fits — never a generic, opaque excerpt the way #14's runtime
backstop (`mantis.security.MODEL_TOOL_RESULT_MAX_CHARS`, applied to
*every* tool result as a last resort) would produce. This tool must
stay within its own documented ceiling on its own; it never relies on
the generic backstop to keep its result well-formed. See
`tests/test_git_tools.py::test_oversized_result_is_trimmed_before_the_14_backstop`.

## Security and bounds

`git_recent_changes` is registered with `mutating=False` and
`contains_untrusted_text=True` (commit subjects, author names, and
file paths are external, presenter-controlled data — the same
untrusted-output treatment as every other evidence tool; see
[docs/security.md](security.md)).

**Why repository selection is alias-only, never an arbitrary path**:
allowing a caller to name a filesystem path (or remote URL, ref,
revision expression, or Git option) directly would turn this tool into
a general-purpose repository-traversal capability against *whatever
the model chooses* — a fundamentally different, much larger capability
than "inspect one of a small, operator-approved set of repositories."
Restricting selection to a fixed, server-side-configured alias set
keeps the tool's entire capability surface auditable from
`mantis.config.GitRepositoriesConfig` alone — the same reasoning
`dns_lookup`/`http_probe`/`tls_certificate_inspect` apply to their own
target selection.

**No mutation of any kind.** There is no checkout, reset, fetch, pull,
push, working-tree modification, or ref update anywhere in this tool
— it only ever reads already-committed history via `dulwich`'s
read-only object-store APIs. Deploying it with the configured
repository mounted **read-only** (see
`deploy/standalone/compose.yaml`) is a further, deployment-level
defense-in-depth measure, not a substitute for this code-level
guarantee.

Malicious commit text is preserved as evidence, not stripped — see
`tests/test_git_tools.py::test_malicious_commit_subject_remains_untrusted_not_stripped`/
`test_malicious_file_path_remains_untrusted_not_stripped`, and
[docs/security.md](security.md)'s "Why prompt-like text is preserved
as evidence" for the rationale. Unicode, spaces, and unusual/control
bytes in commit data never corrupt result parsing — commit
messages/paths are decoded best-effort (`errors="replace"`), never
raised on; see `tests/test_git.py::test_unicode_and_unusual_bytes_do_not_corrupt_parsing`.

## Non-goals

Deliberately excluded from #17 (candidates for a distinct, later,
separately-scoped issue if ever needed):

- **GitHub/GitLab/remote-API history.** v1 is local-repository-only.
  A later provider must live behind `mantis.integrations.git`'s
  boundary without changing `git_recent_changes`'s contract.
- **Arbitrary ref/revision-expression/branch/tag/SHA selection.** Only
  commits reachable from the configured repository's `HEAD` are ever
  inspected.
- **Full diff/patch content.** Only bounded `{"path", "change_type"}`
  changed-file entries — never a unified diff or file contents.
- **Addition/deletion line statistics.** Not required for v1.
- **Combined/parent-by-parent merge diffing.** Changed-file evidence
  is always first-parent only.
- **Rename detection.** A rename appears as a delete/add pair.
- **Author email.** Only `author_name` is returned by default.
- **Any mutation** — checkout, reset, fetch, pull, push, or ref
  update. This tool is read-only by construction.
- **Arbitrary Git command passthrough or shell execution.** All
  mechanics go through `dulwich`'s typed Python API.
- **Agent-specific Git logic.** All mechanics live in
  `mantis.integrations.git`/`mantis.tools.git`, reusable by any
  current or future agent that declares `git_recent_changes` in its
  `ALLOWED_TOOLS` — see [docs/agents.md](agents.md). The System
  Troubleshooter (#11) is the first to do so.

## Agent integration

The System Troubleshooter (#11) declares `git_recent_changes` in its
`ALLOWED_TOOLS` and describes it in its system prompt explicitly as
source-history/correlation evidence, never deployment-state evidence
— see `mantis.agents.system_troubleshooter.SYSTEM_PROMPT`'s dedicated
Git rule (the same "three separate claims" from above, restated as an
instruction). No dependency on a not-yet-implemented Incident Triage
agent was required to wire this in; any future agent can reuse the
same registered tool the same way.

## Evaluation

`mantis.eval.fixtures.git.build_git_recent_changes_tool` builds a
fixture-backed `git_recent_changes` tool using the real production
tool code (only `collect_recent_commits` is swapped for canned data),
mirroring every other Mantis tool's evaluation-fixture convention.

The `system-troubleshooter-git-correlation` scenario (in
`mantis.eval.fixtures.system_troubleshooter`) exercises exactly the
"three separate claims" requirement: the fixture places a plausible,
temporally-correlated commit (a firewall-allowlist change committed
shortly before the incident) alongside the System Troubleshooter's
existing AWX/TCP/Prometheus/Loki evidence, and **deliberately contains
no evidence the commit was ever deployed**. Deterministic expectations
hard-fail an answer that asserts the commit caused the incident
(`UnsupportedDefinitiveClaim`) or that it was deployed without hedging
(`HypothesisLabeled` with `hard=True`, since a genuinely good answer is
expected to *raise* deployment as an open question, e.g. "check
whether this was rolled out" — a bare forbidden-phrase match couldn't
tell that apart from actually asserting it happened). A correct answer
may identify the change as temporally relevant and recommend checking
deployment-state evidence as the next step. See
[docs/evaluation.md](evaluation.md#git-correlation-scenario) and
`tests/eval/test_system_troubleshooter_scenarios.py`'s git-correlation
tests for the deterministic good/bad-answer proofs.

## Follow-up

No follow-up issue is required to satisfy #17 as scoped — every
explicit requirement in the ticket is implemented and tested. A
future, separately-scoped issue could reasonably cover a
GitHub/GitLab-API-backed provider behind the same integration
boundary, combined/parent-by-parent merge diffing for a concrete
motivated use case, or wiring `git_recent_changes` into a future
Incident Triage agent. None of these are started here.
