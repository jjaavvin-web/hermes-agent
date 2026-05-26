"""Peer-Review Orchestrator — Opus pane pool for Codex-session diffs.

Implements ``isas/P2-peer-review.md`` + ``module-specs/peer-review-orchestrator.md``.

When a Codex session transitions to ``phase: verify``, the dispatcher calls
:meth:`PeerReviewOrchestrator.review` which acquires a warm tmux pane running
interactive ``claude``, injects a review prompt via ``send-keys``, polls
``capture-pane`` for a ``VERDICT:`` sentinel, and returns a structured verdict.

Design principle: lineage diversity. Opus 4.7 reading a Codex-produced diff
catches what Codex missed during write. Invocation is exclusively interactive
Claude — ``claude -p``, ``claude --print``, and the Agent SDK are forbidden
(billing constraint past 2026-06-15).

Pane lifecycle mirrors ``~/.hermes/scripts/templates/ruflo-launch-interactive.template.sh``
which is production-proven for Ruflo hives.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

_DEFAULT_POOL_SIZE = 2
_DEFAULT_ITERATION_CAP = 3
_DEFAULT_DAILY_CAP = 10
_DEFAULT_REVIEW_TIMEOUT_SEC = 300
_DEFAULT_IDLE_THRESHOLD_SEC = 15
_DEFAULT_PANE_RECYCLE_AFTER = 50
_DIFF_SUMMARY_THRESHOLD = 20480
_STARTUP_DIALOG_TIMEOUT_SEC = 120

# Sentinel the reviewer is instructed to emit so we can distinguish a
# verdict (in the reviewer's reply) from the orchestrator's own typed-in
# invocation echoed back into the pane capture. Without this, a literal
# "VERDICT: APPROVE | REVISE | ESCALATE" in the prompt itself matches
# the regex and returns a fake-positive APPROVE within ~20s of dispatch
# (no real review having happened). Discovered 2026-05-26 during the
# first live smoke after the ISA-bootstrap fix.
_VERDICT_SENTINEL = "==CODEX_REVIEW_VERDICT=="

_VERDICT_PATTERN = re.compile(
    r"==CODEX_REVIEW_VERDICT==\s*(APPROVE|REVISE|ESCALATE)\b",
    re.MULTILINE,
)
_FUZZY_VERDICT_PATTERN = re.compile(
    r"==CODEX_REVIEW_VERDICT==\s*(APPROOVE|APROVE|APPROVE|REWISE|REVIESE|REVISE|ESCAATE|ESCALATE|ESCALAT)\b",
    re.MULTILINE | re.IGNORECASE,
)


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass
class Verdict:
    kind: Literal["APPROVE", "REVISE", "ESCALATE"]
    rationale: str
    iteration: int
    raw_capture: str
    duration_sec: float
    pane_id: str


@dataclass
class _Pane:
    pane_id: str          # e.g. "codex-review-0"
    state: Literal["WARM", "BUSY", "DEAD"] = "DEAD"
    review_count: int = 0
    last_used_at: Optional[datetime] = None


@dataclass
class _ReviewState:
    """Per-session counters persisted to codex-review-state.json."""
    iterations: int = 0
    reviews_today: int = 0
    day_started: str = ""
    last_verdict: Optional[str] = None
    last_review_at: Optional[str] = None


class PeerReviewError(RuntimeError):
    """Base class for unrecoverable orchestrator errors."""


class PanePoolFailedToStart(PeerReviewError):
    """Raised when ``start()`` cannot bring any pane to WARM."""


# ── Orchestrator ─────────────────────────────────────────────────────────────


class PeerReviewOrchestrator:
    """Maintains a pool of warm Opus tmux panes and reviews Codex diffs."""

    def __init__(
        self,
        *,
        hermes_home: Path,
        pool_size: int = _DEFAULT_POOL_SIZE,
        iteration_cap: int = _DEFAULT_ITERATION_CAP,
        daily_cap: int = _DEFAULT_DAILY_CAP,
        review_timeout_sec: int = _DEFAULT_REVIEW_TIMEOUT_SEC,
        idle_threshold_sec: int = _DEFAULT_IDLE_THRESHOLD_SEC,
        pane_recycle_after: int = _DEFAULT_PANE_RECYCLE_AFTER,
        # Indirection for tests — production passes the real callables.
        subprocess_run: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        sleep_async: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._hermes_home = Path(hermes_home)
        self._pool_size = pool_size
        self._iteration_cap = iteration_cap
        self._daily_cap = daily_cap
        self._review_timeout_sec = review_timeout_sec
        self._idle_threshold_sec = idle_threshold_sec
        self._pane_recycle_after = pane_recycle_after

        self._run = subprocess_run or subprocess.run
        self._sleep = sleep_async or asyncio.sleep

        self._panes: dict[str, _Pane] = {
            f"codex-review-{i}": _Pane(pane_id=f"codex-review-{i}")
            for i in range(pool_size)
        }
        self._warm_queue: asyncio.Queue[str] = asyncio.Queue()
        self._sid_locks: dict[str, asyncio.Lock] = {}
        self._sid_locks_guard = asyncio.Lock()
        self._state_path = self._hermes_home / "codex-review-state.json"

    # ── public lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn each pane and bring it to WARM."""
        for pane_id in list(self._panes.keys()):
            try:
                await self._spawn_pane(pane_id)
            except Exception as exc:
                log.error("peer_review.start: pane %s failed: %s", pane_id, exc)
        warm = [p for p in self._panes.values() if p.state == "WARM"]
        if not warm:
            raise PanePoolFailedToStart(
                f"No panes reached WARM state — pool size {self._pool_size}"
            )
        log.info("peer_review.start: %d/%d panes WARM", len(warm), self._pool_size)

    async def stop(self) -> None:
        """Kill all tmux sessions and drain in-flight queue entries."""
        for pane_id, pane in self._panes.items():
            if pane.state != "DEAD":
                self._tmux("kill-session", "-t", pane_id, check=False)
                pane.state = "DEAD"
        log.info("peer_review.stop: all panes killed")

    async def review(
        self,
        *,
        session_id: str,
        isa_path: Path,
        diff: str,
    ) -> Verdict:
        """Review a Codex diff. Never raises — all failure modes return ESCALATE."""
        start = time.monotonic()
        state = self._load_session_state(session_id)

        # Cap checks first — don't claim a pane if we already know we'll bounce.
        if state.iterations >= self._iteration_cap:
            return self._escalate_no_pane(
                session_id, state, start,
                rationale=f"iteration cap of {self._iteration_cap} reached for sid",
            )
        if state.reviews_today >= self._daily_cap:
            return self._escalate_no_pane(
                session_id, state, start,
                rationale=f"daily review cap of {self._daily_cap} reached for sid",
            )

        async with await self._get_sid_lock(session_id):
            try:
                pane_id = await self._acquire_pane()
            except Exception as exc:
                return self._escalate_no_pane(
                    session_id, state, start,
                    rationale=f"pane acquisition failed: {exc}",
                )

            pane = self._panes[pane_id]
            pane.state = "BUSY"
            try:
                verdict = await self._run_review_on_pane(
                    pane=pane,
                    session_id=session_id,
                    isa_path=isa_path,
                    diff=diff,
                    state=state,
                    start=start,
                )
            finally:
                # Pane returns to WARM unless it died mid-review.
                pane.review_count += 1
                pane.last_used_at = datetime.now(timezone.utc)
                if pane.state != "DEAD":
                    if pane.review_count >= self._pane_recycle_after:
                        await self._recycle_pane(pane_id)
                    else:
                        pane.state = "WARM"
                        await self._warm_queue.put(pane_id)
                else:
                    # Respawn in background; don't block this review path.
                    asyncio.create_task(self._respawn_pane(pane_id))

            self._persist_session_state(session_id, state, verdict)
            return verdict

    # ── pane lifecycle helpers ──────────────────────────────────────────

    async def _spawn_pane(self, pane_id: str) -> None:
        """tmux new-session -d 'claude' + pipe-pane + dialog-clear."""
        log_path = self._hermes_home / f"{pane_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        kill = self._tmux("kill-session", "-t", pane_id, check=False)
        if kill.returncode == 0:
            log.debug("peer_review._spawn_pane: killed pre-existing %s", pane_id)

        # Pre-approve the tools the reviewer needs and the directory the
        # prompt file lives in. Without these flags Claude Code v2.1.150+
        # blocks the Read tool by default and the pane has to ask the
        # operator — there is no operator on a headless reviewer pane,
        # so the review just stalls.
        #
        # Why not ``--dangerously-skip-permissions``: it triggers a
        # "Bypass Permissions" confirmation dialog whose default
        # selection is "No, exit". Our generic dialog_clear blindly
        # presses Enter on that dialog and kills the pane. Discovered
        # the hard way 2026-05-26.
        spawn = self._tmux(
            "new-session", "-d", "-s", pane_id,
            "claude",
            "--allowed-tools", "Read,Bash",
            "--add-dir", "/tmp",
            check=False,
        )
        if spawn.returncode != 0:
            raise PeerReviewError(
                f"tmux new-session failed for {pane_id}: {spawn.stderr.strip()}"
            )

        pipe = self._tmux(
            "pipe-pane", "-o", "-t", pane_id, f"cat >> {log_path}",
            check=False,
        )
        if pipe.returncode != 0:
            log.warning(
                "peer_review._spawn_pane: pipe-pane failed for %s: %s",
                pane_id, pipe.stderr.strip(),
            )

        await self._dialog_clear(pane_id)
        self._panes[pane_id].state = "WARM"
        await self._warm_queue.put(pane_id)
        log.info("peer_review._spawn_pane: pane %s WARM", pane_id)

    async def _dialog_clear(self, pane_id: str) -> None:
        """Press Enter on workspace-trust / MCP approval dialogs.

        Mirrors ``ruflo-launch-interactive.template.sh:117-145`` exactly:
        24 attempts of 5s each, advance once a prompt appears and stays
        for two consecutive captures.
        """
        dialogs = 0
        clear_streak = 0
        for _ in range(24):
            await self._sleep(5)
            check = self._tmux("has-session", "-t", pane_id, check=False)
            if check.returncode != 0:
                raise PeerReviewError(
                    f"pane {pane_id} died during dialog-clear"
                )
            cap = self._tmux("capture-pane", "-p", "-t", pane_id, check=False)
            pane_text = cap.stdout or ""
            if "Enter to confirm" in pane_text:
                self._tmux("send-keys", "-t", pane_id, "Enter", check=False)
                dialogs += 1
                clear_streak = 0
            else:
                clear_streak += 1
                if dialogs > 0 and clear_streak >= 2:
                    break
        if dialogs == 0:
            log.warning(
                "peer_review._dialog_clear: no startup dialogs seen on %s — "
                "verify the pane is actually running claude",
                pane_id,
            )

    async def _respawn_pane(self, pane_id: str) -> None:
        try:
            await self._spawn_pane(pane_id)
        except Exception as exc:
            log.error("peer_review._respawn_pane: %s failed: %s", pane_id, exc)
            self._panes[pane_id].state = "DEAD"

    async def _recycle_pane(self, pane_id: str) -> None:
        log.info("peer_review._recycle_pane: %s hit recycle cap, respawning", pane_id)
        self._panes[pane_id].state = "DEAD"
        self._panes[pane_id].review_count = 0
        asyncio.create_task(self._respawn_pane(pane_id))

    async def _acquire_pane(self) -> str:
        """Block until a WARM pane is free; return its id."""
        return await self._warm_queue.get()

    # ── review dispatch ─────────────────────────────────────────────────

    async def _run_review_on_pane(
        self,
        *,
        pane: _Pane,
        session_id: str,
        isa_path: Path,
        diff: str,
        state: _ReviewState,
        start: float,
    ) -> Verdict:
        prompt_path = Path(f"/tmp/review-{session_id}.md")
        try:
            isa_text = isa_path.read_text(encoding="utf-8") if isa_path.exists() else ""
        except OSError as exc:
            isa_text = f"<could not read ISA: {exc}>"
        diff_payload, _ = self._maybe_summarize_diff(diff)
        prompt_path.write_text(
            self._render_prompt(isa_path, isa_text, diff_payload),
            encoding="utf-8",
        )

        # Probe pane is alive before send-keys; treat tmux death here as
        # ESCALATE per spec §7.
        if self._tmux("has-session", "-t", pane.pane_id, check=False).returncode != 0:
            pane.state = "DEAD"
            self._cleanup_prompt(prompt_path)
            return Verdict(
                kind="ESCALATE",
                rationale=f"pane {pane.pane_id} dead before dispatch",
                iteration=state.iterations,
                raw_capture="",
                duration_sec=time.monotonic() - start,
                pane_id=pane.pane_id,
            )

        # The reviewer is instructed to use a UNIQUE sentinel
        # (``_VERDICT_SENTINEL``) so the orchestrator's polling regex
        # cannot accidentally match the orchestrator's own typed-in
        # invocation in the pane capture. See _VERDICT_PATTERN docstring.
        invocation = (
            f"Read the review document at {prompt_path}. "
            f"Then, on a line by itself, write {_VERDICT_SENTINEL} followed "
            f"by exactly one of APPROVE, REVISE, or ESCALATE, and on the "
            f"following lines give your rationale. The "
            f"{_VERDICT_SENTINEL} marker must appear nowhere else in your reply."
        )
        # Claude Code v2.1.150 TUI swallows ``send-keys <text> Enter`` when
        # both are passed in a single call (the text lands in the input box
        # but Enter doesn't submit). Send Enter as a separate keystroke
        # with a small settle delay. Matches the working pattern from the
        # ``Tmux wake Claude TUI queen`` memory.
        self._tmux("send-keys", "-t", pane.pane_id, invocation, check=False)
        await self._sleep(0.5)
        self._tmux("send-keys", "-t", pane.pane_id, "Enter", check=False)

        verdict = await self._poll_for_verdict(
            pane=pane,
            session_id=session_id,
            state=state,
            start=start,
        )
        self._cleanup_prompt(prompt_path)
        return verdict

    async def _poll_for_verdict(
        self,
        *,
        pane: _Pane,
        session_id: str,
        state: _ReviewState,
        start: float,
    ) -> Verdict:
        deadline = time.monotonic() + self._review_timeout_sec
        sentinel_ts: Optional[float] = None
        last_capture = ""
        while time.monotonic() < deadline:
            await self._sleep(5)
            check = self._tmux("has-session", "-t", pane.pane_id, check=False)
            if check.returncode != 0:
                pane.state = "DEAD"
                return Verdict(
                    kind="ESCALATE",
                    rationale=f"pane {pane.pane_id} died mid-review",
                    iteration=state.iterations,
                    raw_capture=last_capture,
                    duration_sec=time.monotonic() - start,
                    pane_id=pane.pane_id,
                )
            cap = self._tmux("capture-pane", "-p", "-t", pane.pane_id, check=False)
            text = cap.stdout or ""
            if text != last_capture:
                last_capture = text
                if _VERDICT_PATTERN.search(text):
                    sentinel_ts = time.monotonic()
            elif sentinel_ts is not None:
                if time.monotonic() - sentinel_ts >= self._idle_threshold_sec:
                    return self._parse_verdict(
                        capture=text,
                        pane=pane,
                        state=state,
                        start=start,
                    )
        return Verdict(
            kind="ESCALATE",
            rationale="review timeout",
            iteration=state.iterations,
            raw_capture=last_capture,
            duration_sec=time.monotonic() - start,
            pane_id=pane.pane_id,
        )

    def _parse_verdict(
        self,
        *,
        capture: str,
        pane: _Pane,
        state: _ReviewState,
        start: float,
    ) -> Verdict:
        matches = list(_VERDICT_PATTERN.finditer(capture))
        if matches:
            last = matches[-1]
            kind = last.group(1).upper()
        else:
            fuzzy = list(_FUZZY_VERDICT_PATTERN.finditer(capture))
            if not fuzzy:
                return Verdict(
                    kind="ESCALATE",
                    rationale="no VERDICT sentinel in capture",
                    iteration=state.iterations,
                    raw_capture=capture,
                    duration_sec=time.monotonic() - start,
                    pane_id=pane.pane_id,
                )
            kind = _canonicalize_fuzzy_verdict(fuzzy[-1].group(1))
            last = fuzzy[-1]
        rationale = capture[last.end():].strip()
        return Verdict(
            kind=kind,  # type: ignore[arg-type]
            rationale=rationale,
            iteration=state.iterations,
            raw_capture=capture,
            duration_sec=time.monotonic() - start,
            pane_id=pane.pane_id,
        )

    # ── prompt assembly ─────────────────────────────────────────────────

    def _render_prompt(self, isa_path: Path, isa_text: str, diff_payload: str) -> str:
        return (
            f"You are reviewing the diff produced by a Codex session for the ISA at {isa_path}.\n"
            f"Lineage: GPT-5.5 (via the openai-codex backend) wrote the code in a per-thread\n"
            f"worktree; you are the auto-reviewer checking what the implementer missed.\n\n"
            f"## ISA\n\n{isa_text}\n\n"
            f"## Diff\n\n```diff\n{diff_payload}\n```\n\n"
            f"## How to reply\n\n"
            f"On a line by itself, write exactly:\n\n"
            f"    {_VERDICT_SENTINEL} APPROVE\n\n"
            f"or `{_VERDICT_SENTINEL} REVISE` or `{_VERDICT_SENTINEL} ESCALATE`. "
            f"Then provide your rationale on the lines that follow.\n\n"
            f"- **APPROVE**: the diff matches the ISA's stated goals + ISCs, has no obvious "
            f"correctness/security regressions, and is mergeable as-is.\n"
            f"- **REVISE**: there are addressable issues. List each one concretely so the "
            f"implementer can fix and re-submit.\n"
            f"- **ESCALATE**: the diff has fundamental problems or scope drift that need "
            f"human triage. Explain why.\n\n"
            f"The `{_VERDICT_SENTINEL}` token must appear in your reply exactly once and only "
            f"on the verdict line; the orchestrator looks for it to parse your decision.\n"
        )

    def _maybe_summarize_diff(self, diff: str) -> tuple[str, bool]:
        if len(diff) <= _DIFF_SUMMARY_THRESHOLD:
            return diff, False
        # Deterministic summary: keep only diff headers + ±3 lines context.
        out_lines: list[str] = []
        kept = 0
        for line in diff.splitlines():
            if line.startswith(("--- ", "+++ ", "@@ ", "diff --git ")):
                out_lines.append(line)
                kept = 6
            elif kept > 0:
                out_lines.append(line)
                kept -= 1
        out_lines.append(
            f"\n<truncated — original diff {len(diff)} bytes summarized to "
            f"{sum(len(l) + 1 for l in out_lines)} bytes>"
        )
        return "\n".join(out_lines), True

    # ── tmux + state helpers ────────────────────────────────────────────

    def _tmux(self, *args, check: bool = False) -> subprocess.CompletedProcess:
        return self._run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            check=check,
        )

    async def _get_sid_lock(self, session_id: str) -> asyncio.Lock:
        async with self._sid_locks_guard:
            lock = self._sid_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._sid_locks[session_id] = lock
            return lock

    def _cleanup_prompt(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_session_state(self, session_id: str) -> _ReviewState:
        if not self._state_path.exists():
            return _ReviewState(day_started=self._today_utc())
        try:
            with open(self._state_path, "r+", encoding="utf-8") as fd:
                fcntl.flock(fd, fcntl.LOCK_SH)
                try:
                    raw = json.load(fd)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        except (json.JSONDecodeError, OSError):
            return _ReviewState(day_started=self._today_utc())
        entry = raw.get("sessions", {}).get(session_id, {})
        st = _ReviewState(
            iterations=int(entry.get("iterations", 0)),
            reviews_today=int(entry.get("reviews_today", 0)),
            day_started=entry.get("day_started", self._today_utc()),
            last_verdict=entry.get("last_verdict"),
            last_review_at=entry.get("last_review_at"),
        )
        # Day rollover.
        today = self._today_utc()
        if st.day_started != today:
            st.day_started = today
            st.reviews_today = 0
        return st

    def _persist_session_state(
        self,
        session_id: str,
        state: _ReviewState,
        verdict: Verdict,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Mutate state for the new verdict.
        state.last_verdict = verdict.kind
        state.last_review_at = now_iso
        state.reviews_today += 1
        if verdict.kind == "REVISE":
            state.iterations += 1
        elif verdict.kind == "APPROVE":
            state.iterations = 0
        # ESCALATE: leave iterations as-is — the operator drives reset.

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {"version": 1, "sessions": {}}
        if self._state_path.exists():
            try:
                with open(self._state_path, "r", encoding="utf-8") as fd:
                    existing = json.load(fd)
            except (json.JSONDecodeError, OSError):
                pass
        existing.setdefault("sessions", {})[session_id] = {
            "iterations": state.iterations,
            "reviews_today": state.reviews_today,
            "day_started": state.day_started,
            "last_verdict": state.last_verdict,
            "last_review_at": state.last_review_at,
        }
        tmp_path = self._state_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                json.dump(existing, fd, indent=2)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        tmp_path.replace(self._state_path)

    def _escalate_no_pane(
        self,
        session_id: str,
        state: _ReviewState,
        start: float,
        *,
        rationale: str,
    ) -> Verdict:
        verdict = Verdict(
            kind="ESCALATE",
            rationale=rationale,
            iteration=state.iterations,
            raw_capture="",
            duration_sec=time.monotonic() - start,
            pane_id="(no pane)",
        )
        # ESCALATE-no-pane still counts toward reviews_today per spec
        # ("cap is on Opus pane time, not on APPROVE outcomes" — see ISA).
        self._persist_session_state(session_id, state, verdict)
        return verdict


# ── module helpers ───────────────────────────────────────────────────────


def _canonicalize_fuzzy_verdict(raw: str) -> str:
    """Map edit-distance-1 misspellings back to the three canonical verdicts."""
    upper = raw.upper()
    if upper.startswith("APP") or upper.startswith("APR"):
        return "APPROVE"
    if upper.startswith("REV") or upper.startswith("REW"):
        return "REVISE"
    return "ESCALATE"
