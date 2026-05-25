"""Tests for gateway.codex_merge_watcher (P3.5)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from gateway.codex_merge_watcher import (
    CodexMergeWatcher,
    _classify_pr_state,
)


class _FakeDispatcher:
    """Minimal dispatcher stub: just _load_state / _write_state on JSON."""

    def __init__(self, hermes_home: Path, rows: dict) -> None:
        self._sessions_path = hermes_home / "codex_sessions.json"
        self._state = {"version": 1, "sessions": rows}
        self._write_state(self._state)

    def _load_state(self) -> dict:
        return json.loads(self._sessions_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        self._sessions_path.write_text(json.dumps(state), encoding="utf-8")


def _row(state: str = "MERGING", *, pr_number: int | None = 42, **extra) -> dict:
    row = {
        "session_id": "sid-1",
        "thread_id": "t1",
        "state": state,
        "pr_number": pr_number,
        "pr_url": "https://example/pr/42",
        "head_branch": "codex/sid-1/task",
        "merge_label": "auto-merge",
    }
    row.update(extra)
    return row


class TestClassifyPrState:
    def test_merged_explicit(self):
        assert _classify_pr_state({"state": "MERGED"}) == "MERGED"

    def test_merged_via_mergedAt_even_if_state_closed(self):
        assert _classify_pr_state({"state": "CLOSED", "mergedAt": "2026-05-25T00:00:00Z"}) == "MERGED"

    def test_closed_unmerged(self):
        assert _classify_pr_state({"state": "CLOSED", "mergedAt": None}) == "CLOSED"

    def test_open(self):
        assert _classify_pr_state({"state": "OPEN"}) == "OPEN"

    def test_unknown_defaults_to_open(self):
        assert _classify_pr_state({}) == "OPEN"


class TestTickFiltering:
    @pytest.mark.asyncio
    async def test_skips_non_merging_rows(self, tmp_path):
        rows = {
            "t1": _row(state="EXECUTING"),
            "t2": _row(state="CLAIMED"),
            "t3": _row(state="COMPLETE"),
            "t4": _row(state="ESCALATED"),
        }
        disp = _FakeDispatcher(tmp_path, rows)
        gh_calls: list[int] = []
        def fake_gh(num: int) -> Optional[dict]:
            gh_calls.append(num)
            return {"state": "OPEN"}
        merged_fired: list[str] = []
        closed_fired: list[str] = []
        async def on_merged(tid, p): merged_fired.append(tid)
        async def on_closed(tid, p): closed_fired.append(tid)
        w = CodexMergeWatcher(
            dispatcher=disp, on_pr_merged=on_merged,
            on_pr_closed_unmerged=on_closed, poll_interval_sec=10.0,
            gh_pr_view=fake_gh,
        )
        await w._tick()
        assert gh_calls == []  # no MERGING rows -> no gh calls
        assert merged_fired == [] and closed_fired == []

    @pytest.mark.asyncio
    async def test_skips_merging_row_without_pr_number(self, tmp_path):
        rows = {"t1": _row(state="MERGING", pr_number=None)}
        disp = _FakeDispatcher(tmp_path, rows)
        gh_calls: list[int] = []
        def fake_gh(num: int) -> Optional[dict]:
            gh_calls.append(num); return {"state": "OPEN"}
        w = CodexMergeWatcher(
            dispatcher=disp,
            on_pr_merged=lambda tid, p: asyncio.sleep(0),
            on_pr_closed_unmerged=lambda tid, p: asyncio.sleep(0),
            gh_pr_view=fake_gh,
        )
        await w._tick()
        assert gh_calls == []


class TestTransitions:
    @pytest.mark.asyncio
    async def test_open_to_merged_fires_callback(self, tmp_path):
        rows = {"t1": _row(state="MERGING")}
        disp = _FakeDispatcher(tmp_path, rows)
        fired: list[tuple[str, dict]] = []
        async def on_merged(tid, payload): fired.append((tid, payload))
        async def on_closed(tid, payload): pass
        payloads = iter([
            {"state": "OPEN", "mergedAt": None},
            {"state": "MERGED", "mergedAt": "2026-05-25T01:00:00Z",
             "mergeCommit": {"oid": "abc123"}},
        ])
        def fake_gh(num: int): return next(payloads)
        w = CodexMergeWatcher(
            dispatcher=disp, on_pr_merged=on_merged,
            on_pr_closed_unmerged=on_closed, gh_pr_view=fake_gh,
        )
        await w._tick()
        assert fired == []  # first tick: OPEN, no transition (last is None)
        await w._tick()
        assert len(fired) == 1
        assert fired[0][0] == "t1"
        assert fired[0][1]["state"] == "MERGED"

    @pytest.mark.asyncio
    async def test_open_to_closed_fires_callback(self, tmp_path):
        rows = {"t1": _row(state="MERGING")}
        disp = _FakeDispatcher(tmp_path, rows)
        fired: list[str] = []
        async def on_merged(tid, payload): pass
        async def on_closed(tid, payload): fired.append(tid)
        payloads = iter([
            {"state": "OPEN", "mergedAt": None},
            {"state": "CLOSED", "mergedAt": None,
             "closedAt": "2026-05-25T01:00:00Z"},
        ])
        def fake_gh(num: int): return next(payloads)
        w = CodexMergeWatcher(
            dispatcher=disp, on_pr_merged=on_merged,
            on_pr_closed_unmerged=on_closed, gh_pr_view=fake_gh,
        )
        await w._tick()
        await w._tick()
        assert fired == ["t1"]

    @pytest.mark.asyncio
    async def test_no_double_fire_within_run(self, tmp_path):
        rows = {"t1": _row(state="MERGING")}
        disp = _FakeDispatcher(tmp_path, rows)
        fired: list[str] = []
        async def on_merged(tid, payload): fired.append(tid)
        async def on_closed(tid, payload): pass
        # Always return MERGED on every call.
        def fake_gh(num: int):
            return {"state": "MERGED", "mergedAt": "2026-05-25T01:00:00Z",
                    "mergeCommit": {"oid": "abc"}}
        w = CodexMergeWatcher(
            dispatcher=disp, on_pr_merged=on_merged,
            on_pr_closed_unmerged=on_closed, gh_pr_view=fake_gh,
        )
        await w._tick()  # first time seeing MERGED -> fires
        await w._tick()  # already MERGED -> no fire
        await w._tick()
        assert fired == ["t1"]

    @pytest.mark.asyncio
    async def test_rehydrates_pr_state_on_start(self, tmp_path):
        # Row says pr_state was MERGED last cache.  Watcher should NOT
        # re-fire on_merged when it sees MERGED again post-restart.
        rows = {"t1": _row(state="MERGING", pr_state="MERGED")}
        disp = _FakeDispatcher(tmp_path, rows)
        fired: list[str] = []
        async def on_merged(tid, payload): fired.append(tid)
        async def on_closed(tid, payload): pass
        def fake_gh(num: int):
            return {"state": "MERGED", "mergedAt": "2026-05-25T01:00:00Z"}
        w = CodexMergeWatcher(
            dispatcher=disp, on_pr_merged=on_merged,
            on_pr_closed_unmerged=on_closed, gh_pr_view=fake_gh,
            poll_interval_sec=10.0,
        )
        # Mimic start() rehydration (without actually running the loop).
        state = disp._load_state()
        for tid, r in state.get("sessions", {}).items():
            if r.get("pr_state"):
                w._last_pr_state[tid] = r["pr_state"]
        await w._tick()
        assert fired == []


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_then_stop_cleanly(self, tmp_path):
        rows = {"t1": _row(state="MERGING")}
        disp = _FakeDispatcher(tmp_path, rows)
        ticks: list[int] = []
        def fake_gh(num: int):
            ticks.append(num)
            return {"state": "OPEN"}
        w = CodexMergeWatcher(
            dispatcher=disp,
            on_pr_merged=lambda tid, p: asyncio.sleep(0),
            on_pr_closed_unmerged=lambda tid, p: asyncio.sleep(0),
            poll_interval_sec=0.05,
            gh_pr_view=fake_gh,
        )
        await w.start()
        await asyncio.sleep(0.18)
        await w.stop()
        # At least one gh call should have fired in 180 ms with a 50 ms interval.
        assert len(ticks) >= 1
        # After stop, the watcher task is cleared.
        assert w._task is None
