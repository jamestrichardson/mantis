"""Shared local Git repository fixture helpers for tests/test_git.py
and tests/test_git_tools.py (#17).

Uses the real ``git`` CLI (present on any dev machine and every GitHub
Actions runner by default -- it's what checks the repository out in
the first place) to build small, temporary, throwaway repositories
with deterministic, fully-controlled commit timestamps. This is a
TEST-ONLY convenience, exactly like ``tests/_tls_fixtures.py``'s use of
the ``cryptography`` library to build certificates: production code
(``mantis.integrations.git``) never shells out to ``git`` and never
requires an OS ``git`` executable at all -- see that module's
docstring for why. Using the real CLI here, rather than hand-building
Git objects, is what lets these fixtures build genuinely realistic
history (merges, root commits, rebased-looking authored/committed
timestamp splits) with confidence it matches what a real repository
actually looks like on disk.

Every invocation here explicitly disables commit signing
(``-c commit.gpgsign=false``) and sets a local, throwaway identity, so
these tests never depend on -- or are broken by -- a developer's or
CI runner's own global Git configuration (e.g. a global
``commit.gpgsign=true`` requiring an interactive passphrase, which
would otherwise hang or fail non-interactively).

Not a test file itself (no ``test_`` functions) -- imported by the
real test modules.
"""

from __future__ import annotations

import os
import subprocess

_GIT_TEST_IDENTITY = (
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
    "-c", "user.name=Mantis Test",
    "-c", "user.email=mantis-test@example.invalid",
)


def _run_git(repo_dir: str, *args: str, env: dict[str, str] | None = None) -> None:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    subprocess.run(
        ["git", "-C", repo_dir, *_GIT_TEST_IDENTITY, *args],
        check=True,
        capture_output=True,
        env=full_env,
    )


class GitTestRepo:
    """A real, temporary local Git repository with fully-controlled
    commit timestamps -- built with the real ``git`` CLI (test-only;
    see this module's docstring)."""

    def __init__(self, tmp_path) -> None:
        self.path = str(tmp_path / "repo")
        os.makedirs(self.path, exist_ok=True)
        _run_git(self.path, "init", "-q", "-b", "main")

    def write_file(self, relative_path: str, content: str | bytes) -> None:
        full_path = os.path.join(self.path, relative_path)
        os.makedirs(os.path.dirname(full_path) or self.path, exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full_path, mode) as f:
            f.write(content)

    def remove_file(self, relative_path: str) -> None:
        os.remove(os.path.join(self.path, relative_path))

    def commit(
        self,
        message: str,
        *,
        authored_at: str | None = None,
        committed_at: str | None = None,
        allow_empty: bool = False,
    ) -> str:
        """Stage every current working-tree change (``git add -A``)
        and commit it. ``authored_at``/``committed_at`` are RFC3339-ish
        strings ``git`` accepts directly via ``GIT_AUTHOR_DATE``/
        ``GIT_COMMITTER_DATE`` -- passing different values for each is
        how ``test_git.py`` simulates a rebase/cherry-pick (authored
        long ago, committed just now). Returns the new commit's full
        SHA."""
        _run_git(self.path, "add", "-A")
        env: dict[str, str] = {}
        if authored_at:
            env["GIT_AUTHOR_DATE"] = authored_at
        if committed_at:
            env["GIT_COMMITTER_DATE"] = committed_at
        args = ["commit", "-q", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        _run_git(self.path, *args, env=env)
        return self.head_sha()

    def checkout_new_branch(self, name: str) -> None:
        _run_git(self.path, "checkout", "-q", "-b", name)

    def checkout(self, name: str) -> None:
        _run_git(self.path, "checkout", "-q", name)

    def merge(self, branch: str, message: str, *, committed_at: str | None = None) -> str:
        env: dict[str, str] = {}
        if committed_at:
            env["GIT_COMMITTER_DATE"] = committed_at
            env["GIT_AUTHOR_DATE"] = committed_at
        _run_git(self.path, "merge", "-q", "--no-ff", branch, "-m", message, env=env)
        return self.head_sha()

    def head_sha(self) -> str:
        result = subprocess.run(
            ["git", "-C", self.path, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
