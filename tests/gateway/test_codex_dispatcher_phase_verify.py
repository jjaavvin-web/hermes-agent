"""Tests for the dispatcher's on_phase_verify + verdict handlers (P2.5)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.peer_review import Verdict
from gateway.codex_session_dispatcher import (
    CodexSessionDispatcher,
    SlashContext,
)


def _seed_row(tmp_path: Path) -> tuple[CodexSessionDispatcher, dict, AsyncMock]:
    """Build a dispatcher with a single session row pre-seeded."""
    discord_send = AsyncMock()
    broker = MagicMock()

    peer_review = MagicMock()
    peer_review.start = AsyncMock()
    peer_review.review = AsyncMock()

    disp = CodexSessionDispatcher(
        hermes_home=tmp_path,
        worktree_broker=broker,
        peer_review_orchestrator=peer_review,
        merge_broker=None,
        discord_send=discord_send,
        kanban_complete=None,
    )

    state = disp._load_state()
    state["sessions"]["t1"] = {
        "session_id": "sid-abc",
        "thread_id": "t1",
        "channel_id": "c1",
        "kanban_card_id": "card-9",
        "worktree_path": str(tmp_path / "wt"),
        "tmux_session": None,
        "isa_id": "test",
        "isa_path": str(tmp_path / "ISA.md"),
        "state": "EXECUTING",
        "paused": False,
        "queued_messages": [],
        "last_message_id": None,
        "last_message_at": None,
        "created_at": "2026-05-25T00:00:00+00:00",
        "review_round": 0,
        "port": 50000,
        "isa_phase": "execute",
    }
    disp._write_state(state)
    # Pre-create the worktree path + ISA file so _collect_diff doesn't fail.
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ISA.md").write_text(
        "---\nisa: x\ntask: y\ntier: E2\nphase: verify\nprogress: 1/1\n---\n\n## Problem\n\nx\n",
        encoding="utf-8",
    )
    return disp, state["sessions"]["t1"], discord_send


@pytest.mark.asyncio
async def test_approve_marks_merging_and_posts_discord(tmp_path):
    disp, row, discord_send = _seed_row(tmp_path)
    disp._peer_review.review.return_value = Verdict(
        kind="APPROVE", rationale="looks great", iteration=0,
        raw_capture="VERDICT: APPROVE\nlooks great",
        duration_sec=12.5, pane_id="codex-review-0",
    )
    await disp.on_phase_verify("t1")

    persisted = disp._load_state()["sessions"]["t1"]
    assert persisted["state"] == "MERGING"
    discord_send.assert_awaited()
    args, _ = discord_send.await_args
    assert "APPROVE" in args[1]
    assert "Ready to merge" in args[1]


@pytest.mark.asyncio
async def test_revise_marks_executing_and_kanban_comments(tmp_path, monkeypatch):
    disp, row, discord_send = _seed_row(tmp_path)
    disp._peer_review.review.return_value = Verdict(
        kind="REVISE", rationale="needs better input validation on line 42",
        iteration=1,
        raw_capture="VERDICT: REVISE\nneeds better input validation on line 42",
        duration_sec=8.0, pane_id="codex-review-1",
    )
    # Mock the kanban side effect — we just want to know it was attempted.
    kb_mock = MagicMock()
    kb_conn_mock = MagicMock()
    monkeypatch.setattr(
        "tools.kanban_tools._connect",
        lambda: (kb_mock, kb_conn_mock),
        raising=False,
    )

    await disp.on_phase_verify("t1")

    persisted = disp._load_state()["sessions"]["t1"]
    assert persisted["state"] == "EXECUTING"
    kb_mock.add_comment.assert_called_once()
    call_kwargs = kb_mock.add_comment.call_args.kwargs
    assert call_kwargs.get("author") == "peer-review-opus"
    assert "REVISE" in call_kwargs.get("body", "")
    discord_send.assert_awaited()


@pytest.mark.asyncio
async def test_escalate_marks_escalated_and_pings_operator(tmp_path):
    disp, row, discord_send = _seed_row(tmp_path)
    disp._peer_review.review.return_value = Verdict(
        kind="ESCALATE", rationale="cap reached", iteration=3,
        raw_capture="VERDICT: ESCALATE\ncap reached",
        duration_sec=0.0, pane_id="(no pane)",
    )
    await disp.on_phase_verify("t1")
    persisted = disp._load_state()["sessions"]["t1"]
    assert persisted["state"] == "ESCALATED"
    args, _ = discord_send.await_args
    assert "ESCALATE" in args[1]


@pytest.mark.asyncio
async def test_revise_appends_isa_decision(tmp_path):
    disp, row, discord_send = _seed_row(tmp_path)
    disp._peer_review.review.return_value = Verdict(
        kind="REVISE", rationale="add tests for the new path",
        iteration=2,
        raw_capture="...",
        duration_sec=4.0, pane_id="codex-review-0",
    )
    await disp.on_phase_verify("t1")

    isa_text = (tmp_path / "ISA.md").read_text(encoding="utf-8")
    assert "Peer review 2" in isa_text
    assert "REVISE" in isa_text
    assert "add tests for the new path" in isa_text


@pytest.mark.asyncio
async def test_on_phase_verify_starts_orchestrator_once(tmp_path):
    """Orchestrator.start() is called lazily on first review, not twice."""
    disp, row, discord_send = _seed_row(tmp_path)
    disp._peer_review.review.return_value = Verdict(
        kind="APPROVE", rationale="ok", iteration=0,
        raw_capture="VERDICT: APPROVE\nok",
        duration_sec=1.0, pane_id="codex-review-0",
    )
    await disp.on_phase_verify("t1")
    # Reset state for a second call.
    state = disp._load_state()
    state["sessions"]["t1"]["state"] = "EXECUTING"
    disp._write_state(state)
    await disp.on_phase_verify("t1")

    # start() should have been awaited exactly once across both calls.
    assert disp._peer_review.start.await_count == 1


@pytest.mark.asyncio
async def test_review_slash_command_invokes_on_phase_verify(tmp_path):
    disp, row, discord_send = _seed_row(tmp_path)
    disp._peer_review.review.return_value = Verdict(
        kind="APPROVE", rationale="ok", iteration=0,
        raw_capture="VERDICT: APPROVE\nok",
        duration_sec=1.0, pane_id="codex-review-0",
    )
    ctx = SlashContext(thread_id="t1", channel_id="c1", options={})
    resp = await disp.slash_command("review", ctx)
    assert "dispatched" in resp.content.lower()
    # Orchestrator was called.
    disp._peer_review.review.assert_awaited()


@pytest.mark.asyncio
async def test_review_slash_command_rejects_untracked_thread(tmp_path):
    disp, row, discord_send = _seed_row(tmp_path)
    ctx = SlashContext(thread_id="not-a-tracked-thread", channel_id="c1", options={})
    resp = await disp.slash_command("review", ctx)
    assert "no active session" in resp.content.lower()
    disp._peer_review.review.assert_not_awaited()
