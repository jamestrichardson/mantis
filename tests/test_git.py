"""Tests for mantis.integrations.git: repository traversal, first-parent
diff mechanics, deterministic ordering, and bounds (#17).

Uses real temporary local Git repositories built with the real `git`
CLI (tests/_git_fixtures.py) -- never a mock of dulwich itself, and
never live network/GitHub access or a developer's real repository.
"""

from __future__ import annotations

import os
import stat

import pytest

from mantis.integrations.git import (
    MAX_FILES_PER_COMMIT,
    MAX_TOTAL_FILES_RETURNED,
    MAX_WINDOW_SECONDS,
    GitError,
    GitLimitValidationError,
    GitTimeWindowValidationError,
    collect_recent_commits,
    validate_commit_limit,
    validate_time_window,
)
from mantis.reliability import Deadline, IntegrationErrorKind

from _git_fixtures import GitTestRepo

FAR_PAST = "2020-01-01T00:00:00+00:00"
FAR_FUTURE = "2030-01-01T00:00:00+00:00"


def _collect(repo_path, *, start=FAR_PAST, end=FAR_FUTURE, limit=25, **kwargs):
    import datetime

    start_dt = datetime.datetime.fromisoformat(start)
    end_dt = datetime.datetime.fromisoformat(end)
    return collect_recent_commits(repo_path, start=start_dt, end=end_dt, limit=limit, **kwargs)


