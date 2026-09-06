from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import git_janitor as gj


def _classified_worktrees(tmp_path: Path) -> list[tuple[dict, str]]:
    merged = tmp_path / "merged-wt"
    orphaned = tmp_path / "orphaned-wt"
    active = tmp_path / "active-wt"
    for root in (merged, orphaned, active):
        root.mkdir()
    (merged / "payload.bin").write_bytes(b"m" * 17)
    (orphaned / "payload.bin").write_bytes(b"o" * 23)
    (active / "payload.bin").write_bytes(b"a" * 101)
    return [
        ({"path": str(merged), "branch": "merged-branch", "head": "a" * 40}, "MERGED"),
        ({"path": str(orphaned), "branch": "orphaned-branch", "head": "b" * 40}, "ORPHANED"),
        ({"path": str(active), "branch": "active-branch", "head": "c" * 40}, "ACTIVE"),
    ]


def test_dry_run_report_has_reclaimable_bytes_and_class_counts(tmp_path, monkeypatch, capsys):
    classified = _classified_worktrees(tmp_path)
    monkeypatch.setattr(gj, "gather_classified", lambda repo, stale_days=7: classified)

    rc = gj.run_janitor(tmp_path, confirm=None)

    out = capsys.readouterr().out
    assert rc == 0
    assert "MERGED=1" in out
    assert "ORPHANED=1" in out
    assert "ACTIVE=1" in out
    assert "reclaimable-bytes" in out
    assert "reclaimable-bytes=40" in out, "MERGED + ORPHANED payload bytes should be included; ACTIVE bytes excluded"
    assert "dry-run" in out


def test_dry_run_never_reaps_or_creates_deleted_markers(tmp_path, monkeypatch, capsys):
    classified = _classified_worktrees(tmp_path)
    calls: list[tuple[dict, str]] = []

    def fail_if_called(wt: dict, klass: str):  # pragma: no cover - failure path
        calls.append((wt, klass))
        raise AssertionError("dry-run must not reap worktrees")

    monkeypatch.setattr(gj, "gather_classified", lambda repo, stale_days=7: classified)
    monkeypatch.setattr(gj, "reap_worktree", fail_if_called)

    rc = gj.run_janitor(tmp_path, confirm=None)

    assert rc == 0
    assert calls == []
    assert not list(tmp_path.glob("*.deleted.*"))
    assert "dry-run" in capsys.readouterr().out


def test_git_health_janitor_cli_dry_run_does_not_reap(tmp_path, monkeypatch, capsys):
    classified = _classified_worktrees(tmp_path)
    calls: list[tuple[dict, str]] = []
    args = SimpleNamespace(
        git_health_command="janitor",
        repo=str(tmp_path),
        stale_days=7,
        confirm=None,
    )

    monkeypatch.setattr(gj, "validate_janitor_repo_root", lambda repo: Path(repo))
    monkeypatch.setattr(gj, "gather_classified", lambda repo, stale_days=7: classified)
    monkeypatch.setattr(gj, "reap_worktree", lambda wt, klass: calls.append((wt, klass)))

    rc = gj.git_health_command(args)

    assert rc == 0
    assert calls == []
    assert not list(tmp_path.glob("*.deleted.*"))
    assert "reclaimable-bytes" in capsys.readouterr().out


def test_validate_janitor_repo_root_rejects_tmp_roots():
    with pytest.raises(ValueError, match="must not be under /tmp"):
        gj.validate_janitor_repo_root(Path("/tmp/hermes-unsafe-janitor-root"))
