#!/usr/bin/env python3
"""Portable inert single-card proof of the Sol post-READY lifecycle."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from hermes_cli import kanban_db as kb

AUDIT = Path.home() / ".hermes" / "audits" / "sol-post-ready-manual-pilot"
PILOT_HOME = AUDIT / "pilot-home"
PILOT_BOARD = "sol-pilot"
PILOT_DB = PILOT_HOME / "kanban" / "boards" / PILOT_BOARD / "kanban.db"
EVIDENCE = AUDIT / "manual-pilot-receipts"


def snapshot(conn, task_id: str) -> dict:
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    events = conn.execute(
        "SELECT kind,payload,run_id,created_at FROM task_events WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall()
    runs = conn.execute(
        "SELECT id,status,outcome,summary,metadata,started_at,ended_at FROM task_runs WHERE task_id=? ORDER BY id",
        (task_id,),
    ).fetchall()
    keys = (
        "id", "status", "assignee", "claim_lock", "claim_expires", "worker_pid",
        "last_heartbeat_at", "current_run_id", "result", "consecutive_failures",
    )
    return {
        "task": {k: task[k] for k in keys},
        "events": [dict(row) for row in events],
        "runs": [dict(row) for row in runs],
    }


def write_json(name: str, value) -> None:
    (EVIDENCE / name).write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    shutil.rmtree(PILOT_HOME, ignore_errors=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    kb.init_db(board=PILOT_BOARD)
    summary = {
        "mode": "manual/inert", "real_worker_spawned": False,
        "live_sol_mutated": False, "board": PILOT_BOARD, "db": str(PILOT_DB),
    }
    with kb.connect(board=PILOT_BOARD) as conn:
        task_id = kb.create_task(
            conn, title="manual post-READY pilot", assignee="default",
            created_by="MOTHERSHIP", workspace_kind="scratch", board=PILOT_BOARD,
        )
        summary["task_id"] = task_id
        first = kb.claim_task(conn, task_id, claimer="FRESH:manual-owner", ttl_seconds=300)
        second = kb.claim_task(conn, task_id, claimer="FRESH:duplicate-owner", ttl_seconds=300)
        assert first is not None and second is None
        run_id = first.current_run_id
        write_json("01-atomic-claim.json", snapshot(conn, task_id))
        assert kb.heartbeat_claim(conn, task_id, claimer="FRESH:manual-owner", ttl_seconds=300)
        assert kb.heartbeat_worker(
            conn, task_id, note="IMPLEMENTED payload; VERIFYING sha256", expected_run_id=run_id,
        )
        write_json("02-running-progress.json", snapshot(conn, task_id))
        payload = EVIDENCE / "payload.txt"
        payload.write_text("SOL-PILOT-OK\n", encoding="utf-8")
        expected = hashlib.sha256(b"SOL-PILOT-OK\n").hexdigest()
        actual = hashlib.sha256(payload.read_bytes()).hexdigest()
        verify_line = f"PASS sha256 expected={expected} actual={actual} bytes={payload.stat().st_size}"
        assert actual == expected
        assert kb.complete_task(
            conn, task_id, result=verify_line, summary="DONE: canonical sha256 passed.",
            metadata={"artifacts": [str(payload)], "verification": verify_line},
            expected_run_id=run_id,
        )
        done = snapshot(conn, task_id)
        write_json("03-done-evidence.json", done)

        live_id = kb.create_task(conn, title="live lease guard", assignee="default", board=PILOT_BOARD)
        live_claim = kb.claim_task(conn, live_id, claimer="FRESH:live-owner", ttl_seconds=1)
        assert live_claim is not None
        assert kb.claim_task(conn, live_id, claimer="FRESH:duplicate") is None

        dead_id = kb.create_task(
            conn, title="confirmed-dead timeout", assignee="default", board=PILOT_BOARD,
            max_runtime_seconds=1, max_retries=2,
        )
        dead_claim = kb.claim_task(conn, dead_id, claimer="FRESH:dead-owner", ttl_seconds=30)
        assert dead_claim is not None
        old_run = dead_claim.current_run_id
        assert old_run is not None
        kb._set_worker_pid(conn, dead_id, 444444)
        past = int(time.time()) - 30
        conn.execute("UPDATE tasks SET started_at=? WHERE id=?", (past, dead_id))
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (past, old_run))
        original_alive = kb._pid_alive
        try:
            kb._pid_alive = lambda _pid: False
            assert kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None) == [dead_id]
        finally:
            kb._pid_alive = original_alive
        new_claim = kb.claim_task(conn, dead_id, claimer="FRESH:replacement", ttl_seconds=30)
        assert new_claim is not None and new_claim.current_run_id != old_run
        assert kb.complete_task(conn, dead_id, result="stale", expected_run_id=old_run) is False
        question = "Can you approve one bounded retry after the proven timeout?"
        assert kb.block_task(
            conn, dead_id, reason=question, kind="needs_input",
            expected_run_id=new_claim.current_run_id,
        )
        write_json("04-duplicate-lease-timeout-blocked.json", snapshot(conn, dead_id))

    summary.update({
        "ordinary_terminal_state": "done", "verification": verify_line,
        "atomic_claims": 1, "duplicate_claim_rejected": True,
        "meaningful_progress_recorded": True, "timeout_requeued_once": True,
        "old_run_completion_rejected": True, "blocked_question": question,
    })
    write_json("PILOT-SUMMARY.json", summary)
    return 0
