from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import dashboard_slo
from hermes_cli import observability_slo as slo


SCRIPT_EXPORTER_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "observability" / "slo_exporter.py"
)
_SCRIPT_EXPORTER_SPEC = importlib.util.spec_from_file_location(
    "slo_exporter_script_observability_tests", SCRIPT_EXPORTER_PATH
)
assert _SCRIPT_EXPORTER_SPEC is not None and _SCRIPT_EXPORTER_SPEC.loader is not None
script_exporter = importlib.util.module_from_spec(_SCRIPT_EXPORTER_SPEC)
sys.modules[_SCRIPT_EXPORTER_SPEC.name] = script_exporter
_SCRIPT_EXPORTER_SPEC.loader.exec_module(script_exporter)


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
    # Canary-ledger records: target-in-top-k hit flags (no gap field -> gap_ok).
    canary = tmp_path / "recall-canary.jsonl"
    canary.write_text(
        '{"ts": 1015, "target_hit": 1}\n{"ts": 1016, "target_hit": 0}\n',
        encoding="utf-8",
    )
    service = tmp_path / "recall-events.jsonl"  # empty -> service_up no_data (hermetic)

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
        recall_canary_path=canary,
        recall_service_path=service,
    )

    assert snapshot["turn_count"] == 3
    assert snapshot["sources"]["state_db_mode"] == "ro"
    assert snapshot["metrics"]["turn_error_rate"] == 1 / 3
    # fallbacks are journald-only now; turn retries (retry_count>0) are NOT fallbacks
    assert snapshot["metrics"]["fallback_trigger_rate"] == 1 / 3
    assert snapshot["metrics"]["watchdog_restart_count"] == 1
    assert snapshot["metrics"]["cost_burn_rate_usd_24h"] == 0.6
    assert snapshot["recall"]["hit_rate"] == 0.5


