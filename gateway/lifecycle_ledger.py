"""Gateway lifecycle ledger — durable termination-reason evidence (NS-608).

The gateway already has *graceful* shutdown forensics
(:mod:`gateway.shutdown_forensics` — who sent the SIGTERM) and an exit-path
diagnostic log (``gateway-exit-diag.log`` — every way ``asyncio.run`` can
return).  What it does NOT have is any record of an **unclean death**: a
SIGKILL, a kernel OOM kill, or the whole VM dying takes the process out
before any handler runs, so the next boot has no idea the previous life
ended violently — support tickets like NS-608 then require manually
cross-correlating four log files and two external APIs to answer "what
killed the gateway?".

This module closes that gap with a tiny state machine persisted to
``<HERMES_HOME>/state/gateway.lifecycle.json``:

* On startup, :func:`record_startup` reads the sentinel left by the
  previous life.  ``phase == "running"`` means that life never reached any
  exit path → it died uncleanly.  The finding — including the last
  heartbeat's memory sample, which is the closest thing to a pre-death
  telemetry snapshot — is appended to ``gateway-exit-diag.log`` as a
  ``gateway.previous_unclean_exit`` record and logged at WARNING.  The
  sentinel is then rewritten as ``phase=running`` for the new life.
* On every clean exit path, :func:`mark_exited` rewrites the sentinel as
  ``phase=exited`` with the exit code and a reason string.  Wired into
  ``_exit_after_graceful_shutdown`` (the single funnel for all graceful
  exits, #53107) and the two watchdog ``os._exit`` sites in
  :mod:`gateway.shutdown_watchdog`.

:func:`sample_memory` provides the cheap (<1ms, pure /proc reads) memory
snapshot that :func:`gateway.shutdown_watchdog.write_loop_heartbeat`
embeds in the 30s heartbeat — giving every unclean-death report a
"memory available N seconds before death" data point so OOM crash cycles
are classifiable from the volume alone (no Prometheus retention races).

Everything here is best-effort: a forensics failure must never affect the
gateway lifecycle it is observing.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LIFECYCLE_RELATIVE = ("state", "gateway.lifecycle.json")
_EXIT_DIAG_RELATIVE = ("logs", "gateway-exit-diag.log")
_STATE_DB_RELATIVE = ("state.db",)

# Heuristic OOM-suspicion thresholds applied to the last heartbeat's memory
# sample.  Deliberately conservative: this only annotates the report with a
# hint; classification stays with the human reading the evidence.
_LOW_MEM_AVAILABLE_KIB = 64 * 1024  # < 64 MiB available
_LOW_MEM_AVAILABLE_FRACTION = 0.05  # < 5% of MemTotal available


def _process_hermes_home() -> Path:
    """HERMES_HOME for process-level identity files (ignore task overrides)."""
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def get_lifecycle_sentinel_path(home: Optional[Path] = None) -> Path:
    """Return ``<HERMES_HOME>/state/gateway.lifecycle.json``."""
    base = home if home is not None else _process_hermes_home()
    return base.joinpath(*_LIFECYCLE_RELATIVE)


def sample_memory() -> Dict[str, Any]:
    """Cheap memory snapshot: own RSS + system availability + swap.

    Pure ``/proc`` reads, Linux-only (returns ``{}`` elsewhere), never
    raises.  Values in KiB to match the kernel's units.
    """
    sample: Dict[str, Any] = {}
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    sample["rss_kib"] = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        meminfo: Dict[str, int] = {}
        wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key = line.split(":", 1)[0]
                if key in wanted:
                    meminfo[key] = int(line.split()[1])
                    if len(meminfo) == len(wanted):
                        break
        if "MemTotal" in meminfo:
            sample["mem_total_kib"] = meminfo["MemTotal"]
        if "MemAvailable" in meminfo:
            sample["mem_available_kib"] = meminfo["MemAvailable"]
        if "SwapTotal" in meminfo and "SwapFree" in meminfo:
            sample["swap_used_kib"] = meminfo["SwapTotal"] - meminfo["SwapFree"]
    except (OSError, ValueError, IndexError):
        pass
    return sample


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_sentinel(payload: Dict[str, Any], home: Optional[Path]) -> None:
    path = get_lifecycle_sentinel_path(home)
    try:
        from utils import atomic_json_write

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, payload, indent=None)
    except Exception:
        logger.debug("Failed to write lifecycle sentinel", exc_info=True)


def _append_exit_diag(record: Dict[str, Any], home: Optional[Path]) -> None:
    """Append a JSON line to gateway-exit-diag.log (same format as the CLI's
    ``_exit_diag`` records so existing tooling greps both)."""
    base = home if home is not None else _process_hermes_home()
    path = base.joinpath(*_EXIT_DIAG_RELATIVE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        logger.debug("Failed to append unclean-exit record", exc_info=True)


def _pid_alive_with_start_time(pid: Any, start_time: Any) -> bool:
    """True when ``pid`` is a live process matching ``start_time`` (±2s).

    Guards the takeover race: during ``--replace`` the old gateway can still
    be mid-teardown when the new one boots — a live matching owner is a
    planned handover, not an unclean death.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        # NOT os.kill(pid, 0): on Windows that sends CTRL_C_EVENT to the
        # target's console group (bpo-14484). _pid_exists is the repo's
        # canonical no-kill cross-platform probe (psutil-backed).
        from gateway.status import _pid_exists

        if not _pid_exists(pid_int):
            return False
    except Exception:
        return False
    if start_time is None:
        return True  # alive; can't disambiguate PID reuse — err on "alive"
    try:
        from gateway.status import get_process_start_time

        actual = get_process_start_time(pid_int)
        if actual is None:
            return True
        return abs(float(actual) - float(start_time)) <= 2.0
    except Exception:
        return True


