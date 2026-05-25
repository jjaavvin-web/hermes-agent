"""Tests for gateway.codex_phase_watcher (P2.5)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from gateway.codex_phase_watcher import CodexPhaseWatcher, _read_phase


def _write_isa(path: Path, phase: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n"
        f"isa: test-isa\n"
        f"task: test\n"
        f"tier: E2\n"
        f"phase: {phase}\n"
        f"progress: 0/1\n"
        f"---\n\n"
        f"## Problem\n\nNone\n",
        encoding="utf-8",
    )


class _FakeDispatcher:
    def __init__(self, hermes_home: Path, rows: dict) -> None:
        self._sessions_path = hermes_home / "codex_sessions.json"
        self._state = {"version": 1, "sessions": rows}
        self._write_state(self._state)

    def _load_state(self) -> dict:
        return json.loads(self._sessions_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        self._sessions_path.write_text(json.dumps(state), encoding="utf-8")


class TestReadPhase:
    def test_missing_file_returns_none(self, tmp_path):
        assert _read_phase(tmp_path / "nope.md") is None

    def test_reads_phase_from_frontmatter(self, tmp_path):
        isa = tmp_path / "isa.md"
        _write_isa(isa, "execute")
        assert _read_phase(isa) == "execute"

    def test_returns_none_for_file_without_frontmatter(self, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text("just a body, no frontmatter")
        assert _read_phase(f) is None


class TestWatcherPolling:
    @pytest.mark.asyncio
    async def test_fires_on_transition_into_verify(self, tmp_path):
        isa = tmp_path / "isa.md"
        _write_isa(isa, "execute")
        rows = {"t1": {
            "session_id": "sid-1",
            "isa_path": str(isa),
            "isa_phase": "execute",
            "worktree_path": str(tmp_path),
            "state": "EXECUTING",
        }}
        disp = _FakeDispatcher(tmp_path, rows)

        fired: list[str] = []
        async def on_verify(thread_id: str) -> None:
            fired.append(thread_id)

        watcher = CodexPhaseWatcher(
            dispatcher=disp,
            on_phase_verify=on_verify,
            poll_interval_sec=0.05,
        )
        await watcher.start()
        # Initial tick: still at execute, no fire.
        await asyncio.sleep(0.1)
        assert fired == []
        # Flip to verify.
        _write_isa(isa, "verify")
        await asyncio.sleep(0.15)
        await watcher.stop()
        assert fired == ["t1"]

    @pytest.mark.asyncio
    async def test_no_fire_on_repeated_verify_within_same_run(self, tmp_path):
        isa = tmp_path / "isa.md"
        _write_isa(isa, "verify")
        rows = {"t1": {
            "session_id": "sid-1",
            "isa_path": str(isa),
            "isa_phase": "execute",  # last-seen was execute, now seeing verify
            "worktree_path": str(tmp_path),
            "state": "EXECUTING",
        }}
        disp = _FakeDispatcher(tmp_path, rows)

        fires = 0
        async def on_verify(thread_id: str) -> None:
            nonlocal fires
            fires += 1

        watcher = CodexPhaseWatcher(
            dispatcher=disp,
            on_phase_verify=on_verify,
            poll_interval_sec=0.05,
        )
        await watcher.start()
        await asyncio.sleep(0.2)
        await watcher.stop()
        # Should fire exactly once on first transition execute -> verify.
        assert fires == 1

    @pytest.mark.asyncio
    async def test_rehydrate_from_row_avoids_double_fire(self, tmp_path):
        """If a session was already at verify last run, don't re-fire on restart."""
        isa = tmp_path / "isa.md"
        _write_isa(isa, "verify")
        rows = {"t1": {
            "session_id": "sid-1",
            "isa_path": str(isa),
            "isa_phase": "verify",  # row says we already acted on this
            "worktree_path": str(tmp_path),
            "state": "EXECUTING",
        }}
        disp = _FakeDispatcher(tmp_path, rows)

        fires = 0
        async def on_verify(thread_id: str) -> None:
            nonlocal fires
            fires += 1

        watcher = CodexPhaseWatcher(
            dispatcher=disp,
            on_phase_verify=on_verify,
            poll_interval_sec=0.05,
        )
        await watcher.start()
        await asyncio.sleep(0.15)
        await watcher.stop()
        # Rehydrated _last_phase = verify, current = verify → no transition.
        assert fires == 0

    @pytest.mark.asyncio
    async def test_persists_phase_to_row(self, tmp_path):
        isa = tmp_path / "isa.md"
        _write_isa(isa, "execute")
        rows = {"t1": {
            "session_id": "sid-1",
            "isa_path": str(isa),
            "isa_phase": "execute",
            "worktree_path": str(tmp_path),
            "state": "EXECUTING",
        }}
        disp = _FakeDispatcher(tmp_path, rows)

        async def on_verify(thread_id: str) -> None:
            return None

        watcher = CodexPhaseWatcher(
            dispatcher=disp,
            on_phase_verify=on_verify,
            poll_interval_sec=0.05,
        )
        await watcher.start()
        _write_isa(isa, "verify")
        await asyncio.sleep(0.15)
        await watcher.stop()

        persisted = disp._load_state()["sessions"]["t1"]["isa_phase"]
        assert persisted == "verify"
