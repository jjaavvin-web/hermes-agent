from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


EXPORTER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "observability" / "slo_exporter.py"
spec = importlib.util.spec_from_file_location("slo_exporter_script", EXPORTER_PATH)
assert spec is not None and spec.loader is not None
slo_exporter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = slo_exporter
spec.loader.exec_module(slo_exporter)


def make_state_db(path: Path, *, count: int = 100, start_ts: float = 1_720_141_200.0) -> None:
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
    for idx in range(count):
        con.execute(
            """
            INSERT INTO turn_usage (
                turn_id, session_id, ts, latency_ms, retry_count, estimated_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (f"t{idx}", "s", start_ts + idx, 100.0 + idx, 0, 0.0),
        )
    con.commit()
    con.close()


# Literal subset copied from
# /home/josep/.hermes/audits/kanban-wave-20260705/K4/counted-gateway-error-lines.redacted.jsonl
# at test-authoring time. Tests must not read that audit path at runtime.
K4_NOISE_LINES = [
    "2026-07-04T07:49:08-07:00 FRESH systemd[1006]: hermes-gateway.service: Main process exited, code=exited, status=1/FAILURE",
    "2026-07-04T07:49:08-07:00 FRESH systemd[1006]: hermes-gateway.service: Failed with result 'exit-code'.",
    "2026-07-04T10:56:46-07:00 FRESH python[10328]: WARNING tools.mcp_tool: MCP server 'notion' keepalive failed, triggering reconnect:",
    "2026-07-04T10:56:46-07:00 FRESH python[10328]: ERROR discord.client: Attempting a reconnect in 1.84s",
    "2026-07-04T10:56:46-07:00 FRESH python[10328]: WARNING tools.mcp_tool: MCP server 'context7' keepalive failed, triggering reconnect:",
    "2026-07-04T10:56:46-07:00 FRESH python[10328]: WARNING tools.mcp_tool: MCP server 'mvms-writer' keepalive failed, triggering reconnect:",
    "2026-07-04T10:56:46-07:00 FRESH python[10328]: WARNING tools.mcp_tool: MCP server 'mvms' keepalive failed, triggering reconnect:",
    "2026-07-04T10:56:46-07:00 FRESH python[10328]: WARNING tools.mcp_tool: MCP server 'context7' connection lost (attempt 1/5), reconnecting in 1s: unhandled errors in a TaskGroup (1 sub-exception)",
    "2026-07-04T10:56:46-07:00 FRESH python[10328]: WARNING tools.mcp_tool: MCP server 'mvms-writer' connection lost (attempt 1/5), reconnecting in 1s: unhandled errors in a TaskGroup (1 sub-exception)",
    "2026-07-04T10:56:46-07:00 FRESH python[10328]: WARNING tools.mcp_tool: MCP server 'mvms' connection lost (attempt 1/5), reconnecting in 1s: unhandled errors in a TaskGroup (1 sub-exception)",
    "2026-07-04T11:00:07-07:00 FRESH python[10328]: ERROR discord.client: Attempting a reconnect in 3.77s",
    "2026-07-04T14:37:35-07:00 FRESH systemd[1006]: hermes-gateway.service: Main process exited, code=exited, status=1/FAILURE",
    "2026-07-04T14:37:35-07:00 FRESH systemd[1006]: hermes-gateway.service: Failed with result 'exit-code'.",
    "2026-07-04T15:06:26-07:00 FRESH systemd[1006]: hermes-gateway.service: Main process exited, code=exited, status=1/FAILURE",
    "2026-07-04T15:06:26-07:00 FRESH systemd[1006]: hermes-gateway.service: Failed with result 'exit-code'.",
    "2026-07-04T19:15:21-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'context7' keepalive failed, triggering reconnect:",
    "2026-07-04T19:15:21-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'notion' keepalive failed, triggering reconnect:",
    "2026-07-04T19:15:21-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'mvms-writer' keepalive failed, triggering reconnect:",
    "2026-07-04T19:15:21-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'mvms' keepalive failed, triggering reconnect:",
    "2026-07-04T19:15:21-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'mvms' connection lost (attempt 1/5), reconnecting in 1s: unhandled errors in a TaskGroup (1 sub-exception)",
    "2026-07-04T19:22:49-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'notion' keepalive failed, triggering reconnect:",
    "2026-07-04T19:22:49-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'mvms' keepalive failed, triggering reconnect:",
    "2026-07-04T19:22:49-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'context7' keepalive failed, triggering reconnect:",
    "2026-07-04T19:22:49-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'mvms-writer' keepalive failed, triggering reconnect:",
    "2026-07-04T19:22:49-07:00 FRESH python[519845]: WARNING tools.mcp_tool: MCP server 'mvms-writer' connection lost (attempt 1/5), reconnecting in 1s: unhandled errors in a TaskGroup (1 sub-exception)",
    "2026-07-04T19:58:16-07:00 FRESH systemd[1006]: hermes-gateway.service: Watchdog timeout (limit 5min)!",
    "2026-07-04T19:58:27-07:00 FRESH systemd[1006]: hermes-gateway.service: Failed to kill control group /user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service, ignoring: Invalid argument",
    "2026-07-04T19:58:27-07:00 FRESH systemd[1006]: hermes-gateway.service: Failed with result 'watchdog'.",
    "2026-07-04T22:55:50-07:00 FRESH systemd[1006]: hermes-gateway.service: Watchdog timeout (limit 5min)!",
    "2026-07-04T22:55:56-07:00 FRESH systemd[1006]: hermes-gateway.service: Failed to kill control group /user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service, ignoring: Invalid argument",
    "2026-07-04T22:55:56-07:00 FRESH systemd[1006]: hermes-gateway.service: Failed with result 'watchdog'.",
]


def test_k4_noise_corpus_splits_restart_reconnect_watchdog_without_turn_errors(tmp_path: Path):
    db = tmp_path / "state.db"
    make_state_db(db, count=100)
    canary = tmp_path / "recall-canary.jsonl"
    service = tmp_path / "recall-events.jsonl"

    # Old behavior documented by K4: broad error-looking line counting produces a
    # non-zero numerator from exit pairs, reconnect churn, and watchdog lines even
    # though this fixture has zero verified failed-turn markers.
    old_line_count = sum(slo_exporter._base._is_counted_gateway_error(line) for line in K4_NOISE_LINES)
    assert old_line_count > 0

    snapshot = slo_exporter.build_slo_snapshot(
        state_db=db,
        output_dir=tmp_path,
        now=1_720_142_000.0,
        window_seconds=10_000,
        gateway_lines=K4_NOISE_LINES,
        watchdog_lines=[],
        recall_canary_path=canary,
        recall_service_path=service,
    )

    assert snapshot["turn_count"] == 100
    assert snapshot["metrics"]["turn_error_rate"] == 0.0
    assert snapshot["journal_counts"]["turn_error_events"] == 0
    assert snapshot["metrics"]["gateway_restart_count"] == 3
    assert snapshot["journal_counts"]["gateway_restart_events"] == 3
    assert snapshot["metrics"]["reconnect_burst_count"] == 4
    assert snapshot["journal_counts"]["reconnect_burst_events"] == 4
    assert snapshot["metrics"]["watchdog_kill_count"] == 2
    assert snapshot["journal_counts"]["watchdog_kill_events"] == 2
    assert snapshot["journal_counts"]["watchdog_kill_line_events"] == 6
    assert snapshot["journal_counts"]["mcp_reconnect_line_events"] == 17
    assert snapshot["journal_counts"]["discord_reconnect_line_events"] == 2


def test_synthetic_failed_turn_marker_still_counts(tmp_path: Path):
    db = tmp_path / "state.db"
    make_state_db(db, count=4)
    canary = tmp_path / "recall-canary.jsonl"
    service = tmp_path / "recall-events.jsonl"
    lines = [
        "2026-07-04T19:22:51-07:00 FRESH python[519845]: WARNING agent.conversation_loop: API call failed (attempt 1/3) error_type=TimeoutError",
        "2026-07-04T19:23:05-07:00 FRESH python[519845]: ERROR agent.turn: TURN_FAILED turn_id=t2 session=s terminal_error=TimeoutError after retries exhausted",
    ]

    snapshot = slo_exporter.build_slo_snapshot(
        state_db=db,
        output_dir=tmp_path,
        now=1_720_142_000.0,
        window_seconds=10_000,
        gateway_lines=lines,
        watchdog_lines=[],
        recall_canary_path=canary,
        recall_service_path=service,
    )

    assert snapshot["journal_counts"]["turn_error_events"] == 1
    assert snapshot["metrics"]["turn_error_rate"] == 0.25
    assert snapshot["journal_counts"]["diagnostic_error_line_events"] == 1
