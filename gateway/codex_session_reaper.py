"""Codex session reaper — reclaim zombie CLAIMED/EXECUTING sessions (DISP-4 / ARCH-4).

BUILT DARK.  This module is intentionally NOT wired into the running gateway.
It stays inert until josep arms it by constructing a :class:`CodexSessionReaper`
and calling :meth:`reap` (the wire-in point is ``CodexGcWatcher`` at the Discord
adapter — see the module's ``needs_integration`` note in the build report).

Background
----------
Audit DISP-4/ARCH-4 found 42 zombie codex sessions (~3.6 GB of stale
worktrees) lingering in ``codex_sessions.json`` as CLAIMED/EXECUTING long
after the work stopped.  The existing :class:`gateway.codex_gc_watcher.CodexGcWatcher`
only sweeps worktree *directories* whose sid is no longer tracked by any row;
it never tears down a row that is *still tracked* but dead.  This reaper closes
that gap from the row side.

Design (safety-first; mirrors the gc/broker conventions)
--------------------------------------------------------
For each CLAIMED / EXECUTING row the reaper applies an **idle gate** then two
**safety gates**:

idle gate (either path qualifies a row as a reap candidate):
  * PRIMARY  — ``last_message_at`` is older than ``reap_idle_days``.
  * FALLBACK — ``created_at`` is older than ``reap_idle_days`` AND the worktree
               has produced **no commits since** ``created_at``.  This is
               REQUIRED because on live data ``last_message_at`` refreshes on
               every inbound Discord message even when the session makes no real
               progress, so the primary gate alone never fires for a chatty-but-
               dead thread.  The created-at fallback reclaims those.

safety gate A — no uncommitted work:
  ``git -C <worktree> status --porcelain`` must be empty.  Any dirty/untracked
  file means a human may still want the diff -> do not delete.

safety gate B — branch not in the open-PR set:
  the row's branch (``codex/<sid>/<isa_slug>``) must NOT appear in the set
  returned by ``gh_open_branches_fn`` (open PRs on the fork).  An open PR means
  the work is in review -> do not delete.

Outcomes
--------
  * idle gate fails                  -> ``skipped`` (left exactly as-is).
  * idle gate passes, both safety
    gates clear                      -> terminal teardown:
                                        ``broker.release(sid)`` then delete the
                                        row.  ``release`` already renames the
                                        worktree into the broker's ``.deleted-<ts>``
                                        recovery bucket, so there is **zero
                                        direct ``rm``** in this module.
  * idle gate passes, ANY safety
    gate fails                       -> mark ``state = "ORPHANED"`` and leave the
                                        row + worktree on disk.  Never deleted.

Every decision is appended (JSONL) to
``~/.hermes/state/codex-reaper/reap-ledger.jsonl`` for an audit trail.

``dry_run=True`` (the default) evaluates and ledgers every decision but performs
no teardown and no state mutation, so an operator can preview a reap safely.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# git status / git log probes should be near-instant on a local worktree.
# Cap defensively so a wedged worktree cannot stall the whole reap loop.
_GIT_TIMEOUT_SEC = 30

# States the reaper is allowed to consider for teardown.  Anything terminal
# (DONE / MERGED / FAILED / ORPHANED / ...) is never touched here.
_REAPABLE_STATES = ("CLAIMED", "EXECUTING")


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
        helpers (same contract :class:`CodexGcWatcher` already relies on).
    broker:
        The worktree broker (``agent.worktree_broker.WorktreeBroker``).  Only
        ``broker.release(sid)`` is called — teardown delegates disk reclamation
        to the broker's existing ``.deleted-<ts>`` recovery window.
    gh_open_branches_fn:
        Zero-arg callable returning the set of open-PR ``headRefName`` strings
        (same shape :class:`CodexGcWatcher` passes as ``live_branches``).  Must
        never raise out of the reaper; the reaper absorbs failures and, on
        error, treats the open-PR set as empty.  Passing an empty set on lookup
        failure is acceptable here because safety gate A (uncommitted work)
        independently protects in-flight work, and the operator-facing ledger
        records the degraded lookup.
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
    def reap(self, reap_idle_days: int = 10, dry_run: bool = True) -> list[dict]:
        """Sweep every CLAIMED/EXECUTING row once and return the decisions.

        Returns a list of decision dicts (also appended to the ledger).  When
        ``dry_run`` is True (default) no teardown or state mutation happens —
        every decision still reflects what *would* happen.
        """
        state = self._dispatcher._load_state()
        sessions = state.get("sessions", {})

        open_branches = self._safe_open_branches()
        now = datetime.now(timezone.utc)

        decisions: list[dict] = []
        mutated = False

        # Iterate over a snapshot of items — we may delete keys on teardown.
        for thread_id, row in list(sessions.items()):
            if row.get("state") not in _REAPABLE_STATES:
                continue

            decision = self._decide(row, thread_id, now, reap_idle_days, open_branches)
            decision["dry_run"] = dry_run

            if not dry_run:
                applied = self._apply(decision, sessions, thread_id, row)
                mutated = mutated or applied

            self._append_ledger(decision)
            decisions.append(decision)

        if mutated:
            self._dispatcher._write_state(state)

        return decisions

    # ------------------------------------------------------------------ #
    # decision logic (pure-ish: reads git/worktree, returns a verdict dict)
    # ------------------------------------------------------------------ #
    def _decide(
        self,
        row: dict,
        thread_id: str,
        now: datetime,
        reap_idle_days: int,
        open_branches: set[str],
    ) -> dict:
        sid = row.get("session_id", "")
        worktree = Path(row.get("worktree_path", "")) if row.get("worktree_path") else None
        branch = self._branch_for(row)

        verdict: dict = {
            "ts": _now_iso(),
            "session_id": sid,
            "thread_id": thread_id,
            "branch": branch,
            "worktree_path": str(worktree) if worktree else None,
            "prior_state": row.get("state"),
        }

        # ---- idle gate -------------------------------------------------- #
        idle = self._idle_reason(row, worktree, now, reap_idle_days)
        verdict["idle_reason"] = idle
        if idle is None:
            verdict["outcome"] = "skipped"
            verdict["reason"] = "not idle (last_message_at recent and created_at fallback not met)"
            return verdict

        # ---- safety gate A: no uncommitted work ------------------------- #
        dirty = self._has_uncommitted_work(worktree)
        verdict["uncommitted_work"] = dirty
        if dirty:
            verdict["outcome"] = "orphaned"
            verdict["reason"] = "safety gate A failed: uncommitted work in worktree"
            return verdict

        # ---- safety gate B: branch not in open-PR set ------------------- #
        in_open_pr = self._branch_in_open_prs(branch, sid, open_branches)
        verdict["in_open_pr"] = in_open_pr
        if in_open_pr:
            verdict["outcome"] = "orphaned"
            verdict["reason"] = "safety gate B failed: branch has an open PR"
            return verdict

        # ---- both safety gates clear: terminal teardown ---------------- #
        verdict["outcome"] = "released"
        verdict["reason"] = f"idle ({idle}); no uncommitted work; no open PR"
        return verdict

    def _apply(self, decision: dict, sessions: dict, thread_id: str, row: dict) -> bool:
        """Execute a non-dry-run decision.  Returns True if state changed."""
        outcome = decision["outcome"]
        sid = decision["session_id"]

        if outcome == "skipped":
            return False

        if outcome == "orphaned":
            row["state"] = "ORPHANED"
            row["orphaned_at"] = _now_iso()
            row["orphaned_reason"] = decision.get("reason")
            return True

        if outcome == "released":
            # Terminal teardown: broker.release renames the worktree into its
            # own .deleted-<ts> recovery bucket (zero direct rm here), then we
            # drop the row.  If release fails we DOWNGRADE to ORPHANED so the
            # row is not silently lost and a human can investigate — we never
            # delete a row whose disk teardown did not succeed.
            try:
                self._broker.release(sid)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("CodexSessionReaper: broker.release(%s) failed: %s", sid, exc)
                decision["outcome"] = "orphaned"
                decision["reason"] = f"release failed, downgraded to orphaned: {exc}"
                row["state"] = "ORPHANED"
                row["orphaned_at"] = _now_iso()
                row["orphaned_reason"] = decision["reason"]
                return True
            sessions.pop(thread_id, None)
            return True

        return False

    # ------------------------------------------------------------------ #
    # gates
    # ------------------------------------------------------------------ #
    def _idle_reason(
        self,
        row: dict,
        worktree: Optional[Path],
        now: datetime,
        reap_idle_days: int,
    ) -> Optional[str]:
        """Return a short reason string if the row is idle, else ``None``.

        PRIMARY: ``last_message_at`` older than the threshold.
        FALLBACK: ``created_at`` older than the threshold AND no commits since
        ``created_at`` (the worktree has produced no real progress).
        """
        threshold_sec = reap_idle_days * 86400

        last_msg = _parse_iso(row.get("last_message_at"))
        if last_msg is not None:
            age = (now - last_msg).total_seconds()
            if age >= threshold_sec:
                return f"last_message_at idle {age / 86400:.1f}d >= {reap_idle_days}d"

        # FALLBACK — last_message_at is recent (or absent) but the session may
        # still be a chatty-but-dead zombie.  Require an old created_at AND zero
        # commits since creation.
        created = _parse_iso(row.get("created_at"))
        if created is not None:
            created_age = (now - created).total_seconds()
            if created_age >= threshold_sec and not self._has_commits_since(worktree, created):
                return (
                    f"created_at {created_age / 86400:.1f}d >= {reap_idle_days}d "
                    "and no commits since created_at (fallback)"
                )

        return None

    def _has_uncommitted_work(self, worktree: Optional[Path]) -> bool:
        """Safety gate A: True if the worktree has any uncommitted/untracked file.

        Fails SAFE: a missing worktree dir means there is nothing to protect
        (no uncommitted work), so returns False.  A git probe error returns
        True (assume dirty) so we never delete on an inconclusive probe.
        """
        if worktree is None or not worktree.exists():
            return False
        res = self._git(worktree, "status", "--porcelain")
        if res is None:
            # Probe failed/timed out — treat as dirty so we ORPHAN, not delete.
            return True
        return bool(res.strip())

    def _has_commits_since(self, worktree: Optional[Path], since: datetime) -> bool:
        """True if the worktree HEAD has any commit authored after ``since``.

        Used by the created-at fallback.  Fails SAFE for the fallback's intent:
        a missing worktree or a failed probe returns True ("assume progress")
        so the fallback does NOT fire on an inconclusive signal — the operator
        would rather leave a possibly-live row than reap on a bad probe.
        """
        if worktree is None or not worktree.exists():
            return True
        since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        res = self._git(
            worktree,
            "log",
            f"--since={since_iso}",
            "--oneline",
            "-n",
            "1",
        )
        if res is None:
            return True
        return bool(res.strip())

    def _branch_in_open_prs(self, branch: str, sid: str, open_branches: set[str]) -> bool:
        """Safety gate B: True if the row's branch maps to an open PR.

        Matches the row branch exactly, and also applies the same sid-substring
        heuristic :meth:`gateway.codex_session_dispatcher` / the broker's gc use
        (``codex/<sid>/...``), so a branch renamed downstream still protects the
        worktree as long as the sid is present.
        """
        if not open_branches:
            return False
        if branch and branch in open_branches:
            return True
        if sid:
            needle = f"/{sid}/"
            return any(needle in b or b.endswith(f"/{sid}") for b in open_branches)
        return False

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

    def _safe_open_branches(self) -> set[str]:
        try:
            branches = self._gh_open_branches_fn()
        except Exception as exc:
            log.warning("CodexSessionReaper: open-branches lookup failed: %s", exc)
            return set()
        return branches if isinstance(branches, set) else set(branches or [])

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
        return Path.home() / ".hermes"

    def _append_ledger(self, decision: dict) -> None:
        """Append one decision to the JSONL ledger (best-effort; never raises)."""
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._ledger_path, "a", encoding="utf-8") as fd:
                fd.write(json.dumps(decision, sort_keys=True) + "\n")
        except OSError as exc:  # pragma: no cover — defensive
            log.warning("CodexSessionReaper: ledger append failed: %s", exc)
