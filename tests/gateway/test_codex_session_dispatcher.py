"""Unit tests for CodexSessionDispatcher.

Covers spec §10 "Unit tests" subsection (~12 bullets).
All subprocess/tmux calls are patched; WorktreeBroker is mocked; discord_send is AsyncMock.
Integration tests (real tmux) are excluded per task scope.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_dispatcher(tmp_path, *, kanban_complete=None):
    """Build a CodexSessionDispatcher with mocked dependencies."""
    from gateway.codex_session_dispatcher import CodexSessionDispatcher

    broker = MagicMock()
    # allocate returns a mock Worktree-like object
    wt = MagicMock()
    wt.path = tmp_path / "codex-wt" / "test-session"
    wt.port = 50000
    broker.allocate.return_value = wt
    broker.release.return_value = None

    discord_send = AsyncMock()
    dispatcher = CodexSessionDispatcher(
        hermes_home=tmp_path,
        worktree_broker=broker,
        peer_review_orchestrator=MagicMock(),
        merge_broker=MagicMock(),
        discord_send=discord_send,
        kanban_complete=kanban_complete,
    )
    return dispatcher, broker, discord_send


def _make_event(thread_id="tid-1", channel_id="ch-1", message_id="msg-1", text="hello"):
    from gateway.codex_session_dispatcher import ThreadEvent
    return ThreadEvent(
        thread_id=thread_id,
        channel_id=channel_id,
        message_id=message_id,
        text=text,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Patch helper: tmux new-session succeeds ───────────────────────────────────

def _mock_tmux_ok():
    """Return a subprocess.run mock that makes tmux new-session succeed."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


def _mock_tmux_fail():
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = "tmux: error"
    return result


# ── Tests: on_thread_create ───────────────────────────────────────────────────


class TestOnThreadCreate:
    def test_new_thread_writes_one_row_and_calls_allocate_once(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        event = _make_event(thread_id="t1")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))

        broker.allocate.assert_called_once()
        state = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert "t1" in state["sessions"]
        assert len(state["sessions"]) == 1

    def test_duplicate_thread_id_is_noop(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        event = _make_event(thread_id="t1")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))
            _run(dispatcher.on_thread_create(event))

        # allocate called only once; only one row
        broker.allocate.assert_called_once()
        state = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert len(state["sessions"]) == 1

    def test_allocation_failure_posts_banner_and_raises(self, tmp_path):
        from gateway.codex_session_dispatcher import WorktreeAllocationError

        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        broker.allocate.side_effect = RuntimeError("disk full")
        event = _make_event(thread_id="t2")
        with pytest.raises(WorktreeAllocationError):
            _run(dispatcher.on_thread_create(event))

        discord_send.assert_awaited_once()
        sent_text = discord_send.await_args[0][1]
        assert "disk full" in sent_text

    def test_tmux_failure_releases_worktree_and_posts_banner(self, tmp_path):
        from gateway.codex_session_dispatcher import TmuxLaunchError

        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        event = _make_event(thread_id="t3")
        with patch("subprocess.run", return_value=_mock_tmux_fail()):
            with pytest.raises(TmuxLaunchError):
                _run(dispatcher.on_thread_create(event))

        broker.release.assert_called_once()
        discord_send.assert_awaited_once()
        assert "tmux launch failed" in discord_send.await_args[0][1]


# ── Tests: on_thread_message ──────────────────────────────────────────────────