def detect_unclean_exit(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Inspect the previous life's sentinel; return an evidence dict when it
    died uncleanly, else ``None``.  Read-only — does not rewrite the sentinel.
    """
    sentinel = _read_json(get_lifecycle_sentinel_path(home))
    if not sentinel or sentinel.get("phase") != "running":
        return None
    if _pid_alive_with_start_time(sentinel.get("pid"), sentinel.get("start_time")):
        return None  # live owner — planned takeover in flight, not a death

    evidence: Dict[str, Any] = {
        "prior_pid": sentinel.get("pid"),
        "prior_started_at": sentinel.get("started_at"),
        "prior_start_time": sentinel.get("start_time"),
    }

    # Enrich with the last heartbeat: when did the loop last prove liveness,
    # and what did memory look like at that moment?
    try:
        from gateway.shutdown_watchdog import get_loop_heartbeat_path

        hb = _read_json(get_loop_heartbeat_path(home))
    except Exception:
        hb = None
    if hb:
        evidence["last_heartbeat_at"] = hb.get("updated_at")
        mem = hb.get("mem")
        if isinstance(mem, dict):
            evidence["last_heartbeat_mem"] = mem
            total = mem.get("mem_total_kib")
            avail = mem.get("mem_available_kib")
            if isinstance(avail, int) and (
                avail < _LOW_MEM_AVAILABLE_KIB
                or (
                    isinstance(total, int)
                    and total > 0
                    and avail / total < _LOW_MEM_AVAILABLE_FRACTION
                )
            ):
                evidence["suspected_oom"] = True
    return evidence


def check_state_db_integrity(
    home: Optional[Path] = None, *, _track_connection: bool = False
) -> str:
    """Return ``"ok"``, ``"absent"``, or the first ``quick_check`` complaint.

    Called only after an unclean death, because that is when the store may
    have been torn: a SIGKILL landing on a gateway mid-WAL-checkpoint can
    leave half-written b-tree pages behind (see
    ``_enforce_macos_synchronous_full`` in :mod:`hermes_state` — macOS
    ``fsync`` guarantees neither data-on-platter nor write ordering).

    The ``(1)`` in ``quick_check(1)`` caps how many corruption reports come
    back, not how much of the file is read: a *healthy* store is scanned in
    full regardless, so the cost scales with store size — cheap on a small
    store, minutes on a multi-GB one with a cold page cache. That is why the
    gateway runs this off the startup critical path (see
    :func:`start_state_db_integrity_check`) rather than inline. Corruption
    used to sit undetected for days (2026-08-26 → 08-30) because nothing
    ever looked.

    The connection is opened normally, not read-only: a WAL store needs its
    -shm sidecar for a read-only open. The PRAGMA itself writes nothing.
    Never raises — this is forensics, not lifecycle.

    ``_track_connection`` is private: when true, the live connection is
    registered with the module's background-worker registry for the
    duration of the query so :func:`interrupt_state_db_integrity_check` can
    reach it from another thread. Existing callers never pass it, and the
    default behaviour is unchanged.
    """
    global _integrity_connection
    base = home if home is not None else _process_hermes_home()
    path = base.joinpath(*_STATE_DB_RELATIVE)
    if not path.exists():
        return "absent"
    try:
        # A WAL store cannot be opened read-only without its -shm sidecar, so
        # open normally; quick_check itself writes nothing.
        conn = sqlite3.connect(str(path))
        if _track_connection:
            with _integrity_lock:
                _integrity_connection = conn
        try:
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
        finally:
            if _track_connection:
                with _integrity_lock:
                    _integrity_connection = None
            conn.close()
    except Exception as exc:  # sqlite3.Error, OSError, anything
        return _format_check_failure(exc)
    if not row or row[0] is None:
        return "check-failed: no result"
    return str(row[0])


def _format_check_failure(exc: BaseException) -> str:
    """The exact verdict string :func:`check_state_db_integrity` returns for
    a given exception. Shared so a caller can recognise one *specific*
    failure — SQLite's own ``interrupted`` — without a loose substring
    search of arbitrary corruption text (see
    :func:`_is_sqlite_interrupt_verdict`)."""
    return f"check-failed: {exc}"


def _is_sqlite_interrupt_verdict(verdict: str) -> bool:
    """True only when ``verdict`` is exactly what
    :func:`check_state_db_integrity` produces for its own
    ``sqlite3.OperationalError("interrupted")`` — i.e. the failure is
    ``connection.interrupt()`` breaking its own query, never a genuine
    corruption verdict that merely happens to be returned around the same
    time an interrupt was requested. Deliberately an exact comparison
    against the same formatting ``check_state_db_integrity`` uses, not a
    substring search, so real corruption text can never be swallowed."""
    return verdict == _format_check_failure(sqlite3.OperationalError("interrupted"))


# ── background integrity-check registry (shutdown safety) ──────────────────
#
# The gateway now runs check_state_db_integrity() on a daemon thread after
# startup (see start_state_db_integrity_check below) instead of inline, so a
# multi-minute scan of a multi-GB store never blocks TimeoutStartSec. That
# scan must not become a shutdown problem instead: sqlite3.Connection.interrupt()
# is documented safe to call from a different thread, so this tiny registry
# lets an atexit callback (armed once, the first time a worker starts) reach
# the live connection and stop it cleanly rather than leaving it to whatever
# the interpreter does with an abandoned daemon thread mid-syscall.

_integrity_lock = threading.Lock()
_integrity_connection: Optional[sqlite3.Connection] = None
_integrity_thread: Optional[threading.Thread] = None
_integrity_interrupt_event: Optional[threading.Event] = None
_integrity_atexit_registered = False


def interrupt_state_db_integrity_check(timeout: float = 2.0) -> None:
    """Stop a pending background integrity check and wait for it to finish.

    Safe to call with none pending (no-op) and safe to call more than once.
    Sets the registered interrupt event (so the worker labels its own
    verdict "interrupted" instead of a genuine failure), calls
    ``connection.interrupt()`` on the registered live connection if any,
    then joins the worker thread up to ``timeout`` seconds. Never raises.
    """
    with _integrity_lock:
        connection = _integrity_connection
        thread = _integrity_thread
        event = _integrity_interrupt_event
    if event is not None:
        event.set()
    if connection is not None:
        try:
            connection.interrupt()
        except Exception:
            logger.debug("state.db integrity check interrupt() failed", exc_info=True)
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def start_state_db_integrity_check(
    evidence: Optional[Dict[str, Any]], home: Optional[Path] = None
) -> Optional[threading.Thread]:
    """Run :func:`check_state_db_integrity` off the startup critical path.

    Call this only after the gateway has reported ready. ``evidence`` is
    the dict :func:`record_startup` returned (``None`` means the previous
    life exited cleanly, so there is nothing to check — this returns
    ``None`` without starting anything).

    Starts exactly one daemon thread (never ``asyncio.to_thread`` and never
    the default executor: executor threads are joined at interpreter exit,
    which would turn this startup-time optimization into a shutdown hang —
    a plain daemon thread is simply abandoned). The thread measures its own
    elapsed time, runs the real check with its connection tracked for
    interruption, appends one ``gateway.state_db_integrity_check`` exit-diag
    record, and logs ERROR only for a genuine failure (never for one this
    process itself interrupted, e.g. via
    :func:`interrupt_state_db_integrity_check` at shutdown). It never
    touches the lifecycle sentinel — that stays exclusively synchronous
    bookkeeping in :func:`record_startup` / :func:`mark_exited` so a late
    worker can never clobber a replacement gateway's claim on it.
    """
    global _integrity_thread, _integrity_interrupt_event, _integrity_atexit_registered
    if evidence is None:
        return None

    interrupt_event = threading.Event()

    def _worker() -> None:
        global _integrity_connection, _integrity_thread, _integrity_interrupt_event
        start = time.monotonic()
        try:
            verdict = check_state_db_integrity(home=home, _track_connection=True)
        except Exception as exc:  # check_state_db_integrity never raises; belt & suspenders
            verdict = f"check-failed: {exc}"
        elapsed = time.monotonic() - start
        # Relabel only when the failure IS the interrupt — a genuine
        # corruption verdict that happens to land around the same moment a
        # shutdown requested an interrupt must keep its real text (and its
        # ERROR log below), never be swallowed as "interrupted".
        if interrupt_event.is_set() and _is_sqlite_interrupt_verdict(verdict):
            verdict = "interrupted"
        try:
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "tag": "gateway.state_db_integrity_check",
                "pid": os.getpid(),
                "prior_pid": evidence.get("prior_pid"),
                "prior_started_at": evidence.get("prior_started_at"),
                "state_db_integrity": verdict,
                "elapsed_s": round(elapsed, 1),
            }
            _append_exit_diag(record, home)
            if verdict not in ("ok", "absent", "interrupted"):
                logger.error(
                    "state.db FAILED integrity check after an unclean gateway "
                    "exit: %s — sessions may read as missing until it is "
                    "repaired. Run `hermes doctor`.",
                    verdict,
                )
            else:
                logger.info(
                    "Background state.db integrity check finished: %s (%.1fs)",
                    verdict,
                    elapsed,
                )
        except Exception:
            logger.debug("Background integrity-check bookkeeping failed", exc_info=True)
        finally:
            with _integrity_lock:
                _integrity_connection = None
                _integrity_thread = None
                _integrity_interrupt_event = None

    thread = threading.Thread(
        target=_worker, daemon=True, name="state-db-integrity-check"
    )
    with _integrity_lock:
        _integrity_thread = thread
        _integrity_interrupt_event = interrupt_event
        if not _integrity_atexit_registered:
            atexit.register(interrupt_state_db_integrity_check)
            _integrity_atexit_registered = True
    thread.start()
    return thread


def record_startup(
    home: Optional[Path] = None, *, defer_integrity_check: bool = False
) -> Optional[Dict[str, Any]]:
    """Boot-time entry point: report any unclean previous exit, then claim
    the sentinel for the current life.

    Returns the unclean-exit evidence dict (also persisted to
    ``gateway-exit-diag.log`` and logged at WARNING) or ``None``.  Never
    raises.

    ``defer_integrity_check`` (default ``False``, unchanged behaviour): when
    ``True``, the state.db scan is skipped here — ``evidence["state_db_
    integrity"]`` is set to ``"deferred"`` instead of a real verdict, and
    the caller is expected to run it separately via
    :func:`start_state_db_integrity_check`. Unclean-exit detection, the
    exit-diag record, the WARNING log and the sentinel claim below are all
    synchronous either way; only the (potentially minutes-long) scan moves.
    """
    evidence: Optional[Dict[str, Any]] = None
    try:
        evidence = detect_unclean_exit(home)
        if evidence is not None:
            # The death may have torn the store. This is the only moment
            # we know to look, and looking is what turns a 3.5-day silent
            # corruption into a startup warning.
            if defer_integrity_check:
                verdict = "deferred"
            else:
                verdict = check_state_db_integrity(home=home)
            evidence["state_db_integrity"] = verdict
            if verdict not in ("ok", "absent", "deferred"):
                logger.error(
                    "state.db FAILED integrity check after an unclean gateway "
                    "exit: %s — sessions may read as missing until it is "
                    "repaired. Run `hermes doctor`.",
                    verdict,
                )
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "tag": "gateway.previous_unclean_exit",
                "pid": os.getpid(),
                **evidence,
            }
            _append_exit_diag(record, home)
            logger.warning(
                "Previous gateway life (pid=%s, started_at=%s) exited UNCLEANLY "
                "(no exit path ran — SIGKILL / OOM / VM death). "
                "last_heartbeat_at=%s last_mem=%s suspected_oom=%s",
                evidence.get("prior_pid"),
                evidence.get("prior_started_at"),
                evidence.get("last_heartbeat_at"),
                evidence.get("last_heartbeat_mem"),
                evidence.get("suspected_oom", False),
            )
    except Exception:
        logger.debug("Unclean-exit detection failed", exc_info=True)

    try:
        claim: Dict[str, Any] = {
            "phase": "running",
            "pid": os.getpid(),
            "start_time": time.time(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        # Carry the verdict on the PREVIOUS life forward on the new
        # sentinel: it is the only place the finding survives in
        # machine-readable form (the exit-diag log is append-only prose
        # for humans), and /api/status reads it to tell the user "your
        # agent restarted after (suspected) running out of memory"
        # (NS-656).  Scoped to this life only — the next clean exit or
        # boot rewrites the sentinel and the flags age out with it.
        if evidence is not None:
            claim["prior_unclean_exit"] = True
            if evidence.get("suspected_oom"):
                claim["prior_suspected_oom"] = True
        _write_sentinel(claim, home)
    except Exception:
        logger.debug("Failed to claim lifecycle sentinel", exc_info=True)
    return evidence


def mark_exited(
    exit_code: Optional[int] = None,
    reason: str = "graceful_shutdown",
    home: Optional[Path] = None,
) -> None:
    """Mark the current life as cleanly exited.  Idempotent, never raises.

    Only rewrites the sentinel when it is provably owned by this process —
    during a ``--replace`` takeover the replacement claims the sentinel
    before the old process finishes teardown, and the old life must not
    clobber the new owner's ``running`` phase on its way out.  A sentinel
    with ``pid=None`` (or a malformed pid) has *unknown* ownership and is
    likewise left alone: we must not overwrite evidence we cannot prove is
    ours with a ``clean exit`` claim.
    """
    try:
        sentinel = _read_json(get_lifecycle_sentinel_path(home))
        if sentinel is not None and sentinel.get("pid") != os.getpid():
            return
        _write_sentinel(
            {
                "phase": "exited",
                "pid": os.getpid(),
                "exit_code": exit_code,
                "exit_reason": reason,
                "exited_at": datetime.now(timezone.utc).isoformat(),
            },
            home,
        )
    except Exception:
        logger.debug("Failed to mark lifecycle sentinel exited", exc_info=True)


def read_prior_exit_label(profile_home: Path) -> str:
    """Container-boot helper: one-word summary of how the profile's last
    gateway life ended.  ``clean`` / ``unclean`` / ``unknown`` (no sentinel
    or never ran).  Read-only and exception-free — used by
    ``hermes_cli.container_boot`` to annotate ``container-boot.log``.
    """
    try:
        sentinel = _read_json(get_lifecycle_sentinel_path(profile_home))
        if not sentinel:
            return "unknown"
        phase = sentinel.get("phase")
        if phase == "exited":
            return "clean"
        if phase == "running":
            # At container boot the old PID namespace is gone — any
            # "running" sentinel is from a life that never exited cleanly.
            return "unclean"
    except Exception:
        pass
    return "unknown"
