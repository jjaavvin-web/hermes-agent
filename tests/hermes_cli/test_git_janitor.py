"""Tests for ``hermes_cli.git_janitor`` — the ``hermes git-health`` suite.

Covers worktree classification, the dry-run-default behaviour of the
janitor, and the ``--confirm`` reap guard (never reaps ACTIVE, never
reaps a protected branch like ``main``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import git_janitor as gj


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _wt(branch="feat/x", head="abc123", detached=False, bare=False):
    return {"branch": branch, "head": head, "detached": detached, "bare": bare}


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway git repo with one commit on ``main``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@example.com"], repo)
    _run(["git", "config", "user.name", "Tester"], repo)
    (repo / "f.txt").write_text("hello\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
def git_repo_with_worktree(git_repo):
    """``git_repo`` plus a ``feature/x`` worktree one commit ahead."""
    wt = git_repo.parent / "wt"
    _run(["git", "worktree", "add", "-q", "-b", "feature/x", str(wt)], git_repo)
    (wt / "new.txt").write_text("change\n")
    _run(["git", "add", "-A"], wt)
    _run(["git", "commit", "-q", "-m", "feat"], wt)
    return git_repo, wt


# ---------------------------------------------------------------------------
# classify_worktree — classification logic
# ---------------------------------------------------------------------------

def test_classify_protected_branch_is_active():
    # main/master/dashboard-live are categorically off-limits.
    assert gj.classify_worktree(
        _wt(branch="main"), lock=None, is_merged=False,
        card_status=None, tmux_alive=False, age_days=999,
    ) == "ACTIVE"


def test_classify_no_lock_not_merged_is_orphaned():
    assert gj.classify_worktree(
        _wt(), lock=None, is_merged=False,
        card_status=None, tmux_alive=False, age_days=30,
    ) == "ORPHANED"


def test_classify_no_lock_merged_is_merged():
    assert gj.classify_worktree(
        _wt(), lock=None, is_merged=True,
        card_status=None, tmux_alive=False, age_days=30,
    ) == "MERGED"


def test_classify_live_tmux_is_active():
    # A live tmux session pins the worktree ACTIVE even when everything
    # else (merged, archived card, old commit) says reapable.
    assert gj.classify_worktree(
        _wt(), lock={"branch": "feat/x"}, is_merged=True,
        card_status="archived", tmux_alive=True, age_days=999,
    ) == "ACTIVE"


def test_classify_running_card_is_active():
    assert gj.classify_worktree(
        _wt(), lock={"branch": "feat/x"}, is_merged=False,
        card_status="running", tmux_alive=False, age_days=999,
    ) == "ACTIVE"


def test_classify_merged_with_lock():
    assert gj.classify_worktree(
        _wt(), lock={"branch": "feat/x"}, is_merged=True,
        card_status="done", tmux_alive=False, age_days=1,
    ) == "MERGED"


def test_classify_stale_when_archived_dead_and_old():
    assert gj.classify_worktree(
        _wt(), lock={"branch": "feat/x"}, is_merged=False,
        card_status="archived", tmux_alive=False, age_days=30, stale_days=7,
    ) == "STALE"


def test_classify_recent_commit_is_not_stale():
    # Archived + dead session but a recent commit => conservative ACTIVE.
    assert gj.classify_worktree(
        _wt(), lock={"branch": "feat/x"}, is_merged=False,
        card_status="archived", tmux_alive=False, age_days=2, stale_days=7,
    ) == "ACTIVE"


def test_classify_ambiguous_lock_is_active():
    # Registry entry, dead session, no terminal card => never auto-reap.
    assert gj.classify_worktree(
        _wt(), lock={"branch": "feat/x"}, is_merged=False,
        card_status=None, tmux_alive=False, age_days=99,
    ) == "ACTIVE"


# ---------------------------------------------------------------------------
# select_reapable — the --confirm guard
# ---------------------------------------------------------------------------

def test_select_reapable_filters_to_the_confirmed_class():
    classified = [
        (_wt(branch="feat/a"), "MERGED"),
        (_wt(branch="feat/b"), "STALE"),
        (_wt(branch="feat/c"), "ORPHANED"),
    ]
    sel = gj.select_reapable(classified, "MERGED")
    assert [wt["branch"] for wt, _ in sel] == ["feat/a"]


def test_select_reapable_never_reaps_active():
    classified = [(_wt(branch="feat/a"), "ACTIVE")]
    assert gj.select_reapable(classified, "ACTIVE") == []


def test_select_reapable_never_reaps_protected_branch():
    # A protected branch mis-classified MERGED must still be excluded.
    classified = [
        (_wt(branch="main"), "MERGED"),
        (_wt(branch="master"), "MERGED"),
        (_wt(branch="feat/a"), "MERGED"),
    ]
    sel = gj.select_reapable(classified, "MERGED")
    assert [wt["branch"] for wt, _ in sel] == ["feat/a"]


def test_reap_worktree_skips_protected_branch(tmp_path):
    action, detail = gj.reap_worktree(
        tmp_path,
        {"path": str(tmp_path / "x"), "branch": "main", "head": "deadbeef"},
        "MERGED",
    )
    assert action == "skipped"
    assert "protected" in detail


def test_reap_worktree_never_touches_the_main_checkout(git_repo):
    # git_repo's .git is a real directory => it is a primary checkout.
    action, detail = gj.reap_worktree(
        git_repo,
        {"path": str(git_repo), "branch": "feature/x", "head": "deadbeef"},
        "ORPHANED",
    )
    assert action == "skipped"
    assert "main working tree" in detail
    assert git_repo.exists()


# ---------------------------------------------------------------------------
# inventory / janitor — integration against a real git repo
# ---------------------------------------------------------------------------

def test_inventory_worktrees_parses_porcelain(git_repo_with_worktree):
    repo, wt = git_repo_with_worktree
    inv = gj.inventory_worktrees(str(repo))
    branches = {entry["branch"] for entry in inv}
    assert "main" in branches
    assert "feature/x" in branches
    paths = {entry["path"] for entry in inv}
    assert str(wt) in paths


def test_janitor_dry_run_is_default_and_mutates_nothing(
    git_repo_with_worktree, capsys
):
    repo, wt = git_repo_with_worktree
    # confirm=None is the dry-run default — no worktree may be removed.
    rc = gj.run_janitor(str(repo), confirm=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out
    assert Path(wt).exists(), "dry-run must not remove the worktree"


def test_janitor_confirm_does_not_touch_active(git_repo_with_worktree, capsys):
    # The feature worktree classifies ACTIVE (no lock + not merged is
    # ORPHANED, but confirm=MERGED only ever targets the MERGED class).
    repo, wt = git_repo_with_worktree
    gj.run_janitor(str(repo), confirm="MERGED")
    capsys.readouterr()
    assert Path(wt).exists()


def test_install_hooks_writes_fallback_hook(git_repo):
    results = gj.install_hooks(str(git_repo))
    assert len(results) == 1
    # No .pre-commit-config.yaml in the repo => always the thin fallback.
    assert results[0]["mode"] == "fallback"
    hook = gj._git_hooks_dir(str(git_repo)) / "pre-commit"
    assert hook.exists()
    body = hook.read_text()
    assert gj._HOOK_MARKER in body
    assert "direct commits to" in body


def test_merge_ready_report_for_a_branch(git_repo_with_worktree):
    repo, _wt = git_repo_with_worktree
    report = gj.merge_ready_report("feature/x", str(repo), base="main")
    assert report.get("error") is None
    assert report["ahead"] == 1
    assert report["behind"] == 0
    assert "new.txt" in report["changed_files"]
    assert report["conflict_prediction"] in ("CLEAN", "CONFLICT", "UNKNOWN")


def test_merge_ready_report_unknown_branch(git_repo):
    report = gj.merge_ready_report("no/such/branch", str(git_repo), base="main")
    assert "error" in report
