"""Codex registry GC — archive-first retirement of terminal session rows (C7 / Gate 7).

Why this module exists
----------------------
C7 makes terminal session rows **tombstones**: the reaper no longer pops a row
out of ``codex_sessions.json`` when it releases a worktree, it rewrites the row
to ``RELEASED`` and leaves it there.  That is what stops
:meth:`gateway.codex_session_dispatcher.CodexSessionDispatcher.discover_threads`
from re-materialising a fresh session for a thread that was already reaped
(the resurrection loop diagnosed in the C7 recon brief §3 defect 2).

Tombstones therefore accumulate forever unless something retires them — this
module.  It is deliberately a separate, dependency-free unit so it can be
reasoned about and tested without importing the dispatcher, the broker, or
Discord.

The archive-first invariant
---------------------------
A terminal row older than ``max_terminal_age_days`` is retired in exactly this
order, and never any other:

1. append the **full row** (plus GC provenance) as one JSON line to
   ``~/.hermes/state/codex-reaper/tombstone-archive.jsonl``;
2. ``flush()`` + ``os.fsync()`` that append so it survives a crash;
3. only then delete the row from ``codex_sessions.json``.

If step 1 or 2 fails the row is **left in the registry** — we never delete an
unarchived row.  A crash between 2 and 3 leaves a row that is both archived and
present, which the next tick resolves idempotently (the archive is keyed by
thread_id and re-appending is harmless).

Because the archive retains every retired ``thread_id``,
:func:`load_archived_thread_ids` lets ``discover_threads`` keep refusing those
threads long after the registry row itself is gone — the tombstone's protection
outlives the tombstone.

Quarantine is never collected
-----------------------------
C7 blocker B3.  ``ORPHANED`` is *quarantine*-terminal: the reaper parks a row
there whenever a safety probe came back inconclusive or custody was unprovable,
and both the reaper docstring and the gc watcher promise such a row is kept
until a human looks at it.  This module honours that promise — rows in
:data:`gateway.codex_session_dispatcher.QUARANTINE_STATES` are examined,
reported, and always ``kept``.  Only genuinely completed terminal states
(:data:`gateway.codex_session_dispatcher.GC_ELIGIBLE_TERMINAL_STATES`) age out.

Age resolution fails SAFE
-------------------------
"Terminal age" is read from the first parseable timestamp in
:data:`_TERMINAL_TS_FIELDS`, and every field in that tuple is a timestamp some
writer stamps **at the moment the row goes terminal**.  Non-terminal
timestamps such as ``created_at`` and ``last_message_at`` are deliberately NOT
fallbacks: they predate the transition by an arbitrary amount, so using them
would age a row out of the registry on a clock that never measured its terminal
life (C7 blocker B2).  A row whose terminal age cannot be established is
**kept**, never retired: an unknown age must never authorise a delete.

Writes re-read and merge
------------------------
C7 HIGH-3.  ``codex_sessions.json`` is owned by the live gateway, which writes
it continuously.  Loading the whole file, popping rows across N fsync'd archive
appends and writing the whole snapshot back would clobber every concurrent edit
made in between.  Each retirement is therefore its own read-modify-write against
the current file, re-verifying the row's identity and state before deleting it —
the same discipline :mod:`gateway.codex_session_reaper` uses.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)

#: Terminal rows younger than this are retained in the live registry.
DEFAULT_MAX_TERMINAL_AGE_DAYS = 90

#: States that are terminal but must never be garbage-collected (C7 blocker B3).
#: Mirrors :data:`gateway.codex_session_dispatcher.QUARANTINE_STATES`, duplicated
#: as a literal so this module keeps its no-gateway-imports property; the two are
#: pinned equal by test.
DEFAULT_QUARANTINE_STATES = frozenset({"ORPHANED"})

#: Candidate timestamp fields, most authoritative first.  Every entry is a
#: timestamp that some writer stamps **at the moment a row goes terminal** —
#: audited against every terminal transition in the gateway:
#:
#: =============================================  ============  ==============
#: writer                                          state         field
#: =============================================  ============  ==============
#: ``codex_session_reaper._stamp_released``        RELEASED      ``released_at``
#: ``codex_session_reaper._stamp_orphaned``        ORPHANED      ``orphaned_at``
#: ``dispatcher.on_bot_restart`` (missing wt)      ORPHANED      ``orphaned_at``
#: ``dispatcher.on_pr_merged``                     COMPLETE      ``merged_at``
#: ``dispatcher.on_pr_closed_unmerged``            ESCALATED     ``closed_at``
#: ``dispatcher._apply_verdict`` (ESCALATE)        ESCALATED     ``escalated_at``
#: =============================================  ============  ==============
#:
#: ``closed_at`` was missing pre-fix (C7 blocker B2) — the dispatcher writes it
#: for every PR that closes unmerged.  ``completed_at`` / ``terminal_at`` are
#: kept for forward compatibility with writers that adopt those names.
#:
#: ``created_at`` and ``last_message_at`` are deliberately absent: they are not
#: terminal-transition stamps, and treating them as an age basis is what made
#: pre-fix rows GC-eligible the instant they went terminal.
_TERMINAL_TS_FIELDS = (
    "released_at",
    "orphaned_at",
    "escalated_at",
    "closed_at",
    "completed_at",
    "merged_at",
    "terminal_at",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string (tolerating a trailing ``Z``) to aware UTC.

    Returns ``None`` for falsy / unparseable input so callers treat a missing
    timestamp as "no signal" rather than crashing the sweep.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def archive_path(hermes_home: Any) -> Path:
    """Canonical tombstone-archive path below ``hermes_home``."""
    return Path(hermes_home) / "state" / "codex-reaper" / "tombstone-archive.jsonl"


def load_archived_thread_ids(hermes_home: Any) -> set[str]:
    """Return every ``thread_id`` ever retired into the tombstone archive.

    Consulted by ``discover_threads`` so an archived thread can never be
    re-discovered into a fresh session.

    Tolerant by design: a truncated or malformed line is skipped (the archive
    is append-only JSONL, so a torn tail is the expected crash artefact), and a
    missing/unreadable archive yields an empty set with a warning.  A read
    failure must not wedge thread discovery — and the rows in the archive are
    >90 days terminal by construction, so the worst case of an unreadable
    archive is a cosmetic re-discovery, not lost work.
    """
    path = archive_path(hermes_home)
    thread_ids: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as fd:
            for line in fd:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                tid = entry.get("thread_id")
                if tid:
                    thread_ids.add(str(tid))
    except FileNotFoundError:
        return thread_ids
    except OSError as exc:
        log.warning("codex_registry_gc: tombstone archive unreadable (%s): %s", path, exc)
        return thread_ids
    return thread_ids


class CodexRegistryGc:
    """Retire aged terminal rows from ``codex_sessions.json``, archive-first.

    Parameters
    ----------
    dispatcher_state:
        The :class:`gateway.codex_session_dispatcher.CodexSessionDispatcher`,
        used read/write through its ``_load_state`` / ``_write_state`` helpers
        (the same private contract :class:`gateway.codex_gc_watcher.CodexGcWatcher`
        and the reaper already rely on).
    hermes_home:
        Explicit Hermes home.  When omitted it is resolved from the dispatcher's
        ``hermes_home`` / ``_hermes_home`` attribute, falling back to
        ``~/.hermes``.
    max_terminal_age_days:
        Retention window for terminal rows.  Default 90 days.
    """

    def __init__(
        self,
        dispatcher_state: Any,
        *,
        hermes_home: Any = None,
        max_terminal_age_days: int = DEFAULT_MAX_TERMINAL_AGE_DAYS,
    ) -> None:
        self._dispatcher = dispatcher_state
        self._max_age_days = max_terminal_age_days
        self._hermes_home_override = Path(hermes_home) if hermes_home else None
        self._archive_path = archive_path(self._hermes_home())

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def collect(
        self,
        *,
        terminal_states: Iterable[str],
        now: Optional[datetime] = None,
        dry_run: bool = False,
        quarantine_states: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        """Retire every collectable terminal row older than the retention window.

        ``terminal_states`` is supplied by the caller (canonically
        :data:`gateway.codex_session_dispatcher.GC_ELIGIBLE_TERMINAL_STATES`) so
        this module stays free of gateway imports and the state vocabulary keeps
        a single source of truth.

        ``quarantine_states`` names states that are terminal but must never be
        collected (canonically
        :data:`gateway.codex_session_dispatcher.QUARANTINE_STATES`).  A row in
        one of them is examined and reported, but always ``kept`` — even if the
        caller also listed it in ``terminal_states``.  This is belt-and-braces
        for C7 blocker B3: quarantine survives a caller passing the whole
        terminal vocabulary by mistake.

        Returns one decision dict per examined terminal row.  With
        ``dry_run=True`` nothing is archived and nothing is deleted; the
        decisions still describe exactly what would happen.
        """
        terminal = {str(s) for s in terminal_states}
        quarantine = {
            str(s) for s in (
                quarantine_states if quarantine_states is not None
                else DEFAULT_QUARANTINE_STATES
            )
        }
        now = now or datetime.now(timezone.utc)
        cutoff_sec = self._max_age_days * 86400

        # Snapshot only to *choose* candidates.  Every delete below re-reads the
        # registry (HIGH-3) so a concurrent gateway write is never clobbered.
        sessions = self._dispatcher._load_state().get("sessions", {})

        decisions: list[dict] = []
        retired = 0

        for thread_id, row in list(sessions.items()):
            row_state = row.get("state") if isinstance(row, dict) else None
            if not isinstance(row, dict) or row_state not in (terminal | quarantine):
                continue

            field, since = self._terminal_since(row)
            decision: dict = {
                "ts": _now_iso(),
                "thread_id": thread_id,
                "session_id": row.get("session_id"),
                "state": row_state,
                "age_field": field,
                "dry_run": dry_run,
            }

            # ---- B3: quarantine is never collected ---------------------- #
            if row_state in quarantine:
                decision["outcome"] = "kept"
                decision["quarantined"] = True
                decision["reason"] = (
                    f"{row_state} is quarantine-terminal — retained until an "
                    "operator dispositions it, never garbage-collected"
                )
                decisions.append(decision)
                continue

            if since is None:
                # Unknown age must never authorise a delete.
                decision["outcome"] = "kept"
                decision["reason"] = "terminal age unresolvable — no parseable timestamp"
                decisions.append(decision)
                continue

            age_sec = (now - since).total_seconds()
            decision["age_days"] = round(age_sec / 86400, 2)
            if age_sec < cutoff_sec:
                decision["outcome"] = "kept"
                decision["reason"] = (
                    f"terminal {age_sec / 86400:.1f}d < retention {self._max_age_days}d"
                )
                decisions.append(decision)
                continue

            decision["reason"] = (
                f"terminal {age_sec / 86400:.1f}d >= retention {self._max_age_days}d"
            )

            if dry_run:
                decision["outcome"] = "would_retire"
                decisions.append(decision)
                continue

            # ---- archive FIRST, delete only on a durable append ---------- #
            if not self._archive_row(thread_id, row, decision["reason"]):
                decision["outcome"] = "kept"
                decision["reason"] = "archive append failed — row retained (never delete unarchived)"
                decisions.append(decision)
                continue

            # ---- HIGH-3: re-read + merge, never write back a snapshot ---- #
            deleted, drift = self._delete_row(
                thread_id,
                expect_state=row_state,
                expect_sid=row.get("session_id"),
            )
            if not deleted:
                decision["outcome"] = "kept"
                decision["reason"] = (
                    f"row changed under us during archive ({drift}) — "
                    "delete abandoned; the archive entry is harmless and the "
                    "next tick re-evaluates"
                )
                decisions.append(decision)
                continue

            retired += 1
            decision["outcome"] = "retired"
            decisions.append(decision)

        if retired:
            log.info(
                "codex_registry_gc: retired %d terminal row(s) to %s",
                retired, self._archive_path,
            )

        return decisions

    def archived_thread_ids(self) -> set[str]:
        """Convenience wrapper around :func:`load_archived_thread_ids`."""
        return load_archived_thread_ids(self._hermes_home())

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _terminal_since(self, row: dict) -> tuple[Optional[str], Optional[datetime]]:
        """Resolve when the row went terminal.  ``(field, when)`` or ``(None, None)``."""
        for field in _TERMINAL_TS_FIELDS:
            parsed = _parse_iso(row.get(field))
            if parsed is not None:
                return field, parsed
        return None, None

    def _delete_row(
        self,
        thread_id: str,
        *,
        expect_state: Any,
        expect_sid: Any,
    ) -> tuple[bool, Optional[str]]:
        """Re-read the registry and delete one row.  ``(deleted, drift_reason)``.

        C7 HIGH-3.  The registry belongs to the live gateway; between choosing
        this row and finishing its fsync'd archive append, the gateway may have
        rewritten the file (a new session, a state transition, a message stamp).
        Writing back a whole snapshot taken before the append would silently
        revert all of it, which is precisely the hazard the reaper's own
        docstring warns about.  So: re-read, verify the row is still the row we
        archived, delete only that key, write back the *fresh* state.
        """
        state = self._dispatcher._load_state()
        sessions = state.get("sessions", {})
        row = sessions.get(thread_id)
        if not isinstance(row, dict):
            return False, "row disappeared from the registry"
        if row.get("state") != expect_state:
            return False, f"state changed ({expect_state!r} -> {row.get('state')!r})"
        if row.get("session_id") != expect_sid:
            return False, (
                f"session_id changed ({expect_sid!r} -> {row.get('session_id')!r})"
            )
        sessions.pop(thread_id, None)
        self._dispatcher._write_state(state)
        return True, None

    def _archive_row(self, thread_id: str, row: dict, reason: str) -> bool:
        """Append the full row to the archive and fsync it.  True on success.

        The entry carries the complete row verbatim so the archive is a real
        restore source, not a summary.
        """
        entry = {
            "archived_at": _now_iso(),
            "thread_id": thread_id,
            "session_id": row.get("session_id"),
            "state": row.get("state"),
            "reason": reason,
            "row": row,
        }
        try:
            self._archive_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._archive_path, "a", encoding="utf-8") as fd:
                fd.write(json.dumps(entry, sort_keys=True) + "\n")
                fd.flush()
                os.fsync(fd.fileno())
        except (OSError, TypeError, ValueError) as exc:
            log.warning(
                "codex_registry_gc: archive append failed for thread %s: %s",
                thread_id, exc,
            )
            return False
        return True

    def _hermes_home(self) -> Path:
        """Resolve Hermes home from the override, the dispatcher, or the default.

        Only str/Path attribute values are accepted so a test double's auto-created
        mock attribute cannot poison the path resolution.
        """
        if self._hermes_home_override is not None:
            return self._hermes_home_override
        for attr in ("hermes_home", "_hermes_home"):
            home = getattr(self._dispatcher, attr, None)
            if isinstance(home, (str, Path)):
                return Path(home)
        # Canonical resolver, not ``Path.home() / ".hermes"`` — see the same
        # note in ``codex_session_reaper._hermes_home``.  This one guards the
        # tombstone archive, which no test has reached yet only because every
        # current caller happens to pass ``hermes_home=``.
        return get_hermes_home()
