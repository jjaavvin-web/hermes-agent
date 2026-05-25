"""Codex Merge Watcher — polls open PRs for ``MERGING`` codex sessions.

P3 hands the diff to ``MergeBroker.merge()`` which opens a PR with an
``auto-merge`` (or ``needs-human``) label.  Mergify / the alternate
Actions workflow then merges the PR server-side.  That leaves a gap:
the dispatcher's session row stays at ``state: MERGING`` indefinitely
unless something watches the PR and closes the loop.

This module is that watcher.  Once per ``poll_interval_sec`` (default
60 s) it:

1. Loads ``codex_sessions.json``.
2. For each row in ``state: MERGING`` that has a ``pr_number``, calls
   ``gh pr view <num> --json state,mergedAt,mergeCommit,closedAt``.
3. On the first transition out of OPEN, fires
   ``dispatcher.on_pr_merged(thread_id, pr_meta)`` or
   ``dispatcher.on_pr_closed_unmerged(thread_id, pr_meta)``.

Design parallels with :class:`CodexPhaseWatcher`:

- Targets are derived from ``codex_sessions.json`` per tick — the
  dispatcher's row set is the source of truth.
- Last-seen PR state is cached in-process per ``thread_id`` and
  rehydrated from each row's ``pr_state`` field on ``start()`` so a
  gateway restart does not re-fire the transition.

The watcher only OBSERVES.  It never invokes any mutating ``gh``
subcommand itself — Mergify and the operator own merge decisions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# ``gh pr view`` should be quick (<5 s in practice).  Cap it so a hung
# call cannot stall the watcher loop.
_GH_TIMEOUT_SEC = 30


def _gh_pr_view(pr_number: int) -> Optional[dict]:
    """Return ``gh pr view`` JSON for ``pr_number`` or None on any failure.

    Failure modes that legitimately return None: PR doesn't exist (gh
    exits non-zero), gh times out, gh isn't on PATH, network blip.
    Each is logged as a warning; the next tick will retry.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view", str(pr_number),
                "--json", "state,mergedAt,mergeCommit,closedAt,url,number",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        log.warning("CodexMergeWatcher: gh CLI not on PATH; watcher inert")
        return None
    except subprocess.TimeoutExpired:
        log.warning("CodexMergeWatcher: gh pr view %s timed out", pr_number)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("CodexMergeWatcher: gh pr view %s crashed: %s", pr_number, exc)
        return None
    if result.returncode != 0:
        log.warning(
            "CodexMergeWatcher: gh pr view %s exit %s: %s",
            pr_number, result.returncode, result.stderr.strip(),
        )
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.warning("CodexMergeWatcher: gh pr view %s JSON parse: %s", pr_number, exc)
        return None


def _classify_pr_state(payload: dict) -> str:
    """Reduce a ``gh pr view`` payload to ``OPEN`` | ``MERGED`` | ``CLOSED``.

    GitHub's PR ``state`` field is ``OPEN`` / ``CLOSED`` / ``MERGED``.
    The CLI returns ``MERGED`` distinctly, but defensively we also
    treat ``state == CLOSED`` + non-null ``mergedAt`` as MERGED.
    """
    state = (payload.get("state") or "").upper()
    if state == "MERGED" or payload.get("mergedAt"):
        return "MERGED"
    if state == "CLOSED":
        return "CLOSED"
    return "OPEN"


class CodexMergeWatcher:
    """Background task that watches open PRs and fires transition callbacks.

    Mirrors :class:`gateway.codex_phase_watcher.CodexPhaseWatcher` so the
    adapter's start / stop wire-up is symmetric.
    """

    def __init__(
        self,
        *,
        dispatcher: Any,
        on_pr_merged: Callable[[str, dict], Awaitable[None]],
        on_pr_closed_unmerged: Callable[[str, dict], Awaitable[None]],
        poll_interval_sec: float = 60.0,
        gh_pr_view: Callable[[int], Optional[dict]] | None = None,
    ) -> None:
        """
        ``gh_pr_view`` is injectable for testing.  Default is the real
        subprocess call above.
        """
        self._dispatcher = dispatcher
        self._on_pr_merged = on_pr_merged
        self._on_pr_closed_unmerged = on_pr_closed_unmerged
        self._poll_interval = poll_interval_sec
        self._gh_pr_view = gh_pr_view or _gh_pr_view
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Last-seen pr_state per thread_id; rehydrated from row on start.
        self._last_pr_state: dict[str, str] = {}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # Rehydrate from current dispatcher state so a row whose PR was
        # already at MERGED last tick does not re-fire on_pr_merged.
        state = self._dispatcher._load_state()
        for thread_id, row in state.get("sessions", {}).items():
            recorded = row.get("pr_state")
            if recorded:
                self._last_pr_state[thread_id] = recorded
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="codex-merge-watcher")
        log.info("CodexMergeWatcher started (interval=%.1fs)", self._poll_interval)

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
        log.info("CodexMergeWatcher stopped")

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("CodexMergeWatcher tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        """One poll cycle: check every MERGING row's PR; fire on transitions."""
        state = self._dispatcher._load_state()
        for thread_id, row in state.get("sessions", {}).items():
            if row.get("state") != "MERGING":
                continue
            pr_number = row.get("pr_number")
            if not pr_number:
                # MergeBroker crashed before opening the PR — operator triage.
                continue
            payload = self._gh_pr_view(int(pr_number))
            if payload is None:
                continue
            current = _classify_pr_state(payload)
            last = self._last_pr_state.get(thread_id)
            self._last_pr_state[thread_id] = current
            self._persist_pr_state(thread_id, current)
            if current == last:
                continue
            log.info(
                "CodexMergeWatcher: thread %s pr_state %s -> %s",
                thread_id, last, current,
            )
            if current == "MERGED":
                try:
                    await self._on_pr_merged(thread_id, payload)
                except Exception as exc:
                    log.error(
                        "CodexMergeWatcher: on_pr_merged failed for %s: %s",
                        thread_id, exc,
                    )
            elif current == "CLOSED":
                try:
                    await self._on_pr_closed_unmerged(thread_id, payload)
                except Exception as exc:
                    log.error(
                        "CodexMergeWatcher: on_pr_closed_unmerged failed for %s: %s",
                        thread_id, exc,
                    )

    def _persist_pr_state(self, thread_id: str, pr_state: str) -> None:
        """Write the observed PR state onto the row for cross-restart cache."""
        try:
            state = self._dispatcher._load_state()
            row = state.get("sessions", {}).get(thread_id)
            if row is None:
                return
            row["pr_state"] = pr_state
            self._dispatcher._write_state(state)
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "CodexMergeWatcher: persist_pr_state failed for %s: %s",
                thread_id, exc,
            )
