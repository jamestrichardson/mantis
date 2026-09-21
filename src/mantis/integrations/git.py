"""Bounded, read-only local Git repository inspection mechanics (#17):
commits reachable from a configured local repository's ``HEAD``, within
a bounded time window, with bounded first-parent changed-file evidence.

This module knows how to open a local repository and walk/diff its
commit history; it has no knowledge of agents, LLMs, tool schemas, or
repository *aliases* -- see ``mantis.tools.git`` for the semantic,
LLM-facing layer that resolves an alias (via
``mantis.config.GitRepositoriesConfig``) into the concrete filesystem
path this module receives.

**In-process, never a shell.** Uses ``dulwich`` (a pure-Python Git
implementation) to read repository objects directly -- there is no
``subprocess`` import anywhere in this module, no shell command
construction, and no generic Git command passthrough. This is what lets
the Mantis runtime avoid requiring an OS ``git`` executable at all (see
#17's explicit requirement). See ``docs/git.md`` for the full design.

v1 scope, deliberately narrow (see #17's non-goals):

- Local repositories only -- no GitHub/GitLab/remote-API history. A
  later provider must live behind this same integration boundary
  without changing ``mantis.tools.git``'s semantic contract.
- Commits reachable from the configured repository's ``HEAD`` only --
  never an arbitrary caller-supplied ref/SHA/revision expression.
- Changed-file evidence is always **first-parent**: a merge commit is
  compared against its first parent only (never a combined/
  parent-by-parent diff); a root commit is compared against an empty
  tree. Full diff/patch content and addition/deletion statistics are
  out of scope for v1.
- No checkout, reset, fetch, pull, push, or any working-tree/ref
  mutation -- this module never opens a repository for anything but
  reading already-committed history.

Reliability posture (#15): this is local, synchronous, CPU-bound
filesystem I/O, not a network call -- there is no connect/read timeout
to configure and no transient failure that retrying would fix, so
:func:`collect_recent_commits` is never wrapped in
``mantis.reliability.retry_call()`` (see ``docs/reliability.md``'s
local-read note). The caller's remaining ``Deadline`` is still
respected: commit traversal checks it between commits and stops early
(marking the result as inspection-capped) rather than running
unbounded against a pathologically large history.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from dulwich.diff_tree import tree_changes
from dulwich.repo import NotGitRepository, Repo

from mantis.reliability import Deadline, IntegrationError, IntegrationErrorKind

SOURCE_SYSTEM = "git"

MAX_COMMITS_INSPECTED = 500
"""Hard cap on how many commits the ``HEAD`` walk visits for one call,
regardless of how many actually fall within the requested time window --
protects against unbounded traversal of a pathologically large history.
Reaching this cap before the walk naturally exhausts marks the result
inspection-capped (see :class:`GitRecentChangesResult`)."""

MAX_COMMITS_RETURNED = 25
"""Hard ceiling on ``limit`` -- the most commits any single call can
ever return, regardless of what a caller requests."""

DEFAULT_COMMIT_LIMIT = 20
"""Default ``limit`` when a caller doesn't specify one."""

MAX_FILES_PER_COMMIT = 25
"""Cap on the number of bounded changed-file entries returned for any
one commit -- a commit's own ``changed_file_count`` is always the true
total, never bounded, so truncation here is always visible rather than
silently undercounted."""

MAX_TOTAL_FILES_RETURNED = 200
"""Cap on the total number of changed-file entries returned across
*every* commit in one result -- protects against a small number of
huge commits (e.g. a vendored-dependency update) each individually
under :data:`MAX_FILES_PER_COMMIT` but still ballooning the aggregate
result size."""

MAX_AUTHOR_NAME_CHARS = 128
"""Cap on the bounded ``author_name`` field's length."""

MAX_SUBJECT_CHARS = 256
"""Cap on the bounded commit ``subject`` field's length."""

MAX_PATH_CHARS = 512
"""Cap on each bounded changed-file ``path`` field's length."""

MAX_RESULT_JSON_BYTES = 64 * 1024
"""Ceiling on this tool's own serialized result size, in bytes --
enforced by ``mantis.tools.git`` *before* #14's separate, generic
``mantis.security.MODEL_TOOL_RESULT_MAX_CHARS`` runtime backstop ever
applies (see that module's ``_bound_result_size``). Deliberately a
similar order of magnitude to the runtime backstop, but independently
defined and enforced -- this tool must never rely on the generic
backstop to keep its own result well-formed and useful rather than an
opaque excerpt."""


class GitError(IntegrationError):
    """Raised for a repository-access failure: the configured path
    isn't a usable Git repository (missing, not a repository,
    unreadable -- ``dulwich`` itself cannot reliably distinguish these
    from each other, see :func:`collect_recent_commits`), the
    repository has no commits reachable from ``HEAD``, or an
    unexpected error occurs while reading repository objects. Never
    raised for a normal, successful (possibly empty) result."""

    def __init__(self, message: str, *, kind: IntegrationErrorKind) -> None:
        super().__init__(message, kind=kind, source_system=SOURCE_SYSTEM)


