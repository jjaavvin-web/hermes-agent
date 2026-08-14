from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


PILOT = Path(__file__).parent / "fixtures" / "sol_post_ready_manual_pilot.py"


def _load_pilot():
    spec = importlib.util.spec_from_file_location("sol_manual_pilot", PILOT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manual_pilot_full_loop_receipts(tmp_path, monkeypatch):
    pilot = _load_pilot()
    audit = tmp_path / "audit"
    home = audit / "pilot-home"
    evidence = audit / "manual-pilot-receipts"
    monkeypatch.setattr(pilot, "AUDIT", audit)
    monkeypatch.setattr(pilot, "PILOT_HOME", home)
    monkeypatch.setattr(pilot, "PILOT_DB", home / "kanban" / "boards" / pilot.PILOT_BOARD / "kanban.db")
    monkeypatch.setattr(pilot, "EVIDENCE", evidence)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert pilot.main() == 0
    summary = json.loads((evidence / "PILOT-SUMMARY.json").read_text(encoding="utf-8"))
    assert summary["ordinary_terminal_state"] == "done"
    assert summary["atomic_claims"] == 1
    assert summary["duplicate_claim_rejected"] is True
    assert summary["meaningful_progress_recorded"] is True
    assert summary["timeout_requeued_once"] is True
    assert summary["old_run_completion_rejected"] is True
    assert summary["real_worker_spawned"] is False
    assert summary["live_sol_mutated"] is False

    payload = evidence / "payload.txt"
    assert payload.read_text(encoding="utf-8") == "SOL-PILOT-OK\n"
    assert hashlib.sha256(payload.read_bytes()).hexdigest() == "82981565f2ec532547cd6b125264c2439f60a28f3767f0516b61328f16d8cb4e"

    with kb.connect(board=pilot.PILOT_BOARD) as conn:
        done_id = summary["task_id"]
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (done_id,)).fetchone()
        assert row["status"] == "done"
        event_counts = {
            row["kind"]: row["n"]
            for row in conn.execute(
                "SELECT kind,COUNT(*) AS n FROM task_events WHERE task_id=? GROUP BY kind",
                (done_id,),
            )
        }
        assert event_counts["claimed"] == 1
        assert event_counts["heartbeat"] == 1
        assert event_counts["completed"] == 1
