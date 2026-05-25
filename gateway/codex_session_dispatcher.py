"""Codex Session Dispatcher — Discord thread ↔ Codex session lifecycle router.

Spec: refs/discord-gateway.md §3–§6. ISC: ISC-2, ISC-5, ISC-10, ISC-11, ISC-12.
Slash command sub-handlers delegated to _CommandsMixin (codex_session_dispatcher_commands.py).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from agent.worktree_broker import slugify_ref
from gateway.codex_session_dispatcher_commands import _CommandsMixin

log = logging.getLogger(__name__)

CURRENT_VERSION = 1

# _MIGRATIONS: (from_ver, to_ver) → fn. No migrations needed at v1.
_MIGRATIONS: dict[tuple[int, int], Any] = {}


class SessionNotFoundError(KeyError):
    """Raised when on_thread_message targets an untracked thread_id."""


class TmuxLaunchError(RuntimeError):
    """Raised when tmux new-session fails during on_thread_create."""


class TmuxDeadError(RuntimeError):
    """Raised when a tmux session expected to be alive is gone."""


class WorktreeAllocationError(RuntimeError):
    """Re-raised from WorktreeBroker when allocation fails."""


class UnknownCommandError(ValueError):
    """Raised by slash_command for unregistered command names."""


class UnsupportedSchemaVersion(RuntimeError):
    """Raised when codex_sessions.json version > CURRENT_VERSION."""


@dataclass
class ThreadEvent:
    """Thin event wrapper passed to dispatcher hook methods.

    Spec §3 note: MessageEvent.from_thread does not exist in base.py today,
    so callers pass ThreadEvent instead.  Tests construct these directly.
    """

    thread_id: str
    channel_id: str = ""
    message_id: str = ""
    text: str = ""
    author_id: str = ""
    isa_slug: str = "task"


@dataclass
class SlashContext:
    thread_id: str
    channel_id: str = ""
    user_id: str = ""
    options: dict = field(default_factory=dict)


@dataclass
class SlashResponse:
    content: str
    ephemeral: bool = True


@dataclass
class ReattachResult:
    sid: str
    thread_id: str
    status: str   # "live" | "orphaned"


_MAX_QUEUED_MESSAGES = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sid_short(sid: str) -> str:
    return sid[:8]


def _tmux_name(sid: str) -> str:
    return f"codex-sess-{_sid_short(sid)}"


class CodexSessionDispatcher(_CommandsMixin):
    """Routes Discord thread events to Codex session lifecycle operations.

    Spec §3 public API.  Constructor performs no network calls.
    Slash command sub-handlers are provided by _CommandsMixin.
    """

    def __init__(
        self,
        *,
        hermes_home: Path,
        worktree_broker: Any,
        peer_review_orchestrator: Any,
        merge_broker: Any,
        discord_send: Callable[[str, str], Awaitable[None]],
        kanban_complete: Callable[[str], Any] | None = None,
        base_branch: str = "origin/main",
    ) -> None:
        """
        Pre-conditions: hermes_home exists and is writable.
        Post-conditions: codex_sessions.json loaded or created empty.
        Raises: PermissionError if hermes_home is not writable.
        """
        self._hermes_home = Path(hermes_home)
        self._broker = worktree_broker
        self._peer_review = peer_review_orchestrator  # P2+ — stored, not called in P1
        self._merge_broker = merge_broker              # P3+ — stored, not called in P1
        self._discord_send = discord_send
        self._kanban_complete = kanban_complete
        self._base_branch = base_branch

        self._sessions_path = self._hermes_home / "codex_sessions.json"

        if not os.access(self._hermes_home, os.W_OK):
            raise PermissionError(
                f"hermes_home {self._hermes_home} is not writable"
            )

        self._state = self._load_state()
        if not self._sessions_path.exists():
            self._write_state(self._state)

    # ── Public event hooks ────────────────────────────────────────────────────

    async def on_thread_create(self, event: ThreadEvent) -> None:
        """Allocate worktree + write session row (spec §3, pivot Phase A).

        Phase A: tmux+raw-codex was dropped — Hermes itself processes thread
        messages via the regular Discord adapter path. The dispatcher just
        reserves a worktree + records the assignment. Per-message cwd
        plumbing lands in P1.5.
        """
        thread_id = event.thread_id
        if not thread_id:
            log.warning("on_thread_create: empty thread_id — ignored")
            return

        state = self._load_state()
        if thread_id in state["sessions"]:
            log.warning(
                "on_thread_create: session already exists for thread %s — skipped",
                thread_id,
            )
            return

        sid = str(uuid.uuid4())
        isa_slug = slugify_ref(getattr(event, "isa_slug", None) or "task")

        try:
            wt = self._broker.allocate(sid, isa_slug=isa_slug, base_branch=self._base_branch)
        except Exception as exc:
            log.error("on_thread_create: allocation failed for thread %s: %s", thread_id, exc)
            await self._discord_send(thread_id, f"Could not allocate session — reason: {exc}")
            raise WorktreeAllocationError(str(exc)) from exc

        now = _now_iso()
        row = {
            "session_id": sid,
            "thread_id": thread_id,
            "channel_id": event.channel_id,
            "kanban_card_id": None,
            "worktree_path": str(wt.path),
            "tmux_session": None,  # deprecated — kept for schema back-compat
            "isa_id": isa_slug,
            "isa_path": str(self._hermes_home / "work" / isa_slug / "ISA.md"),
            "state": "CLAIMED",
            "paused": False,
            "queued_messages": [],
            "last_message_id": None,
            "last_message_at": None,
            "created_at": now,
            "review_round": 0,
            "port": wt.port,
        }
        state["sessions"][thread_id] = row
        self._write_state(state)

        await self._discord_send(
            thread_id,
            f"Session `{sid[:8]}` started. Hermes will process this thread "
            f"using assigned worktree `{wt.path}` (branch `{wt.branch}`).",
        )
        log.info("on_thread_create: session %s created for thread %s", sid, thread_id)

    async def on_thread_message(self, event: ThreadEvent) -> None:
        """Record message metadata; let regular Hermes agent handle the turn.

        Phase A: tmux send-keys path removed — the Discord adapter falls
        through to the regular agent which processes the message. This hook
        is metadata-only: dedup, pause-queueing, last_message_id/state
        update. The agent's actual response is produced by the existing
        Discord chat path in gateway/run.py.
        """
        thread_id = event.thread_id
        state = self._load_state()

        if thread_id not in state["sessions"]:
            await self._discord_send(thread_id, "No active session in this thread.")
            raise SessionNotFoundError(thread_id)

        row = state["sessions"][thread_id]

        # Deduplication
        if event.message_id and event.message_id == row.get("last_message_id"):
            log.debug("on_thread_message: duplicate message_id %s — dropped", event.message_id)
            return

        # Paused: queue message (operator drains via /resume)
        if row.get("paused"):
            queue = row.setdefault("queued_messages", [])
            if len(queue) >= _MAX_QUEUED_MESSAGES:
                queue.pop(0)
            queue.append({"message_id": event.message_id, "text": event.text, "ts": _now_iso()})
            row["queued_messages"] = queue
            self._write_state(state)
            log.info("on_thread_message: session %s paused, queued message", row["session_id"])
            return

        # State transition + metadata update.
        row["last_message_id"] = event.message_id or row.get("last_message_id")
        row["last_message_at"] = _now_iso()
        row["state"] = "EXECUTING"
        self._write_state(state)
        log.debug(
            "on_thread_message: recorded for session %s (thread %s)",
            row["session_id"], thread_id,
        )

        # P1: post /review prompt if ISA phase is 'verify' (P2 wires the
        # actual peer-review trigger; this is just an operator nudge).
        if row.get("isa_phase") == "verify":
            await self._discord_send(
                thread_id,
                "ISA phase is `verify`. Use `/review` to trigger peer review.",
            )

    async def on_thread_archive(self, event: ThreadEvent) -> None:
        """Handle thread archive/delete — terminal cleanup (spec §3, pivot Phase A)."""
        thread_id = event.thread_id
        state = self._load_state()
        if thread_id not in state["sessions"]:
            return

        row = state["sessions"][thread_id]
        sid = row["session_id"]
        terminal_states = {"COMPLETE", "MERGING"}

        try:
            if row["state"] in terminal_states and self._kanban_complete and row.get("kanban_card_id"):
                try:
                    self._kanban_complete(row["kanban_card_id"])
                except Exception as exc:
                    log.warning("on_thread_archive: kanban_complete failed: %s", exc)
            try:
                self._broker.release(sid)
            except Exception as exc:
                log.warning("on_thread_archive: release failed for %s: %s", sid, exc)
            del state["sessions"][thread_id]
            self._write_state(state)
            log.info("on_thread_archive: session %s archived (was %s)", sid, row["state"])
        except Exception as exc:
            log.error("on_thread_archive: unexpected error for %s: %s", sid, exc)

    async def on_bot_restart(self) -> list[ReattachResult]:
        """Rehydrate dispatcher state after a bot restart (pivot Phase A).

        Phase A: no tmux processes to probe — the dispatcher's state IS the
        truth. Just verify each session's worktree still exists on disk
        (operator might have removed one manually) and rehydrate the
        broker's in-memory registry so subsequent allocate/release calls
        are idempotent.
        """
        state = self._load_state()
        sessions = state.get("sessions", {})
        results: list[ReattachResult] = []

        for thread_id, row in sessions.items():
            sid = row["session_id"]
            try:
                wt_path = Path(row.get("worktree_path", ""))
                if wt_path.exists() and (wt_path / ".git").exists():
                    log.info("on_bot_restart: session %s rehydrated (thread %s)", sid, thread_id)
                    results.append(ReattachResult(sid=sid, thread_id=thread_id, status="live"))
                else:
                    log.warning(
                        "on_bot_restart: session %s worktree missing at %s — marking ORPHANED",
                        sid, wt_path,
                    )
                    row["state"] = "ORPHANED"
                    results.append(ReattachResult(sid=sid, thread_id=thread_id, status="orphaned"))
            except Exception as exc:
                log.warning("on_bot_restart: per-row error sid %s: %s", sid, exc)

        state["sessions"] = sessions
        self._write_state(state)
        return results

    async def slash_command(self, name: str, ctx: SlashContext) -> SlashResponse:
        """Unified slash command entry point; routes to _cmd_* methods (spec §4)."""
        handlers = {
            "spawn": self._cmd_spawn,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "kill": self._cmd_kill,
            "status": self._cmd_status,
            "handoff-to-ruflo": self._cmd_handoff_to_ruflo,
        }
        handler = handlers.get(name)
        if handler is None:
            raise UnknownCommandError(f"Unknown command: {name!r}")
        return await handler(ctx)

    def is_tracked(self, thread_id: str) -> bool:
        """Return True if thread_id has an active session row."""
        state = self._load_state()
        return thread_id in state.get("sessions", {})

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Read codex_sessions.json with shared lock (spec §5)."""
        if not self._sessions_path.exists():
            return {"version": CURRENT_VERSION, "sessions": {}}
        try:
            with open(self._sessions_path, "r", encoding="utf-8") as fd:
                fcntl.flock(fd, fcntl.LOCK_SH)
                try:
                    data = json.load(fd)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("codex_sessions.json unreadable, starting empty: %s", exc)
            return {"version": CURRENT_VERSION, "sessions": {}}

        version = data.get("version", 1)
        if version > CURRENT_VERSION:
            raise UnsupportedSchemaVersion(
                f"codex_sessions.json version {version} > supported {CURRENT_VERSION}"
            )
        while version < CURRENT_VERSION:
            fn = _MIGRATIONS.get((version, version + 1))
            if fn is None:
                break
            data = fn(data)
            version += 1
            data["version"] = version

        data.setdefault("sessions", {})
        return data

    def _write_state(self, state: dict) -> None:
        """Write codex_sessions.json with exclusive lock + atomic rename (spec §5)."""
        self._sessions_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._sessions_path.with_suffix(".json.tmp")

        with open(self._sessions_path, "a+", encoding="utf-8") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                with open(tmp_path, "w", encoding="utf-8") as tmp_fd:
                    json.dump(state, tmp_fd, indent=2)
                os.replace(tmp_path, self._sessions_path)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    # ── tmux helpers ──────────────────────────────────────────────────────────

    def _tmux_has_session(self, name: str) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True, text=True, check=False,
        )
        return result.returncode == 0

    def _get_live_tmux_sessions(self) -> set[str]:
        """Return set of codex-sess-* tmux session names."""
        result = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return set()
        return {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("codex-sess-")
        }

    def _get_pane_pid(self, tmux_session: str) -> str | None:
        """Return pane PID string for the given tmux session, or None."""
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_session, "#{pane_pid}"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    def _hermes_alive_in_tmux(self, tmux_session: str) -> bool:
        """Two-step liveness check per amendment C1.

        Step 1: get pane PID via tmux display-message.
        Step 2: pgrep -P <pid> hermes — returncode 0 means hermes running.
        """
        pane_pid = self._get_pane_pid(tmux_session)
        if not pane_pid:
            return False
        result = subprocess.run(
            ["pgrep", "-P", pane_pid, "hermes"],
            capture_output=True, text=True, check=False,
        )
        return result.returncode == 0

    # ── Banner ────────────────────────────────────────────────────────────────

    def _revive_banner(self, row: dict) -> str:
        """NEEDS_REVIVE banner text verbatim per spec §6."""
        sid = row.get("session_id", "unknown")
        short = _sid_short(sid)
        wt_path = row.get("worktree_path", "<unknown>")
        last_active = row.get("last_message_at") or "never"
        isa_path = row.get("isa_path", "<unknown>")
        exists_label = "exists" if Path(wt_path).exists() else "missing"
        return (
            f"[Session needs revive]\n"
            f"Session {sid} was running when the bot restarted but its tmux session\n"
            f"(codex-sess-{short}) is gone.\n\n"
            f"Worktree: {wt_path} [{exists_label}]\n"
            f"Last active: {last_active}\n"
            f"ISA: {isa_path}\n\n"
            f"Warning: the old worktree at {wt_path} may contain uncommitted\n"
            f"source changes. Run `git -C {wt_path} diff` before reviving to\n"
            f"capture any unsaved work. The /revive command will post a `git diff\n"
            f"--stat` summary before allocating the new worktree, but you can inspect\n"
            f"the full diff now if the worktree is still on disk.\n\n"
            f"Use /revive to launch a fresh session on the same worktree and branch,\n"
            f"or /kill to discard and free the slot."
        )
