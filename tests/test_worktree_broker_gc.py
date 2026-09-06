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


def test_orphan_worktrees_not_in_tracked_sids_are_swept_to_deleted_bucket(tmp_path):
    """``WorktreeBroker.gc()`` has no ``dry_run``/``merged_base``/tmux- or
    ancestor-liveness API (never implemented — see ``agent/worktree_broker.py``
    ``gc()``, unchanged since fork base); the ONLY thing that makes a sid an
    orphan is being absent from ``tracked_sids`` (and not matching
    ``live_branches``). An orphan is renamed into
    ``codex-wt/.deleted-<ts>/<sid>/`` — never dry-run, always a real rename —
    with a fixed reason string and a ``git worktree prune`` per action."""
    broker = _make_broker(tmp_path)
    before_paths = {sid: _seed_worktree(broker, sid) for sid in CENSUS_MERGED_SIDS}

    broker._git = MagicMock()

    actions = broker.gc(
        tracked_sids=set(),
        live_branches=set(),
        allow_empty_tracked_sids=True,
    )

    assert {action.sid for action in actions} == CENSUS_MERGED_SIDS
    for action in actions:
        assert not action.old_path.exists()
        assert action.new_path.exists()
        assert action.reason == "orphan: not in tracked_sids and no open PR"
    for sid, old_path in before_paths.items():
        assert not old_path.exists()
    assert broker._git.call_count == len(CENSUS_MERGED_SIDS)
    broker._git.assert_called_with("worktree", "prune")


def test_active_non_ancestor_sandbox_31f6fb9e_is_excluded_from_sweep(tmp_path):
    """``tracked_sids`` is the only exclusion mechanism ``gc()`` has (no
    tmux/git-ancestor introspection exists): a sandbox sid that IS tracked
    is left in place at its original path while untracked sids are swept."""
    broker = _make_broker(tmp_path)
    for sid in [*CENSUS_MERGED_SIDS, ACTIVE_SANDBOX_SID]:
        _seed_worktree(broker, sid)

    broker._git = MagicMock()

    actions = broker.gc(
        tracked_sids={ACTIVE_SANDBOX_SID},
        live_branches=set(),
    )

    assert {action.sid for action in actions} == CENSUS_MERGED_SIDS
    assert ACTIVE_SANDBOX_SID not in {action.sid for action in actions}
    # The tracked sandbox worktree stays exactly where it was.
    assert (broker.hermes_home / "codex-wt" / ACTIVE_SANDBOX_SID).exists()
    # The untracked census sids were actually swept (real rename, no dry-run).
    for action in actions:
        assert action.new_path.exists()
        assert not action.old_path.exists()


@pytest.mark.asyncio
async def test_gc_watcher_tick_tracks_every_session_row_and_delegates_to_broker(tmp_path):
    """``CodexGcWatcher`` has no ``dry_run`` constructor arg and ``_tick()``
    does no state/``created_at`` filtering — per its own docstring, "any
    session with a row in codex_sessions.json is tracked" REGARDLESS of
    state, so both the active and the terminal row below end up in
    ``tracked_sids``. ``_tick()`` unconditionally delegates to
    ``broker.gc(tracked_sids=..., live_branches=...)`` and then
    ``broker.reap_deleted(...)``."""
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
    hermes_home = tmp_path / "dispatcher-home"
    hermes_home.mkdir()
    sessions_path = hermes_home / "codex_sessions.json"
    state = {"version": 1, "sessions": rows}
    sessions_path.write_text(json.dumps(state), encoding="utf-8")
    dispatcher = SimpleNamespace(
        hermes_home=hermes_home,
        _sessions_path=sessions_path,
        _load_state=lambda: state,
    )
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    watcher = CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=lambda: set(),
    )

    await watcher._tick()

    assert broker.gc.call_args.kwargs["tracked_sids"] == {ACTIVE_SANDBOX_SID, "2dc50bcd"}
    assert broker.gc.call_args.kwargs["live_branches"] == set()
    broker.reap_deleted.assert_called_once()


def test_gc_leaves_tracked_worktrees_untouched(tmp_path):
    """``WorktreeBroker.gc()`` has no ``dry_run`` mode — ``tracked_sids`` is
    the only protection it offers. Worktrees whose sid IS in ``tracked_sids``
    must be left completely alone: no rename, no ``.deleted-<ts>`` bucket,
    no ``git`` call."""
    broker = _make_broker(tmp_path)
    before_paths = {_seed_worktree(broker, sid) for sid in CENSUS_MERGED_SIDS}

    broker._git = MagicMock()

    actions = broker.gc(tracked_sids=set(CENSUS_MERGED_SIDS))

    assert actions == []
    assert all(path.exists() for path in before_paths)
    assert not list((broker.hermes_home / "codex-wt").glob(".deleted-*"))
    broker._git.assert_not_called()
