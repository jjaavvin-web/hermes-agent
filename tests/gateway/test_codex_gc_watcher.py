"""Tests for gateway.codex_gc_watcher (P5.1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.codex_gc_watcher import CodexGcWatcher


class _FakeDispatcher:
    def __init__(self, hermes_home: Path, rows: dict) -> None:
        self._sessions_path = hermes_home / "codex_sessions.json"
        self._state = {"version": 1, "sessions": rows}
        self._sessions_path.write_text(json.dumps(self._state))

    def _load_state(self) -> dict:
        return json.loads(self._sessions_path.read_text(encoding="utf-8"))


def _row(sid: str) -> dict:
    return {"session_id": sid, "state": "EXECUTING", "thread_id": f"t-{sid}"}


@pytest.mark.asyncio
async def test_tick_derives_tracked_sids_from_rows(tmp_path):
    rows = {"t1": _row("sid-aaa"), "t2": _row("sid-bbb"), "t3": _row("sid-ccc")}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker)
    await w._tick()
    broker.gc.assert_called_once()
    sids = broker.gc.call_args.kwargs["tracked_sids"]
    assert sids == {"sid-aaa", "sid-bbb", "sid-ccc"}


@pytest.mark.asyncio
async def test_tick_handles_empty_sessions(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker)
    await w._tick()
    broker.gc.assert_called_once_with(tracked_sids=set())


@pytest.mark.asyncio
async def test_tick_calls_broker_reap_deleted(tmp_path):
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker, reap_max_age_days=14)
    await w._tick()
    broker.reap_deleted.assert_called_once_with(max_age_days=14)


@pytest.mark.asyncio
async def test_gc_exception_does_not_skip_reap(tmp_path):
    """ISA D-3: gc raising must not abort the tick or skip reap."""
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.side_effect = RuntimeError("disk full")
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker)
    # Must not raise.
    await w._tick()
    broker.reap_deleted.assert_called_once()


@pytest.mark.asyncio
async def test_reap_exception_does_not_kill_loop(tmp_path):
    """Symmetric: reap raising must not propagate."""
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.side_effect = OSError("read-only fs")
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker)
    await w._tick()  # must not raise


@pytest.mark.asyncio
async def test_start_then_stop_clean(tmp_path):
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(
        dispatcher=disp, worktree_broker=broker, poll_interval_sec=0.05,
    )
    await w.start()
    await asyncio.sleep(0.18)
    await w.stop()
    # At least one tick should have run in 180ms with a 50ms interval.
    assert broker.gc.call_count >= 1
    assert broker.reap_deleted.call_count >= 1
    assert w._task is None
