"""Tests for gateway.codex_session_events (P4 SSE)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gateway.codex_session_events import (
    codex_session_events_iter,
    _diff_changes,
    _row_snapshot,
    _WATCHED_FIELDS,
)


def _write(p: Path, sessions: dict) -> None:
    p.write_text(json.dumps({"version": 1, "sessions": sessions}), encoding="utf-8")


def _row(state: str = "EXECUTING", **extra) -> dict:
    row = {
        "session_id": extra.pop("sid", "sid-abc"),
        "thread_id": "t1",
        "state": state,
        "isa_phase": "execute",
    }
    row.update(extra)
    return row


class TestSnapshotAndDiff:
    def test_snapshot_only_watched_fields(self):
        row = {"state": "EXECUTING", "isa_phase": "execute",
               "session_id": "x", "last_message_at": "ignore me"}
        snap = _row_snapshot(row)
        assert set(snap.keys()) == set(_WATCHED_FIELDS)
        assert snap["state"] == "EXECUTING"
        assert "last_message_at" not in snap

    def test_diff_empty_when_unchanged(self):
        prev = {k: "v" for k in _WATCHED_FIELDS}
        cur = dict(prev)
        assert _diff_changes(prev, cur) == {}

    def test_diff_captures_state_transition(self):
        prev = _row_snapshot({"state": "EXECUTING", "isa_phase": "execute"})
        cur = _row_snapshot({"state": "MERGING", "isa_phase": "verify"})
        d = _diff_changes(prev, cur)
        assert d["state"] == {"from": "EXECUTING", "to": "MERGING"}
        assert d["isa_phase"] == {"from": "execute", "to": "verify"}


class TestIterator:
    @pytest.mark.asyncio
    async def test_appeared_event_on_first_observation(self, tmp_path):
        p = tmp_path / "codex_sessions.json"
        _write(p, {"t1": _row("CLAIMED", sid="sid-aaa")})
        stop = asyncio.Event()
        gen = codex_session_events_iter(p, stop_event=stop, poll_interval_sec=0.05)
        events: list[dict] = []
        async def consume():
            async for ev in gen:
                events.append(ev)
                if len(events) >= 1:
                    stop.set()
                    return
        await asyncio.wait_for(consume(), timeout=2.0)
        assert events[0]["type"] == "appeared"
        assert events[0]["thread_id"] == "t1"
        assert events[0]["sid"] == "sid-aaa"
        assert events[0]["state"] == "CLAIMED"
        assert events[0]["kind"] == "codex-session"

    @pytest.mark.asyncio
    async def test_changed_event_on_state_transition(self, tmp_path):
        p = tmp_path / "codex_sessions.json"
        _write(p, {"t1": _row("EXECUTING", sid="sid-aaa")})
        stop = asyncio.Event()
        gen = codex_session_events_iter(p, stop_event=stop, poll_interval_sec=0.05)
        events: list[dict] = []
        async def consume():
            async for ev in gen:
                events.append(ev)
                if len(events) >= 2:
                    stop.set()
                    return
        async def mutate():
            await asyncio.sleep(0.15)  # let initial 'appeared' fire
            _write(p, {"t1": _row("MERGING", sid="sid-aaa")})
        await asyncio.wait_for(asyncio.gather(consume(), mutate()), timeout=3.0)
        kinds = [e["type"] for e in events]
        assert "appeared" in kinds
        assert "changed" in kinds
        changed = next(e for e in events if e["type"] == "changed")
        assert changed["changes"]["state"] == {"from": "EXECUTING", "to": "MERGING"}

    @pytest.mark.asyncio
    async def test_removed_event_on_row_deletion(self, tmp_path):
        p = tmp_path / "codex_sessions.json"
        _write(p, {"t1": _row("EXECUTING", sid="sid-aaa")})
        stop = asyncio.Event()
        gen = codex_session_events_iter(p, stop_event=stop, poll_interval_sec=0.05)
        events: list[dict] = []
        async def consume():
            async for ev in gen:
                events.append(ev)
                if len(events) >= 2:
                    stop.set()
                    return
        async def mutate():
            await asyncio.sleep(0.15)
            _write(p, {})  # remove all rows
        await asyncio.wait_for(asyncio.gather(consume(), mutate()), timeout=3.0)
        removed = next(e for e in events if e["type"] == "removed")
        assert removed["thread_id"] == "t1"
        assert removed["last_state"] == "EXECUTING"

    @pytest.mark.asyncio
    async def test_no_event_on_unwatched_field_change(self, tmp_path):
        """Changing last_message_at (not in WATCHED_FIELDS) must not emit."""
        p = tmp_path / "codex_sessions.json"
        _write(p, {"t1": _row("EXECUTING", sid="sid-aaa", last_message_at="t0")})
        stop = asyncio.Event()
        gen = codex_session_events_iter(p, stop_event=stop, poll_interval_sec=0.05)
        events: list[dict] = []
        async def consume():
            try:
                async for ev in gen:
                    events.append(ev)
            except asyncio.CancelledError:
                pass
        async def mutate_and_stop():
            await asyncio.sleep(0.15)  # initial appeared
            _write(p, {"t1": _row("EXECUTING", sid="sid-aaa", last_message_at="t1")})
            await asyncio.sleep(0.25)  # give the poller time to see + diff
            stop.set()
        await asyncio.wait_for(asyncio.gather(consume(), mutate_and_stop()), timeout=3.0)
        change_events = [e for e in events if e["type"] == "changed"]
        assert change_events == []  # only the unwatched field changed

    @pytest.mark.asyncio
    async def test_missing_file_does_not_crash(self, tmp_path):
        """File-not-found must keep the loop alive."""
        p = tmp_path / "absent.json"
        stop = asyncio.Event()
        gen = codex_session_events_iter(p, stop_event=stop, poll_interval_sec=0.05)
        async def runner():
            await asyncio.sleep(0.2)
            stop.set()
        async def consume():
            try:
                async for _ in gen:
                    pass
            except asyncio.CancelledError:
                pass
        await asyncio.wait_for(asyncio.gather(consume(), runner()), timeout=2.0)

    @pytest.mark.asyncio
    async def test_malformed_json_does_not_crash(self, tmp_path):
        p = tmp_path / "codex_sessions.json"
        p.write_text("{not valid json", encoding="utf-8")
        stop = asyncio.Event()
        gen = codex_session_events_iter(p, stop_event=stop, poll_interval_sec=0.05)
        async def runner():
            await asyncio.sleep(0.2)
            stop.set()
        async def consume():
            try:
                async for _ in gen:
                    pass
            except asyncio.CancelledError:
                pass
        await asyncio.wait_for(asyncio.gather(consume(), runner()), timeout=2.0)

    @pytest.mark.asyncio
    async def test_pr_meta_change_surfaces(self, tmp_path):
        """When MergeBroker persists pr_number/pr_url/pr_state, a changed event fires."""
        p = tmp_path / "codex_sessions.json"
        _write(p, {"t1": _row("EXECUTING", sid="sid-aaa")})
        stop = asyncio.Event()
        gen = codex_session_events_iter(p, stop_event=stop, poll_interval_sec=0.05)
        events: list[dict] = []
        async def consume():
            async for ev in gen:
                events.append(ev)
                if len(events) >= 2:
                    stop.set()
                    return
        async def mutate():
            await asyncio.sleep(0.15)
            _write(p, {"t1": _row("MERGING", sid="sid-aaa",
                       pr_number=99, pr_url="https://example/pr/99",
                       pr_state="OPEN")})
        await asyncio.wait_for(asyncio.gather(consume(), mutate()), timeout=3.0)
        changed = next(e for e in events if e["type"] == "changed")
        assert "pr_number" in changed["changes"]
        assert changed["changes"]["pr_number"] == {"from": None, "to": 99}
        assert "pr_state" in changed["changes"]
