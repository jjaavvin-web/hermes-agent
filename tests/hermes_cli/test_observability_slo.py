from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import dashboard_slo
from hermes_cli import observability_slo as slo


def make_state_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE turn_usage (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT,
            ts REAL NOT NULL,
            provider TEXT,
            model TEXT,
            prompt_version TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            latency_ms REAL,
            retry_count INTEGER DEFAULT 0,
            tool_count INTEGER DEFAULT 0
        )
        """
    )
    rows = [
        ("t1", "s", 1000.0, 100.0, 0, 0.10),
        ("t2", "s", 1010.0, 200.0, 1, 0.20),
        ("t3", "s", 1020.0, 1000.0, 0, 0.30),
    ]
    for turn_id, sid, ts, latency, retry, cost in rows:
        con.execute(
            "INSERT INTO turn_usage (turn_id, session_id, ts, latency_ms, retry_count, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (turn_id, sid, ts, latency, retry, cost),
        )
    con.commit()
    con.close()


def test_exporter_reads_state_db_mode_ro_and_journal_counts(tmp_path: Path):
    db = tmp_path / "state.db"
    make_state_db(db)
    recall = tmp_path / "recall-events.jsonl"
    recall.write_text('{"ts": 1015, "hit": true}\n{"ts": 1016, "hit": false}\n')

    snapshot = slo.build_slo_snapshot(
        state_db=db,
        output_dir=tmp_path,
        now=1100.0,
        window_seconds=500,
        gateway_lines=[
            "1970-01-01T00:16:50+00:00 host python[1]: ERROR provider failed",
            "1970-01-01T00:16:51+00:00 host python[1]: provider fallback triggered",
        ],
        watchdog_lines=[
            # heartbeat (must NOT count — description contains the word "restart")
            "1970-01-01T00:16:52+00:00 host systemd[1]: Starting hermes-gateway-watchdog.service (no restart)",
            # a genuine restart action (must count)
            "1970-01-01T00:16:53+00:00 host watchdog[1]: gateway unhealthy — restarting hermes-gateway.service",
        ],
        recall_events_path=recall,
    )

    assert snapshot["turn_count"] == 3
    assert snapshot["sources"]["state_db_mode"] == "ro"
    assert snapshot["metrics"]["turn_error_rate"] == 1 / 3
    # fallbacks are journald-only now; turn retries (retry_count>0) are NOT fallbacks
    assert snapshot["metrics"]["fallback_trigger_rate"] == 1 / 3
    assert snapshot["metrics"]["watchdog_restart_count"] == 1
    assert snapshot["metrics"]["cost_burn_rate_usd_24h"] == 0.6
    assert snapshot["recall"]["hit_rate"] == 0.5


def test_write_snapshot_and_dashboard_panel_render(tmp_path: Path, monkeypatch):
    payload = {
        "generated_at": "2026-06-24T00:00:00+00:00",
        "turn_count": 1,
        "metrics": {"gateway_turn_p95_latency_ms": 123.0, "turn_error_rate": 0.0},
        "series": [{"bucket_start": "now", "gateway_turn_p95_latency_ms": 123.0}],
        "sources": {},
        "slo_definitions": slo.SLO_DEFINITIONS,
    }
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps(payload))
    monkeypatch.setenv("HERMES_SLO_LATEST", str(latest))
    app = FastAPI()
    app.include_router(dashboard_slo.router)

    client = TestClient(app)
    data = client.get("/api/dashboard/slo").json()
    assert data["metrics"]["gateway_turn_p95_latency_ms"] == 123.0
    html = client.get("/api/dashboard/slo/panel")
    assert html.status_code == 200
    assert "Hermes SLO panel" in html.text
    assert "gateway_turn_p95_latency_ms" in html.text


def test_alert_synthetic_breach_renders():
    from scripts.observability import slo_alert_check

    snap = slo_alert_check.synthetic_snapshot()
    rows = slo_alert_check.breaches(snap)
    text = slo_alert_check.render_alert(snap, rows)
    assert rows
    assert "Hermes SLO breach" in text
    # watchdog_restart_count=3 (>critical 1) is a paging breach
    assert "watchdog_restart_count" in text
    # p95 is page=False (informational, mixed lane/interactive turns) — never paged
    assert "gateway_turn_p95_latency_ms" not in text
