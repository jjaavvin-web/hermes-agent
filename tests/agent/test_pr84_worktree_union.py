"""Benign behavior checks for PR84 broker/GC feature reconciliation."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import worktree_broker
from gateway import codex_gc_watcher


def _broker(tmp_path):
    broker = object.__new__(worktree_broker.WorktreeBroker)
    broker.hermes_home = tmp_path
    broker._wt_root = tmp_path / "codex-wt"
    broker._wt_root.mkdir()
    broker._git = MagicMock()
    return broker


def test_gc_preview_preserves_tracked_and_open_pr_worktrees(tmp_path):
    broker = _broker(tmp_path)
    for sid in ("tracked", "open-pr", "orphan"):
        (broker._wt_root / sid).mkdir()

    actions = broker.gc(
        tracked_sids={"tracked"},
        live_branches={"codex/open-pr/task"},
        dry_run=True,
    )

    assert [action.sid for action in actions] == ["orphan"]
    assert actions[0].reason.startswith("dry-run:")
    assert set(path.name for path in broker._wt_root.iterdir()) == {
        "tracked", "open-pr", "orphan",
    }
    assert actions[0].old_path.is_dir()
    assert not actions[0].new_path.exists()
    broker._git.assert_not_called()


def test_gc_preview_retains_empty_registry_safety_floor(tmp_path):
    broker = _broker(tmp_path)
    (broker._wt_root / "unverified").mkdir()

    assert broker.gc(tracked_sids=set(), dry_run=True) == []
    assert (broker._wt_root / "unverified").is_dir()
    broker._git.assert_not_called()


def test_run_registry_lease_is_off_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_RUN_REGISTRY_WRITE", raising=False)
    monkeypatch.setattr(worktree_broker, "read_raw_config", lambda: {})

    worktree_broker.write_lease(tmp_path, object(), repo_root=tmp_path / "repo")

    assert not (tmp_path / "run-registry").exists()


def test_opted_in_lease_round_trip_is_scoped_to_session(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree_broker, "_lease_write_enabled", lambda: True)
    wt = SimpleNamespace(
        session_id="session-one", branch="codex/session-one/task",
        path=tmp_path / "codex-wt" / "session-one",
        created_at=datetime.now(timezone.utc),
    )

    worktree_broker.write_lease(tmp_path, wt, repo_root=tmp_path / "repo")
    lease_path = tmp_path / "run-registry" / "session-one.lock"
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    assert lease["worktree_path"] == str(wt.path)
    assert lease["branch"] == wt.branch
    assert lease["repo_root"] == str(tmp_path / "repo")
    assert lease["spawner"] == "worktree_broker"
    assert lease["tmux_session"] == "codex-sess-session-one"
    sibling = lease_path.with_name("session-two.lock")
    sibling.write_text("retained", encoding="utf-8")

    worktree_broker.remove_lease(tmp_path, wt.session_id)

    assert not lease_path.exists()
    assert sibling.read_text(encoding="utf-8") == "retained"


@pytest.mark.asyncio
async def test_watcher_preview_calls_gc_without_running_reapers(monkeypatch):
    broker = SimpleNamespace(gc=MagicMock(return_value=[]), reap_deleted=MagicMock())
    monkeypatch.setattr(codex_gc_watcher, "_checked_tracked_sids", lambda _: {"tracked"})
    watcher = codex_gc_watcher.CodexGcWatcher(
        dispatcher=object(), worktree_broker=broker,
        gh_list_open_branches=lambda: {"codex/live/task"}, dry_run=True,
    )

    await watcher._tick()

    broker.gc.assert_called_once_with(
        tracked_sids={"tracked"}, live_branches={"codex/live/task"}, dry_run=True,
    )
    broker.reap_deleted.assert_not_called()


@pytest.mark.asyncio
async def test_watcher_preview_keeps_unknown_pr_state_fail_closed(monkeypatch):
    broker = SimpleNamespace(gc=MagicMock(), reap_deleted=MagicMock())
    monkeypatch.setattr(codex_gc_watcher, "_checked_tracked_sids", lambda _: {"tracked"})

    def unavailable():
        raise codex_gc_watcher.OpenPrLookupError("fixture-unavailable")

    watcher = codex_gc_watcher.CodexGcWatcher(
        dispatcher=object(), worktree_broker=broker,
        gh_list_open_branches=unavailable, dry_run=True,
    )

    await watcher._tick()

    broker.gc.assert_not_called()
    broker.reap_deleted.assert_not_called()
