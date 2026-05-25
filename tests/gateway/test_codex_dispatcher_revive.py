"""Tests for the dispatcher's /revive slash command (P5)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.codex_session_dispatcher import (
    CodexSessionDispatcher,
    SlashContext,
)


def _seed(tmp_path: Path, state: str = "ORPHANED"):
    discord_send = AsyncMock()
    broker = MagicMock()
    # broker.allocate is what on_thread_create calls; make it succeed.
    fake_wt = MagicMock()
    fake_wt.path = tmp_path / "wt-new"
    fake_wt.branch = "codex/sid-new/test"
    fake_wt.port = 50000
    broker.allocate.return_value = fake_wt

    disp = CodexSessionDispatcher(
        hermes_home=tmp_path,
        worktree_broker=broker,
        peer_review_orchestrator=None,
        merge_broker=None,
        discord_send=discord_send,
        kanban_complete=None,
    )

    isa_dir = tmp_path / "work" / "test"
    isa_dir.mkdir(parents=True)
    isa_path = isa_dir / "ISA.md"
    isa_path.write_text(
        "---\nphase: execute\nprogress: 1/5\n---\n## Problem\nbody",
        encoding="utf-8",
    )

    sessions = disp._load_state()
    sessions["sessions"]["t1"] = {
        "session_id": "sid-old",
        "thread_id": "t1",
        "channel_id": "c1",
        "kanban_card_id": None,
        "worktree_path": str(tmp_path / "wt-old"),
        "tmux_session": None,
        "isa_id": "test",
        "isa_path": str(isa_path),
        "state": state,
        "paused": False,
        "queued_messages": [],
        "last_message_id": None,
        "last_message_at": None,
        "created_at": "2026-05-25T00:00:00+00:00",
        "review_round": 0,
        "port": 50000,
    }
    disp._write_state(sessions)
    return disp, discord_send, broker, isa_path


@pytest.mark.asyncio
async def test_revive_orphaned_archives_isa_and_reallocates(tmp_path):
    disp, discord_send, broker, isa_path = _seed(tmp_path, state="ORPHANED")
    ctx = SlashContext(thread_id="t1", channel_id="c1", options={})
    resp = await disp.slash_command("revive", ctx)
    assert "revived" in resp.content.lower()
    # Archive was created.
    ephem = isa_path.parent / "_ephemeral"
    assert ephem.is_dir()
    archives = list(ephem.glob("orphaned-*.md"))
    assert len(archives) == 1
    # Broker.allocate was called for the new session.
    broker.allocate.assert_called()
    # New row exists with a different sid.
    state = disp._load_state()
    new_row = state["sessions"]["t1"]
    assert new_row["session_id"] != "sid-old"


@pytest.mark.asyncio
async def test_revive_rejects_completed_session(tmp_path):
    disp, discord_send, broker, _ = _seed(tmp_path, state="COMPLETE")
    ctx = SlashContext(thread_id="t1", channel_id="c1", options={})
    resp = await disp.slash_command("revive", ctx)
    assert "COMPLETE" in resp.content
    broker.allocate.assert_not_called()


@pytest.mark.asyncio
async def test_revive_with_no_session_returns_helpful(tmp_path):
    disp, discord_send, broker, _ = _seed(tmp_path, state="ORPHANED")
    ctx = SlashContext(thread_id="totally-unknown", channel_id="c1", options={})
    resp = await disp.slash_command("revive", ctx)
    assert "no prior session" in resp.content.lower()
    broker.allocate.assert_not_called()
