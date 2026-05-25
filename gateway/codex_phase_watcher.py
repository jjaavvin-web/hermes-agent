"""Codex Phase Watcher — polls tracked sessions' ISA files for transitions.

The Codex Session Dispatcher (P1) is event-driven on Discord hooks. The
ISA is the session's plan document, mutated by the Codex worker itself
when it transitions between scaffold → execute → verify → complete. The
phase watcher closes the gap: it polls each tracked session's ISA on a
configurable interval and fires a callback when ``phase`` transitions
into a watched value (in P2.5 scope, just ``verify``).

Why polling, not file-system events:

- The ISA files live under ``~/.hermes/work/<isa_id>/`` which may be
  on a network mount or a WSL2 9P share where inotify is unreliable.
- Codex workers write ISA updates non-atomically (the ISA-SPEC §10
  rescue-automation can mid-write the file), so an inotify event
  for a partial write is noise. Polling reads a known-good state.
- The cadence we care about (30 s) is far slower than any inotify
  throttle would buy us.

Design parallels:

- The dispatcher's ``codex_sessions.json`` is the source of truth for
  which sessions exist. The watcher derives its targets from there
  (one cycle per tick = stat-then-parse for each row).
- Last-seen phase per sid lives in-process. On bot restart the watcher
  rehydrates from each session row's ``isa_phase`` field (also
  written by the dispatcher on transition).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# Reused YAML-ish frontmatter parser. Lifted from scripts.isa_common but
# trimmed to the one field we need; importing scripts/ as a package is
# brittle since it's not declared as one.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_PHASE_RE = re.compile(r"^\s*phase:\s*(\S+)\s*$", re.MULTILINE)


def _read_phase(isa_path: Path) -> Optional[str]:
    """Return the frontmatter ``phase`` of an ISA, or None if unreadable."""
    if not isa_path.exists() or not isa_path.is_file():
        return None
    try:
        text = isa_path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return None
    phase_match = _PHASE_RE.search(fm_match.group(1))
    if not phase_match:
        return None
    return phase_match.group(1).strip()


class CodexPhaseWatcher:
    """Background task that fires callbacks on ISA phase transitions."""

    def __init__(
        self,
        *,
        dispatcher: "Any",  # noqa: F821 — CodexSessionDispatcher (avoid circular import)
        on_phase_verify: Callable[[str], Awaitable[None]],
        poll_interval_sec: float = 30.0,
    ) -> None:
        self._dispatcher = dispatcher
        self._on_phase_verify = on_phase_verify
        self._poll_interval = poll_interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Last-seen phase per thread_id; rehydrated from dispatcher rows on start.
        self._last_phase: dict[str, str] = {}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # Rehydrate from current dispatcher state so we don't re-trigger
        # verify for a session whose ISA was already at ``verify`` last time
        # the bot was up (e.g. across a gateway restart).
        state = self._dispatcher._load_state()
        for thread_id, row in state.get("sessions", {}).items():
            recorded = row.get("isa_phase")
            if recorded:
                self._last_phase[thread_id] = recorded
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="codex-phase-watcher")
        log.info("CodexPhaseWatcher started (interval=%.1fs)", self._poll_interval)

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:  # pragma: no cover — defensive
                pass
        self._task = None
        self._stop_event = None
        log.info("CodexPhaseWatcher stopped")

    async def _run(self) -> None:
        """Main loop. Exits when ``_stop_event`` is set."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("CodexPhaseWatcher tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        """One poll cycle: read each tracked session's ISA, fire on transitions."""
        state = self._dispatcher._load_state()
        for thread_id, row in state.get("sessions", {}).items():
            isa_path = Path(row.get("isa_path", ""))
            current = _read_phase(isa_path)
            if current is None:
                continue
            last = self._last_phase.get(thread_id)
            self._last_phase[thread_id] = current
            if last == current:
                continue
            log.info(
                "CodexPhaseWatcher: thread %s phase %s -> %s",
                thread_id, last, current,
            )
            # Persist the new phase on the row so on_bot_restart's
            # rehydration sees the same value next time.
            self._persist_phase(thread_id, current)
            if current == "verify" and last != "verify":
                try:
                    await self._on_phase_verify(thread_id)
                except Exception as exc:
                    log.error(
                        "CodexPhaseWatcher: on_phase_verify failed for %s: %s",
                        thread_id, exc,
                    )

    def _persist_phase(self, thread_id: str, phase: str) -> None:
        """Write the new phase onto the dispatcher row so we don't double-fire."""
        try:
            state = self._dispatcher._load_state()
            row = state.get("sessions", {}).get(thread_id)
            if row is None:
                return
            row["isa_phase"] = phase
            self._dispatcher._write_state(state)
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "CodexPhaseWatcher: persist_phase failed for %s: %s",
                thread_id, exc,
            )
