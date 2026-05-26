"""Tests for P1.4: dispatcher.discover_threads + silent on_thread_create."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.codex_session_dispatcher import (
    CodexSessionDispatcher,
    ThreadEvent,
)


def _make_dispatcher(tmp_path):
    broker = MagicMock()
    # broker.allocate returns a worktree-like object with .path/.port/.branch
    def _alloc(sid, *, isa_slug, base_branch):
        wt = MagicMock()
        wt.path = tmp_path / "codex-wt" / sid
        wt.path.mkdir(parents=True, exist_ok=True)
        wt.port = 50000
        wt.branch = f"codex/{sid}/{isa_slug}"
        return wt
    broker.allocate.side_effect = _alloc
    broker.release.return_value = None

    discord_send = AsyncMock()
    dispatcher = CodexSessionDispatcher(
        hermes_home=tmp_path,
        worktree_broker=broker,
        peer_review_orchestrator=MagicMock(),
        merge_broker=MagicMock(),
        discord_send=discord_send,
        kanban_complete=None,
    )
    return dispatcher, broker, discord_send


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSilentOnThreadCreate:
    def test_silent_true_skips_discord_banner(self, tmp_path):
        d, broker, discord_send = _make_dispatcher(tmp_path)
        event = ThreadEvent(thread_id="t-silent", channel_id="c1", isa_slug="my-task")
        _run(d.on_thread_create(event, silent=True))

        # Row created (allocation happened).
        state = d._load_state()
        assert "t-silent" in state["sessions"]
        broker.allocate.assert_called_once()
        # But discord_send was NOT awaited (silent=True).
        discord_send.assert_not_awaited()

    def test_silent_false_sends_banner(self, tmp_path):
        d, broker, discord_send = _make_dispatcher(tmp_path)
        event = ThreadEvent(thread_id="t-loud", channel_id="c1", isa_slug="x")
        _run(d.on_thread_create(event))  # default silent=False

        discord_send.assert_awaited_once()


class TestDiscoverThreads:
    def test_discover_allocates_only_untracked(self, tmp_path):
        d, broker, discord_send = _make_dispatcher(tmp_path)
        # Pre-seed one tracked row.
        state = d._load_state()
        state["sessions"]["already-here"] = {
            "session_id": "sid-existing", "thread_id": "already-here",
            "channel_id": "c1", "kanban_card_id": None, "worktree_path": "",
            "tmux_session": None, "isa_id": "existing", "isa_path": "",
            "state": "EXECUTING", "paused": False, "queued_messages": [],
            "last_message_id": None, "last_message_at": None,
            "created_at": "2026-05-25T00:00:00+00:00", "review_round": 0,
            "port": 50001,
        }
        d._write_state(state)

        results = _run(d.discover_threads([
            ("already-here", "c1", "Existing"),       # skip
            ("new-1", "c1", "Obsidian"),              # discover
            ("new-2", "c1", "HTML"),                  # discover
        ]))

        # Two new sessions discovered, one pre-existing skipped.
        assert len(results) == 2
        discovered_ids = {r.thread_id for r in results}
        assert discovered_ids == {"new-1", "new-2"}
        for r in results:
            assert r.status == "discovered"

        final = d._load_state()["sessions"]
        assert set(final.keys()) == {"already-here", "new-1", "new-2"}

        # Silent: NO discord_send for the discovered threads.
        discord_send.assert_not_awaited()

    def test_discover_empty_input_is_noop(self, tmp_path):
        d, broker, discord_send = _make_dispatcher(tmp_path)
        results = _run(d.discover_threads([]))
        assert results == []
        broker.allocate.assert_not_called()

    def test_discover_skips_empty_thread_ids(self, tmp_path):
        d, broker, discord_send = _make_dispatcher(tmp_path)
        results = _run(d.discover_threads([
            ("", "c1", "ignored"),
            ("real", "c1", "Cron"),
        ]))
        assert len(results) == 1
        assert results[0].thread_id == "real"

    def test_discover_uses_thread_name_as_isa_slug(self, tmp_path):
        d, broker, discord_send = _make_dispatcher(tmp_path)
        _run(d.discover_threads([("t-slug", "c1", "NOTIONFORMAT")]))
        # broker.allocate called with isa_slug derived from "NOTIONFORMAT"
        broker.allocate.assert_called_once()
        kw = broker.allocate.call_args.kwargs
        # slugify_ref lowercases.
        assert kw["isa_slug"] == "notionformat"

    def test_discover_per_thread_error_does_not_abort_loop(self, tmp_path):
        d, broker, discord_send = _make_dispatcher(tmp_path)
        # First allocate raises; second succeeds.
        original = broker.allocate.side_effect
        calls = [0]
        def _raise_then_succeed(sid, *, isa_slug, base_branch):
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("disk full")
            return original(sid, isa_slug=isa_slug, base_branch=base_branch)
        broker.allocate.side_effect = _raise_then_succeed

        results = _run(d.discover_threads([
            ("bad", "c1", "Bad"),
            ("good", "c1", "Good"),
        ]))
        # First failed, second succeeded.
        assert len(results) == 1
        assert results[0].thread_id == "good"

    def test_discover_returns_status_discovered(self, tmp_path):
        d, broker, discord_send = _make_dispatcher(tmp_path)
        results = _run(d.discover_threads([("only-one", "c1", "X")]))
        assert results[0].status == "discovered"
