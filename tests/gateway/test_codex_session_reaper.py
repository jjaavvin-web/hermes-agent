"""Tests for gateway.codex_session_reaper (DISP-4 / ARCH-4, built dark)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.codex_session_reaper import CodexSessionReaper


# --------------------------------------------------------------------------- #
# fakes / helpers
# --------------------------------------------------------------------------- #
class _FakeDispatcher:
    """Round-trips state through real JSON I/O, like the production dispatcher."""

    def __init__(self, hermes_home: Path, rows: dict) -> None:
        self.hermes_home = hermes_home
        self._sessions_path = hermes_home / "codex_sessions.json"
        self._sessions_path.write_text(
            json.dumps({"version": 1, "sessions": rows}), encoding="utf-8"
        )

    def _load_state(self) -> dict:
        return json.loads(self._sessions_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        self._sessions_path.write_text(json.dumps(state), encoding="utf-8")


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _git(worktree: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True, text=True, check=True,
    )


def _make_worktree(path: Path, *, dirty: bool, commit_days_ago: float | None) -> Path:
    """Create a real git repo. If commit_days_ago is set, make a back-dated commit.

    ``dirty`` leaves an untracked file so ``git status --porcelain`` is non-empty.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    if commit_days_ago is not None:
        (path / "seed.txt").write_text("seed", encoding="utf-8")
        _git(path, "add", "seed.txt")
        when = (
            datetime.now(timezone.utc) - timedelta(days=commit_days_ago)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", "seed",
             "--date", when],
            capture_output=True, text=True, check=True,
            env={"GIT_COMMITTER_DATE": when, **_base_env()},
        )
    if dirty:
        (path / "uncommitted.txt").write_text("WIP", encoding="utf-8")
    return path


def _base_env() -> dict:
    import os
    return dict(os.environ)


def _row(sid: str, worktree: Path, *, state: str = "EXECUTING",
         last_message_at: str | None = None, created_at: str | None = None,
         isa_slug: str = "task") -> dict:
    return {
        "session_id": sid,
        "thread_id": f"t-{sid}",
        "worktree_path": str(worktree),
        "state": state,
        "isa_slug": isa_slug,
        "last_message_at": last_message_at,
        "created_at": created_at,
    }


def _reaper(disp, broker=None, open_branches=None) -> CodexSessionReaper:
    broker = broker or MagicMock()
    return CodexSessionReaper(
        dispatcher_state=disp,
        broker=broker,
        gh_open_branches_fn=lambda: open_branches if open_branches is not None else set(),
    )


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_idle_below_threshold_is_skipped(tmp_path):
    """last_message_at recent + created_at recent -> skipped, no teardown."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=1)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(2), created_at=_iso(3))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert len(out) == 1
    assert out[0]["outcome"] == "skipped"
    broker.release.assert_not_called()
    # row untouched
    assert disp._load_state()["sessions"]["t-a"]["state"] == "EXECUTING"


def test_uncommitted_work_orphans(tmp_path):
    """Idle + dirty worktree -> ORPHANED, never released."""
    wt = _make_worktree(tmp_path / "wt", dirty=True, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "orphaned"
    assert out[0]["uncommitted_work"] is True
    broker.release.assert_not_called()
    row = disp._load_state()["sessions"]["t-a"]
    assert row["state"] == "ORPHANED"
    assert "orphaned_reason" in row


def test_open_pr_orphans(tmp_path):
    """Idle + clean worktree but branch has an open PR -> ORPHANED."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20),
                        isa_slug="feat")}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker, open_branches={"codex/a/feat"}).reap(
        reap_idle_days=10, dry_run=False
    )

    assert out[0]["outcome"] == "orphaned"
    assert out[0]["in_open_pr"] is True
    broker.release.assert_not_called()
    assert disp._load_state()["sessions"]["t-a"]["state"] == "ORPHANED"


def test_clean_and_idle_releases(tmp_path):
    """Idle (primary) + clean + no open PR -> broker.release + row deleted."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "released"
    broker.release.assert_called_once_with("a")
    # row removed from state
    assert "t-a" not in disp._load_state()["sessions"]


def test_created_at_fallback_fires_when_last_message_recent_no_commits(tmp_path):
    """last_message_at recent but created_at old AND no commits since -> released.

    This is the load-bearing fallback: a chatty-but-dead zombie whose
    last_message_at keeps refreshing must still be reclaimable.
    """
    # Worktree commit predates created_at, so there are NO commits *since* created_at.
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=40)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(1), created_at=_iso(30))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "released"
    assert "fallback" in out[0]["idle_reason"]
    broker.release.assert_called_once_with("a")
    assert "t-a" not in disp._load_state()["sessions"]


def test_created_at_fallback_does_not_fire_when_commits_since(tmp_path):
    """created_at old but a recent commit exists -> fallback must NOT fire -> skipped."""
    # Commit is NEWER than created_at -> there IS progress since creation.
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=1)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(2), created_at=_iso(30))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "skipped"
    broker.release.assert_not_called()


def test_dry_run_performs_no_teardown(tmp_path):
    """dry_run=True still ledgers a 'released' decision but mutates nothing."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    reaper = _reaper(disp, broker)
    # point ledger into tmp
    reaper._ledger_path = tmp_path / "state" / "codex-reaper" / "reap-ledger.jsonl"

    out = reaper.reap(reap_idle_days=10, dry_run=True)

    assert out[0]["outcome"] == "released"
    assert out[0]["dry_run"] is True
    broker.release.assert_not_called()
    # state untouched
    assert disp._load_state()["sessions"]["t-a"]["state"] == "EXECUTING"
    # ledger written
    assert reaper._ledger_path.exists()
    lines = reaper._ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["outcome"] == "released"


def test_terminal_state_rows_are_ignored(tmp_path):
    """A non-CLAIMED/EXECUTING row is never considered, even if ancient + clean."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=40)
    rows = {"t-a": _row("a", wt, state="DONE",
                        last_message_at=_iso(40), created_at=_iso(40))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out == []
    broker.release.assert_not_called()
    assert disp._load_state()["sessions"]["t-a"]["state"] == "DONE"


def test_release_failure_downgrades_to_orphaned(tmp_path):
    """If broker.release raises, the row is ORPHANED (not silently deleted)."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    broker.release.side_effect = RuntimeError("worktree locked")
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "orphaned"
    row = disp._load_state()["sessions"]["t-a"]
    assert row["state"] == "ORPHANED"


def test_ledger_records_every_decision(tmp_path):
    """Every swept row appends exactly one JSONL line to the ledger."""
    wt1 = _make_worktree(tmp_path / "wt1", dirty=False, commit_days_ago=20)
    wt2 = _make_worktree(tmp_path / "wt2", dirty=True, commit_days_ago=20)
    rows = {
        "t-a": _row("a", wt1, last_message_at=_iso(20), created_at=_iso(20)),
        "t-b": _row("b", wt2, last_message_at=_iso(20), created_at=_iso(20)),
    }
    disp = _FakeDispatcher(tmp_path, rows)
    reaper = _reaper(disp)
    reaper._ledger_path = tmp_path / "ledger.jsonl"

    reaper.reap(reap_idle_days=10, dry_run=True)

    lines = reaper._ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    outcomes = {json.loads(line)["session_id"]: json.loads(line)["outcome"] for line in lines}
    assert outcomes == {"a": "released", "b": "orphaned"}
