"""Slash command sub-handlers for CodexSessionDispatcher.

Imported and delegated to by CodexSessionDispatcher.slash_command().
Spec: refs/discord-gateway.md §4.

Each handler is an async method on _CommandsMixin, which CodexSessionDispatcher
inherits so slash_command() can call self._cmd_*().
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.codex_session_dispatcher import SlashContext, SlashResponse

log = logging.getLogger(__name__)

_SESSION_CAP = 8


class _CommandsMixin:
    """Mixin that provides all /slash command sub-handlers.

    Requires self._load_state(), self._write_state(), self._broker,
    self._discord_send, self._tmux_has_session() from the base class.
    """

    async def _cmd_spawn(self, ctx: "SlashContext") -> "SlashResponse":
        from gateway.codex_session_dispatcher import (
            SlashResponse, ThreadEvent, WorktreeAllocationError, TmuxLaunchError,
        )

        state = self._load_state()
        if ctx.thread_id in state["sessions"]:
            return SlashResponse(
                "Session already exists for this thread — use `/status` to check it."
            )
        if len(state["sessions"]) >= _SESSION_CAP:
            return SlashResponse(
                "Session cap (8) reached — kill or merge an existing session first."
            )

        task = ctx.options.get("task", "")
        isa_path_str = ctx.options.get("isa_path", "")
        event = ThreadEvent(
            thread_id=ctx.thread_id,
            channel_id=ctx.channel_id,
        )
        event.isa_slug = Path(isa_path_str).stem if isa_path_str else "task"  # type: ignore[attr-defined]

        try:
            await self.on_thread_create(event)
        except (WorktreeAllocationError, TmuxLaunchError) as exc:
            return SlashResponse(f"Spawn failed: {exc}")
        return SlashResponse(f"Session spawned for task: {task!r}", ephemeral=False)

    async def _cmd_pause(self, ctx: "SlashContext") -> "SlashResponse":
        from gateway.codex_session_dispatcher import SlashResponse

        state = self._load_state()
        if ctx.thread_id not in state["sessions"]:
            return SlashResponse("No active session in this thread.")
        row = state["sessions"][ctx.thread_id]
        if row.get("paused"):
            return SlashResponse("Session is already paused.")
        tmux_session = row.get("tmux_session", "")
        if tmux_session:
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_session, "C-c", ""],
                capture_output=True, text=True, check=False,
            )
        row["paused"] = True
        self._write_state(state)
        return SlashResponse("Session paused.", ephemeral=False)

    async def _cmd_resume(self, ctx: "SlashContext") -> "SlashResponse":
        from gateway.codex_session_dispatcher import SlashResponse

        state = self._load_state()
        if ctx.thread_id not in state["sessions"]:
            return SlashResponse("No active session in this thread.")
        row = state["sessions"][ctx.thread_id]
        if not row.get("paused"):
            return SlashResponse("Session is not paused.")
        row["paused"] = False
        queued = row.get("queued_messages", [])
        tmux_session = row.get("tmux_session", "")
        for msg in queued:
            if tmux_session:
                subprocess.run(
                    ["tmux", "send-keys", "-t", tmux_session, msg["text"], "Enter"],
                    capture_output=True, text=True, check=False,
                )
        row["queued_messages"] = []
        self._write_state(state)
        flushed = len(queued)
        return SlashResponse(
            f"Session resumed. {flushed} queued message(s) forwarded.", ephemeral=False
        )

    async def _cmd_kill(self, ctx: "SlashContext") -> "SlashResponse":
        from gateway.codex_session_dispatcher import SlashResponse

        if not ctx.options.get("confirm"):
            return SlashResponse("confirm=True required.")
        state = self._load_state()
        if ctx.thread_id not in state["sessions"]:
            return SlashResponse("No active session in this thread.")
        row = state["sessions"][ctx.thread_id]
        sid = row["session_id"]
        tmux_session = row.get("tmux_session", "")
        if tmux_session:
            subprocess.run(
                ["tmux", "kill-session", "-t", tmux_session],
                capture_output=True, text=True, check=False,
            )
        try:
            self._broker.release(sid)
        except Exception as exc:
            log.warning("_cmd_kill: broker release failed for %s: %s", sid, exc)
        del state["sessions"][ctx.thread_id]
        self._write_state(state)
        return SlashResponse(
            "Session killed. Worktree and tmux session removed.", ephemeral=False
        )

    async def _cmd_status(self, ctx: "SlashContext") -> "SlashResponse":
        from gateway.codex_session_dispatcher import SlashResponse

        state = self._load_state()
        if ctx.thread_id not in state["sessions"]:
            return SlashResponse("No active session in this thread.")
        row = state["sessions"][ctx.thread_id]
        tmux_session = row.get("tmux_session", "")
        tmux_alive = self._tmux_has_session(tmux_session) if tmux_session else False
        text = (
            f"**Session:** `{row['session_id']}`\n"
            f"**State:** {row.get('state')}\n"
            f"**ISA:** {row.get('isa_id')}\n"
            f"**tmux alive:** {'yes' if tmux_alive else 'no'}\n"
            f"**Last active:** {row.get('last_message_at') or 'never'}\n"
        )
        return SlashResponse(text, ephemeral=False)

    async def _cmd_handoff_to_ruflo(self, ctx: "SlashContext") -> "SlashResponse":
        from gateway.codex_session_dispatcher import SlashResponse

        summary = ctx.options.get("summary", "").strip()
        if not summary:
            return SlashResponse("Summary must not be empty.")
        state = self._load_state()
        if ctx.thread_id not in state["sessions"]:
            return SlashResponse("No active session in this thread.")
        row = state["sessions"][ctx.thread_id]

        row["paused"] = True
        row["state"] = "HANDOFF"

        wt_path = Path(row.get("worktree_path", ""))
        if wt_path.name:
            ephemeral_dir = wt_path / "_ephemeral"
            ephemeral_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            handoff_file = ephemeral_dir / f"handoff-{ts}.md"
            handoff_content = (
                f"# Handoff to Ruflo\n\n"
                f"**Summary:** {summary}\n\n"
                f"**ISA:** {row.get('isa_path')}\n"
                f"**State:** {row.get('state')}\n"
                f"**Session:** {row.get('session_id')}\n"
                f"**Created:** {row.get('created_at')}\n"
            )
            try:
                handoff_file.write_text(handoff_content, encoding="utf-8")
            except OSError as exc:
                log.warning("_cmd_handoff_to_ruflo: could not write handoff file: %s", exc)

        self._write_state(state)
        await self._discord_send(ctx.thread_id, f"**Handoff to Ruflo:**\n{summary}")
        return SlashResponse(
            "Session handed off to Ruflo. Session is now paused.", ephemeral=False
        )
