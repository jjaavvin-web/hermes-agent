"""An unclean gateway death must trigger a state.db integrity check.

Regression for the 2026-08-31 incident. ``state.db`` was corrupt from
2026-08-26 evening (a SIGKILL landed on a gateway mid-WAL-checkpoint during a
``--replace`` restart storm), but nothing checked the file. The damage sat in
old, rarely-read session rows for 3.5 days until a Desktop read tripped over
it on 2026-08-30 17:15 and surfaced as "Session not found".

``record_startup`` already detects the unclean exit and logs "SIGKILL / OOM /
VM death" — it just never looked at the database that death may have torn.
The check is gated on the unclean exit precisely because it costs ~2s on a
500MB store; a clean boot must not pay it.

The second half of this module (T1-T7) is the regression suite for the
2026-09-11 cold-boot-timeout fix: the store is now multi-GB, so the check
above is deferred off the startup critical path and run on a background
daemon thread instead — see ``gateway.lifecycle_ledger.
start_state_db_integrity_check`` and ``DIAGNOSIS.md`` under this fix's audit
directory for the incident evidence.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from gateway.lifecycle_ledger import (
    check_state_db_integrity,
    get_lifecycle_sentinel_path,
    record_startup,
)

_DEAD_PID = 2 ** 22 + 12345  # beyond default pid_max; never alive


def _write_sentinel(home: Path, phase: str = "running") -> None:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "phase": phase,
            "pid": _DEAD_PID,
            "start_time": 1000.0,
            "started_at": "2026-08-26T23:56:45+00:00",
        }),
        encoding="utf-8",
    )


def _make_state_db(home: Path, *, corrupt: bool) -> Path:
    """Build a real SQLite file, optionally with a genuinely torn b-tree page."""
    path = home / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}" * 40,) for i in range(4000)]
    )
    conn.commit()
    conn.close()
    if corrupt:
        with open(path, "r+b") as handle:
            handle.seek(4096 * 6)
            handle.write(b"\xEF" * 4096)
    return path


def _exit_diag_records(home: Path) -> list:
    log = home / "logs" / "gateway-exit-diag.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ── the checker itself ──────────────────────────────────────────────────────


def test_checker_passes_a_healthy_store(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    assert check_state_db_integrity(home=tmp_path) == "ok"


def test_checker_reports_a_torn_btree_page(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    verdict = check_state_db_integrity(home=tmp_path)
    assert verdict != "ok"
    assert "btreeInitPage" in verdict or "malformed" in verdict.lower()


def test_checker_tolerates_a_missing_store(tmp_path: Path) -> None:
    assert check_state_db_integrity(home=tmp_path) == "absent"


# ── wiring into the unclean-exit path ───────────────────────────────────────


def test_unclean_exit_records_the_corruption_verdict(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    assert evidence["state_db_integrity"] != "ok"
    record = _exit_diag_records(tmp_path)[0]
    assert record["state_db_integrity"] != "ok"


def test_unclean_exit_on_a_healthy_store_records_ok(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    assert evidence["state_db_integrity"] == "ok"


def test_clean_exit_does_not_pay_for_the_check(tmp_path: Path, monkeypatch) -> None:
    """A clean boot must not scan the store — that is the whole cost gate."""
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path, phase="exited")

    called = []
    import gateway.lifecycle_ledger as ledger

    monkeypatch.setattr(
        ledger, "check_state_db_integrity", lambda **kw: called.append(1) or "ok"
    )
    record_startup(home=tmp_path)

    assert not called, "integrity check ran on a clean boot"


# ── deferred check: the 2026-09-11 cold-boot-timeout fix ───────────────────
#
# The store is now 11x the docstring's "500MB" budget and the scan is
# I/O-bound on a cold page cache (see DIAGNOSIS.md), so ``record_startup``
# gets a ``defer_integrity_check`` escape hatch and the real scan moves to a
# background daemon thread the gateway starts only after it is READY.


def test_T1_defer_is_fast_and_keeps_synchronous_bookkeeping(tmp_path: Path, monkeypatch) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    def _slow_check(**kw):
        time.sleep(3)
        return "ok"

    import gateway.lifecycle_ledger as ledger

    monkeypatch.setattr(ledger, "check_state_db_integrity", _slow_check)

    start = time.monotonic()
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"deferred record_startup blocked for {elapsed:.2f}s"
    assert evidence is not None
    assert evidence["state_db_integrity"] == "deferred"

    records = _exit_diag_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tag"] == "gateway.previous_unclean_exit"
    assert records[0]["prior_pid"] == _DEAD_PID

    sentinel = json.loads(get_lifecycle_sentinel_path(tmp_path).read_text(encoding="utf-8"))
    assert sentinel["phase"] == "running"
    assert sentinel["pid"] == os.getpid()


def test_T1_default_call_still_runs_the_check_synchronously(tmp_path: Path, monkeypatch) -> None:
    """No kwarg → existing behaviour: the check runs inline, on the caller's
    thread, before record_startup returns."""
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    calls = []
    import gateway.lifecycle_ledger as ledger

    real_check = ledger.check_state_db_integrity

    def _counting_check(**kw):
        calls.append(1)
        return real_check(**kw)

    monkeypatch.setattr(ledger, "check_state_db_integrity", _counting_check)
    evidence = record_startup(home=tmp_path)

    assert calls == [1]
    assert evidence is not None
    assert evidence["state_db_integrity"] == "ok"


def test_T2_background_check_keeps_loop_responsive_and_persists_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None

    import gateway.lifecycle_ledger as ledger

    gate = threading.Event()

    def _blocking_check(*, home=None, _track_connection=False):
        gate.wait(timeout=5)
        return "ok"

    monkeypatch.setattr(ledger, "check_state_db_integrity", _blocking_check)

    thread = ledger.start_state_db_integrity_check(evidence, home=tmp_path)
    assert thread is not None
    assert thread.is_alive()

    ticks = 0

    async def _count_ticks_while_blocked() -> None:
        nonlocal ticks
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            ticks += 1

    asyncio.run(_count_ticks_while_blocked())
    assert ticks > 5, "the event loop stalled behind the background check"
    assert thread.is_alive(), "the background check finished before the loop was probed"

    gate.set()
    thread.join(timeout=2)
    assert not thread.is_alive()

    records = [
        r for r in _exit_diag_records(tmp_path)
        if r["tag"] == "gateway.state_db_integrity_check"
    ]
    assert len(records) == 1
    rec = records[0]
    assert rec["pid"] == os.getpid()
    assert rec["prior_pid"] == evidence.get("prior_pid")
    assert rec["prior_started_at"] == evidence.get("prior_started_at")
    assert rec["state_db_integrity"] == "ok"
    assert isinstance(rec["elapsed_s"], (int, float))
    assert rec["elapsed_s"] >= 0


def test_T3_background_check_on_real_corruption_logs_error(
    tmp_path: Path, caplog
) -> None:
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None

    import gateway.lifecycle_ledger as ledger

    with caplog.at_level(logging.ERROR, logger="gateway.lifecycle_ledger"):
        thread = ledger.start_state_db_integrity_check(evidence, home=tmp_path)
        assert thread is not None
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert any("FAILED integrity check" in r.getMessage() for r in caplog.records)

    records = [
        r for r in _exit_diag_records(tmp_path)
        if r["tag"] == "gateway.state_db_integrity_check"
    ]
    assert len(records) == 1
    assert records[0]["state_db_integrity"] != "ok"


def test_T4_background_check_tolerates_concurrent_wal_writes(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    seed.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}",) for i in range(500)]
    )
    seed.commit()
    seed.close()

    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None

    import gateway.lifecycle_ledger as ledger

    writer_errors: list = []

    def _writer() -> None:
        try:
            wconn = sqlite3.connect(path, timeout=10)
            for i in range(500, 700):
                wconn.execute("INSERT INTO sessions (v) VALUES (?)", (f"row-{i}",))
                wconn.commit()
            wconn.close()
        except Exception as exc:  # pragma: no cover - assertion below reports it
            writer_errors.append(exc)

    writer = threading.Thread(target=_writer)
    writer.start()

    thread = ledger.start_state_db_integrity_check(evidence, home=tmp_path)
    assert thread is not None
    thread.join(timeout=10)
    writer.join(timeout=10)

    assert not writer_errors, writer_errors
    assert not writer.is_alive()
    assert not thread.is_alive()

    records = [
        r for r in _exit_diag_records(tmp_path)
        if r["tag"] == "gateway.state_db_integrity_check"
    ]
    assert records[-1]["state_db_integrity"] == "ok"

    verify = sqlite3.connect(path)
    count = verify.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    verify.close()
    assert count == 700


def test_T5a_shutdown_with_pending_check_exits_promptly(tmp_path: Path) -> None:
    """A daemon worker (not asyncio.to_thread / the default executor) must
    never turn a startup hang into a shutdown hang."""
    repo_root = Path(__file__).resolve().parents[2]
    code = (
        "import os, sys, threading\n"
        "import gateway.lifecycle_ledger as ledger\n"
        "def _blocking(*, home=None, _track_connection=False):\n"
        "    threading.Event().wait()\n"
        "    return 'ok'\n"
        "ledger.check_state_db_integrity = _blocking\n"
        "ledger.start_state_db_integrity_check({'prior_pid': 1}, home=os.environ['PROBE_HOME'])\n"
        "sys.exit(0)\n"
    )
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PROBE_HOME"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        timeout=10,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"subprocess did not exit cleanly: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_T5b_interrupt_stops_the_check_and_records_interrupted_not_error(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """A real sqlite connection, registered through the same shutdown-safety
    registry the background worker uses, running an endless recursive CTE —
    proves ``interrupt_state_db_integrity_check`` actually stops a live,
    blocking query cross-thread (``sqlite3.Connection.interrupt`` is
    documented safe for that), and that the worker labels the result
    "interrupted" rather than a genuine failure."""
    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None

    import gateway.lifecycle_ledger as ledger

    started = threading.Event()

    def _fake_check(*, home=None, _track_connection=False):
        conn = sqlite3.connect(":memory:")
        if _track_connection:
            with ledger._integrity_lock:
                ledger._integrity_connection = conn
        # sqlite3_interrupt() has NO EFFECT on a statement started after it
        # returns (documented behaviour) — signalling "started" before
        # conn.execute() begins would race: interrupt() could land in the
        # gap and the recursive CTE would then run unaffected, forever. A
        # progress handler fires from *inside* the running query, so it
        # proves the statement is actually executing before we interrupt it.
        conn.set_progress_handler(lambda: started.set() or False, 1000)
        try:
            conn.execute(
                "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt) "
                "SELECT max(x) FROM cnt"
            )
            return "ok"
        except sqlite3.OperationalError as exc:
            return f"check-failed: {exc}"
        finally:
            if _track_connection:
                with ledger._integrity_lock:
                    ledger._integrity_connection = None
            conn.close()

    monkeypatch.setattr(ledger, "check_state_db_integrity", _fake_check)

    with caplog.at_level(logging.DEBUG, logger="gateway.lifecycle_ledger"):
        thread = ledger.start_state_db_integrity_check(evidence, home=tmp_path)
        assert thread is not None
        assert started.wait(timeout=5), "fake check never started"
        ledger.interrupt_state_db_integrity_check()
        thread.join(timeout=2)

    assert not thread.is_alive()

    records = [
        r for r in _exit_diag_records(tmp_path)
        if r["tag"] == "gateway.state_db_integrity_check"
    ]
    assert records[-1]["state_db_integrity"] == "interrupted"

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert not error_records, "an interrupted check must never log ERROR"
    info_records = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "interrupted" in r.getMessage()
    ]
    assert info_records


def test_T6_background_worker_never_writes_the_sentinel(
    tmp_path: Path, monkeypatch
) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None

    import gateway.lifecycle_ledger as ledger

    gate = threading.Event()

    def _blocking_check(*, home=None, _track_connection=False):
        gate.wait(timeout=5)
        return "ok"

    monkeypatch.setattr(ledger, "check_state_db_integrity", _blocking_check)
    thread = ledger.start_state_db_integrity_check(evidence, home=tmp_path)
    assert thread is not None

    sentinel_path = get_lifecycle_sentinel_path(tmp_path)

    # Simulate a replacement gateway claiming the sentinel while the worker
    # is still blocked inside the (fake) check.
    sentinel_path.write_text(
        json.dumps({"phase": "running", "pid": 999999, "start_time": 0.0}),
        encoding="utf-8",
    )
    replaced = sentinel_path.read_bytes()

    gate.set()
    thread.join(timeout=2)
    assert not thread.is_alive()

    after = sentinel_path.read_bytes()
    assert after == replaced, "background worker must never write the sentinel"


def test_T7_info_line_includes_elapsed_seconds(tmp_path: Path, caplog) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None

    import gateway.lifecycle_ledger as ledger

    with caplog.at_level(logging.INFO, logger="gateway.lifecycle_ledger"):
        thread = ledger.start_state_db_integrity_check(evidence, home=tmp_path)
        assert thread is not None
        thread.join(timeout=10)

    info_records = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "integrity check" in r.getMessage().lower()
    ]
    assert info_records, "no INFO record for the finished background check"
    message = info_records[-1].getMessage()
    assert "ok" in message
    match = re.search(r"(\d+(?:\.\d+)?)\s*s", message)
    assert match is not None, f"no elapsed-seconds figure in: {message!r}"
    assert float(match.group(1)) >= 0


def test_T5c_interrupt_does_not_relabel_a_genuine_unrelated_failure(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """A real, non-interrupt failure verdict returned while an interrupt is
    in flight must keep its own text and its ERROR log — only SQLite's own
    "interrupted" OperationalError (see T5b) may be relabelled. Regression
    for the original ``_worker`` bug: it relabelled ANY failing verdict as
    "interrupted" whenever ``interrupt_event.is_set()``, which would have
    swallowed a genuine corruption verdict discovered right as a shutdown
    happened to be requested."""
    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None

    import gateway.lifecycle_ledger as ledger

    release = threading.Event()

    def _fake_check(*, home=None, _track_connection=False):
        # No connection registered: this check is unrelated to the
        # interrupt plumbing entirely — it just happens to be in flight
        # when interrupt_state_db_integrity_check() is called below.
        release.wait(timeout=5)
        return "database disk image is malformed"

    monkeypatch.setattr(ledger, "check_state_db_integrity", _fake_check)

    with caplog.at_level(logging.INFO, logger="gateway.lifecycle_ledger"):
        thread = ledger.start_state_db_integrity_check(evidence, home=tmp_path)
        assert thread is not None

        # Request an interrupt while the genuinely-failing check is still
        # in flight (no live connection to actually interrupt — the check
        # ignores it and keeps running until released below). timeout=0.2
        # so this does not block waiting on a thread that cannot finish yet.
        ledger.interrupt_state_db_integrity_check(timeout=0.2)
        assert thread.is_alive(), "fake check returned before being released"

        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()

    records = [
        r for r in _exit_diag_records(tmp_path)
        if r["tag"] == "gateway.state_db_integrity_check"
    ]
    assert records[-1]["state_db_integrity"] == "database disk image is malformed", (
        "an interrupt request must never relabel an unrelated genuine failure"
    )

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("FAILED integrity check" in r.getMessage() for r in error_records), (
        "a genuine failure coinciding with an interrupt request must still log ERROR"
    )


# ── the REAL check_state_db_integrity(_track_connection=True) wiring ───────
#
# T5b/T6/T7 above exercise start_state_db_integrity_check's registry and
# labeling logic against a *fake* check_state_db_integrity. Nothing drove
# the real function's own registration lines — deleting them left every
# test in this module green.


def test_T8_real_check_registers_the_live_connection_and_is_interruptible(
    tmp_path: Path, monkeypatch
) -> None:
    """Runs the REAL ``check_state_db_integrity(_track_connection=True)`` in
    a thread against a real WAL database, holding its query in flight
    deterministically via a wrapped ``sqlite3.connect`` whose progress
    handler blocks until released. Proves: the registry holds the live
    connection while the real check is running, ``interrupt_state_db_
    integrity_check()`` actually stops it, the verdict comes back
    "interrupted", and the registry is cleared afterward. Bounded waits
    only — must never hang."""
    path = tmp_path / "state.db"
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    seed.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}" * 40,) for i in range(4000)]
    )
    seed.commit()
    seed.close()

    import gateway.lifecycle_ledger as ledger

    in_flight = threading.Event()
    release = threading.Event()
    real_connect = sqlite3.connect

    def _progress_handler() -> int:
        in_flight.set()
        release.wait(timeout=5)
        return 0  # always continue — never abort itself

    def _connect_wrapper(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_progress_handler(_progress_handler, 1)
        return conn

    monkeypatch.setattr(ledger.sqlite3, "connect", _connect_wrapper)

    result: dict = {}

    def _runner() -> None:
        result["verdict"] = ledger.check_state_db_integrity(
            home=tmp_path, _track_connection=True
        )

    # daemon=True + release always set in `finally` below: if an assertion
    # here fails, the progress handler must still be unblocked so quick_check
    # can finish (N=1 means it would otherwise re-block on every remaining
    # VM instruction) — a bounded test must never leave a runaway thread
    # behind just because it failed.
    runner_thread = threading.Thread(target=_runner, daemon=True)
    runner_thread.start()

    try:
        assert in_flight.wait(timeout=5), "the real quick_check never started running"
        with ledger._integrity_lock:
            assert ledger._integrity_connection is not None, (
                "the registry must hold the live connection while the real "
                "check is in flight"
            )
        ledger.interrupt_state_db_integrity_check(timeout=0)
    finally:
        release.set()

    runner_thread.join(timeout=5)
    assert not runner_thread.is_alive(), "real check did not stop after interrupt()"

    assert result.get("verdict") == "check-failed: interrupted"
    with ledger._integrity_lock:
        assert ledger._integrity_connection is None, (
            "the registry must be cleared once the real check returns"
        )


# ── gateway/run.py wiring: the boot-start regression itself ────────────────
#
# Nothing exercised gateway.run.start_gateway's actual call sites: flipping
# defer_integrity_check=True back to False at the record_startup call, or
# moving/removing the start_state_db_integrity_check call relative to the
# runner.start() success gate, left every other test in this module green.
# AST-based (not a brittle full-string match) so unrelated formatting
# changes elsewhere in the (huge) function don't false-positive this.


def _start_gateway_ast() -> ast.AsyncFunctionDef:
    import gateway.run as run_module

    source = Path(inspect.getfile(run_module)).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=inspect.getfile(run_module))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "start_gateway":
            return node
    raise AssertionError("gateway.run.start_gateway not found")


def _imported_alias(func: ast.AST, module: str, name: str) -> str:
    for node in ast.walk(func):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            for alias in node.names:
                if alias.name == name:
                    return alias.asname or alias.name
    raise AssertionError(f"start_gateway no longer imports {module}.{name}")


def _calls_to_name(func: ast.AST, name: str) -> list:
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]


def test_T9_start_gateway_defers_the_integrity_check_at_record_startup() -> None:
    """Regression: flipping ``defer_integrity_check=True`` back to
    ``False`` (or dropping the kwarg) at the ``record_startup`` call site
    silently reverts the whole cold-boot-timeout fix — nothing else drives
    ``start_gateway`` in this suite, so that regression previously left
    every test green."""
    func = _start_gateway_ast()
    alias = _imported_alias(func, "gateway.lifecycle_ledger", "record_startup")

    calls = _calls_to_name(func, alias)
    assert len(calls) == 1, f"expected exactly one call to {alias}(), found {len(calls)}"
    call = calls[0]

    defer_kwargs = [kw for kw in call.keywords if kw.arg == "defer_integrity_check"]
    assert len(defer_kwargs) == 1, (
        f"{alias}() call is missing the defer_integrity_check= keyword"
    )
    value = defer_kwargs[0].value
    assert isinstance(value, ast.Constant) and value.value is True, (
        f"{alias}() must be called with defer_integrity_check=True, "
        f"found {ast.dump(value)}"
    )


def test_T10_start_gateway_starts_the_background_check_after_the_success_gate() -> None:
    """Regression: the background scan must start only after ``runner.
    start()``'s success gate (``if not success: ... return False``) — a
    move that put it earlier, or dropped it, previously left every test in
    this module green."""
    func = _start_gateway_ast()
    alias = _imported_alias(func, "gateway.lifecycle_ledger", "start_state_db_integrity_check")

    calls = _calls_to_name(func, alias)
    assert len(calls) == 1, f"expected exactly one call to {alias}(), found {len(calls)}"
    call_line = calls[0].lineno

    success_assign = next(
        (
            n for n in ast.walk(func)
            if isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "success"
        ),
        None,
    )
    assert success_assign is not None, (
        "no `success = ...` assignment found in start_gateway"
    )

    success_gate = next(
        (
            n for n in ast.walk(func)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.UnaryOp)
            and isinstance(n.test.op, ast.Not)
            and isinstance(n.test.operand, ast.Name)
            and n.test.operand.id == "success"
        ),
        None,
    )
    assert success_gate is not None, (
        "no `if not success:` gate found in start_gateway"
    )

    assert success_assign.lineno < success_gate.lineno, (
        "the `if not success` gate must come after the `success` assignment"
    )
    gate_end = getattr(success_gate, "end_lineno", success_gate.lineno)
    assert call_line > gate_end, (
        f"{alias}() must be called after the `if not success` gate ends "
        f"(gate ends at line {gate_end}, call is at line {call_line})"
    )
