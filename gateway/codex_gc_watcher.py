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

``live_branches`` comes from a ``gh pr list`` lookup that fails
**closed** (C7 MED-1): if the lookup cannot be completed, gc is
skipped for the tick rather than run with an empty set, because an
empty set is indistinguishable from "no PR protects any of these
worktrees" — the most dangerous possible reading.

Per WORKFLOW-LESSONS §3 rule 5: this module performs NO direct
filesystem deletion of any kind.  All deletion is delegated to
:meth:`WorktreeBroker.reap_deleted`, which is itself scoped
exclusively to the ``.deleted-<ts>`` namespace gc owns.

C7 / Gate 7
-----------
The tick also drives the two row-side sweeps:

* :class:`gateway.codex_session_reaper.CodexSessionReaper` — pre-C7 hardwired
  to ``reap(reap_idle_days=10, dry_run=True)``, i.e. permanently inert.  It is
  now *configurable*, and configuration is the only thing that can arm it.
* :class:`gateway.codex_registry_gc.CodexRegistryGc` — retires aged terminal
  tombstones, archive-first.  Quarantine (``ORPHANED``) is never collected.

Arming the reaper: two flags, both explicit (C7 blocker B1)
-----------------------------------------------------------
The shipped defaults tear nothing down.  A deploy alone can never start
destroying sessions; that takes two separate deliberate acts:

===========================  ==================================  =============
flag                          env var                             default
===========================  ==================================  =============
``reap_armed``                ``HERMES_CODEX_REAP_ARMED``         ``False``
``reap_confirmed``            ``HERMES_CODEX_REAP_CONFIRMED``     ``False``
===========================  ==================================  =============

* **unarmed** (the shipped state) — the reaper evaluates and ledgers every
  decision and mutates nothing.  Identical to pre-C7 behaviour, plus a real
  ledger of what a live run *would* do.
* **armed, unconfirmed** — the PREVIEW state.  Still no destructive action:
  the tick writes its full set of proposals to the reap ledger, tagged
  ``mode="preview"``, so an operator reads the actual proposed teardown list
  for *this* registry before anything can act on it.
* **armed and confirmed** — live.  Only now does the reaper mutate rows or
  call the broker, and only now does the registry GC delete anything.

The idle window defaults to :data:`gateway.codex_session_reaper.DEFAULT_REAP_IDLE_HOURS`
(240h = 10 days, the pre-C7 window).  A 6-hour window is reachable, but only by
explicitly configuring it — measured against the live registry, a 6-hour window
made every currently-running session a release candidate.

``reap_dry_run`` / ``HERMES_CODEX_REAP_DRY_RUN`` remain as a one-way safety
override: setting them true forces preview even when both arming flags are set.
Setting them false does NOT arm anything.

Nothing here takes effect until the branch is deployed and the gateway is
restarted; that is the named cutover gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from gateway.codex_session_reaper import DEFAULT_REAP_IDLE_HOURS
from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)

# ``gh pr list`` should be quick (<5 s in practice).  Cap defensively
# so a hung call cannot stall the gc loop.
_GH_TIMEOUT_SEC = 30
_REPO_ROOT = Path(__file__).resolve().parents[1]

# C7 blocker B1 — shipped defaults tear nothing down.  The idle window matches
# the pre-C7 10-day behaviour, and live mode requires BOTH arming flags.
_DEFAULT_REAP_IDLE_HOURS = DEFAULT_REAP_IDLE_HOURS  # 240h = 10 days
_DEFAULT_REAP_ARMED = False
_DEFAULT_REAP_CONFIRMED = False
_DEFAULT_REGISTRY_GC_MAX_AGE_DAYS = 90

_ENV_REAP_IDLE_HOURS = "HERMES_CODEX_REAP_IDLE_HOURS"
_ENV_REAP_DRY_RUN = "HERMES_CODEX_REAP_DRY_RUN"
_ENV_REAP_ARMED = "HERMES_CODEX_REAP_ARMED"
_ENV_REAP_CONFIRMED = "HERMES_CODEX_REAP_CONFIRMED"
_ENV_REGISTRY_GC_ENABLED = "HERMES_CODEX_REGISTRY_GC_ENABLED"
_ENV_REGISTRY_GC_MAX_AGE_DAYS = "HERMES_CODEX_REGISTRY_GC_MAX_AGE_DAYS"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

#: Reaper execution modes, most conservative first.
MODE_UNARMED = "unarmed"
MODE_PREVIEW = "preview"
MODE_LIVE = "live"


