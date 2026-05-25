"""Codex Session Events — async iterator emitting session row deltas.

Polls ``~/.hermes/codex_sessions.json`` (the dispatcher's source of
truth, see :mod:`gateway.codex_session_dispatcher`) and yields one
``{'kind': 'codex-session', ...}`` event per observed row change.

Why polling, not a pub/sub callback on the dispatcher:

- The dispatcher writes ``codex_sessions.json`` atomically via
  ``_write_state`` — every state mutation hits the file.  An mtime
  + diff scan catches every transition without needing to plumb a
  callback through every write site.
- Polling is loosely coupled — the SSE stream can consume this iter
  without the dispatcher knowing it exists.  Easier to test, easier
  to add a second consumer later.
- The 2 s default interval is more than fast enough for the human
  cadence of state transitions (one per Discord message / phase
  edit / PR transition).

Event shape (yielded dicts):

- ``appeared``: first time observing ``thread_id``.
- ``changed``: one of (state, isa_phase, pr_state, pr_number) differs
  from the cached snapshot.  ``changes`` maps each changed key to
  ``{'from': ..., 'to': ...}``.
- ``removed``: ``thread_id`` was present last tick, absent now.

The iter is hot-restart safe: on cold start, the first observed
session set emits ``appeared`` events.  That is intentional — the
SPA tab uses those to populate the initial list rather than separately
calling ``/api/codex-sessions``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SEC = 2.0

# Fields whose change we want to surface.  Order matters for stable
# event keys in the SPA tab; keep this set tight — adding a "noisy"
# field (e.g. ``last_message_at``) would flood the stream.
_WATCHED_FIELDS: tuple[str, ...] = (
    "state",
    "isa_phase",
    "pr_state",
    "pr_number",
    "pr_url",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_sessions(sessions_path: Path) -> Optional[dict]:
    """Return parsed ``codex_sessions.json`` or None on read/parse failure."""
    try:
        return json.loads(sessions_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("codex_session_events: read/parse failed: %s", exc)
        return None


def _row_snapshot(row: dict) -> dict:
    """Project a session row down to the watched fields (cache shape)."""
    return {key: row.get(key) for key in _WATCHED_FIELDS}


def _diff_changes(prev: dict, cur: dict) -> dict:
    """Return ``{field: {'from': ..., 'to': ...}}`` for any watched key that differs."""
    changes: dict = {}
    for key in _WATCHED_FIELDS:
        a = prev.get(key)
        b = cur.get(key)
        if a != b:
            changes[key] = {"from": a, "to": b}
    return changes


async def codex_session_events_iter(
    sessions_path: Path,
    *,
    stop_event: asyncio.Event | None = None,
    poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
) -> AsyncIterator[dict]:
    """Async iterator over codex session row deltas.

    Yields one dict per change.  Cooperative cancellation via
    ``stop_event``; otherwise loops indefinitely.
    """
    cached: dict[str, dict] = {}  # thread_id -> snapshot of watched fields
    last_mtime: float = 0.0

    while True:
        if stop_event is not None and stop_event.is_set():
            return

        await asyncio.sleep(poll_interval_sec)

        if stop_event is not None and stop_event.is_set():
            return

        try:
            mtime = sessions_path.stat().st_mtime
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("codex_session_events: stat failed: %s", exc)
            continue

        if mtime == last_mtime:
            continue
        last_mtime = mtime

        data = _read_sessions(sessions_path)
        if data is None:
            continue

        sessions = data.get("sessions", {})
        if not isinstance(sessions, dict):
            continue

        now_iso = _now_iso()
        seen_threads: set[str] = set()

        for thread_id, row in sessions.items():
            if not isinstance(row, dict):
                continue
            seen_threads.add(thread_id)
            snap = _row_snapshot(row)
            prev = cached.get(thread_id)
            sid = row.get("session_id")

            if prev is None:
                cached[thread_id] = snap
                yield {
                    "kind": "codex-session",
                    "type": "appeared",
                    "thread_id": thread_id,
                    "sid": sid,
                    "state": row.get("state"),
                    "isa_phase": row.get("isa_phase"),
                    "pr_number": row.get("pr_number"),
                    "pr_url": row.get("pr_url"),
                    "ts": now_iso,
                }
                await asyncio.sleep(0)
                continue

            changes = _diff_changes(prev, snap)
            if changes:
                cached[thread_id] = snap
                yield {
                    "kind": "codex-session",
                    "type": "changed",
                    "thread_id": thread_id,
                    "sid": sid,
                    "state": row.get("state"),
                    "changes": changes,
                    "ts": now_iso,
                }
                await asyncio.sleep(0)

        # Removed rows.
        for thread_id in list(cached.keys()):
            if thread_id not in seen_threads:
                prev = cached.pop(thread_id)
                yield {
                    "kind": "codex-session",
                    "type": "removed",
                    "thread_id": thread_id,
                    "last_state": prev.get("state"),
                    "ts": now_iso,
                }
                await asyncio.sleep(0)
