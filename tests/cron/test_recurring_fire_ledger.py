import json

from cron import jobs as cron_jobs


def test_recurring_fire_ledger_surfaces_advance_execute_crash_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path)
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cron_jobs, "CRON_FIRE_LEDGER_FILE", tmp_path / "fire_ledger.jsonl")

    job = {
        "id": "job-1",
        "name": "hourly",
        "schedule": {"kind": "interval", "seconds": 3600},
        "next_run_at": "2026-07-07T10:00:00+00:00",
    }

    fire_id = cron_jobs.record_recurring_fire_claim(job, scheduled_for=job["next_run_at"])

    incomplete = cron_jobs.reconcile_incomplete_recurring_fires()
    assert len(incomplete) == 1
    assert incomplete[0]["fire_id"] == fire_id
    assert incomplete[0]["status"] == "claimed"

    rows = [json.loads(line) for line in (tmp_path / "fire_ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["job_id"] == "job-1"
    assert rows[0]["scheduled_for"] == "2026-07-07T10:00:00+00:00"


def test_recurring_fire_ledger_completion_clears_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_jobs, "CRON_DIR", tmp_path)
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(cron_jobs, "CRON_FIRE_LEDGER_FILE", tmp_path / "fire_ledger.jsonl")
    job = {"id": "job-1", "schedule": {"kind": "cron"}, "next_run_at": "2026-07-07T10:00:00+00:00"}

    fire_id = cron_jobs.record_recurring_fire_claim(job, scheduled_for=job["next_run_at"])
    cron_jobs.mark_recurring_fire_complete(fire_id, "job-1", success=True)

    assert cron_jobs.incomplete_recurring_fires() == []
