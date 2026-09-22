"""Fixture-backed ``git_recent_changes`` tool for evaluation scenarios
(#17).

:func:`build_git_recent_changes_tool` builds a ``Tool`` bound to a
canned :class:`~mantis.integrations.git.GitRecentChangesResult` via the
real ``git_recent_changes``'s ``_collect_fn`` override -- the same
production preprocessing/contract logic (validation, ``QueryMeta``,
truncation-reason assembly, the fixed source-history-vs-deployment-
vs-causality ``limitations`` disclaimer) runs against fixture data, not
a hand-faked shortcut of it. ``_config`` is a
:class:`~mantis.config.GitRepositoriesConfig` resolving every alias the
scenario's prompt actually uses to a placeholder path -- never really
opened, since ``_collect_fn`` is overridden and the real
``mantis.integrations.git`` code never runs in a scenario.

The one golden scenario here (``system-troubleshooter-git-correlation``)
is registered in ``mantis.eval.fixtures.system_troubleshooter``, which
also reuses :func:`build_git_recent_changes_tool` for the "secondary
tool" registration every other System Troubleshooter scenario needs
now that ``git_recent_changes`` is part of
``mantis.agents.system_troubleshooter.ALLOWED_TOOLS``.
"""

from __future__ import annotations

from typing import Any

from mantis.config import GitRepositoriesConfig
from mantis.integrations.git import GitRecentChangesResult
from mantis.registry import Tool
from mantis.tools.git import GIT_RECENT_CHANGES_SCHEMA, git_recent_changes

GIT_TOOL_NAME = "git_recent_changes"


def build_git_recent_changes_tool(result: GitRecentChangesResult, *, repository_alias: str = "infra_core") -> Tool:
    """Build a ``Tool`` for ``git_recent_changes`` bound to a canned
    :class:`GitRecentChangesResult` via the real tool function's
    ``_collect_fn`` override -- uses the real schema and real tool
    function; only the repository-traversal mechanics are swapped out.
    ``repository_alias`` must match whatever alias the scenario's
    prompt tells the model to use."""
    config = GitRepositoriesConfig(repositories={repository_alias: "/unused/fixture/path"})

    def _fixture_handler(repository_alias: str, start: str, end: str, limit: int = 20) -> dict[str, Any]:
        return git_recent_changes(
            repository_alias,
            start,
            end,
            limit,
            _config=config,
            _collect_fn=lambda *a, **kw: result,
        )

    return Tool(
        name=GIT_TOOL_NAME,
        schema=GIT_RECENT_CHANGES_SCHEMA,
        handler=_fixture_handler,
        category="git",
        mutating=False,
        description="Fixture-backed git_recent_changes for evaluation scenarios.",
    )