# ---------------------------------------------------------------------------
# validate_time_window / validate_commit_limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,reason",
    [
        ("not-a-date", "2024-01-02T00:00:00+00:00", "malformed start"),
        ("2024-01-01T00:00:00+00:00", "not-a-date", "malformed end"),
        ("2024-01-01T00:00:00", "2024-01-02T00:00:00+00:00", "naive start"),
        ("2024-01-01T00:00:00+00:00", "2024-01-02T00:00:00", "naive end"),
        ("2024-01-02T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "backwards window"),
        ("2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "zero-width window"),
        (123, "2024-01-02T00:00:00+00:00", "start not a string"),
        ("2024-01-01T00:00:00+00:00", None, "end not a string"),
    ],
)
def test_validate_time_window_rejects_invalid_input(start, end, reason):
    with pytest.raises(GitTimeWindowValidationError):
        validate_time_window(start, end)


def test_validate_time_window_rejects_a_window_over_thirty_days():
    with pytest.raises(GitTimeWindowValidationError):
        validate_time_window("2024-01-01T00:00:00+00:00", "2024-03-01T00:00:00+00:00")


def test_validate_time_window_accepts_a_window_at_exactly_the_cap():
    start = "2024-01-01T00:00:00+00:00"
    import datetime

    start_dt = datetime.datetime.fromisoformat(start)
    end_dt = start_dt + datetime.timedelta(seconds=MAX_WINDOW_SECONDS)
    result = validate_time_window(start, end_dt.isoformat())
    assert result[1] - result[0] == datetime.timedelta(seconds=MAX_WINDOW_SECONDS)


def test_validate_time_window_normalizes_to_utc():
    start_dt, end_dt = validate_time_window("2024-01-01T00:00:00-05:00", "2024-01-02T00:00:00-05:00")
    assert start_dt.utcoffset().total_seconds() == 0
    assert start_dt.hour == 5


@pytest.mark.parametrize("limit", [0, -1, 26, 1000, "20", None, True, False, 3.5])
def test_validate_commit_limit_rejects_invalid_input(limit):
    with pytest.raises(GitLimitValidationError):
        validate_commit_limit(limit)


@pytest.mark.parametrize("limit", [1, 20, 25])
def test_validate_commit_limit_accepts_in_range_values(limit):
    assert validate_commit_limit(limit) == limit


# ---------------------------------------------------------------------------
# Basic traversal
# ---------------------------------------------------------------------------


def test_empty_matching_window_is_a_successful_result(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("a.txt", "1\n")
    repo.commit("only commit", authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")

    result = _collect(repo.path, start="2020-01-01T00:00:00+00:00", end="2020-01-02T00:00:00+00:00")

    assert result.commits == []
    assert result.matched_count == 0


def test_one_commit_is_returned_with_full_evidence(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("a.txt", "hello\n")
    sha = repo.commit("add a.txt", authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")

    result = _collect(repo.path)

    assert len(result.commits) == 1
    commit = result.commits[0]
    assert commit.sha == sha
    assert commit.subject == "add a.txt"
    assert commit.author_name == "Mantis Test"
    assert commit.parent_count == 0
    assert commit.changed_file_count == 1
    assert commit.changed_files[0].path == "a.txt"
    assert commit.changed_files[0].change_type == "added"
    assert commit.authored_at == "2024-06-01T00:00:00+00:00"
    assert commit.committed_at == "2024-06-01T00:00:00+00:00"


def test_multiple_commits_are_ordered_by_committed_at_descending(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("a.txt", "1\n")
    repo.commit("first", authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")
    repo.write_file("b.txt", "2\n")
    repo.commit("second", authored_at="2024-06-02T00:00:00+00:00", committed_at="2024-06-02T00:00:00+00:00")
    repo.write_file("c.txt", "3\n")
    repo.commit("third", authored_at="2024-06-03T00:00:00+00:00", committed_at="2024-06-03T00:00:00+00:00")

    result = _collect(repo.path)

    assert [c.subject for c in result.commits] == ["third", "second", "first"]


def test_identical_committed_timestamps_break_ties_by_sha_descending(tmp_path):
    repo = GitTestRepo(tmp_path)
    same_ts = "2024-06-01T00:00:00+00:00"
    repo.write_file("a.txt", "1\n")
    repo.commit("commit A", authored_at=same_ts, committed_at=same_ts)
    repo.write_file("b.txt", "2\n")
    repo.commit("commit B", authored_at=same_ts, committed_at=same_ts)

    result = _collect(repo.path)

    assert len(result.commits) == 2
    assert result.commits[0].committed_at == result.commits[1].committed_at == same_ts
    # Deterministic tie-break: full SHA descending, regardless of
    # subject/commit order -- proves the sort key really is (time, sha),
    # not incidentally stable on insertion order.
    assert result.commits[0].sha > result.commits[1].sha


def test_authored_and_committed_timestamps_can_differ_and_filtering_follows_committed(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("a.txt", "1\n")
    # Simulates a rebase: authored long ago, committed (landed on this
    # branch) just now.
    repo.commit("rebased commit", authored_at="2020-01-01T00:00:00+00:00", committed_at="2024-06-15T00:00:00+00:00")

    # A window covering the authored time but NOT the committed time
    # must not match -- filtering is by committed_at.
    not_matching = _collect(repo.path, start="2019-12-01T00:00:00+00:00", end="2020-02-01T00:00:00+00:00")
    assert not_matching.commits == []

    # A window covering the committed time but not the authored time
    # must match.
    matching = _collect(repo.path, start="2024-06-01T00:00:00+00:00", end="2024-07-01T00:00:00+00:00")
    assert len(matching.commits) == 1
    assert matching.commits[0].authored_at == "2020-01-01T00:00:00+00:00"
    assert matching.commits[0].committed_at == "2024-06-15T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Root commit / merge commit (first-parent) changed-file semantics
# ---------------------------------------------------------------------------


def test_root_commit_is_compared_against_an_empty_tree(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("a.txt", "1\n")
    repo.write_file("b.txt", "2\n")
    repo.commit("root", authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")

    result = _collect(repo.path)

    commit = result.commits[0]
    assert commit.parent_count == 0
    assert commit.changed_file_count == 2
    assert {f.path for f in commit.changed_files} == {"a.txt", "b.txt"}
    assert {f.change_type for f in commit.changed_files} == {"added"}


def test_merge_commit_uses_first_parent_semantics_only(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("base.txt", "base\n")
    repo.commit("base commit", authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")

    repo.checkout_new_branch("feature")
    repo.write_file("feature.txt", "feature\n")
    repo.commit("feature commit", authored_at="2024-06-02T00:00:00+00:00", committed_at="2024-06-02T00:00:00+00:00")

    repo.checkout("main")
    repo.write_file("mainline.txt", "mainline\n")
    repo.commit("mainline commit", authored_at="2024-06-03T00:00:00+00:00", committed_at="2024-06-03T00:00:00+00:00")

    repo.merge("feature", "merge feature into main", committed_at="2024-06-04T00:00:00+00:00")

    result = _collect(repo.path)
    merge_commit = result.commits[0]
    assert merge_commit.subject == "merge feature into main"
    assert merge_commit.parent_count == 2
    # First-parent diff: only feature.txt is "new" relative to the
    # first parent (mainline commit) -- mainline.txt is NOT reported,
    # since it already existed on the first-parent side.
    assert {f.path for f in merge_commit.changed_files} == {"feature.txt"}


# ---------------------------------------------------------------------------
# Bounds: commit-return cap, internal inspection cap, per-commit and
# aggregate file caps.
# ---------------------------------------------------------------------------


def test_commit_return_cap_reports_matched_count_above_limit(tmp_path):
    repo = GitTestRepo(tmp_path)
    for i in range(5):
        repo.write_file(f"f{i}.txt", str(i))
        repo.commit(f"commit {i}", authored_at="2024-06-01T00:00:00+00:00", committed_at=f"2024-06-0{i + 1}T00:00:00+00:00")

    result = _collect(repo.path, limit=2)

    assert len(result.commits) == 2
    assert result.matched_count == 5
    assert result.commits[0].subject == "commit 4"
    assert result.commits[1].subject == "commit 3"


def test_internal_commit_inspection_cap_stops_the_walk_early(tmp_path, monkeypatch):
    import mantis.integrations.git as git_integration

    monkeypatch.setattr(git_integration, "MAX_COMMITS_INSPECTED", 3)

    repo = GitTestRepo(tmp_path)
    for i in range(5):
        repo.write_file(f"f{i}.txt", str(i))
        repo.commit(f"commit {i}", authored_at="2024-06-01T00:00:00+00:00", committed_at=f"2024-06-0{i + 1}T00:00:00+00:00")

    result = _collect(repo.path, limit=25)

    assert result.commits_inspected == 3
    assert result.inspection_capped is True
    # Only the 3 most-recently-walked commits were ever inspected --
    # the walker visits newest-first, so this is commits 4, 3, 2.
    assert result.matched_count == 3


def test_per_commit_file_cap_bounds_entries_but_reports_the_true_total(tmp_path):
    repo = GitTestRepo(tmp_path)
    for i in range(MAX_FILES_PER_COMMIT + 5):
        repo.write_file(f"f{i}.txt", str(i))
    repo.commit("many files", authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")

    result = _collect(repo.path)

    commit = result.commits[0]
    assert commit.changed_file_count == MAX_FILES_PER_COMMIT + 5
    assert len(commit.changed_files) == MAX_FILES_PER_COMMIT
    assert commit.files_truncated is True


def test_files_under_the_per_commit_cap_are_not_flagged_truncated(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("a.txt", "1\n")
    repo.commit("small commit", authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")

    result = _collect(repo.path)

    assert result.commits[0].files_truncated is False
    assert result.aggregate_files_capped is False


def test_aggregate_file_cap_spans_commits(tmp_path):
    repo = GitTestRepo(tmp_path)
    per_commit = MAX_FILES_PER_COMMIT
    commits_needed = (MAX_TOTAL_FILES_RETURNED // per_commit) + 2
    for batch in range(commits_needed):
        for i in range(per_commit):
            repo.write_file(f"batch{batch}/f{i}.txt", str(i))
        repo.commit(
            f"batch {batch}",
            authored_at="2024-06-01T00:00:00+00:00",
            committed_at=f"2024-06-{batch + 1:02d}T00:00:00+00:00",
        )

    result = _collect(repo.path, limit=25)

    total_returned = sum(len(c.changed_files) for c in result.commits)
    assert total_returned <= MAX_TOTAL_FILES_RETURNED
    assert result.aggregate_files_capped is True


# ---------------------------------------------------------------------------
# Repository-access failures -- mapped into the shared taxonomy, never
# a raw exception.
# ---------------------------------------------------------------------------


def test_missing_configured_repository_raises_git_error(tmp_path):
    missing_path = str(tmp_path / "does-not-exist")
    with pytest.raises(GitError) as exc_info:
        _collect(missing_path)
    assert exc_info.value.kind == IntegrationErrorKind.NOT_FOUND
    # The raised error's own message reaches the model via
    # AgentRuntime's generic IntegrationError handling (ToolError.message
    # = str(exc)) and structured logs -- it must never contain the raw
    # configured path either, not just a successful/invalid_input result.
    assert missing_path not in str(exc_info.value)


def test_configured_path_that_is_not_a_git_repository_raises_git_error(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    (plain_dir / "file.txt").write_text("hello\n")

    with pytest.raises(GitError) as exc_info:
        _collect(str(plain_dir))
    assert exc_info.value.kind == IntegrationErrorKind.NOT_FOUND
    assert str(plain_dir) not in str(exc_info.value)


def test_repository_with_no_commits_raises_git_error(tmp_path):
    repo = GitTestRepo(tmp_path)  # init only, no commits

    with pytest.raises(GitError) as exc_info:
        _collect(repo.path)
    assert exc_info.value.kind == IntegrationErrorKind.NOT_FOUND
    assert repo.path not in str(exc_info.value)


def test_inaccessible_repository_raises_git_error(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.commit("x", allow_empty=True, authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")

    git_dir = os.path.join(repo.path, ".git")
    original_mode = os.stat(git_dir).st_mode
    os.chmod(git_dir, 0)
    try:
        with pytest.raises(GitError) as exc_info:
            _collect(repo.path)
        assert exc_info.value.kind == IntegrationErrorKind.NOT_FOUND
        assert repo.path not in str(exc_info.value)
    finally:
        os.chmod(git_dir, stat.S_IMODE(original_mode) or 0o700)


# ---------------------------------------------------------------------------
# Deadline handling
# ---------------------------------------------------------------------------


def test_deadline_already_expired_stops_the_walk_before_any_commit_is_inspected(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("a.txt", "1\n")
    repo.commit("x", authored_at="2024-06-01T00:00:00+00:00", committed_at="2024-06-01T00:00:00+00:00")

    deadline = Deadline.after(-1.0)
    result = _collect(repo.path, deadline=deadline)

    assert result.deadline_stopped is True
    assert result.commits_inspected == 0
    assert result.commits == []


# ---------------------------------------------------------------------------
# Unicode / control characters / newline-bearing Git data never
# corrupt parsing.
# ---------------------------------------------------------------------------


def test_unicode_and_unusual_bytes_do_not_corrupt_parsing(tmp_path):
    repo = GitTestRepo(tmp_path)
    repo.write_file("héllo wörld ☠.txt", "unicode filename\n")
    repo.write_file("file with spaces.txt", "spaced\n")
    sha = repo.commit(
        "Subject with a\nsecond line that must never leak into the subject field",
        authored_at="2024-06-01T00:00:00+00:00",
        committed_at="2024-06-01T00:00:00+00:00",
    )

    result = _collect(repo.path)

    commit = result.commits[0]
    assert commit.sha == sha
    assert commit.subject == "Subject with a"
    assert "second line" not in commit.subject
    paths = {f.path for f in commit.changed_files}
    assert "héllo wörld ☠.txt" in paths
    assert "file with spaces.txt" in paths
