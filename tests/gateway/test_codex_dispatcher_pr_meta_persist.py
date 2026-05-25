"""Tests for _apply_verdict APPROVE persisting PR meta on the row (P3.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.peer_review import Verdict
from gateway.codex_session_dispatcher import CodexSessionDispatcher


def _seed_dispatcher(tmp_path: Path):
    discord_send = AsyncMock()
    broker = MagicMock()

    peer_review = MagicMock()
    peer_review.start = AsyncMock()
    peer_review.review = AsyncMock()

    # MergeBroker stub returning a successful MergeResult.
    merge_broker = MagicMock()
    async def fake_merge(**kw):
        from agent.merge_broker import MergeResult
        return MergeResult(
            ok=True,
            pr_number=99,
            pr_url="https://example/pr/99",
            classification="auto-merge",
            duration_sec=1.2,
        )
    merge_broker.merge = fake_merge

    disp = CodexSessionDispatcher(
        hermes_home=tmp_path,
        worktree_broker=broker,
        peer_review_orchestrator=peer_review,
        merge_broker=merge_broker,
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
        "isa_id": "demo",
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
    (tmp_path / "wt").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ISA.md").write_text(
        "---\nisa: x\ntask: y\ntier: E2\nphase: verify\nprogress: 1/1\n---\n\n## Problem\n\nx\n",
        encoding="utf-8",
    )
    return disp, discord_send


@pytest.mark.asyncio
async def test_approve_persists_pr_meta_on_row(tmp_path):
    disp, discord_send = _seed_dispatcher(tmp_path)
    disp._peer_review.review.return_value = Verdict(
        kind="APPROVE", rationale="looks great", iteration=0,
        raw_capture="VERDICT: APPROVE", duration_sec=1.0, pane_id="p",
    )
    await disp.on_phase_verify("t1")
    row = disp._load_state()["sessions"]["t1"]
    assert row["state"] == "MERGING"
    assert row["pr_number"] == 99
    assert row["pr_url"] == "https://example/pr/99"
    assert row["head_branch"] == "codex/sid-abc/demo"
    assert row["merge_label"] == "auto-merge"
    assert row["pr_state"] == "OPEN"
    assert "merge_requested_at" in row


@pytest.mark.asyncio
async def test_approve_with_failing_merge_broker_does_not_set_pr_meta(tmp_path):
    """If MergeBroker returns ok=False, do NOT persist PR meta — the row
    stays at MERGING with no PR number, which the merge watcher skips."""
    disp, discord_send = _seed_dispatcher(tmp_path)
    async def failing_merge(**kw):
        from agent.merge_broker import MergeResult
        return MergeResult(ok=False, error="rebase failed", duration_sec=0.5)
    disp._merge_broker.merge = failing_merge
    disp._peer_review.review.return_value = Verdict(
        kind="APPROVE", rationale="looks great", iteration=0,
        raw_capture="VERDICT: APPROVE", duration_sec=1.0, pane_id="p",
    )
    await disp.on_phase_verify("t1")
    row = disp._load_state()["sessions"]["t1"]
    assert row["state"] == "MERGING"  # still MERGING, operator triage
    assert "pr_number" not in row
    assert "pr_url" not in row