class TestOnThreadMessage:
    def _create_session(self, dispatcher, thread_id="t1"):
        event = _make_event(thread_id=thread_id)
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))

    def test_nontracked_thread_raises_session_not_found(self, tmp_path):
        from gateway.codex_session_dispatcher import SessionNotFoundError

        dispatcher, _, _ = _make_dispatcher(tmp_path)
        event = _make_event(thread_id="unknown")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            with pytest.raises(SessionNotFoundError):
                _run(dispatcher.on_thread_message(event))

    def test_paused_session_queues_message_without_tmux_call(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        self._create_session(dispatcher, "t1")

        # Pause via state file
        sessions_path = tmp_path / "codex_sessions.json"
        state = json.loads(sessions_path.read_text())
        state["sessions"]["t1"]["paused"] = True
        sessions_path.write_text(json.dumps(state))

        send_calls_before = 0
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _mock_tmux_ok()
            _run(dispatcher.on_thread_message(_make_event(thread_id="t1", message_id="msg-2")))
            # tmux send-keys should NOT have been called
            tmux_send_calls = [
                c for c in mock_run.call_args_list
                if c.args and "send-keys" in c.args[0]
            ]
            assert len(tmux_send_calls) == 0

        state = json.loads(sessions_path.read_text())
        assert len(state["sessions"]["t1"]["queued_messages"]) == 1

    def test_message_forwarded_when_tmux_alive(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        self._create_session(dispatcher, "t1")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _mock_tmux_ok()
            _run(dispatcher.on_thread_message(_make_event(thread_id="t1", message_id="msg-2")))

            send_key_calls = [
                c for c in mock_run.call_args_list
                if c.args and "send-keys" in c.args[0]
            ]
            assert len(send_key_calls) == 1


# ── Tests: on_thread_archive ──────────────────────────────────────────────────


class TestOnThreadArchive:
    def _create_session(self, dispatcher, thread_id="t1"):
        event = _make_event(thread_id=thread_id)
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))

    def test_archive_executing_kills_tmux_and_leaves_kanban_interrupted(self, tmp_path):
        kanban = MagicMock()
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path, kanban_complete=kanban)
        self._create_session(dispatcher, "t1")

        # Ensure state is EXECUTING
        sessions_path = tmp_path / "codex_sessions.json"
        state = json.loads(sessions_path.read_text())
        state["sessions"]["t1"]["state"] = "EXECUTING"
        state["sessions"]["t1"]["kanban_card_id"] = "card-1"
        sessions_path.write_text(json.dumps(state))

        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_archive(_make_event(thread_id="t1")))

        kanban.assert_not_called()
        state = json.loads(sessions_path.read_text())
        assert "t1" not in state["sessions"]

    def test_archive_complete_calls_kanban_and_removes_row(self, tmp_path):
        kanban = MagicMock()
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path, kanban_complete=kanban)
        self._create_session(dispatcher, "t1")

        sessions_path = tmp_path / "codex_sessions.json"
        state = json.loads(sessions_path.read_text())
        state["sessions"]["t1"]["state"] = "COMPLETE"
        state["sessions"]["t1"]["kanban_card_id"] = "card-42"
        sessions_path.write_text(json.dumps(state))

        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_archive(_make_event(thread_id="t1")))

        kanban.assert_called_once_with("card-42")
        state = json.loads(sessions_path.read_text())
        assert "t1" not in state["sessions"]

    def test_archive_no_row_is_noop(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        _run(dispatcher.on_thread_archive(_make_event(thread_id="nonexistent")))
        broker.release.assert_not_called()


# ── Tests: on_bot_restart ─────────────────────────────────────────────────────


class TestOnBotRestart:
    def _write_session_row(self, tmp_path, thread_id, sid, tmux_name, state_val="EXECUTING"):
        sessions_path = tmp_path / "codex_sessions.json"
        row = {
            "session_id": sid,
            "thread_id": thread_id,
            "channel_id": "ch",
            "kanban_card_id": None,
            "worktree_path": str(tmp_path / "codex-wt" / sid),
            "tmux_session": tmux_name,
            "isa_id": "task",
            "isa_path": str(tmp_path / "ISA.md"),
            "state": state_val,
            "paused": False,
            "queued_messages": [],
            "last_message_id": None,
            "last_message_at": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "review_round": 0,
            "port": 50000,
        }
        data = {"version": 1, "sessions": {thread_id: row}}
        sessions_path.write_text(json.dumps(data))

    def test_all_live_returns_all_live_no_banners(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        self._write_session_row(tmp_path, "t1", "sid-abc123", "codex-sess-sid-abc1")

        def fake_run(cmd, **kw):
            r = MagicMock()
            r.returncode = 0
            if "ls" in cmd:
                r.stdout = "codex-sess-sid-abc1\n"
            elif "display-message" in cmd:
                r.stdout = "12345\n"
            else:
                r.stdout = "hermes\n"
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            results = _run(dispatcher.on_bot_restart())

        assert len(results) == 1
        assert results[0].status == "live"
        discord_send.assert_not_awaited()

    def test_all_orphaned_calls_discord_send_per_row(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        self._write_session_row(tmp_path, "t1", "sid-abc123", "codex-sess-sid-abc1")

        def fake_run(cmd, **kw):
            r = MagicMock()
            # tmux ls returns nothing — session gone
            if "ls" in cmd:
                r.returncode = 0
                r.stdout = ""
            else:
                r.returncode = 1
                r.stdout = ""
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            results = _run(dispatcher.on_bot_restart())

        assert len(results) == 1
        assert results[0].status == "orphaned"
        discord_send.assert_awaited_once()
        assert "[Session needs revive]" in discord_send.await_args[0][1]

    def test_missing_sessions_file_returns_empty_list(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        # Remove the sessions file if it was created
        sessions_path = tmp_path / "codex_sessions.json"
        if sessions_path.exists():
            sessions_path.unlink()

        def fake_run(cmd, **kw):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            results = _run(dispatcher.on_bot_restart())

        assert results == []

    def test_corrupt_sessions_file_falls_back_to_empty(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        sessions_path = tmp_path / "codex_sessions.json"
        sessions_path.write_text("not valid json{{{")

        def fake_run(cmd, **kw):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            results = _run(dispatcher.on_bot_restart())

        assert results == []


# ── Tests: atomic write ───────────────────────────────────────────────────────


class TestAtomicWrite:
    def test_write_state_creates_file(self, tmp_path):
        dispatcher, _, _ = _make_dispatcher(tmp_path)
        state = {"version": 1, "sessions": {"t-write": {"session_id": "sid-w"}}}
        dispatcher._write_state(state)
        loaded = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert "t-write" in loaded["sessions"]

    def test_write_is_atomic_no_data_loss(self, tmp_path):
        """Write twice; second write must not corrupt the file."""
        dispatcher, _, _ = _make_dispatcher(tmp_path)
        s1 = {"version": 1, "sessions": {"t1": {"session_id": "s1"}}}
        s2 = {"version": 1, "sessions": {"t2": {"session_id": "s2"}}}
        dispatcher._write_state(s1)
        dispatcher._write_state(s2)
        loaded = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert "t2" in loaded["sessions"]
        # No residual .tmp file
        assert not (tmp_path / "codex_sessions.json.tmp").exists()


# ── Tests: schema migration ───────────────────────────────────────────────────


class TestSchemaMigration:
    def test_v1_file_loads_without_migration(self, tmp_path):
        sessions_path = tmp_path / "codex_sessions.json"
        sessions_path.write_text(json.dumps({"version": 1, "sessions": {}}))
        dispatcher, _, _ = _make_dispatcher(tmp_path)
        state = dispatcher._load_state()
        assert state["version"] == 1

    def test_future_version_raises_unsupported(self, tmp_path):
        from gateway.codex_session_dispatcher import UnsupportedSchemaVersion

        # Build dispatcher with empty state first, then plant a future-version file
        dispatcher, _, _ = _make_dispatcher(tmp_path)
        sessions_path = tmp_path / "codex_sessions.json"
        sessions_path.write_text(json.dumps({"version": 99, "sessions": {}}))
        with pytest.raises(UnsupportedSchemaVersion):
            dispatcher._load_state()


# ── Tests: slash commands ─────────────────────────────────────────────────────


class TestSlashCommands:
    def _make_ctx(self, tmp_path, thread_id="t1", **opts):
        from gateway.codex_session_dispatcher import SlashContext
        return SlashContext(thread_id=thread_id, channel_id="ch", options=opts)

    def test_unknown_command_raises(self, tmp_path):
        from gateway.codex_session_dispatcher import UnknownCommandError
        dispatcher, _, _ = _make_dispatcher(tmp_path)
        ctx = self._make_ctx(tmp_path)
        with pytest.raises(UnknownCommandError):
            _run(dispatcher.slash_command("nonexistent", ctx))

    def test_spawn_creates_session(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        ctx = self._make_ctx(tmp_path, thread_id="spawn-t", task="build feature")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            resp = _run(dispatcher.slash_command("spawn", ctx))
        assert "spawned" in resp.content.lower()
        broker.allocate.assert_called_once()

    def test_pause_and_resume_cycle(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        # Create a session first
        event = _make_event(thread_id="t1")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))

        ctx = self._make_ctx(tmp_path, thread_id="t1")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            resp = _run(dispatcher.slash_command("pause", ctx))
        assert "paused" in resp.content.lower()

        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            resp = _run(dispatcher.slash_command("resume", ctx))
        assert "resumed" in resp.content.lower()

    def test_kill_requires_confirm(self, tmp_path):
        dispatcher, _, _ = _make_dispatcher(tmp_path)
        event = _make_event(thread_id="t1")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))

        ctx = self._make_ctx(tmp_path, thread_id="t1", confirm=False)
        resp = _run(dispatcher.slash_command("kill", ctx))
        assert "confirm=True required" in resp.content

    def test_kill_with_confirm_removes_row(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        event = _make_event(thread_id="t1")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))

        ctx = self._make_ctx(tmp_path, thread_id="t1", confirm=True)
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            resp = _run(dispatcher.slash_command("kill", ctx))
        assert "killed" in resp.content.lower()
        state = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert "t1" not in state["sessions"]

    def test_status_no_session(self, tmp_path):
        dispatcher, _, _ = _make_dispatcher(tmp_path)
        ctx = self._make_ctx(tmp_path, thread_id="nope")
        resp = _run(dispatcher.slash_command("status", ctx))
        assert "No active session" in resp.content

    def test_handoff_to_ruflo_writes_marker_file(self, tmp_path):
        dispatcher, broker, discord_send = _make_dispatcher(tmp_path)
        event = _make_event(thread_id="t1")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))

        # Point worktree_path to a tmp dir so we can check file creation
        wt_dir = tmp_path / "codex-wt" / "fake-wt"
        wt_dir.mkdir(parents=True, exist_ok=True)
        sessions_path = tmp_path / "codex_sessions.json"
        state = json.loads(sessions_path.read_text())
        state["sessions"]["t1"]["worktree_path"] = str(wt_dir)
        sessions_path.write_text(json.dumps(state))

        ctx = self._make_ctx(tmp_path, thread_id="t1", summary="Codex done, Ruflo please merge")
        resp = _run(dispatcher.slash_command("handoff-to-ruflo", ctx))
        assert "handed off" in resp.content.lower()

        ephemeral_dir = wt_dir / "_ephemeral"
        assert ephemeral_dir.exists()
        handoff_files = list(ephemeral_dir.glob("handoff-*.md"))
        assert len(handoff_files) == 1
        assert "Ruflo" in handoff_files[0].read_text()

    def test_handoff_empty_summary_returns_error(self, tmp_path):
        dispatcher, _, _ = _make_dispatcher(tmp_path)
        event = _make_event(thread_id="t1")
        with patch("subprocess.run", return_value=_mock_tmux_ok()):
            _run(dispatcher.on_thread_create(event))
        ctx = self._make_ctx(tmp_path, thread_id="t1", summary="")
        resp = _run(dispatcher.slash_command("handoff-to-ruflo", ctx))
        assert "must not be empty" in resp.content