class GitTimeWindowValidationError(ValueError):
    """Raised by :func:`validate_time_window` for a ``start``/``end``
    pair that is not a timezone-aware RFC3339 timestamp, has
    ``start >= end``, or spans more than :data:`MAX_WINDOW_SECONDS`."""


class GitLimitValidationError(ValueError):
    """Raised by :func:`validate_commit_limit` for a ``limit`` outside
    ``[1, MAX_COMMITS_RETURNED]``."""


MAX_WINDOW_SECONDS = 30 * 24 * 60 * 60
"""Largest ``end - start`` window one call may request (30 days) --
protects against a request that would force scanning an unbounded
span of history."""


def validate_time_window(start: object, end: object) -> tuple[datetime, datetime]:
    """Validate ``start``/``end`` are timezone-aware RFC3339 timestamps
    with ``start < end`` and a window no larger than
    :data:`MAX_WINDOW_SECONDS`. Returns ``(start_utc, end_utc)``, both
    normalized to UTC -- callers never need to reason about whatever
    UTC offset a caller's input happened to use."""
    if not isinstance(start, str):
        raise GitTimeWindowValidationError(f"start must be a string, got {type(start).__name__}")
    if not isinstance(end, str):
        raise GitTimeWindowValidationError(f"end must be a string, got {type(end).__name__}")

    try:
        start_dt = datetime.fromisoformat(start)
    except ValueError as exc:
        raise GitTimeWindowValidationError(f"start is not a valid RFC3339 timestamp: {start!r}") from exc
    try:
        end_dt = datetime.fromisoformat(end)
    except ValueError as exc:
        raise GitTimeWindowValidationError(f"end is not a valid RFC3339 timestamp: {end!r}") from exc

    if start_dt.tzinfo is None or start_dt.utcoffset() is None:
        raise GitTimeWindowValidationError(f"start must be timezone-aware: {start!r}")
    if end_dt.tzinfo is None or end_dt.utcoffset() is None:
        raise GitTimeWindowValidationError(f"end must be timezone-aware: {end!r}")

    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)

    if start_utc >= end_utc:
        raise GitTimeWindowValidationError(f"start ({start!r}) must be before end ({end!r})")
    if (end_utc - start_utc).total_seconds() > MAX_WINDOW_SECONDS:
        raise GitTimeWindowValidationError(
            f"requested window ({start!r} to {end!r}) exceeds the maximum of "
            f"{MAX_WINDOW_SECONDS} seconds (30 days)"
        )
    return start_utc, end_utc


def validate_commit_limit(limit: object) -> int:
    """Validate ``limit`` is an ``int`` in ``[1, MAX_COMMITS_RETURNED]``
    (rejected outright, never silently clamped -- an out-of-range
    ``limit`` is invalid input, exactly like an out-of-range time
    window)."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise GitLimitValidationError(f"limit must be an integer, got {type(limit).__name__}")
    if not (1 <= limit <= MAX_COMMITS_RETURNED):
        raise GitLimitValidationError(f"limit must be between 1 and {MAX_COMMITS_RETURNED}, got {limit!r}")
    return limit


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(value: bytes) -> str:
    """Best-effort UTF-8 decode of raw Git object data (author lines,
    commit messages, file paths) -- Git itself does not enforce any
    particular text encoding, so malformed byte sequences are replaced
    (``errors="replace"``) rather than raised. #17 requires that
    Unicode, control characters, and other unusual bytes never corrupt
    result parsing; this is the one place that guarantee is made."""
    return value.decode("utf-8", errors="replace")


def _bounded_str(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def _parse_author_name(raw: bytes) -> str:
    """Extract just the name portion of a Git identity line
    (``"Name <email>"``) -- the email is never returned (#17: "author
    email is not returned by default")."""
    text = _decode(raw)
    name = text.split("<", 1)[0].strip()
    return name or text.strip()


def _first_line(raw: bytes) -> str:
    text = _decode(raw)
    return text.splitlines()[0] if text else ""


def _iso_utc(epoch_seconds: int) -> str:
    """Git stores commit/author time as a Unix timestamp, which is
    already UTC -- the separate timezone-offset field Git also stores
    is only the *original* local offset for display purposes and is
    deliberately not applied here; every timestamp #17 returns is
    normalized to UTC."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class ChangedFile:
    """One bounded changed-file entry. Never a diff/patch body and
    never addition/deletion statistics -- see this module's docstring."""

    path: str
    change_type: str
    """One of ``"added"``, ``"modified"``, ``"deleted"`` -- Git rename
    detection is not enabled for v1 (see #17's non-goals), so a rename
    appears as a delete/add pair, never a distinct ``"renamed"`` type."""

    def to_dict(self) -> dict:
        return {"path": self.path, "change_type": self.change_type}


@dataclass(frozen=True)
class GitCommit:
    """One commit's bounded evidence."""

    sha: str
    authored_at: str
    committed_at: str
    author_name: str
    subject: str
    parent_count: int
    changed_file_count: int
    changed_files: list[ChangedFile] = field(default_factory=list)
    files_truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "sha": self.sha,
            "authored_at": self.authored_at,
            "committed_at": self.committed_at,
            "author_name": self.author_name,
            "subject": self.subject,
            "parent_count": self.parent_count,
            "changed_file_count": self.changed_file_count,
            "changed_files": [f.to_dict() for f in self.changed_files],
            "files_truncated": self.files_truncated,
        }


