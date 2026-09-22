"""Semantic Git tool exposed to agents (#17).

Provides ``git_recent_changes(repository_alias, start, end, limit=20)``:
bounded, read-only source-history evidence -- commits reachable from a
server-side-configured local repository's ``HEAD``, within a bounded
time window, with bounded first-parent changed-file evidence. See
``docs/git.md`` for the full design.

**Source history is not deployment evidence, and correlation is not
causation.** A commit existing in a repository's history says nothing
about whether that commit was ever deployed, and a commit landing near
an incident's timeline says nothing about whether it caused that
incident. Every result carries an explicit ``limitations`` field
stating this, and it is the caller/agent's responsibility to keep these
three claims -- "commit exists in source history", "commit was
deployed", "commit caused the incident" -- separate (see
``mantis.agents.system_troubleshooter``'s Git-specific guidance).

All input validation (repository alias, time window, limit) happens
here, before any repository access is attempted (see
``mantis.integrations.git`` for the traversal/diff mechanics this
gates) -- the same reasoning ``mantis.tools.dns``/``.http``/``.tls``
document: invalid input is untrusted, model-supplied data (#14) and
must flow through ``mantis.security.make_model_safe()`` like any other
tool result, never through the runtime's generic last-resort exception
path.

Adopts the shared result contract from ``mantis.contracts`` (``meta`` /
``QueryMeta``), the same pattern every other Mantis tool established.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from mantis.config import GitRepositoriesConfig
from mantis.contracts import QueryMeta
from mantis.integrations.git import (
    MAX_RESULT_JSON_BYTES,
    GitCommit,
    GitLimitValidationError,
    GitRecentChangesResult,
    GitTimeWindowValidationError,
    collect_recent_commits,
    validate_commit_limit,
    validate_time_window,
)
from mantis.registry import Tool, default_registry
from mantis.reliability import Deadline

logger = logging.getLogger(__name__)

# Every field below is either the original request, the repository's
# own configuration-independent state (HEAD SHA), or a direct,
# structural transformation of the repository's own object data
# (timestamps, author names, subjects, changed-file paths/types) --
# nothing here is heuristic interpretation the way AWX's
# failure_excerpt is. See mantis.contracts.QueryMeta.derived_fields.
DERIVED_RESULT_FIELDS: tuple[str, ...] = ()

LIMITATIONS = (
    "Repository history does not prove a commit was deployed.",
    "Temporal correlation between a commit and an incident does not establish causation.",
)


def _get_git_config() -> GitRepositoriesConfig:
    return GitRepositoriesConfig.from_env()


def _commit_to_dict(commit: GitCommit) -> dict[str, Any]:
    return commit.to_dict()


def _invalid_input_result(repository_alias: Any, start: Any, end: Any, limit: Any, message: str) -> dict[str, Any]:
    # Deliberately never interpolates `message`/repository_alias/start/
    # end/limit into the log line -- see mantis.tools.dns/.http/.tls's
    # identical fix (PR #117/#119 review): a validator's exception text
    # is designed for the *returned* "message" field, which goes
    # through mantis.security.make_model_safe() like any other tool
    # result, not for a raw logger.info(..., message) call that would
    # write it straight to container stdout/Loki unredacted.
    logger.info("git_recent_changes rejected invalid input")
    meta = QueryMeta(source_system="git", derived_fields=list(DERIVED_RESULT_FIELDS))
    return {
        "meta": meta.to_dict(),
        "repository_alias": repository_alias,
        "ref": None,
        "head_sha": None,
        "start": start,
        "end": end,
        "requested_limit": limit,
        "returned_count": 0,
        "commits": [],
        "truncation_reasons": [],
        "limitations": list(LIMITATIONS),
        "error": {"type": "invalid_input", "message": message},
    }


def _truncation_reasons(result: GitRecentChangesResult) -> list[str]:
    reasons: list[str] = []
    if result.matched_count > len(result.commits):
        reasons.append("commit_limit")
    if result.inspection_capped:
        reasons.append("inspection_limit")
    if result.deadline_stopped:
        reasons.append("deadline_exceeded")
    if any(c.files_truncated for c in result.commits):
        reasons.append("per_commit_file_limit")
    if result.aggregate_files_capped:
        reasons.append("aggregate_file_limit")
    return reasons


def _bound_result_size(response: dict[str, Any]) -> dict[str, Any]:
    """Enforce :data:`~mantis.integrations.git.MAX_RESULT_JSON_BYTES`
    on this tool's *own* result -- before #14's separate, generic
    ``mantis.security.MODEL_TOOL_RESULT_MAX_CHARS`` runtime backstop
    ever applies. Trims whole commits from the tail (the least
    recent/least relevant within the already-sorted, already-limited
    ``commits`` list) until the serialized result fits, marking
    ``"result_size_limit"`` in ``truncation_reasons`` if it had to trim
    anything. Never trims below zero commits; an already-tiny result
    (a handful of commits, or none) is never touched.
    """
    serialized = json.dumps(response, default=str)
    if len(serialized.encode("utf-8")) <= MAX_RESULT_JSON_BYTES:
        return response

    trimmed = dict(response)
    commits = list(trimmed["commits"])
    while commits and len(json.dumps({**trimmed, "commits": commits}, default=str).encode("utf-8")) > MAX_RESULT_JSON_BYTES:
        commits.pop()

    trimmed["commits"] = commits
    trimmed["returned_count"] = len(commits)
    if "result_size_limit" not in trimmed["truncation_reasons"]:
        trimmed["truncation_reasons"] = [*trimmed["truncation_reasons"], "result_size_limit"]
    trimmed["meta"] = {**trimmed["meta"], "truncated": True}
    return trimmed


def git_recent_changes(
    repository_alias: Any,
    start: Any,
    end: Any,
    limit: Any = 20,
    *,
    _deadline: Deadline | None = None,
    _config: GitRepositoriesConfig | None = None,
    _collect_fn: Callable[..., GitRecentChangesResult] = collect_recent_commits,
) -> dict[str, Any]:
    """Fetch commits reachable from a server-side-configured local Git
    repository's ``HEAD``, whose committed timestamp falls in
    ``[start, end)``, as bounded source-history evidence.

    Args:
        repository_alias: The *name* of a server-side-configured Git
            repository (e.g. ``"infra_core"``) -- see
            ``mantis.config.GitRepositoriesConfig``. This is the
            **only** repository-selecting input a caller may supply:
            never a filesystem path, remote URL, branch, tag, SHA,
            revision expression, or Git option/command. An alias with
            no matching configured repository is rejected as invalid
            input *before* any repository access is attempted -- no
            filesystem/Git access happens for an unknown alias.
        start: Timezone-aware RFC3339 timestamp -- the (inclusive)
            start of the commit window.
        end: Timezone-aware RFC3339 timestamp -- the (exclusive) end of
            the commit window. Must be after ``start``, and
            ``end - start`` must not exceed 30 days
            (``mantis.integrations.git.MAX_WINDOW_SECONDS``).
        limit: Maximum number of commits to return. Defaults to 20;
            rejected (not clamped) if outside ``[1, 25]``
            (``mantis.integrations.git.MAX_COMMITS_RETURNED``).
        _deadline: Remaining tool-call time budget, set by
            ``AgentRuntime`` -- see ``mantis.reliability.Deadline`` and
            ``docs/reliability.md``. Same keyword-only/underscore
            convention as every other Mantis tool. This is a local,
            synchronous read (no network), so it bounds how much of the
            repository's history is traversed, never a connect/read
            timeout.
        _config: Test/evaluation-only
            :class:`~mantis.config.GitRepositoriesConfig` override --
            same convention as every other Mantis tool's ``_config``.
        _collect_fn: Test/evaluation-only override for
            :func:`mantis.integrations.git.collect_recent_commits` --
            same convention as ``mantis.tools.network``'s
            ``_connect_fn``.

    Returns:
        A dict with ``meta`` (provenance -- ``source_system="git"``,
        ``observation_time`` left unset since this returns multiple
        independently-timestamped commits, and ``query_window`` set to
        the normalized UTC window actually inspected), the request
        echoed back (``repository_alias``), ``ref`` (always
        ``"HEAD"``), ``head_sha`` (the resolved ``HEAD`` SHA --  never
        the configured filesystem path), ``requested_limit``/
        ``returned_count``, ``commits`` (bounded records: ``sha``,
        ``authored_at``, ``committed_at``, ``author_name``,
        ``subject``, ``parent_count``, ``changed_file_count``,
        ``changed_files`` (bounded ``{"path", "change_type"}``
        entries), ``files_truncated``), ``truncation_reasons`` (which
        bound(s) actually omitted evidence -- empty when nothing was
        omitted), and ``limitations`` (the fixed source-history-vs-
        deployment-vs-causality disclaimer, always present).

        If ``repository_alias``/``start``/``end``/``limit`` fail
        validation, the result carries ``"error": {"type":
        "invalid_input", "message": ...}`` and every network-observation
        field is ``None``/empty -- returned as a normal result, not
        raised, since the rejected text is untrusted, model-supplied
        data (#14). A successful call always has ``"error": None`` --
        including a window matching zero commits, which is a normal
        result with ``commits=[]``, never an error.
    """
    try:
        safe_limit = validate_commit_limit(limit)
    except GitLimitValidationError as exc:
        return _invalid_input_result(repository_alias, start, end, limit, str(exc))

    try:
        start_dt, end_dt = validate_time_window(start, end)
    except GitTimeWindowValidationError as exc:
        return _invalid_input_result(repository_alias, start, end, limit, str(exc))

    config = _config or _get_git_config()
    repository_path = config.resolve_repository(repository_alias)
    if repository_path is None:
        return _invalid_input_result(
            repository_alias, start, end, limit, f"Unknown Git repository alias: {repository_alias!r}"
        )

    result = _collect_fn(
        repository_path,
        start=start_dt,
        end=end_dt,
        limit=safe_limit,
        deadline=_deadline,
    )

    truncation_reasons = _truncation_reasons(result)

    meta = QueryMeta(
        source_system="git",
        query_time=result.observed_at,
        observation_time=None,
        query_window={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        truncated=bool(truncation_reasons),
        derived_fields=list(DERIVED_RESULT_FIELDS),
    )

    response = {
        "meta": meta.to_dict(),
        "repository_alias": repository_alias,
        "ref": "HEAD",
        "head_sha": result.head_sha,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "requested_limit": limit,
        "returned_count": len(result.commits),
        "commits": [_commit_to_dict(c) for c in result.commits],
        "truncation_reasons": truncation_reasons,
        "limitations": list(LIMITATIONS),
        "error": None,
    }

    return _bound_result_size(response)


GIT_RECENT_CHANGES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_recent_changes",
        "description": (
            "List commits reachable from a server-side-configured local "
            "Git repository's HEAD, committed within [start, end) (max "
            "30-day window), as bounded source-history evidence: SHA, "
            "authored/committed timestamps, author name, subject, and "
            "bounded first-parent changed-file entries. This tool proves "
            "only that a commit exists in source history -- it never "
            "proves the commit was deployed, and a commit near an "
            "incident's timeline is only a temporal correlation, never a "
            "proven cause. You may only select a repository by its "
            "configured alias -- you cannot supply a filesystem path, "
            "remote URL, branch, tag, SHA, revision expression, or Git "
            "option/command directly. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repository_alias": {
                    "type": "string",
                    "description": (
                        "Name of a server-side-configured Git repository "
                        "(e.g. \"infra_core\"). Never a path/URL/ref directly."
                    ),
                },
                "start": {
                    "type": "string",
                    "description": "Timezone-aware RFC3339 timestamp: start of the commit window (inclusive).",
                },
                "end": {
                    "type": "string",
                    "description": (
                        "Timezone-aware RFC3339 timestamp: end of the commit window "
                        "(exclusive). Must be after start; window must not exceed 30 days."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of commits to return.",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 25,
                },
            },
            "required": ["repository_alias", "start", "end"],
        },
    },
}


default_registry.register(
    Tool(
        name="git_recent_changes",
        schema=GIT_RECENT_CHANGES_SCHEMA,
        handler=git_recent_changes,
        category="git",
        mutating=False,
        # Commit subjects, author names, and file paths are external,
        # Mantis-uncontrolled evidence -- same untrusted-output
        # treatment as every other evidence tool. See mantis.security
        # and docs/security.md.
        contains_untrusted_text=True,
        description=(
            "List commits reachable from a server-side-configured local "
            "Git repository's HEAD within a bounded time window, as "
            "source-history evidence (never deployment or causality "
            "evidence)."
        ),
    )
)
