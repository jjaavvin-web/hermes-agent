from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.worktree_broker import WorktreeBroker
from gateway.codex_gc_watcher import CodexGcWatcher


CENSUS_MERGED_SIDS = {"2dc50bcd", "4165f4dc", "57dee01c"}
ACTIVE_SANDBOX_SID = "31f6fb9e"


def _make_broker(tmp_path: Path) -> WorktreeBroker:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / ".hermes"
    return WorktreeBroker(repo_root=repo, hermes_home=home)


def _seed_worktree(broker: WorktreeBroker, sid: str) -> Path:
    path = broker.hermes_home / "codex-wt" / sid
    path.mkdir(parents=True, exist_ok=True)
    (path / "payload.txt").write_text(sid, encoding="utf-8")
    return path


def test_tracked_merged_ancestor_without_tmux_is_reapable_in_dry_run(tmp_path, monkeypatch):
    broker = _make_broker(tmp_path)
    for sid in CENSUS_MERGED_SIDS:
        _seed_worktree(broker, sid)

    monkeypatch.setattr(broker, "_tmux_session_alive", lambda sid: False)
    monkeypatch.setattr(broker, "_worktree_head", lambda path: f"head-{path.name}")
    monkeypatch.setattr(broker, "_head_is_ancestor", lambda head, base: True)
    broker._git = MagicMock()

    actions = broker.gc(
        tracked_sids=set(CENSUS_MERGED_SIDS),
        live_branches=set(),
        dry_run=True,
        merged_base="fork/main",
    )

    assert {action.sid for action in actions} == CENSUS_MERGED_SIDS
    for action in actions:
        assert action.old_path.exists()
        assert action.reason.startswith("dry-run:")
        assert "merged into fork/main" in action.reason
    broker._git.assert_not_called()


def test_active_non_ancestor_sandbox_31f6fb9e_is_excluded_from_dry_run(tmp_path, monkeypatch):
    broker = _make_broker(tmp_path)
    for sid in [*CENSUS_MERGED_SIDS, ACTIVE_SANDBOX_SID]:
        _seed_worktree(broker, sid)

    monkeypatch.setattr(broker, "_tmux_session_alive", lambda sid: False)
    monkeypatch.setattr(broker, "_worktree_head", lambda path: f"head-{path.name}")
    monkeypatch.setattr(
        broker,
        "_head_is_ancestor",
        lambda head, base: not head.endswith(ACTIVE_SANDBOX_SID),
    )
    broker._git = MagicMock()

    actions = broker.gc(
        tracked_sids={*CENSUS_MERGED_SIDS, ACTIVE_SANDBOX_SID},
        live_branches=set(),
        dry_run=True,
        merged_base="fork/main",
    )

    assert {action.sid for action in actions} == CENSUS_MERGED_SIDS
    assert (broker.hermes_home / "codex-wt" / ACTIVE_SANDBOX_SID).exists()
    assert all(not action.new_path.exists() for action in actions)
    broker._git.assert_not_called()


@pytest.mark.asyncio
async def test_gc_watcher_ignores_future_tz_naive_created_at_and_tracks_active_rows(tmp_path):
    future = datetime.utcnow() + timedelta(days=7)
    rows = {
        "active": {
            "session_id": ACTIVE_SANDBOX_SID,
            "state": "EXECUTING",
            "created_at": future.isoformat(),
        },
        "terminal": {
            "session_id": "2dc50bcd",
            "state": "DONE",
            "created_at": future.isoformat(),
        },
    }
    dispatcher = SimpleNamespace(_load_state=lambda: {"version": 1, "sessions": rows})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    watcher = CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=lambda: set(),
        dry_run=True,
    )

    await watcher._tick()

    assert broker.gc.call_args.kwargs["tracked_sids"] == {ACTIVE_SANDBOX_SID}
    assert broker.gc.call_args.kwargs["dry_run"] is True
    broker.reap_deleted.assert_not_called()


def test_gc_dry_run_mutates_nothing_for_reapable_tracked_worktrees(tmp_path, monkeypatch):
    broker = _make_broker(tmp_path)
    before_paths = {_seed_worktree(broker, sid) for sid in CENSUS_MERGED_SIDS}

    monkeypatch.setattr(broker, "_tmux_session_alive", lambda sid: False)
    monkeypatch.setattr(broker, "_worktree_head", lambda path: f"head-{path.name}")
    monkeypatch.setattr(broker, "_head_is_ancestor", lambda head, base: True)
    broker._git = MagicMock()

    actions = broker.gc(tracked_sids=set(CENSUS_MERGED_SIDS), dry_run=True)

    assert {action.sid for action in actions} == CENSUS_MERGED_SIDS
    assert all(path.exists() for path in before_paths)
    assert not list((broker.hermes_home / "codex-wt").glob(".deleted-*"))
    broker._git.assert_not_called()
