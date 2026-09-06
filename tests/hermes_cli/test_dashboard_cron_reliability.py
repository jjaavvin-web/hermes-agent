from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import dashboard_cron_reliability as d


def test_synthetic_non_zero_exit_unit_is_red(monkeypatch):
    rows = [{"unit": "bad.timer", "activates": "bad.service", "next": 1_800_000_000_000_000, "last": 1_799_999_100_000_000}]

    def fake_show(unit: str, properties=None):
        if unit == "bad.timer":
            return {"ActiveState": "active", "SubState": "waiting", "Unit": "bad.service", "Result": "success", "NRestarts": "0"}
        if unit == "bad.service":
            return {
                "ActiveState": "inactive",
                "SubState": "dead",
                "Result": "exit-code",
                "ExecMainCode": "1",
                "ExecMainStatus": "42",
                "NRestarts": "3",
            }
        return {}

    monkeypatch.setattr(d, "_load_cron_jobs", lambda: ([], Path("/tmp/jobs.json"), None))
    monkeypatch.setattr(d, "_list_user_timers", lambda: (rows, None))
    monkeypatch.setattr(d, "_systemctl_show", fake_show)
    monkeypatch.setattr(
        d,
        "_journal_lines",
        lambda unit, limit: [
            "2026-06-23T07:51:37-07:00 host systemd[1]: bad.service: Main process exited, code=exited, status=42/FAILURE",
            "2026-06-23T07:51:37-07:00 host systemd[1]: bad.service: Failed with result 'exit-code'.",
        ],
    )
    monkeypatch.setattr(d.time, "time", lambda: 1_800_000_100.0)
    monkeypatch.setattr(d, "_STANDALONE_SERVICE_ALLOWLIST", set())

    payload = d.build_reliability_snapshot(history_limit=20)

    assert payload["summary"]["systemd_timers"] == 1
    unit = payload["systemd_timers"][0]
    assert unit["health"] == "red"
    assert unit["last_exit_status"]["code"] == 42
    assert unit["n_restarts"] == 3
    assert "last_exit_non_zero" in unit["reasons"]
    assert unit["success_rate"]["failures"] == 1


def test_overdue_timer_sets_missed_flag(monkeypatch):
    rows = [{"unit": "late.timer", "activates": "late.service", "next": 1_000_000_000_000_000, "last": 999_999_400_000_000}]

    def fake_show(unit: str, properties=None):
        if unit == "late.timer":
            return {"ActiveState": "active", "SubState": "waiting", "Unit": "late.service", "Result": "success"}
        return {"ActiveState": "inactive", "SubState": "dead", "Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0", "NRestarts": "0"}

    monkeypatch.setattr(d, "_load_cron_jobs", lambda: ([], Path("/tmp/jobs.json"), None))
    monkeypatch.setattr(d, "_list_user_timers", lambda: (rows, None))
    monkeypatch.setattr(d, "_systemctl_show", fake_show)
    monkeypatch.setattr(d, "_journal_lines", lambda unit, limit: [])
    monkeypatch.setattr(d.time, "time", lambda: 1_000_001_000.0)
    monkeypatch.setattr(d, "_STANDALONE_SERVICE_ALLOWLIST", set())

    payload = d.build_reliability_snapshot(history_limit=20)
    freshness = payload["systemd_timers"][0]["freshness_vs_interval"]

    assert freshness["overdue"] is True
    assert freshness["missed"] is True
    assert freshness["state"] == "missed"
    assert payload["systemd_timers"][0]["health"] == "red"
    assert "missed_run" in payload["systemd_timers"][0]["reasons"]


def test_jobs_json_non_ok_status_is_red(monkeypatch):
    job = {
        "id": "job-1",
        "name": "failing job",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 15, "display": "every 15m"},
        "schedule_display": "every 15m",
        "last_run_at": "2026-06-23T07:51:00+00:00",
        "next_run_at": "2026-06-23T08:06:00+00:00",
        "last_status": "error",
        "last_error": "boom",
    }
    monkeypatch.setattr(d, "_load_cron_jobs", lambda: ([job], Path("/tmp/jobs.json"), None))
    monkeypatch.setattr(d, "_list_user_timers", lambda: ([], None))
    monkeypatch.setattr(d.time, "time", lambda: 1_800_000_000.0)
    monkeypatch.setattr(d, "_STANDALONE_SERVICE_ALLOWLIST", set())

    payload = d.build_reliability_snapshot()
    item = payload["cron_jobs"][0]

    assert item["health"] == "red"
    assert item["last_exit_status"]["code"] == 1
    assert item["last_failure_excerpt"] == "boom"
    assert item["success_rate"]["rate"] == 0.0


def test_route_registered_on_web_server():
    from hermes_cli import web_server

    # Import-time registration is the contract the dashboard process consumes.
    paths = {getattr(route, "path", None) for route in web_server.app.routes}
    assert "/api/cron/reliability" in paths
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    resp = TestClient(web_server.app, base_url="http://127.0.0.1:9119").get("/api/cron/reliability?limit=5", headers=headers)
    assert resp.status_code == 200


def test_router_contract_shape(monkeypatch):
    monkeypatch.setattr(
        d,
        "build_reliability_snapshot",
        lambda history_limit=80: {
            "generated_at": "2026-06-23T00:00:00+00:00",
            "history_limit": history_limit,
            "sources": {},
            "summary": {"cron_jobs": 0, "systemd_timers": 0, "systemd_services": 0, "total_units": 0, "by_health": {}},
            "cron_jobs": [],
            "systemd_timers": [],
            "systemd_services": [],
            "units": [],
            "warnings": [],
        },
    )
    app = FastAPI()
    app.include_router(d.router)

    resp = TestClient(app).get("/api/cron/reliability?limit=17")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["history_limit"] == 17
    assert {"generated_at", "summary", "cron_jobs", "systemd_timers", "systemd_services", "units", "warnings"} <= set(payload)
