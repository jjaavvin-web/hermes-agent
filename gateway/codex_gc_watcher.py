"""Codex GC Watcher — periodic broker.gc() + broker.reap_deleted() tick.

P5 shipped :meth:`WorktreeBroker.gc` and :meth:`WorktreeBroker.reap_deleted`
but never wired them into a background interval — the operator had to
call them by hand.  This module is the missing wire-up.

Once per ``poll_interval_sec`` (default 3600 s = 1 hour) it:

1. Loads ``codex_sessions.json`` for the live ``tracked_sids`` set.
2. Calls ``broker.gc(tracked_sids=...)``.  gc renames each orphan
   into ``~/.hermes/codex-wt/.deleted-<ts>/<sid>/`` — never a real
   delete.
3. Calls ``broker.reap_deleted(max_age_days=7)``.  reaper purges the
   ``.deleted-<ts>`` buckets older than 7 days — the recovery margin
   if gc was too eager.

The watcher does NOT pass ``live_branches`` to gc on first ship: the
``tracked_sids`` set alone is sufficient defense against accidentally
nuking an in-flight session (any session with a row in
``codex_sessions.json`` is tracked).  Adding a ``gh pr list`` lookup
to populate ``live_branches`` is a defense-in-depth tombstone — not
load-bearing for correctness.

Per WORKFLOW-LESSONS §3 rule 5: this module performs NO direct
filesystem deletion of any kind.  All deletion is delegated to
:meth:`WorktreeBroker.reap_deleted`, which is itself scoped
exclusively to the ``.deleted-<ts>`` namespace gc owns.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


class CodexGcWatcher:
    """Background task that periodically sweeps worktree orphans.

    Mirrors :class:`gateway.codex_phase_watcher.CodexPhaseWatcher` and
    :class:`gateway.codex_merge_watcher.CodexMergeWatcher` so the adapter
    start/stop wire-up stays symmetric.
    """

    def __init__(
        self,
        *,
        dispatcher: Any,
        worktree_broker: Any,
        poll_interval_sec: float = 3600.0,
        reap_max_age_days: int = 7,
    ) -> None:
        self._dispatcher = dispatcher
        self._broker = worktree_broker
        self._poll_interval = poll_interval_sec
        self._reap_max_age_days = reap_max_age_days
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="codex-gc-watcher")
        log.info(
            "CodexGcWatcher started (interval=%.1fs, reap_max_age=%dd)",
            self._poll_interval, self._reap_max_age_days,
        )

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
        log.info("CodexGcWatcher stopped")

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("CodexGcWatcher tick crashed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        """One sweep cycle: collect tracked sids, gc orphans, reap old buckets.

        Both broker calls are isolated in their own try/except — a
        crash in gc must not skip the reap, and vice versa.  The
        broker methods are synchronous and quick; no executor needed.
        """
        state = self._dispatcher._load_state()
        tracked_sids: set[str] = set()
        for row in state.get("sessions", {}).values():
            sid = row.get("session_id")
            if sid:
                tracked_sids.add(sid)

        try:
            actions = self._broker.gc(tracked_sids=tracked_sids)
            if actions:
                log.info(
                    "CodexGcWatcher: gc renamed %d orphan(s) to .deleted-<ts>",
                    len(actions),
                )
        except Exception as exc:
            log.warning("CodexGcWatcher: broker.gc failed: %s", exc)

        try:
            purged = self._broker.reap_deleted(max_age_days=self._reap_max_age_days)
            if purged:
                log.info(
                    "CodexGcWatcher: reap_deleted purged %d expired bucket(s)",
                    purged,
                )
        except Exception as exc:
            log.warning("CodexGcWatcher: broker.reap_deleted failed: %s", exc)
