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
from typing import Any, Awaitable, Callable, Optional

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

        # P2.5: lazy-start the orchestrator on first review request so a
        # gateway boot with HERMES_CODEX_DISPATCHER=1 but no active codex
        # threads doesn't burn Opus pane lifetime / Max token quota.
        self._peer_review_started = False
        self._peer_review_start_lock: Optional[Any] = None  # asyncio.Lock, created lazily

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
            "review": self._cmd_review,
            "revive": self._cmd_revive,
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

    async def on_phase_verify(self, thread_id: str) -> None:
        """Auto-trigger Opus peer-review for a session that hit phase: verify.

        Called by CodexPhaseWatcher when an ISA transitions into ``verify``.
        Looks up the session row, collects the diff from the worktree, asks
        the orchestrator for a verdict, then runs the verdict-specific
        side effects (Discord post, kanban comment, ISA Decisions append).
        """
        if self._peer_review is None:
            log.info(
                "on_phase_verify: no orchestrator wired — skipping for thread %s",
                thread_id,
            )
            return
        state = self._load_state()
        row = state.get("sessions", {}).get(thread_id)
        if row is None:
            log.warning("on_phase_verify: thread %s not tracked", thread_id)
            return

        diff = self._collect_diff(Path(row.get("worktree_path", "")))
        isa_path = Path(row.get("isa_path", ""))
        sid = row["session_id"]

        await self._ensure_peer_review_started()

        log.info("on_phase_verify: dispatching review for sid=%s thread=%s", sid, thread_id)
        try:
            verdict = await self._peer_review.review(
                session_id=sid,
                isa_path=isa_path,
                diff=diff,
            )
        except Exception as exc:
            log.error("on_phase_verify: orchestrator.review crashed: %s", exc)
            await self._discord_send(
                thread_id,
                f"⚠️ Peer review crashed: `{exc}` — operator intervention needed.",
            )
            return

        await self._apply_verdict(thread_id=thread_id, row=row, state=state, verdict=verdict)

    async def _ensure_peer_review_started(self) -> None:
        """Start the orchestrator on first use (single-flight)."""
        if self._peer_review is None or self._peer_review_started:
            return
        import asyncio  # noqa: PLC0415 — kept lazy so the module imports cleanly
        if self._peer_review_start_lock is None:
            self._peer_review_start_lock = asyncio.Lock()
        async with self._peer_review_start_lock:
            if self._peer_review_started:
                return
            log.info("on_phase_verify: starting Opus orchestrator on first use")
            await self._peer_review.start()
            self._peer_review_started = True

    def _collect_diff(self, worktree_path: Path) -> str:
        """Run ``git diff <base_branch>...HEAD`` inside the worktree."""
        if not worktree_path.exists():
            return f"<worktree {worktree_path} missing>"
        try:
            result = subprocess.run(
                ["git", "-C", str(worktree_path), "diff", f"{self._base_branch}...HEAD"],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if result.returncode != 0:
                return f"<git diff failed: {result.stderr.strip()}>"
            return result.stdout
        except subprocess.TimeoutExpired:
            return "<git diff timed out>"
        except Exception as exc:
            return f"<git diff exception: {exc}>"

    async def _apply_verdict(
        self,
        *,
        thread_id: str,
        row: dict,
        state: dict,
        verdict: "Any",  # noqa: F821 — Verdict (avoid orchestrator import circularity)
    ) -> None:
        """Verdict side effects per ISA: kanban + ISA Decisions + Discord + state."""
        sid = row["session_id"]
        rationale = (verdict.rationale or "").strip()
        # Truncate rationale for Discord (2k char ceiling; keep margin).
        rationale_short = rationale if len(rationale) <= 1500 else rationale[:1500] + "…"

        if verdict.kind == "APPROVE":
            row["state"] = "MERGING"
            self._write_state(state)
            await self._discord_send(
                thread_id,
                f"✅ **VERDICT: APPROVE** (Opus, iter {verdict.iteration})\n"
                f"{rationale_short}\n\n"
                f"Handing off to merge broker…",
            )
            # P3: hand the approved diff to the merge broker.  Returns a
            # MergeResult with the PR URL + classification + auto-merge label
            # (which Mergify / Actions handles server-side).
            if self._merge_broker is not None:
                try:
                    result = await self._merge_broker.merge(
                        session_id=sid,
                        worktree=Path(row.get("worktree_path", "")),
                        branch=f"codex/{sid}/{row.get('isa_id', 'task')}",
                        isa_path=Path(row.get("isa_path", "")),
                        summary=rationale_short,
                    )
                except Exception as exc:
                    log.exception("_apply_verdict: merge_broker.merge crashed")
                    await self._discord_send(
                        thread_id,
                        f"⚠️ Merge broker crashed: `{exc}` — operator handoff.",
                    )
                    return
                if not result.ok:
                    await self._discord_send(
                        thread_id,
                        f"⛔ Merge failed: {result.error}\n"
                        f"Session stays at MERGING — operator triage needed.",
                    )
                    return
                await self._discord_send(
                    thread_id,
                    f"📦 PR #{result.pr_number} opened — "
                    f"`{result.classification}` — <{result.pr_url}>",
                )
        elif verdict.kind == "REVISE":
            row["state"] = "EXECUTING"
            self._write_state(state)
            self._kanban_comment_for_review(
                row.get("kanban_card_id"), verdict.kind, rationale, verdict.iteration,
            )
            self._append_isa_decision(row.get("isa_path"), verdict.kind, rationale, verdict.iteration)
            await self._discord_send(
                thread_id,
                f"🔁 **VERDICT: REVISE** (Opus, iter {verdict.iteration})\n"
                f"{rationale_short}\n\n"
                f"Session back to `EXECUTING` — address the feedback above.",
            )
        else:  # ESCALATE
            row["state"] = "ESCALATED"
            self._write_state(state)
            await self._discord_send(
                thread_id,
                f"⛔ **VERDICT: ESCALATE** (Opus, iter {verdict.iteration})\n"
                f"{rationale_short}\n\n"
                f"<@OPERATOR> manual intervention needed. "
                f"Further auto-reviews stopped for this session.",
            )

    def _kanban_comment_for_review(
        self,
        card_id: Optional[str],
        kind: str,
        rationale: str,
        iteration: int,
    ) -> None:
        """Post a peer-review comment to the linked kanban task (best effort)."""
        if not card_id:
            return
        try:
            from tools.kanban_tools import _connect  # noqa: PLC0415
            kb, conn = _connect()
            try:
                kb.add_comment(
                    conn,
                    card_id,
                    author="peer-review-opus",
                    body=f"VERDICT: {kind} (iter {iteration})\n\n{rationale}",
                )
            finally:
                conn.close()
        except Exception as exc:
            log.warning("_kanban_comment_for_review: %s", exc)

    def _append_isa_decision(
        self,
        isa_path_str: Optional[str],
        kind: str,
        rationale: str,
        iteration: int,
    ) -> None:
        """Append a Decisions entry to the ISA (best effort)."""
        if not isa_path_str:
            return
        path = Path(isa_path_str)
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8")
            now = _now_iso()
            entry = (
                f"\n**Peer review {iteration} ({now}): {kind}.**\n{rationale}\n"
            )
            # If ## Decisions section exists, append right after its header line.
            marker = "\n## Decisions\n"
            idx = text.find(marker)
            if idx < 0:
                # No Decisions section — append the section + entry at end.
                if not text.endswith("\n"):
                    text += "\n"
                text += "\n## Decisions\n" + entry
            else:
                head = text[: idx + len(marker)]
                tail = text[idx + len(marker):]
                text = head + entry + tail
            path.write_text(text, encoding="utf-8")
        except Exception as exc:
            log.warning("_append_isa_decision: %s", exc)

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
