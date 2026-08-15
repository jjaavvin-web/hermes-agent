"""Codex session reaper — reclaim zombie CLAIMED/EXECUTING sessions (DISP-4 / ARCH-4, C7).

Background
----------
Audit DISP-4/ARCH-4 found 42 zombie codex sessions (~3.6 GB of stale worktrees)
lingering in ``codex_sessions.json`` as CLAIMED/EXECUTING long after the work
stopped.  :class:`gateway.codex_gc_watcher.CodexGcWatcher` only sweeps worktree
*directories* whose sid is no longer tracked by any row; it never tears down a
row that is *still tracked* but dead.  This reaper closes that gap row-side.

C7 / Gate 7 rebuild
-------------------
The original module was built dark and, on the evidence of the C7 recon brief
§3, could not safely be armed.  Four defects are closed here:

* **it deleted the row** on release, and ``discover_threads`` then recreated a
  fresh session for the same Discord thread on the next restart — the reap
  undid itself.  Releases now write a **RELEASED tombstone** (see
  :data:`gateway.codex_session_dispatcher.TERMINAL_STATES`) carrying a custody
  receipt, and the row is retired only by
  :mod:`gateway.codex_registry_gc` after 90 days, archive-first;
* the release decision leaned on a **"no commits since created_at"** probe,
  which says nothing about whether work would be *lost*.  It is replaced by
  real custody: ``git rev-list HEAD --not --remotes`` must be **empty**, i.e.
  every commit in the worktree already exists on a remote;
* the open-PR lookup **failed open** — a ``gh`` timeout degraded to "no open
  PRs", which reads as "nothing to protect".  It now fails **closed**: a failed
  lookup skips every release for the whole tick;
* nothing checked whether a **live process** was sitting in the worktree.

Protections (all must pass; any inconclusive probe skips or quarantines,
never releases)
---------------------------------------------------------------------------
0. **Identity** — a row with no ``session_id`` is refused outright.  Without a
   sid the branch reconstructs to ``""`` (so the open-PR guard can never
   match), the process-owner scan has no worktree to key on, and
   ``release_nonforce("")`` resolves to the codex-wt *root*.  Every downstream
   guard is silently disarmed, so the row is never evaluated at all.
1. **Idle** — ``reap_idle_hours`` (default 240h = 10 days, the pre-C7 window)
   since ``last_message_at``.  A timestamp that is *present but unparseable* is
   fail-closed: the row is never idle.  Reading a malformed ``last_message_at``
   as "no signal" and falling through to ``created_at`` would let a chatty live
   session be released on the age of a row that has been talking all day.
2. **Active-state** — only CLAIMED/EXECUTING rows are considered; terminal rows
   are untouchable.
3. **Open-PR, fail-closed** — a failed/timed-out lookup skips ALL releases this
   tick; only a successful lookup showing no PR for the branch/sid clears it.
4. **Dirty** — ``git status --porcelain`` must be empty; a failed probe
   quarantines.
5. **Unique-commit custody** — ``git rev-list HEAD --not --remotes`` must be
   empty; unprovable custody (missing worktree, failed probe) quarantines.
6. **Process-owner** — no live process with cwd or an open fd inside the
   worktree, and no live tmux session named by the row.
7. **Stability** — the row and the worktree HEAD are re-read after every gate
   has passed; any drift skips, because the gates were evaluated against a
   world that no longer exists.  The re-check is repeated *at write time* (see
   :meth:`_mutate_row`): the gates run in :meth:`_decide` and the tombstone is
   stamped in :meth:`_apply`, and a row that left CLAIMED/EXECUTING in between
   must not be overwritten by a decision taken about its earlier self.
8. **Non-force disk release** — :meth:`agent.worktree_broker.WorktreeBroker.release_nonforce`
   (no ``--force``, no rmtree, no tmux kill).  A refusal downgrades the row to
   ORPHANED with the reason.  The reaper never calls the force ``release()``.

Outcomes
--------
* ``skipped``  — left exactly as-is, re-evaluated next tick.
* ``orphaned`` — quarantined: ``state = "ORPHANED"``, row and worktree both
  kept on disk, forever, until a human looks.  This is a promise the rest of
  the system keeps: :mod:`gateway.codex_registry_gc` classes ORPHANED as
  quarantine-terminal and never ages it out, and
  :class:`gateway.codex_gc_watcher.CodexGcWatcher` leaves a quarantined sid in
  ``tracked_sids`` so its worktree is never swept either.
* ``released`` — RELEASED tombstone written **first**, then the non-force disk
  release.  That order is crash-convergent: a crash in between leaves a
  tombstone plus a live directory, which the gc watcher then routes through the
  broker's ``.deleted-<ts>`` recovery bucket (7-day window).  The reverse order
  could delete the disk and lose the receipt.

Every decision is appended (JSONL) to
``~/.hermes/state/codex-reaper/reap-ledger.jsonl``.

``dry_run=True`` evaluates and ledgers every decision but performs no teardown
and no state mutation, so an operator can preview a reap safely.  It is the
**shipped default** of :class:`gateway.codex_gc_watcher.CodexGcWatcher`; going
live is a two-flag opt-in described there.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from gateway.codex_session_dispatcher import TERMINAL_STATES
from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)

# git status / rev-list probes should be near-instant on a local worktree.
# Cap defensively so a wedged worktree cannot stall the whole reap loop.
_GIT_TIMEOUT_SEC = 30

# tmux has-session is a local socket round-trip; it should never take this long.
_TMUX_TIMEOUT_SEC = 10

# States the reaper is allowed to consider for teardown.  Everything else —
# every state in TERMINAL_STATES, plus in-flight MERGING — is untouchable.
_REAPABLE_STATES = ("CLAIMED", "EXECUTING")

# A process holding more open fds than this is not scanned exhaustively; we
# cannot then prove the *absence* of an fd inside the worktree, so the
# process-owner probe reports itself inconclusive rather than lying.
_MAX_FDS_PER_PROC = 2048

# Conservative default idle window: 10 days, i.e. exactly the pre-C7 behaviour
# (``reap(reap_idle_days=10)``).  C7 blocker B1: shipping a 6-hour window as the
# built-in default would have made the first live tick a candidate for every
# session that had been quiet since breakfast.  Tightening it is an explicit
# configuration decision, never something a deploy does on its own.
DEFAULT_REAP_IDLE_HOURS = 240.0

# The row fields that define "this is still the session I decided about".
# `state` alone is not enough: on_thread_message sets EXECUTING on a row that
# is *already* EXECUTING, so a live session coming back mid-evaluation is
# invisible in the state field and shows up only in last_message_*.
_FINGERPRINT_FIELDS = (
    "session_id",
    "state",
    "last_message_id",
    "last_message_at",
    "paused",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string (tolerating a trailing ``Z``) to aware UTC.

    Returns ``None`` for falsy / unparseable input so callers can treat a
    missing timestamp as "no signal" rather than crashing the sweep.
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


class CodexSessionReaper:
    """Reclaim dead CLAIMED/EXECUTING codex sessions row-side.

    Parameters
    ----------
    dispatcher_state:
        The :class:`gateway.codex_session_dispatcher.CodexSessionDispatcher`.
        Used read/write via its private ``_load_state`` / ``_write_state``
        helpers (same contract :class:`gateway.codex_gc_watcher.CodexGcWatcher`
        already relies on).
    broker:
        The worktree broker (``agent.worktree_broker.WorktreeBroker``).  Only
        ``broker.release_nonforce(sid)`` is ever called — the reaper does not
        have access to the force release path, by design.
    gh_open_branches_fn:
        Zero-arg callable returning the set of open-PR ``headRefName`` strings.
        If it raises, the reaper releases **nothing** for that tick (fail
        closed); the degraded lookup is recorded on every affected decision.
    """

    def __init__(
        self,
        dispatcher_state: Any,
        broker: Any,
        gh_open_branches_fn: Callable[[], set[str]],
    ) -> None:
        self._dispatcher = dispatcher_state
        self._broker = broker
        self._gh_open_branches_fn = gh_open_branches_fn
        self._ledger_path = (
            self._hermes_home() / "state" / "codex-reaper" / "reap-ledger.jsonl"
        )

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def reap(
        self,
        *,
        reap_idle_hours: float = DEFAULT_REAP_IDLE_HOURS,
        dry_run: bool = True,
        reap_idle_days: Optional[float] = None,
    ) -> list[dict]:
        """Sweep every CLAIMED/EXECUTING row once and return the decisions.

        Arguments are keyword-only on purpose: the pre-C7 signature took
        ``reap_idle_days`` as the first positional parameter, and silently
        reinterpreting a positional ``10`` from "days" to "hours" would be a
        catastrophic 40x tightening of the window.  Old callers now fail loudly.
        ``reap_idle_days`` survives as an explicit alias.

        Both defaults are the safe ones (C7 blocker B1): a 10-day window, and
        ``dry_run=True``.  A caller that wants teardown has to ask for it in so
        many words — omitting an argument can never arm this.

        Returns a list of decision dicts (also appended to the ledger).  With
        ``dry_run=True`` no teardown or state mutation happens — every decision
        still reflects what *would* happen.
        """
        if reap_idle_days is not None:
            reap_idle_hours = float(reap_idle_days) * 24.0

        state = self._dispatcher._load_state()
        sessions = state.get("sessions", {})

        # Protection 2 (active-state guard).  The explicit TERMINAL_STATES
        # exclusion is belt-and-braces against _REAPABLE_STATES ever drifting
        # into overlap with the terminal vocabulary.
        candidates: list[tuple[str, dict]] = [
            (thread_id, row)
            for thread_id, row in sessions.items()
            if isinstance(row, dict)
            and row.get("state") in _REAPABLE_STATES
            and row.get("state") not in TERMINAL_STATES
        ]
        if not candidates:
            return []

        # Protection 3 (open-PR, fail-closed) — one lookup for the whole tick.
        open_branches, pr_lookup_ok = self._open_branches()

        # Protection 6 (process-owner) — one /proc pass for the whole tick,
        # rather than re-walking every process per candidate.
        worktrees: dict[str, Path] = {}
        for _thread_id, row in candidates:
            sid = row.get("session_id") or ""
            wt = row.get("worktree_path")
            if sid and wt:
                worktrees[sid] = Path(wt)
        owners_by_sid, owner_probe_ok = self._scan_process_owners(worktrees)

        now = datetime.now(timezone.utc)
        decisions: list[dict] = []

        for thread_id, row in candidates:
            decision = self._decide(
                row=row,
                thread_id=thread_id,
                now=now,
                reap_idle_hours=reap_idle_hours,
                open_branches=open_branches,
                pr_lookup_ok=pr_lookup_ok,
                owners_by_sid=owners_by_sid,
                owner_probe_ok=owner_probe_ok,
            )
            decision["dry_run"] = dry_run

            if not dry_run:
                self._apply(decision, thread_id)

            self._append_ledger(decision)
            decisions.append(decision)

        return decisions

    # ------------------------------------------------------------------ #
    # decision logic
    # ------------------------------------------------------------------ #
    def _decide(
        self,
        *,
        row: dict,
        thread_id: str,
        now: datetime,
        reap_idle_hours: float,
        open_branches: set[str],
        pr_lookup_ok: bool,
        owners_by_sid: dict[str, list[dict]],
        owner_probe_ok: bool,
    ) -> dict:
        sid = row.get("session_id", "")
        worktree = Path(row.get("worktree_path", "")) if row.get("worktree_path") else None
        branch = self._branch_for(row)
        probes: dict[str, Any] = {}

        verdict: dict = {
            "ts": _now_iso(),
            "session_id": sid,
            "thread_id": thread_id,
            "branch": branch,
            "worktree_path": str(worktree) if worktree else None,
            "prior_state": row.get("state"),
            # The snapshot this decision is about.  _apply re-checks it at write
            # time (C7 HIGH-1) — a decision is only valid for the exact row it
            # was taken about, and a Discord message arriving mid-evaluation
            # changes last_message_* without changing `state` at all.
            "row_fingerprint": {f: row.get(f) for f in _FINGERPRINT_FIELDS},
            "probes": probes,
        }

        # ---- protection 0: identity ------------------------------------- #
        # A sid-less row disarms three separate guards at once: `branch` is ""
        # so the open-PR lookup can never match it, `_scan_process_owners` has
        # no worktree keyed to it so `owners` is always empty, and
        # `release_nonforce("")` resolves to the codex-wt ROOT rather than to
        # any one session's directory.  Refuse it before any of that can run.
        if not sid:
            probes["session_id_present"] = False
            return self._skip(
                verdict,
                "row has no session_id — refusing to evaluate (branch, "
                "process-owner and release targeting are all undefined)",
            )
        probes["session_id_present"] = True

        # ---- protection 1: idle ----------------------------------------- #
        idle, idle_block = self._idle_reason(row, now, reap_idle_hours)
        verdict["idle_reason"] = idle
        probes["idle"] = idle
        if idle_block:
            probes["idle_block"] = idle_block
            return self._skip(verdict, idle_block)
        if idle is None:
            return self._skip(verdict, "not idle")

        # ---- protection 3: open-PR guard, FAIL CLOSED -------------------- #
        # A lookup that errored tells us nothing about whether this branch is
        # under review.  Pre-C7 that degraded to an empty set, which reads as
        # "no PR protects this" — the most dangerous possible interpretation.
        probes["pr_lookup_ok"] = pr_lookup_ok
        if not pr_lookup_ok:
            return self._skip(
                verdict,
                "open-PR lookup failed — fail-closed, no releases this tick",
            )
        in_open_pr = self._branch_in_open_prs(branch, sid, open_branches)
        verdict["in_open_pr"] = in_open_pr
        probes["in_open_pr"] = in_open_pr
        if in_open_pr:
            return self._orphan(verdict, "branch has an open PR")

        # ---- protection 4: dirty guard ---------------------------------- #
        dirty = self._has_uncommitted_work(worktree)
        verdict["uncommitted_work"] = dirty
        probes["uncommitted_work"] = dirty
        if dirty:
            return self._orphan(verdict, "uncommitted work in worktree (or probe failed)")

        # ---- protection 5: unique-commit custody ------------------------ #
        branch_only, custody_ok = self._branch_only_commits(worktree)
        probes["custody_probe_ok"] = custody_ok
        probes["branch_only_commits"] = branch_only
        verdict["branch_only_commits"] = branch_only
        if not custody_ok:
            return self._orphan(
                verdict,
                "unique-commit custody unprovable (missing worktree or failed "
                "`git rev-list HEAD --not --remotes`)",
            )
        if branch_only:
            return self._orphan(
                verdict,
                f"{len(branch_only)} commit(s) exist only on this branch — "
                "not present on any remote",
            )

        # ---- protection 6: process-owner guard -------------------------- #
        probes["owner_probe_ok"] = owner_probe_ok
        if not owner_probe_ok:
            return self._skip(verdict, "process-owner probe inconclusive")
        owners = owners_by_sid.get(sid) or []
        probes["process_owners"] = owners
        if owners:
            return self._skip(
                verdict,
                f"{len(owners)} live process(es) hold cwd/fd inside the worktree",
            )
        tmux_owner, tmux_ok = self._tmux_owner(row, sid)
        probes["tmux_probe_ok"] = tmux_ok
        probes["tmux_owner"] = tmux_owner
        if not tmux_ok:
            return self._skip(verdict, "tmux ownership probe inconclusive")
        if tmux_owner:
            return self._skip(verdict, f"live tmux session {tmux_owner!r} owns this session")

        # ---- protection 7: stability guard ------------------------------ #
        # Everything above was evaluated against a snapshot.  Re-read the row
        # and HEAD; if either moved while we were probing, the gates described
        # a world that no longer exists.
        head = self._head_sha(worktree)
        probes["head"] = head
        drift = self._stability_drift(thread_id, row, worktree, head)
        probes["stability_drift"] = drift
        if drift is not None:
            return self._skip(verdict, f"state drifted during evaluation: {drift}")

        # ---- all gates clear: release with a custody receipt ------------- #
        verdict["custody_receipt"] = {
            "head": head,
            "branch": branch,
            "branch_only_commits": branch_only,
            "worktree_path": str(worktree) if worktree else None,
            "probes": dict(probes),
            "evaluated_at": _now_iso(),
        }
        verdict["outcome"] = "released"
        verdict["reason"] = (
            f"{idle}; no open PR; clean worktree; every commit on a remote; "
            "no process or tmux owner; no drift"
        )
        return verdict

    @staticmethod
    def _skip(verdict: dict, reason: str) -> dict:
        verdict["outcome"] = "skipped"
        verdict["reason"] = reason
        return verdict

    @staticmethod
    def _orphan(verdict: dict, reason: str) -> dict:
        verdict["outcome"] = "orphaned"
        verdict["reason"] = reason
        return verdict

    # ------------------------------------------------------------------ #
    # application
    # ------------------------------------------------------------------ #
    def _apply(self, decision: dict, thread_id: str) -> None:
        """Execute a non-dry-run decision.

        Each mutation is its own read-modify-write against the registry rather
        than a mutation of one long-lived snapshot: the gateway owns
        ``codex_sessions.json`` and writes it continuously, so holding a stale
        whole-file snapshot across git and /proc probes and then writing it back
        would clobber concurrent edits to unrelated rows.

        Every mutation also re-verifies the row **at write time** (C7 HIGH-1).
        :meth:`_decide`'s stability guard closes the window between the first
        and the last probe, but the tombstone is stamped here, later still — and
        a Discord message landing in that window flips the row to EXECUTING.
        Writing a decision taken about the row's earlier self would tombstone a
        session that had just come back to life.
        """
        outcome = decision.get("outcome")
        sid = decision.get("session_id", "")

        if outcome == "skipped":
            return

        fingerprint = decision.get("row_fingerprint")

        if outcome == "orphaned":
            reason = decision.get("reason")
            wrote, drift = self._mutate_row(
                thread_id,
                lambda row: self._stamp_orphaned(row, reason),
                expect_states=_REAPABLE_STATES,
                expect_fields=fingerprint,
            )
            if not wrote:
                decision["outcome"] = "skipped"
                decision["write_time_drift"] = drift
                decision["reason"] = (
                    f"row changed before it could be quarantined ({drift})"
                )
            return

        if outcome != "released":
            return

        # Step 1 — tombstone FIRST.  If we crash after this the row is terminal
        # with a full receipt and the directory is merely stale; the gc watcher
        # routes it into .deleted-<ts> on a later tick.  The opposite order can
        # delete the disk and then lose the record of why.
        wrote, drift = self._mutate_row(
            thread_id,
            lambda row: self._stamp_released(row, decision),
            expect_states=_REAPABLE_STATES,
            expect_fields=fingerprint,
        )
        if not wrote:
            decision["outcome"] = "skipped"
            decision["write_time_drift"] = drift
            decision["reason"] = (
                f"row changed before the tombstone could be written ({drift}) — "
                "release abandoned, nothing on disk was touched"
            )
            return

        # Step 2 — non-force disk release.  Refusal downgrades to ORPHANED.
        try:
            self._release_disk(sid)
        except Exception as exc:
            log.warning(
                "CodexSessionReaper: non-force release of %s refused: %s", sid, exc,
            )
            decision["outcome"] = "orphaned"
            decision["release_error"] = str(exc)
            decision["reason"] = (
                f"non-force release refused, downgraded to orphaned: {exc}"
            )
            # The row is RELEASED at this point — we stamped it a moment ago —
            # so the downgrade expects exactly that state, not a reapable one.
            self._mutate_row(
                thread_id,
                lambda row: self._stamp_orphaned(row, decision["reason"]),
                expect_states=("RELEASED",),
                expect_sid=sid,
            )
            return

        decision["released_disk"] = True

    def _release_disk(self, sid: str) -> None:
        """Call the broker's NON-FORCE release, or refuse outright.

        There is deliberately no fallback to :meth:`WorktreeBroker.release`: if
        a deployment ships a broker without ``release_nonforce`` the correct
        behaviour is to quarantine the session, not to quietly escalate an
        unattended reap to a force-remove-and-rmtree.
        """
        fn = getattr(self._broker, "release_nonforce", None)
        if not callable(fn):
            raise RuntimeError(
                "broker exposes no release_nonforce(); refusing to fall back to "
                "the force release path"
            )
        fn(sid)

    def _stamp_released(self, row: dict, decision: dict) -> None:
        row["state"] = "RELEASED"
        row["released_at"] = _now_iso()
        row["release_reason"] = decision.get("reason")
        row["release_receipt"] = decision.get("custody_receipt")

    def _stamp_orphaned(self, row: dict, reason: Any) -> None:
        row["state"] = "ORPHANED"
        row["orphaned_at"] = _now_iso()
        row["orphaned_reason"] = reason

    def _mutate_row(
        self,
        thread_id: str,
        mutate: Callable[[dict], None],
        *,
        expect_states: Optional[tuple[str, ...]] = None,
        expect_sid: Any = None,
        expect_fields: Optional[dict] = None,
    ) -> tuple[bool, Optional[str]]:
        """Re-read the registry, mutate one row, write it back.

        Returns ``(written, drift_reason)``.  The ``expect_*`` arguments are the
        write-time re-verification (C7 HIGH-1): the mutation is applied only if
        the row is still the row the decision was made about.  A caller that
        passes none of them gets the old unconditional behaviour.

        ``expect_fields`` is the decide-time fingerprint
        (:data:`_FINGERPRINT_FIELDS`) and is the strongest of the three — it
        catches a resurrection that leaves ``state`` unchanged.
        """
        state = self._dispatcher._load_state()
        row = state.get("sessions", {}).get(thread_id)
        if not isinstance(row, dict):
            return False, "row disappeared from the registry"
        if expect_states is not None and row.get("state") not in expect_states:
            return False, (
                f"state is {row.get('state')!r}, expected one of "
                f"{list(expect_states)}"
            )
        if expect_sid is not None and row.get("session_id") != expect_sid:
            return False, (
                f"session_id changed ({expect_sid!r} -> {row.get('session_id')!r})"
            )
        if expect_fields:
            for field, expected in expect_fields.items():
                if row.get(field) != expected:
                    return False, (
                        f"row field {field!r} changed "
                        f"({expected!r} -> {row.get(field)!r})"
                    )
        mutate(row)
        self._dispatcher._write_state(state)
        return True, None

    # ------------------------------------------------------------------ #
    # gates
    # ------------------------------------------------------------------ #
    @staticmethod
    def _timestamp_state(value: Any) -> tuple[str, Optional[datetime]]:
        """Classify a row timestamp as ``absent`` / ``malformed`` / ``ok``.

        C7 HIGH-2 turns on this distinction.  ``_parse_iso`` collapses "the
        field isn't there" and "the field is there but I can't read it" into the
        same ``None``, and the idle gate then treated both as "no signal" and
        moved on to the next field.  For ``last_message_at`` those two cases
        have opposite safety meanings: absent means the thread never spoke, and
        malformed means it may have spoken thirty seconds ago.
        """
        if value is None or value == "":
            return "absent", None
        parsed = _parse_iso(value)
        if parsed is None:
            return "malformed", None
        return "ok", parsed

    def _idle_reason(
        self,
        row: dict,
        now: datetime,
        reap_idle_hours: float,
    ) -> tuple[Optional[str], Optional[str]]:
        """``(idle_reason, block_reason)`` — at most one is ever non-None.

        ``idle_reason`` is a short string when the row IS idle.  ``block_reason``
        is a short string when the idle question is **unanswerable**, which is
        not the same as "not idle": it means a timestamp is present but
        unreadable, and the row must be left alone until a human fixes it.

        ``last_message_at`` is the signal; ``created_at`` covers a row that has
        never received a message.  A *malformed* ``last_message_at`` fails
        closed rather than falling through to ``created_at``: a chatty session
        whose timestamp got corrupted would otherwise be judged on the age of
        the row, which for any long-lived thread is unboundedly old (C7 HIGH-2).
        A malformed ``created_at`` on a row with no ``last_message_at`` fails
        closed for the same reason.

        The pre-C7 "created_at is old AND no commits since created_at" fallback
        is gone.  It existed to catch chatty-but-dead threads under a 10-day
        window, and it conflated "made no commits" with "holds nothing worth
        keeping".  Whether work would be lost is now decided by unique-commit
        custody, which every release must pass.
        """
        threshold_sec = float(reap_idle_hours) * 3600.0

        kind, last_msg = self._timestamp_state(row.get("last_message_at"))
        if kind == "malformed":
            return None, (
                "last_message_at is present but unparseable "
                f"({row.get('last_message_at')!r}) — fail-closed, the row is "
                "never idle on an unreadable clock"
            )
        if kind == "ok" and last_msg is not None:
            age = (now - last_msg).total_seconds()
            if age >= threshold_sec:
                return (
                    f"idle {age / 3600:.1f}h >= {reap_idle_hours}h since last_message_at",
                    None,
                )
            return None, None

        kind, created = self._timestamp_state(row.get("created_at"))
        if kind == "malformed":
            return None, (
                "created_at is present but unparseable "
                f"({row.get('created_at')!r}) — fail-closed"
            )
        if kind == "ok" and created is not None:
            age = (now - created).total_seconds()
            if age >= threshold_sec:
                return (
                    f"idle {age / 3600:.1f}h >= {reap_idle_hours}h since created_at "
                    "(no message ever received)",
                    None,
                )
        return None, None

    def _has_uncommitted_work(self, worktree: Optional[Path]) -> bool:
        """Protection 4: True if the worktree has any uncommitted/untracked file.

        Fails SAFE: a git probe error returns True (assume dirty) so we never
        release on an inconclusive probe.  A missing worktree dir returns False
        — there is no uncommitted work in a directory that does not exist — but
        such a row cannot pass protection 5, which needs a real HEAD.
        """
        if worktree is None or not worktree.exists():
            return False
        res = self._git(worktree, "status", "--porcelain")
        if res is None:
            return True
        return bool(res.strip())

    def _branch_only_commits(self, worktree: Optional[Path]) -> tuple[list[str], bool]:
        """Protection 5: commits reachable from HEAD but from no remote ref.

        Returns ``(shas, probe_ok)``.  ``git rev-list HEAD --not --remotes``
        answers the only question that matters before deleting a worktree —
        *would anything be lost?* — because a commit present on some remote
        survives the directory going away.

        ``probe_ok`` is False for a missing worktree or a failed git call: an
        unprovable custody claim is not a custody claim, and the caller
        quarantines.
        """
        if worktree is None or not worktree.exists():
            return [], False
        res = self._git(worktree, "rev-list", "HEAD", "--not", "--remotes")
        if res is None:
            return [], False
        return [line.strip() for line in res.splitlines() if line.strip()], True

    def _head_sha(self, worktree: Optional[Path]) -> Optional[str]:
        """Current HEAD sha of the worktree, or None if unavailable."""
        if worktree is None or not worktree.exists():
            return None
        res = self._git(worktree, "rev-parse", "HEAD")
        if res is None:
            return None
        return res.strip() or None

    def _branch_in_open_prs(self, branch: str, sid: str, open_branches: set[str]) -> bool:
        """Protection 3: True if the row's branch maps to an open PR.

        Matches the row branch exactly, and also applies the sid-substring
        heuristic the broker's gc uses (``codex/<sid>/...``), so a branch
        renamed downstream still protects the worktree as long as the sid is
        present.
        """
        if not open_branches:
            return False
        if branch and branch in open_branches:
            return True
        if sid:
            needle = f"/{sid}/"
            return any(needle in b or b.endswith(f"/{sid}") for b in open_branches)
        return False

    def _scan_process_owners(
        self, worktrees: dict[str, Path]
    ) -> tuple[dict[str, list[dict]], bool]:
        """Protection 6: find live processes rooted inside each worktree.

        One ``/proc`` pass for the whole tick.  For every visible pid we resolve
        ``cwd`` and each entry in ``fd/``; anything landing inside a candidate
        worktree marks that session as owned.

        Returns ``({sid: [owner, ...]}, probe_ok)``.  ``probe_ok`` is False when
        ``/proc`` is unavailable, when no process was readable at all, or when
        some process had more fds than :data:`_MAX_FDS_PER_PROC` — in each case
        we cannot prove *absence* of an owner, and absence is what authorises a
        release.
        """
        owners: dict[str, list[dict]] = {sid: [] for sid in worktrees}
        if not worktrees:
            return owners, True

        prefixes: dict[str, str] = {}
        for sid, path in worktrees.items():
            try:
                prefixes[sid] = os.path.realpath(str(path))
            except OSError:
                continue
        if not prefixes:
            return owners, False

        try:
            pids = [name for name in os.listdir("/proc") if name.isdigit()]
        except OSError as exc:
            log.warning("CodexSessionReaper: /proc unreadable: %s", exc)
            return owners, False

        readable = 0
        truncated = False

        for pid in pids:
            saw_pid = False
            for target in self._proc_cwd(pid):
                saw_pid = True
                self._match_owner(owners, prefixes, pid, target, "cwd")
            fd_targets, fd_truncated, fd_readable = self._proc_fds(pid)
            truncated = truncated or fd_truncated
            if fd_readable:
                saw_pid = True
            for target in fd_targets:
                self._match_owner(owners, prefixes, pid, target, "fd")
            if saw_pid:
                readable += 1

        probe_ok = readable > 0 and not truncated
        if not probe_ok:
            log.warning(
                "CodexSessionReaper: process-owner probe inconclusive "
                "(readable_pids=%d truncated=%s)", readable, truncated,
            )
        return owners, probe_ok

    @staticmethod
    def _match_owner(
        owners: dict[str, list[dict]],
        prefixes: dict[str, str],
        pid: str,
        target: str,
        kind: str,
    ) -> None:
        for sid, prefix in prefixes.items():
            if target == prefix or target.startswith(prefix + os.sep):
                owners[sid].append({"pid": pid, "kind": kind, "path": target})

    @staticmethod
    def _proc_cwd(pid: str) -> list[str]:
        try:
            return [os.readlink(f"/proc/{pid}/cwd")]
        except OSError:
            # Vanished process, or another user's — nothing readable here.
            return []

    @staticmethod
    def _proc_fds(pid: str) -> tuple[list[str], bool, bool]:
        """``(targets, truncated, readable)`` for one process's open fds."""
        fd_dir = f"/proc/{pid}/fd"
        try:
            entries = os.listdir(fd_dir)
        except OSError:
            return [], False, False
        if len(entries) > _MAX_FDS_PER_PROC:
            # Refuse to walk an unbounded fd table; report inconclusive instead.
            return [], True, True
        targets: list[str] = []
        for entry in entries:
            try:
                targets.append(os.readlink(f"{fd_dir}/{entry}"))
            except OSError:
                continue
        return targets, False, True

    def _tmux_owner(self, row: dict, sid: str) -> tuple[Optional[str], bool]:
        """Protection 6 (tmux half): ``(session_name_or_None, probe_ok)``.

        Checks the name recorded on the row and the broker's canonical
        ``codex-sess-<sid>``.  tmux being absent from PATH is *conclusive* — no
        tmux server means no tmux owner — while a timeout or OS error is not.
        """
        names = [
            name for name in (row.get("tmux_session"), f"codex-sess-{sid}" if sid else None)
            if name
        ]
        for name in names:
            try:
                res = subprocess.run(
                    ["tmux", "has-session", "-t", str(name)],
                    capture_output=True, text=True, check=False,
                    timeout=_TMUX_TIMEOUT_SEC,
                )
            except FileNotFoundError:
                return None, True
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("CodexSessionReaper: tmux probe for %s failed: %s", name, exc)
                return None, False
            if res.returncode == 0:
                return str(name), True
        return None, True

    def _stability_drift(
        self,
        thread_id: str,
        snapshot: dict,
        worktree: Optional[Path],
        head: Optional[str],
    ) -> Optional[str]:
        """Protection 7: re-read the row and HEAD; describe any drift, else None."""
        try:
            state = self._dispatcher._load_state()
        except Exception as exc:  # pragma: no cover — defensive
            return f"registry re-read failed: {exc}"
        fresh = state.get("sessions", {}).get(thread_id)
        if not isinstance(fresh, dict):
            return "row disappeared from the registry"
        for field in ("state", "last_message_id", "last_message_at", "paused"):
            if fresh.get(field) != snapshot.get(field):
                return (
                    f"row field {field!r} changed "
                    f"({snapshot.get(field)!r} -> {fresh.get(field)!r})"
                )
        fresh_head = self._head_sha(worktree)
        if fresh_head != head:
            return f"worktree HEAD moved ({head} -> {fresh_head})"
        return None

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _branch_for(self, row: dict) -> str:
        """Reconstruct the row's branch ``codex/<sid>/<isa_slug>``.

        The dispatcher builds the branch this way at allocate-time
        (``branch_name = f"codex/{sid}/{isa_slug}"``) but does not persist it on
        the row, so we recompute it here.
        """
        sid = row.get("session_id", "")
        isa_slug = row.get("isa_slug") or ""
        if not sid:
            return ""
        if isa_slug:
            return f"codex/{sid}/{isa_slug}"
        return f"codex/{sid}"

    def _open_branches(self) -> tuple[set[str], bool]:
        """``(open_pr_branches, lookup_ok)`` — fail-closed on any error.

        Pre-C7 this returned a bare set and swallowed failures into ``set()``,
        which callers could not distinguish from a genuine "no open PRs".
        """
        try:
            branches = self._gh_open_branches_fn()
        except Exception as exc:
            log.warning(
                "CodexSessionReaper: open-branches lookup failed (%s) — "
                "fail-closed, no releases this tick", exc,
            )
            return set(), False
        if branches is None:
            log.warning(
                "CodexSessionReaper: open-branches lookup returned None — fail-closed"
            )
            return set(), False
        return (branches if isinstance(branches, set) else set(branches)), True

    def _git(self, worktree: Path, *args: str) -> Optional[str]:
        """Run ``git -C <worktree> <args>`` and return stdout, or None on error."""
        try:
            res = subprocess.run(
                ["git", "-C", str(worktree), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("CodexSessionReaper: git %s in %s failed: %s", args, worktree, exc)
            return None
        if res.returncode != 0:
            log.warning(
                "CodexSessionReaper: git %s in %s exit %s: %s",
                args, worktree, res.returncode, res.stderr.strip(),
            )
            return None
        return res.stdout

    def _hermes_home(self) -> Path:
        """Resolve the Hermes home dir from the dispatcher, broker, or default.

        The dispatcher is checked first (it owns ``codex_sessions.json`` and is
        the more reliable source); the broker is the fallback.  Only str/Path
        values are accepted so a test double / mock attribute cannot poison the
        path resolution.
        """
        for owner in (self._dispatcher, self._broker):
            for attr in ("hermes_home", "_hermes_home"):
                home = getattr(owner, attr, None)
                if isinstance(home, (str, Path)):
                    return Path(home)
        # Last resort: the canonical resolver, NOT ``Path.home() / ".hermes"``.
        # Both owners can legitimately lack a usable attribute (a hand-rolled
        # test double, or a ``MagicMock`` whose auto-created attribute the
        # isinstance guard above correctly rejects).  ``Path.home()`` ignores
        # ``HERMES_HOME``, so that spelling sent this ledger to the operator's
        # REAL home from under the test suite, and would send it to the default
        # profile's home for a gateway running under a non-default HERMES_HOME.
        return get_hermes_home()

    def _append_ledger(self, decision: dict) -> None:
        """Append one decision to the JSONL ledger (best-effort; never raises)."""
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._ledger_path, "a", encoding="utf-8") as fd:
                fd.write(json.dumps(decision, sort_keys=True, default=str) + "\n")
        except OSError as exc:  # pragma: no cover — defensive
            log.warning("CodexSessionReaper: ledger append failed: %s", exc)
