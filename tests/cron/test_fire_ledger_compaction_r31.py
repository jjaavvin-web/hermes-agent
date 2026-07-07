from __future__ import annotations

import json
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