class GhLookupError(RuntimeError):
    """The open-PR lookup could not be completed.

    C7 MED-1.  Raised instead of degrading to ``set()`` so every caller has to
    decide what an *unknown* PR set means for it — the reaper skips all
    releases, and the watcher skips gc.  Silently returning an empty set made
    "I could not ask" indistinguishable from "nothing is protected".
    """


def _config_float(explicit: Optional[float], env_key: str, default: float) -> float:
    """Explicit argument wins, then the env var, then the built-in default."""
    if explicit is not None:
        return float(explicit)
    raw = os.getenv(env_key)
    if raw:
        try:
            return float(raw)
        except ValueError:
            log.warning("CodexGcWatcher: %s=%r is not a number; using %s",
                        env_key, raw, default)
    return default


def _config_bool(explicit: Optional[bool], env_key: str, default: bool) -> bool:
    """Explicit argument wins, then the env var, then the built-in default."""
    if explicit is not None:
        return bool(explicit)
    raw = (os.getenv(env_key) or "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    if raw:
        log.warning("CodexGcWatcher: %s=%r is not a boolean; using %s",
                    env_key, raw, default)
    return default


def _gh_list_open_branches() -> set[str]:
    """Return the set of ``headRefName`` strings for open PRs on the repo.

    C7 MED-1: every failure mode — gh not on PATH, timeout, non-zero exit,
    unparseable JSON — raises :class:`GhLookupError` instead of degrading to an
    empty set.  An empty set is a *fact* ("no PRs are open"); a failed lookup is
    an *absence of fact*, and only the caller can decide what to do about it.

    This also repairs a hole in the reaper's own fail-closed contract: the
    reaper treats a raising lookup as "release nothing this tick", but the
    default callable it is handed used to swallow every error, so the contract
    could never actually fire in production.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "open",
             "--json", "headRefName", "--limit", "200"],
            capture_output=True, text=True, check=False, timeout=_GH_TIMEOUT_SEC,
            cwd=_REPO_ROOT,
        )
    except FileNotFoundError as exc:
        raise GhLookupError("gh CLI not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhLookupError(f"gh pr list timed out after {_GH_TIMEOUT_SEC}s") from exc
    except Exception as exc:  # pragma: no cover — defensive
        raise GhLookupError(f"gh pr list crashed: {exc}") from exc
    if result.returncode != 0:
        raise GhLookupError(
            f"gh pr list exit {result.returncode}: {result.stderr.strip()}"
        )
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GhLookupError(f"gh pr list returned unparseable JSON: {exc}") from exc
    branches: set[str] = set()
    for pr in prs:
        ref = pr.get("headRefName") if isinstance(pr, dict) else None
        if ref:
            branches.add(ref)
    return branches


class CodexGcWatcher:
    """Background task that periodically sweeps worktree orphans.

    Mirrors :class:`gateway.codex_merge_watcher.CodexMergeWatcher` so the
    adapter start/stop wire-up stays symmetric.
    """

    def __init__(
        self,
        *,
        dispatcher: Any,
        worktree_broker: Any,
        poll_interval_sec: float = 3600.0,
        reap_max_age_days: int = 7,
        gh_list_open_branches: Callable[[], set[str]] | None = None,
        reap_idle_hours: float | None = None,
        reap_dry_run: bool | None = None,
        reap_armed: bool | None = None,
        reap_confirmed: bool | None = None,
        registry_gc_enabled: bool | None = None,
        registry_gc_max_age_days: float | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._broker = worktree_broker
        self._poll_interval = poll_interval_sec
        self._reap_max_age_days = reap_max_age_days
        self._gh_list_open_branches = gh_list_open_branches or _gh_list_open_branches
        # C7 session-reaper config: explicit arg > env var > default.
        self._reap_idle_hours = _config_float(
            reap_idle_hours, _ENV_REAP_IDLE_HOURS, _DEFAULT_REAP_IDLE_HOURS,
        )
        # C7 blocker B1 — two-flag arming.  Neither flag alone does anything
        # destructive, and the legacy dry-run override can only make it safer.
        self._reap_armed = _config_bool(
            reap_armed, _ENV_REAP_ARMED, _DEFAULT_REAP_ARMED,
        )
        self._reap_confirmed = _config_bool(
            reap_confirmed, _ENV_REAP_CONFIRMED, _DEFAULT_REAP_CONFIRMED,
        )
        self._reap_dry_run_override = _config_bool(
            reap_dry_run, _ENV_REAP_DRY_RUN, False,
        )
        self._reap_mode = self._resolve_reap_mode()
        self._reap_dry_run = self._reap_mode != MODE_LIVE
        self._registry_gc_enabled = _config_bool(
            registry_gc_enabled, _ENV_REGISTRY_GC_ENABLED, True,
        )
        self._registry_gc_max_age_days = int(_config_float(
            registry_gc_max_age_days,
            _ENV_REGISTRY_GC_MAX_AGE_DAYS,
            _DEFAULT_REGISTRY_GC_MAX_AGE_DAYS,
        ))
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    def _resolve_reap_mode(self) -> str:
        """Decide the reaper's execution mode from the arming flags.

        ``unarmed`` -> ``preview`` -> ``live``, and nothing but explicit
        configuration advances it.  The dry-run override is deliberately
        one-way: it can force preview, never live (C7 blocker B1(b)).
        """
        if not self._reap_armed:
            if self._reap_dry_run_override is False and os.getenv(_ENV_REAP_DRY_RUN):
                log.warning(
                    "CodexGcWatcher: %s is false but %s is not set — the reaper "
                    "stays UNARMED.  Live mode requires %s=1 and %s=1.",
                    _ENV_REAP_DRY_RUN, _ENV_REAP_ARMED,
                    _ENV_REAP_ARMED, _ENV_REAP_CONFIRMED,
                )
            return MODE_UNARMED
        if self._reap_dry_run_override:
            log.warning(
                "CodexGcWatcher: reaper armed but %s forces preview mode",
                _ENV_REAP_DRY_RUN,
            )
            return MODE_PREVIEW
        if not self._reap_confirmed:
            log.warning(
                "CodexGcWatcher: reaper ARMED but UNCONFIRMED — running in "
                "PREVIEW.  Proposals are written to the reap ledger and nothing "
                "is torn down.  Review them, then set %s=1 to go live.",
                _ENV_REAP_CONFIRMED,
            )
            return MODE_PREVIEW
        return MODE_LIVE

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="codex-gc-watcher")
        log.info(
            "CodexGcWatcher started (interval=%.1fs, reap_max_age=%dd, "
            "reap_idle=%.1fh, reap_mode=%s, registry_gc=%s/%dd)",
            self._poll_interval, self._reap_max_age_days,
            self._reap_idle_hours, self._reap_mode,
            self._registry_gc_enabled, self._registry_gc_max_age_days,
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
            if not isinstance(row, dict):
                continue
            sid = row.get("session_id")
            if not sid:
                continue
            # C7: a RELEASED tombstone is a row whose disk teardown was already
            # *decided*.  Excluding it from tracked_sids is what makes the
            # reaper's tombstone-before-release ordering crash-convergent: if we
            # died between writing the tombstone and removing the worktree, gc
            # now sweeps the leftover directory into .deleted-<ts> and
            # reap_deleted purges it after the usual 7-day recovery window.
            #
            # Every other terminal state stays tracked on purpose. ORPHANED in
            # particular is quarantine — its worktree is preserved indefinitely
            # for a human, and sweeping it would defeat the entire point of
            # quarantining instead of releasing.
            if row.get("state") == "RELEASED":
                continue
            tracked_sids.add(sid)

        # Fetch the set of open-PR branches so a worktree whose session row was
        # lost but whose PR is still open survives gc.  C7 MED-1: this is now
        # FAIL-CLOSED.  On any failure (gh missing / timeout / parse error /
        # custom callable raising) we do NOT hand gc an empty set — an empty set
        # means "no branch is protected by a PR", which is the exact claim we
        # just failed to verify.  gc is skipped for the tick instead; it is an
        # hourly sweep and losing one pass costs nothing.
        live_branches: Optional[set[str]] = None
        try:
            live_branches = self._gh_list_open_branches()
        except Exception as exc:
            log.warning(
                "CodexGcWatcher: live_branches lookup failed (%s) — fail-closed, "
                "skipping broker.gc this tick", exc,
            )

        if live_branches is None:
            log.info("CodexGcWatcher: gc skipped (open-PR set unknown)")
        else:
            try:
                actions = self._broker.gc(
                    tracked_sids=tracked_sids,
                    live_branches=live_branches,
                )
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

        # C7: the row-side reaper.  Pre-C7 this was hardwired to
        # reap(reap_idle_days=10, dry_run=True) — evaluated and ledgered
        # forever, never acted.  It is configurable now, and only the two-flag
        # arming in _resolve_reap_mode can make it act.
        try:
            from gateway.codex_session_reaper import CodexSessionReaper  # noqa: PLC0415

            decisions = CodexSessionReaper(
                dispatcher_state=self._dispatcher,
                broker=self._broker,
                gh_open_branches_fn=self._gh_list_open_branches,
            ).reap(
                reap_idle_hours=self._reap_idle_hours,
                dry_run=self._reap_dry_run,
            )
            if decisions:
                counts: dict[str, int] = {}
                for decision in decisions:
                    outcome = str(decision.get("outcome", "unknown"))
                    counts[outcome] = counts.get(outcome, 0) + 1
                log.info(
                    "CodexGcWatcher: session reaper (%s) evaluated %d row(s): %s",
                    self._reap_mode,
                    len(decisions),
                    ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                )
            if self._reap_mode == MODE_PREVIEW:
                self._write_preview(decisions)
        except Exception as exc:
            log.warning("CodexGcWatcher: session reaper failed: %s", exc)

        # C7: retire aged terminal tombstones, archive-first.  Isolated like
        # every other stage — a GC failure must not affect the sweeps above it.
        #
        # GC_ELIGIBLE_TERMINAL_STATES, not TERMINAL_STATES: quarantine
        # (ORPHANED) is terminal but is kept until a human dispositions it
        # (C7 blocker B3).  QUARANTINE_STATES is passed explicitly too, so the
        # GC refuses those rows even if the eligible set ever drifts.
        if self._registry_gc_enabled:
            try:
                from gateway.codex_registry_gc import CodexRegistryGc  # noqa: PLC0415
                from gateway.codex_session_dispatcher import (  # noqa: PLC0415
                    GC_ELIGIBLE_TERMINAL_STATES,
                    QUARANTINE_STATES,
                )

                gc_decisions = CodexRegistryGc(
                    self._dispatcher,
                    max_terminal_age_days=self._registry_gc_max_age_days,
                ).collect(
                    terminal_states=GC_ELIGIBLE_TERMINAL_STATES,
                    quarantine_states=QUARANTINE_STATES,
                    dry_run=self._reap_dry_run,
                )
                retired = [d for d in gc_decisions if d.get("outcome") == "retired"]
                if retired:
                    log.info(
                        "CodexGcWatcher: registry GC archived + retired %d "
                        "terminal row(s)", len(retired),
                    )
            except Exception as exc:
                log.warning("CodexGcWatcher: registry GC failed: %s", exc)

    def _write_preview(self, decisions: list[dict]) -> None:
        """Record an armed-but-unconfirmed tick's proposals to the reap ledger.

        C7 blocker B1(c).  Arming the reaper must not make the very next tick a
        teardown; the operator has to be able to read what *this* registry would
        actually lose.  The preview record is a single ledger line carrying the
        full proposal list, tagged so it is greppable and can never be mistaken
        for an applied decision.
        """
        proposals = [
            {
                "session_id": d.get("session_id"),
                "thread_id": d.get("thread_id"),
                "outcome": d.get("outcome"),
                "reason": d.get("reason"),
            }
            for d in decisions
            if d.get("outcome") in ("released", "orphaned")
        ]
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "reap_preview",
            "mode": MODE_PREVIEW,
            "armed": self._reap_armed,
            "confirmed": self._reap_confirmed,
            "reap_idle_hours": self._reap_idle_hours,
            "evaluated": len(decisions),
            "proposals": proposals,
            "note": (
                "ARMED but UNCONFIRMED — nothing was torn down.  Review these "
                f"proposals, then set {_ENV_REAP_CONFIRMED}=1 to allow them."
            ),
        }
        path = self._reap_ledger_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fd:
                fd.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except OSError as exc:  # pragma: no cover — defensive
            log.warning("CodexGcWatcher: preview ledger append failed: %s", exc)
        log.warning(
            "CodexGcWatcher: PREVIEW — %d proposal(s) recorded to %s; "
            "set %s=1 to allow them", len(proposals), path, _ENV_REAP_CONFIRMED,
        )

    def _reap_ledger_path(self) -> Path:
        """Same ledger the reaper appends its own decisions to."""
        for attr in ("hermes_home", "_hermes_home"):
            home = getattr(self._dispatcher, attr, None)
            if isinstance(home, (str, Path)):
                return Path(home) / "state" / "codex-reaper" / "reap-ledger.jsonl"
        # Canonical resolver, not ``Path.home() / ".hermes"`` — see the note in
        # ``codex_session_reaper._hermes_home``.  This must resolve to the same
        # file the reaper picks, and the reaper now honours HERMES_HOME.
        return get_hermes_home() / "state" / "codex-reaper" / "reap-ledger.jsonl"