@dataclass(frozen=True)
class GitRecentChangesResult:
    """The full outcome of one :func:`collect_recent_commits` call."""

    head_sha: str
    commits: list[GitCommit]
    matched_count: int
    """How many inspected commits fell within the requested window,
    *before* slicing down to ``limit`` -- lets the caller (see
    ``mantis.tools.git``) tell "more matched than were returned"
    (a commit-count truncation) apart from "the window genuinely only
    contained this many commits"."""
    commits_inspected: int
    inspection_capped: bool
    """True if :data:`MAX_COMMITS_INSPECTED` was reached before the
    ``HEAD`` walk naturally exhausted reachable history."""
    deadline_stopped: bool
    """True if the caller's remaining ``Deadline`` expired before the
    ``HEAD`` walk naturally exhausted reachable history (independent of
    ``inspection_capped`` -- either can trigger alone)."""
    aggregate_files_capped: bool
    """True if :data:`MAX_TOTAL_FILES_RETURNED` was reached before
    every returned commit's changed files (each already bounded by
    :data:`MAX_FILES_PER_COMMIT`) could be fully represented."""
    observed_at: str


def _first_parent_tree(repo: "Repo", commit) -> bytes | None:
    if not commit.parents:
        return None
    parent = repo[commit.parents[0]]
    return parent.tree


def _changed_files(repo: "Repo", commit, *, budget: int) -> tuple[list[ChangedFile], int, bool]:
    """Diff ``commit`` against its first parent (or an empty tree for a
    root commit). Returns ``(bounded_entries, true_total_count,
    files_truncated)``. ``budget`` is the number of entries this call
    may still add, accounting for both the per-commit cap
    (:data:`MAX_FILES_PER_COMMIT`) and whatever remains of the
    aggregate cap (:data:`MAX_TOTAL_FILES_RETURNED`) -- the caller
    computes and passes in the smaller of the two."""
    parent_tree = _first_parent_tree(repo, commit)
    entries: list[ChangedFile] = []
    total = 0
    truncated = False
    for change in tree_changes(repo.object_store, parent_tree, commit.tree):
        total += 1
        if change.type == "add":
            path_bytes, change_type = change.new.path, "added"
        elif change.type == "delete":
            path_bytes, change_type = change.old.path, "deleted"
        else:
            path_bytes, change_type = change.new.path, "modified"
        if len(entries) >= budget:
            truncated = True
            continue
        path, path_truncated = _bounded_str(_decode(path_bytes), MAX_PATH_CHARS)
        truncated = truncated or path_truncated
        entries.append(ChangedFile(path=path, change_type=change_type))
    return entries, total, truncated


