"""Tests for dispatcher.on_pr_merged / on_pr_closed_unmerged (P3.5)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.codex_session_dispatcher import CodexSessionDispatcher


def _make_dispatcher(
    tmp_path: Path,
    *,
    kanban_complete=None,
    discord_archive_thread=None,
):
    broker = MagicMock()
    broker.release.return_value = None
    discord_send = AsyncMock()
    disp = CodexSessionDispatcher(
        hermes_home=tmp_path,
        worktree_broker=broker,
        peer_review_orchestrator=MagicMock(),
        merge_broker=MagicMock(),
        discord_send=discord_send,
        kanban_complete=kanban_complete,
        discord_archive_thread=discord_archive_thread,
    )
    return disp, broker, discord_send


def _seed_merging_row(disp: CodexSessionDispatcher, thread_id: str = "t1") -> dict:
    state = disp._load_state()
    row = {
        "session_id": "sid-abc",
        "thread_id": thread_id,
        "channel_id": "c1",
        "kanban_card_id": "card-9",
        "worktree_path": "/tmp/wt",
        "isa_id": "task",
        "isa_path": "/tmp/ISA.md",
        "state": "MERGING",
        "paused": False,
        "queued_messages": [],
        "created_at": "2026-05-25T00:00:00+00:00",
        "review_round": 0,
        "port": 50000,
        "pr_number": 42,
        "pr_url": "https://example/pr/42",
        "head_branch": "codex/sid-abc/task",
        "merge_label": "auto-merge",
        "merge_requested_at": "2026-05-25T00:30:00+00:00",
        "pr_state": "OPEN",
    }
    state["sessions"][thread_id] = row
    disp._write_state(state)
    return row


_PAYLOAD_MERGED = {
    "state": "MERGED",
    "mergedAt": "2026-05-25T01:00:00Z",
    "mergeCommit": {"oid": "deadbeef"},
    "url": "https://example/pr/42",
    "number": 42,
}

_PAYLOAD_CLOSED = {
    "state": "CLOSED",
    "mergedAt": None,
    "closedAt": "2026-05-25T01:00:00Z",
    "url": "https://example/pr/42",
    "number": 42,
}


class TestOnPrMerged:
    @pytest.mark.asyncio
    async def test_state_write_before_side_effects(self, tmp_path):
        """ISA D-2: state must be COMPLETE before any side-effect runs."""
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        # Make worktree release raise so we can confirm the state was
        # committed BEFORE the failure (no rollback).
        broker.release.side_effect = RuntimeError("disk gone")
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        row = disp._load_state()["sessions"]["t1"]
        assert row["state"] == "COMPLETE"
        assert row["pr_state"] == "MERGED"
        assert row["merge_commit_oid"] == "deadbeef"
        assert row["merged_at"] == "2026-05-25T01:00:00Z"

    @pytest.mark.asyncio
    async def test_calls_worktree_release(self, tmp_path):
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        broker.release.assert_called_once_with("sid-abc")

    @pytest.mark.asyncio
    async def test_calls_kanban_complete_when_card_present(self, tmp_path):
        kb = MagicMock()
        disp, broker, discord_send = _make_dispatcher(tmp_path, kanban_complete=kb)
        _seed_merging_row(disp)
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        kb.assert_called_once_with("card-9")

    @pytest.mark.asyncio
    async def test_calls_discord_archive_when_injected(self, tmp_path):
        archive = AsyncMock()
        disp, broker, discord_send = _make_dispatcher(
            tmp_path, discord_archive_thread=archive,
        )
        _seed_merging_row(disp)
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        archive.assert_awaited_once_with("t1")

    @pytest.mark.asyncio
    async def test_posts_closeout_message(self, tmp_path):
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        discord_send.assert_awaited()
        body = discord_send.await_args.args[1]
        assert "PR #42" in body
        assert "merged" in body.lower()

    @pytest.mark.asyncio
    async def test_idempotent_on_already_complete(self, tmp_path):
        """Re-invocation on a COMPLETE row is a no-op (no double release)."""
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        assert broker.release.call_count == 1
        discord_send.reset_mock()
        broker.release.reset_mock()
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        broker.release.assert_not_called()
        discord_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_worktree_does_not_block_completion(self, tmp_path):
        """If worktree release raises, state must still be COMPLETE."""
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        broker.release.side_effect = FileNotFoundError("gone")
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        row = disp._load_state()["sessions"]["t1"]
        assert row["state"] == "COMPLETE"
        # Closeout message still posted.
        discord_send.assert_awaited()

    @pytest.mark.asyncio
    async def test_missing_row_is_noop(self, tmp_path):
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        # No row seeded -> nothing to finalize.
        await disp.on_pr_merged("nonexistent", _PAYLOAD_MERGED)
        broker.release.assert_not_called()
        discord_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kanban_failure_does_not_skip_archive(self, tmp_path):
        """Each side effect runs in its own try/except — one failure
        must not cascade."""
        archive = AsyncMock()
        kb = MagicMock(side_effect=RuntimeError("kanban down"))
        disp, broker, discord_send = _make_dispatcher(
            tmp_path, kanban_complete=kb, discord_archive_thread=archive,
        )
        _seed_merging_row(disp)
        await disp.on_pr_merged("t1", _PAYLOAD_MERGED)
        archive.assert_awaited_once()
        discord_send.assert_awaited()


class TestOnPrClosedUnmerged:
    @pytest.mark.asyncio
    async def test_marks_escalated(self, tmp_path):
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        await disp.on_pr_closed_unmerged("t1", _PAYLOAD_CLOSED)
        row = disp._load_state()["sessions"]["t1"]
        assert row["state"] == "ESCALATED"
        assert row["pr_state"] == "CLOSED"
        assert row["closed_at"] == "2026-05-25T01:00:00Z"

    @pytest.mark.asyncio
    async def test_does_not_release_worktree(self, tmp_path):
        """ISA D-3: closed-unmerged keeps worktree for operator inspection."""
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        await disp.on_pr_closed_unmerged("t1", _PAYLOAD_CLOSED)
        broker.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_pings_operator(self, tmp_path):
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        await disp.on_pr_closed_unmerged("t1", _PAYLOAD_CLOSED)
        body = discord_send.await_args.args[1]
        assert "OPERATOR" in body
        assert "PR #42" in body

    @pytest.mark.asyncio
    async def test_idempotent_on_already_escalated(self, tmp_path):
        disp, broker, discord_send = _make_dispatcher(tmp_path)
        _seed_merging_row(disp)
        await disp.on_pr_closed_unmerged("t1", _PAYLOAD_CLOSED)
        discord_send.reset_mock()
        await disp.on_pr_closed_unmerged("t1", _PAYLOAD_CLOSED)
        discord_send.assert_not_awaited()