def test_script_exporter_preserves_recall_metric_and_adds_diagnostic(
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.db"
    make_state_db(db)
    canary = tmp_path / "recall-canary.jsonl"
    canary.write_text(
        "\n".join(
            (
                '{"ts": 1015, "target_hit": 1, "discrimination_gap": 0.30}',
                '{"ts": 1016, "target_hit": 0, "discrimination_gap": 0.10}',
            )
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = script_exporter.build_slo_snapshot(
        state_db=db,
        output_dir=tmp_path,
        now=1100.0,
        window_seconds=500,
        gateway_lines=[],
        watchdog_lines=[],
        recall_canary_path=canary,
        recall_service_path=tmp_path / "recall-events.jsonl",
    )

    assert snapshot["metrics"]["recall_hit_rate"] == 0.5
    assert snapshot["metrics"]["recall_discrimination_pass_rate"] == 0.5
    assert snapshot["recall"]["hit_rate"] == 0.5
    assert snapshot["recall"]["discrimination_pass_rate"] == 0.5
    assert snapshot["metrics"]["turn_error_rate"] == 0.0


def test_auxiliary_title_generation_fallback_noise_is_separate_from_provider_fallback(tmp_path: Path):
    db = tmp_path / "state.db"
    make_state_db(db)
    canary = tmp_path / "recall-canary.jsonl"
    canary.write_text('{"ts": 1015, "target_hit": 1}\n', encoding="utf-8")
    service = tmp_path / "recall-events.jsonl"

    gateway_lines = [
        "1970-01-01T00:16:50+00:00 host python[1]: title_generation fallback-chain fail-closed",
        "1970-01-01T00:16:51+00:00 host python[1]: auxiliary fallback kept primary turn alive",
        "1970-01-01T00:16:52+00:00 host python[1]: provider fallback triggered for openai-codex",
    ]
    snapshot = slo.build_slo_snapshot(
        state_db=db,
        output_dir=tmp_path,
        now=1100.0,
        window_seconds=500,
        gateway_lines=gateway_lines,
        watchdog_lines=[],
        recall_canary_path=canary,
        recall_service_path=service,
    )

    assert snapshot["turn_count"] == 3
    assert snapshot["journal_counts"]["auxiliary_fallback_events"] == 2
    assert snapshot["journal_counts"]["fallback_events"] == 1
    assert snapshot["metrics"]["fallback_trigger_rate"] == 1 / 3
    assert sum(bucket["auxiliary_fallback_events"] for bucket in snapshot["series"]) == 2
    assert sum(bucket["fallback_events"] for bucket in snapshot["series"]) == 1


def test_firecrawl_config_noise_excluded_real_500_still_counted(tmp_path: Path):
    # 2026-06-26: ~88% of the live 24h error numerator was repeating Firecrawl
    # plugin-init ERROR lines + "web tools are not configured" fragments — web-tool
    # config noise, not gateway turn failures. They must be excluded from
    # turn_error_rate (both the top-line metric and the per-bucket series), while a
    # genuine "500 server error" turn failure must still count.
    db = tmp_path / "state.db"
    make_state_db(db)  # 3 turns at ts 1000/1010/1020
    canary = tmp_path / "recall-canary.jsonl"
    canary.write_text('{"ts": 1015, "target_hit": 1}\n', encoding="utf-8")
    service = tmp_path / "recall-events.jsonl"  # empty -> service_up no_data

    gateway_lines = [
        # Firecrawl/web-tools config noise (ERROR-level but NOT a turn failure) — excluded.
        # NOTE: both lines carry a real error token (ERROR/"failed") so _ERROR_RE MATCHES
        # them; the exclusion is therefore genuinely exercised (without the ERROR prefix on
        # the second line, _ERROR_RE would never match and the exclusion would be vacuous).
        "1970-01-01T00:16:50+00:00 host python[1]: ERROR plugins.web.firecrawl.provider: "
        "Firecrawl client initialization failed: missing direct config and tool-gateway auth.",
        # Firecrawl/web-tools config noise and provider quota/tool-backend failures
        # are diagnostic only; they should not page as gateway turn errors.
        "1970-01-01T00:16:51+00:00 host python[1]: ERROR Web tools are not configured. Set FIRECRAWL_API_KEY",
        "1970-01-01T00:16:51+00:00 host python[1]: Traceback (most recent call last):",
        "1970-01-01T00:16:51+00:00 host python[1]: ERROR tools.x_search_tool: x_search failed: 429 Client Error: Too Many Requests for url: https://api.x.ai/v1/responses",
        "1970-01-01T00:16:51+00:00 host python[1]: requests.exceptions.HTTPError: 429 Client Error: Too Many Requests for url: https://api.x.ai/v1/responses",
        # genuine gateway turn failure — must still be counted
        "1970-01-01T00:16:52+00:00 host python[1]: ERROR upstream returned 500 server error",
    ]
    snapshot = slo.build_slo_snapshot(
        state_db=db,
        output_dir=tmp_path,
        now=1100.0,
        window_seconds=500,
        gateway_lines=gateway_lines,
        watchdog_lines=[],
        recall_canary_path=canary,
        recall_service_path=service,
    )

    # 3 turns; only the real "500 server error" line counts -> 1/3, not 3/3.
    assert snapshot["turn_count"] == 3
    assert snapshot["metrics"]["turn_error_rate"] == 1 / 3
    assert snapshot["journal_counts"]["tool_config_error_events"] == 2
    assert snapshot["journal_counts"]["tool_backend_error_events"] == 2
    # The per-bucket series uses the same classifier helpers.
    bucket_errors = sum(b["error_events"] for b in snapshot["series"])
    bucket_config = sum(b["tool_config_error_events"] for b in snapshot["series"])
    bucket_backend = sum(b["tool_backend_error_events"] for b in snapshot["series"])
    assert bucket_errors == 1
    assert bucket_config == 2
    assert bucket_backend == 2


def test_disabled_image_generation_and_guardrail_denials_are_diagnostic_only(tmp_path: Path):
    db = tmp_path / "state.db"
    make_state_db(db)
    canary = tmp_path / "recall-canary.jsonl"
    canary.write_text('{"ts": 1015, "target_hit": 1}\n', encoding="utf-8")
    service = tmp_path / "recall-events.jsonl"

    gateway_lines = [
        "1970-01-01T00:16:50+00:00 host python[1]: ERROR tools.image_generation_tool: "
        "Error generating image: Image generation is unavailable in this environment.",
        "1970-01-01T00:16:51+00:00 host python[1]: ValueError: Image generation is unavailable in this environment.",
        "1970-01-01T00:16:52+00:00 host python[1]: ERROR FAL_KEY environment variable is not set",
        "1970-01-01T00:16:53+00:00 host python[1]: WARNING agent.tool_executor: Tool terminal returned error: "
        "WORKTREE_CONFINEMENT: command cwd target is inside protected code root",
        "1970-01-01T00:16:54+00:00 host python[1]: ERROR upstream returned 500 server error",
    ]
    snapshot = slo.build_slo_snapshot(
        state_db=db,
        output_dir=tmp_path,
        now=1100.0,
        window_seconds=500,
        gateway_lines=gateway_lines,
        watchdog_lines=[],
        recall_canary_path=canary,
        recall_service_path=service,
    )

    assert snapshot["turn_count"] == 3
    assert snapshot["metrics"]["turn_error_rate"] == 1 / 3
    assert snapshot["journal_counts"]["tool_config_error_events"] == 3
    assert snapshot["journal_counts"]["tool_guardrail_denial_events"] == 1
    assert sum(bucket["error_events"] for bucket in snapshot["series"]) == 1
    assert sum(bucket["tool_config_error_events"] for bucket in snapshot["series"]) == 3
    assert sum(bucket["tool_guardrail_denial_events"] for bucket in snapshot["series"]) == 1


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


def _write_canary(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(rec) for rec in records) + "\n", encoding="utf-8"
    )


def test_recall_hit_rate_all_target_hits_is_full(tmp_path: Path):
    canary = tmp_path / "recall-canary.jsonl"
    _write_canary(
        canary,
        [
            {"ts": 1000 + i, "target_hit": 1, "discrimination_gap": 0.30}
            for i in range(5)
        ],
    )
    out = slo.read_recall_hit_rate(canary, service_events_path=tmp_path / "none.jsonl")
    assert out["hit_rate"] == 1.0
    assert out["total"] == 5 and out["hits"] == 5
    assert out["target_misses"] == 0 and out["cosine_collapses"] == 0
    assert out["discrimination_pass_rate"] == 1.0


def test_recall_hit_rate_target_miss_drops_below_critical(tmp_path: Path):
    canary = tmp_path / "recall-canary.jsonl"
    # 1 hit + 2 target-misses -> 1/3 = 0.333 < 0.65 critical threshold.
    _write_canary(
        canary,
        [
            {"ts": 1000, "target_hit": 1, "discrimination_gap": 0.30},
            {"ts": 1001, "target_hit": 0, "discrimination_gap": 0.05},
            {"ts": 1002, "target_hit": 0, "discrimination_gap": 0.40},
        ],
    )
    out = slo.read_recall_hit_rate(canary, service_events_path=tmp_path / "none.jsonl")
    assert out["hit_rate"] is not None and out["hit_rate"] < 0.65
    assert out["target_misses"] == 2 and out["hits"] == 1


def test_recall_hit_rate_does_not_conflate_discrimination_margin(tmp_path: Path):
    canary = tmp_path / "recall-canary.jsonl"
    # Every run found the target. A weak in/off-domain cosine gap is still useful
    # diagnostics, but must not rewrite target-in-top-k retrieval as a miss.
    _write_canary(
        canary,
        [
            {"ts": 1000, "target_hit": 1, "discrimination_gap": 0.30},
            {"ts": 1001, "target_hit": 1, "discrimination_gap": 0.05},
            {"ts": 1002, "target_hit": 1, "discrimination_gap": 0.10},
            {"ts": 1003, "target_hit": 1, "discrimination_gap": 0.02},
        ],
    )
    out = slo.read_recall_hit_rate(canary, service_events_path=tmp_path / "none.jsonl")
    assert out["hit_rate"] == 1.0 and out["hits"] == 4
    assert out["cosine_collapses"] == 3
    assert out["discrimination_pass_rate"] == 0.25


def test_recall_hit_rate_null_hit_records_are_skipped(tmp_path: Path):
    canary = tmp_path / "recall-canary.jsonl"
    # The canary's never-raise setup_error path writes target_hit=null; those
    # records must NOT be counted as misses (would falsely tank the rate).
    _write_canary(
        canary,
        [
            {"ts": 1000, "status": "setup_error", "target_hit": None},
            {"ts": 1001, "target_hit": 1, "discrimination_gap": 0.30},
        ],
    )
    out = slo.read_recall_hit_rate(canary, service_events_path=tmp_path / "none.jsonl")
    assert out["total"] == 1 and out["hit_rate"] == 1.0


def test_recall_hit_rate_no_data_when_ledger_missing(tmp_path: Path):
    out = slo.read_recall_hit_rate(
        tmp_path / "missing.jsonl", service_events_path=tmp_path / "none.jsonl"
    )
    assert out["status"] == "no_data" and out["hit_rate"] is None
    assert out["service_up"]["status"] == "no_data"


def test_recall_service_up_subfield_is_separate_from_hit_rate(tmp_path: Path):
    canary = tmp_path / "recall-canary.jsonl"
    _write_canary(canary, [{"ts": 1000, "target_hit": 1, "discrimination_gap": 0.30}])
    # Legacy injection ledger: warm path 'up' on 1 of 2 dispatches.
    service = tmp_path / "recall-events.jsonl"
    _write_canary(service, [{"ts": 1000, "n_lessons": 5}, {"ts": 1001, "n_lessons": 0}])
    out = slo.read_recall_hit_rate(canary, service_events_path=service)
    assert out["hit_rate"] == 1.0  # canary-driven SLO value
    assert out["service_up"]["rate"] == 0.5  # informational, NOT the SLO value
def test_recall_non_numeric_and_nonfinite_gap_is_skipped_not_crash(tmp_path: Path):
    """Invalid discrimination_gap values must not crash or skew aggregation.

    Policy encoded here (source-faithful skip/count):
    - keep the canary record in hit_rate samples when target_hit is measured
    - skip the bad gap from discrimination_samples / discrimination_pass_rate
    - do not invent a numeric gap and do not count it as a cosine collapse
    """
    canary = tmp_path / "recall-canary.jsonl"
    _write_canary(
        canary,
        [
            {
                "ts": 1000,
                "target_hit": 1,
                "discrimination_gap": "n/a",  # malformed / missing measurement
            },
            {
                "ts": 1001,
                "target_hit": 1,
                "discrimination_gap": "NaN",
            },
            {
                "ts": 1002,
                "target_hit": 1,
                "discrimination_gap": "inf",
            },
            {
                "ts": 1003,
                "target_hit": 1,
                "discrimination_gap": 0.30,
            },
            {
                "ts": 1004,
                "target_hit": 0,
                "discrimination_gap": "bad",
            },
        ],
    )
    out = slo.read_recall_hit_rate(canary, service_events_path=tmp_path / "none.jsonl")
    # hit_rate still counts measured target_hit rows only
    assert out["status"] == "ok"
    assert out["total"] == 5
    assert out["hits"] == 4
    assert out["target_misses"] == 1
    assert out["hit_rate"] == 4 / 5
    # Non-numeric and non-finite gaps are excluded from diagnostics.
    assert out["discrimination_samples"] == 1
    assert out["discrimination_passes"] == 1
    assert out["cosine_collapses"] == 0
    assert out["discrimination_pass_rate"] == 1.0


def test_recall_discrimination_pass_rate_is_non_paging_diagnostic():
    """Low discrimination rate is visible but must never page as a breach.

    recall_hit_rate remains the only target-in-top-k paging metric; a healthy
    hit rate with a weak discrimination diagnostic produces no alert rows.
    """
    from scripts.observability import slo_alert_check

    disc = slo.SLO_DEFINITIONS["recall_discrimination_pass_rate"]
    assert disc.get("page") is False
    assert disc.get("critical") is None
    assert disc.get("warn") is None
    assert disc.get("target") == "diagnostic"

    # Healthy target-hit SLO, weak discrimination diagnostic, plus another
    # known non-paging metric to prove the page=False gate is exercised.
    snapshot = {
        "metrics": {
            "recall_hit_rate": 1.0,
            "recall_discrimination_pass_rate": 0.0,
            "gateway_turn_p95_latency_ms": 9999.0,
        }
    }
    rows = slo_alert_check.breaches(snapshot)
    assert rows == []
    assert "recall_discrimination_pass_rate" not in {
        row.get("metric") for row in rows
    }


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