def collect_recent_commits(
    repository_path: str,
    *,
    start: datetime,
    end: datetime,
    limit: int,
    deadline: Deadline | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> GitRecentChangesResult:
    """Collect commits reachable from ``repository_path``'s ``HEAD``
    whose **committed** timestamp falls in ``[start, end)``, sorted
    deterministically (committed timestamp descending, full SHA
    descending as the tie-breaker -- see #17), bounded to ``limit``.

    Callers must validate/normalize ``start``/``end``/``limit``
    themselves (see ``mantis.tools.git``) -- this function trusts its
    arguments, knows nothing about aliases/config, and never shells
    out to ``git``.

    The repository is opened, and its history walked, *only* by this
    call -- never at configuration-parse time (see
    ``mantis.config.GitRepositoriesConfig``).

    Raises :class:`GitError` for:

    - The configured path not being a usable Git repository at all.
      ``dulwich`` raises the same ``NotGitRepository`` for a missing
      path, a path that exists but isn't a repository, *and* (in
      practice) a path whose permissions prevent even detecting a
      repository there -- it cannot reliably tell these apart, so
      neither can this function; all three classify identically as
      :data:`~mantis.reliability.IntegrationErrorKind.NOT_FOUND`.
    - A repository with no commits reachable from ``HEAD`` at all (an
      empty, newly-initialized repository) -- also ``NOT_FOUND``.
    - Any other unexpected error reading repository objects (a
      corrupt object, an I/O error mid-read) --
      :data:`~mantis.reliability.IntegrationErrorKind.SERVER_ERROR`.

    Never raised for a normal, successful result — including a window
    that matches zero commits, which returns
    ``GitRecentChangesResult(commits=[], ...)``, not an error (#17: "An
    empty matching window is a successful result... not a retrieval
    failure").
    """
    observed_at = _utc_now_iso()
    start_epoch = start.timestamp()
    end_epoch = end.timestamp()

    try:
        with Repo(repository_path) as repo:
            try:
                head_sha = repo.head()
            except KeyError as exc:
                # Never interpolate `repository_path` (or any exception
                # text that might echo it back) into this message -- #17
                # requires the raw configured path never appear in any
                # error, and this classification alone (NOT_FOUND) is
                # enough for AgentRuntime to handle it generically.
                raise GitError(
                    "the configured repository has no commits reachable from HEAD",
                    kind=IntegrationErrorKind.NOT_FOUND,
                ) from exc

            inspected = 0
            inspection_capped = False
            deadline_stopped = False
            matched: list = []

            try:
                walker = repo.get_walker(include=[head_sha])
                for entry in walker:
                    if deadline is not None and deadline.expired():
                        deadline_stopped = True
                        break
                    if inspected >= MAX_COMMITS_INSPECTED:
                        inspection_capped = True
                        break
                    inspected += 1
                    commit = entry.commit
                    if start_epoch <= commit.commit_time < end_epoch:
                        matched.append(commit)
            except GitError:
                raise
            except Exception as exc:
                # Only the exception's *type name* is included, never
                # its str() -- an underlying dulwich error's message
                # text is not guaranteed not to echo the repository
                # path back, and #17 requires the raw configured path
                # never appear in any error.
                raise GitError(
                    f"error walking commit history: {type(exc).__name__}", kind=IntegrationErrorKind.SERVER_ERROR
                ) from exc

            matched.sort(key=lambda c: (c.commit_time, c.id), reverse=True)
            matched_count = len(matched)
            selected = matched[:limit]

            commits: list[GitCommit] = []
            aggregate_remaining = MAX_TOTAL_FILES_RETURNED
            aggregate_files_capped = False
            try:
                for commit in selected:
                    per_commit_budget = min(MAX_FILES_PER_COMMIT, aggregate_remaining)
                    entries, true_total, files_truncated = _changed_files(
                        repo, commit, budget=per_commit_budget
                    )
                    # The aggregate cap (as opposed to the per-commit
                    # cap alone) is what caused this commit's own
                    # truncation only if its effective budget was
                    # already shrunk below the normal per-commit
                    # allowance -- i.e. an earlier commit's files had
                    # already consumed some of the aggregate budget.
                    if files_truncated and per_commit_budget < MAX_FILES_PER_COMMIT:
                        aggregate_files_capped = True
                    aggregate_remaining -= len(entries)

                    author_name, _ = _bounded_str(_parse_author_name(commit.author), MAX_AUTHOR_NAME_CHARS)
                    subject, _ = _bounded_str(_first_line(commit.message), MAX_SUBJECT_CHARS)

                    commits.append(
                        GitCommit(
                            sha=commit.id.decode("ascii"),
                            authored_at=_iso_utc(commit.author_time),
                            committed_at=_iso_utc(commit.commit_time),
                            author_name=author_name,
                            subject=subject,
                            parent_count=len(commit.parents),
                            changed_file_count=true_total,
                            changed_files=entries,
                            files_truncated=files_truncated,
                        )
                    )
            except GitError:
                raise
            except Exception as exc:
                # Same reasoning as the walk-error handler above: type
                # name only, never str(exc).
                raise GitError(
                    f"error computing changed files for a commit: {type(exc).__name__}",
                    kind=IntegrationErrorKind.SERVER_ERROR,
                ) from exc

            return GitRecentChangesResult(
                head_sha=head_sha.decode("ascii"),
                commits=commits,
                matched_count=matched_count,
                commits_inspected=inspected,
                inspection_capped=inspection_capped,
                deadline_stopped=deadline_stopped,
                aggregate_files_capped=aggregate_files_capped,
                observed_at=observed_at,
            )
    except NotGitRepository as exc:
        # Never interpolate `exc` here: dulwich's own NotGitRepository
        # message is literally "No git repository was found at
        # <path>", which would leak the configured path straight into
        # this error's text -- see this function's docstring and #17's
        # explicit requirement that the raw configured path never
        # appear in any error, model-facing output, or log line.
        raise GitError(
            "no usable Git repository was found at the configured path", kind=IntegrationErrorKind.NOT_FOUND
        ) from exc
