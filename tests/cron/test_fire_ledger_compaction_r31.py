from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone


def _patch_cron_paths(tmp_path, monkeypatch):
    from cron import jobs as cron_jobs

    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path)
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cron_jobs, "CRON_FIRE_LEDGER_FILE", tmp_path / "fire_ledger.jsonl")
    tmp_path.mkdir(parents=True, exist_ok=True)
    return cron_jobs


def test_fire_ledger_compaction_keeps_outstanding_and_recent_completed(tmp_path, monkeypatch):
    cron_jobs = _patch_cron_paths(tmp_path, monkeypatch)
    now = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=cron_jobs.CRON_FIRE_LEDGER_RETENTION_DAYS + 2)
    recent = now - timedelta(days=1)
    monkeypatch.setattr(cron_jobs, "_hermes_now", lambda: now)
    rows = [
        {"fire_id": "old-done", "job_id": "job", "status": "claimed", "claimed_at": old.isoformat()},
        {"fire_id": "old-done", "job_id": "job", "status": "completed", "completed_at": old.isoformat()},
        {"fire_id": "recent-done", "job_id": "job", "status": "claimed", "claimed_at": recent.isoformat()},
        {"fire_id": "recent-done", "job_id": "job", "status": "completed", "completed_at": recent.isoformat()},
        {"fire_id": "outstanding", "job_id": "job", "status": "claimed", "claimed_at": old.isoformat()},
    ]
    ledger = tmp_path / "fire_ledger.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    incomplete = cron_jobs.reconcile_incomplete_recurring_fires()

    compacted = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["fire_id"] for row in incomplete] == ["outstanding"]
    assert {row["fire_id"] for row in compacted} == {"recent-done", "outstanding"}
    assert len(compacted) == 3


def test_fire_ledger_truncated_final_line_tolerated(tmp_path, monkeypatch, caplog):
    cron_jobs = _patch_cron_paths(tmp_path, monkeypatch)
    good = {"fire_id": "ok", "job_id": "job", "status": "claimed", "claimed_at": "2026-07-07T10:00:00+00:00"}
    ledger = tmp_path / "fire_ledger.jsonl"
    ledger.write_text(json.dumps(good) + "\n" + '{"fire_id": "truncated"', encoding="utf-8")

    with caplog.at_level("WARNING", logger="cron.jobs"):
        incomplete = cron_jobs.reconcile_incomplete_recurring_fires()

    assert [row["fire_id"] for row in incomplete] == ["ok"]
    assert "Skipping unreadable recurring cron fire ledger line" in caplog.text


def test_fire_ledger_append_survives_compaction_stale_snapshot(tmp_path, monkeypatch):
    cron_jobs = _patch_cron_paths(tmp_path, monkeypatch)
    now = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=cron_jobs.CRON_FIRE_LEDGER_RETENTION_DAYS + 2)
    monkeypatch.setattr(cron_jobs, "_hermes_now", lambda: now)
    claim = {"fire_id": "race", "job_id": "job", "status": "claimed", "claimed_at": old.isoformat()}
    ledger = tmp_path / "fire_ledger.jsonl"
    ledger.write_text(json.dumps(claim) + "\n", encoding="utf-8")
    stale_rows = [claim]

    completion = {
        "fire_id": "race",
        "job_id": "job",
        "status": "completed",
        "completed_at": now.isoformat(),
    }
    cron_jobs._append_fire_ledger(completion)
    cron_jobs._compact_fire_ledger(stale_rows)

    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert any(row["fire_id"] == "race" and row["status"] == "completed" for row in rows)
    assert cron_jobs.incomplete_recurring_fires() == []


def test_fire_ledger_append_waits_for_compaction_lock(tmp_path, monkeypatch):
    cron_jobs = _patch_cron_paths(tmp_path, monkeypatch)
    now = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(cron_jobs, "_hermes_now", lambda: now)
    ledger = tmp_path / "fire_ledger.jsonl"
    stale = now - timedelta(days=cron_jobs.CRON_FIRE_LEDGER_RETENTION_DAYS + 2)
    old_row = {"fire_id": "old", "job_id": "job", "status": "completed", "completed_at": stale.isoformat()}
    ledger.write_text(json.dumps(old_row) + "\n", encoding="utf-8")

    lock_entered = threading.Event()
    release_lock = threading.Event()
    original_compact_locked = cron_jobs._compact_fire_ledger_locked

    def blocking_compact(rows):
        lock_entered.set()
        assert release_lock.wait(timeout=2)
        original_compact_locked(rows)

    monkeypatch.setattr(cron_jobs, "_compact_fire_ledger_locked", blocking_compact)
    compact_thread = threading.Thread(target=lambda: cron_jobs._compact_fire_ledger([old_row]))
    compact_thread.start()
    assert lock_entered.wait(timeout=2)

    append_done = threading.Event()
    completion = {"fire_id": "new", "job_id": "job", "status": "completed", "completed_at": now.isoformat()}
    append_thread = threading.Thread(target=lambda: (cron_jobs._append_fire_ledger(completion), append_done.set()))
    append_thread.start()
    time.sleep(0.05)
    assert not append_done.is_set()

    release_lock.set()
    compact_thread.join(timeout=2)
    append_thread.join(timeout=2)
    assert append_done.is_set()
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert rows == [{**completion, "recorded_at": now.isoformat()}]


def test_fire_ledger_retention_cutoff_is_inclusive(tmp_path, monkeypatch):
    cron_jobs = _patch_cron_paths(tmp_path, monkeypatch)
    now = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)
    cutoff = now - timedelta(days=cron_jobs.CRON_FIRE_LEDGER_RETENTION_DAYS)
    monkeypatch.setattr(cron_jobs, "_hermes_now", lambda: now)
    rows = [
        {"fire_id": "edge", "job_id": "job", "status": "claimed", "claimed_at": cutoff.isoformat()},
        {"fire_id": "edge", "job_id": "job", "status": "completed", "completed_at": cutoff.isoformat()},
    ]
    ledger = tmp_path / "fire_ledger.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    cron_jobs.reconcile_incomplete_recurring_fires()

    compacted = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert compacted == rows
