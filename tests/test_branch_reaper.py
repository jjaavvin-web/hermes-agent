from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import git_janitor as gj


def test_classify_branches_reapable_only_when_merged_no_worktree_no_open_pr(monkeypatch):
    branches = [
        "main",
        "dashboard-live",
        "feat/reap-me",
        "feat/unmerged",
        "feat/live-worktree",
        "feat/open-pr",
        "backup/save-me",
        "candidate/save-me",
        "current-topic",
    ]
    worktrees = [
        {"path": "/repo", "branch": "current-topic"},
        {"path": "/repo/.worktrees/live", "branch": "feat/live-worktree"},
    ]
    merged = {
        "main",
        "dashboard-live",
        "feat/reap-me",
        "feat/live-worktree",
        "feat/open-pr",
        "backup/save-me",
        "candidate/save-me",
        "current-topic",
    }

    def fake_git(repo, *args: str):
        if args == ("branch", "--format=%(refname:short)"):
            return SimpleNamespace(returncode=0, stdout="\n".join(branches), stderr="")
        if args == ("branch", "--show-current"):
            return SimpleNamespace(returncode=0, stdout="current-topic\n", stderr="")
        if len(args) == 4 and args[:2] == ("merge-base", "--is-ancestor"):
            return SimpleNamespace(returncode=0 if args[2] in merged else 1, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(gj, "_git", fake_git)
    monkeypatch.setattr(gj, "inventory_worktrees", lambda repo: worktrees)
    monkeypatch.setattr(gj, "_branch_has_open_pr", lambda repo, branch: branch == "feat/open-pr")

    classified = gj.classify_branches("/repo")

    by_branch = {row["branch"]: row for row in classified}
    assert by_branch["feat/reap-me"]["reapable"] is True
    assert by_branch["feat/reap-me"]["reason"] == "merged-no-worktree-no-open-pr"
    assert by_branch["feat/unmerged"]["reapable"] is False
    assert by_branch["feat/unmerged"]["reason"] == "not-merged"
    assert by_branch["feat/live-worktree"]["reapable"] is False
    assert by_branch["feat/live-worktree"]["reason"] == "live-worktree"
    assert by_branch["feat/open-pr"]["reapable"] is False
    assert by_branch["feat/open-pr"]["reason"] == "open-pr"
    assert by_branch["backup/save-me"]["reapable"] is False
    assert by_branch["backup/save-me"]["reason"] == "protected"
    assert by_branch["candidate/save-me"]["reapable"] is False
    assert by_branch["candidate/save-me"]["reason"] == "protected"
    assert by_branch["main"]["reason"] == "protected"
    assert by_branch["dashboard-live"]["reason"] == "protected"
    assert by_branch["current-topic"]["reason"] == "current-head"


def test_gh_pr_lookup_failure_fails_closed_as_open_pr_unknown(monkeypatch):
    branches = ["feat/merged-but-gh-unavailable"]

    monkeypatch.setattr(gj, "inventory_worktrees", lambda repo: [])
    monkeypatch.setattr(gj, "_branch_has_open_pr", lambda repo, branch: None)

    def fake_git(repo, *args: str):
        if args == ("branch", "--format=%(refname:short)"):
            return SimpleNamespace(returncode=0, stdout="\n".join(branches), stderr="")
        if args == ("branch", "--show-current"):
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if len(args) == 4 and args[:2] == ("merge-base", "--is-ancestor"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(gj, "_git", fake_git)

    classified = gj.classify_branches("/repo")

    assert classified == [
        {
            "branch": "feat/merged-but-gh-unavailable",
            "base": gj.DEFAULT_BASE,
            "merged": True,
            "has_live_worktree": False,
            "has_open_pr": None,
            "reapable": False,
            "reason": "open-pr-unknown",
        }
    ]


def test_branch_has_open_pr_raises_lookup_error_on_gh_failure(monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="gh auth failed")

    monkeypatch.setattr(gj.subprocess, "run", fake_run)

    with pytest.raises(gj.BranchPrLookupError, match="gh auth failed"):
        gj._branch_has_open_pr("/repo", "feat/topic")


def test_reap_branches_skips_non_reapable_and_protected_rows(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_git(repo, *args: str):  # pragma: no cover - should not be called
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gj, "_git", fake_git)

    results = gj.reap_branches(
        "/repo",
        [
            {"branch": "backup/save-me", "reapable": True},
            {"branch": "feat/not-reapable", "reapable": False},
        ],
    )

    assert results == [
        ("backup/save-me", "skipped", "not reapable"),
        ("feat/not-reapable", "skipped", "not reapable"),
    ]
    assert calls == []


def test_janitor_dry_run_reports_reapable_branches_without_deleting(monkeypatch, capsys):
    calls: list[tuple[str, str]] = []
    branches = [
        {"branch": "feat/reap-me", "reapable": True, "reason": "merged-no-worktree-no-open-pr"},
        {"branch": "feat/open-pr", "reapable": False, "reason": "open-pr"},
    ]

    monkeypatch.setattr(gj, "gather_classified", lambda repo, stale_days=7: [({"path": str(Path(repo) / "wt"), "branch": "wt"}, "ACTIVE")])
    monkeypatch.setattr(gj, "classify_branches", lambda repo, base=gj.DEFAULT_BASE: branches)
    monkeypatch.setattr(gj, "reap_branches", lambda repo, selected: calls.append((str(repo), selected[0]["branch"])))

    rc = gj.run_janitor("/repo", confirm=None)

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []
    assert "reapable branches" in out
    assert "feat/reap-me" in out
    assert "feat/open-pr" not in out
    assert "dry-run" in out


def test_reap_branches_uses_branch_dash_d_never_force(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_git(repo, *args: str):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gj, "_git", fake_git)

    results = gj.reap_branches("/repo", [{"branch": "feat/reap-me", "reapable": True}])

    assert results == [("feat/reap-me", "deleted", "git branch -d")]
    assert calls == [("branch", "-d", "feat/reap-me")]
    flattened = [part for call in calls for part in call]
    assert "-D" not in flattened
    assert "--force" not in flattened


def test_janitor_explicit_branch_confirm_invokes_branch_reaper(monkeypatch, capsys):
    calls: list[list[dict]] = []
    branches = [
        {"branch": "feat/reap-me", "reapable": True, "reason": "merged-no-worktree-no-open-pr"},
        {"branch": "feat/open-pr", "reapable": False, "reason": "open-pr"},
    ]

    monkeypatch.setattr(gj, "gather_classified", lambda repo, stale_days=7: [({"path": str(Path(repo) / "wt"), "branch": "wt"}, "ACTIVE")])
    monkeypatch.setattr(gj, "classify_branches", lambda repo, base=gj.DEFAULT_BASE: branches)
    monkeypatch.setattr(gj, "reap_branches", lambda repo, selected: calls.append(selected) or [("feat/reap-me", "deleted", "git branch -d")])

    rc = gj.run_janitor("/repo", confirm="BRANCHES")

    assert rc == 0
    assert calls == [[branches[0]]]
    assert "Reaping 1 merged branch(es)" in capsys.readouterr().out
