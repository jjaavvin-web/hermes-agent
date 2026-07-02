from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from hermes_cli import dashboard_cost as dc

TURN_USAGE_DDL = """CREATE TABLE IF NOT EXISTS turn_usage (
    turn_id TEXT PRIMARY KEY, session_id TEXT, ts REAL NOT NULL, provider TEXT, model TEXT,
    prompt_version TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0, estimated_cost_usd REAL,
    cost_status TEXT, cost_source TEXT, latency_ms REAL, retry_count INTEGER DEFAULT 0,
    tool_count INTEGER DEFAULT 0)"""


def _seed_state_db(db_path: Path) -> dict[str, float]:
    now = time.time()
    old = now - 10 * 86_400
    rows = [
        {
            "turn_id": "anthropic-paid-recent",
            "session_id": "session-a",
            "ts": now,
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "prompt_version": "pv-cost",
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_tokens": 25,
            "cache_write_tokens": 5,
            "reasoning_tokens": 10,
            "total_tokens": 175,
            "estimated_cost_usd": 0.45,
            "cost_status": "actual",
            "cost_source": "paid_api",
            "latency_ms": 1200.0,
            "retry_count": 0,
            "tool_count": 1,
        },
        {
            "turn_id": "anthropic-included-recent",
            "session_id": "session-b",
            "ts": now - 60,
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "prompt_version": "pv-cost",
            "input_tokens": 70,
            "output_tokens": 30,
            "cache_read_tokens": 10,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 110,
            "estimated_cost_usd": 0.0,
            "cost_status": "included",
            "cost_source": "subscription",
            "latency_ms": 800.0,
            "retry_count": 0,
            "tool_count": 0,
        },
        {
            "turn_id": "openrouter-estimated-recent",
            "session_id": "session-c",
            "ts": now - 120,
            "provider": "openrouter",
            "model": "openrouter-model",
            "prompt_version": "pv-cost",
            "input_tokens": 50,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 100,
            "estimated_cost_usd": 0.21,
            "cost_status": "estimated",
            "cost_source": "none",
            "latency_ms": 500.0,
            "retry_count": 1,
            "tool_count": 2,
        },
        {
            "turn_id": "claude-cli-recent",
            "session_id": "session-d",
            "ts": now - 180,
            "provider": "claude-cli-subprocess",
            "model": "claude-fable-5",
            "prompt_version": "pv-local",
            "input_tokens": 40,
            "output_tokens": 20,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 60,
            "estimated_cost_usd": 0.99,
            "cost_status": "actual",
            "cost_source": "paid_api",
            "latency_ms": 300.0,
            "retry_count": 0,
            "tool_count": 0,
        },
        {
            "turn_id": "anthropic-paid-old",
            "session_id": "session-old",
            "ts": old,
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "prompt_version": "pv-old",
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 2,
            "estimated_cost_usd": 7.77,
            "cost_status": "actual",
            "cost_source": "paid_api",
            "latency_ms": 100.0,
            "retry_count": 0,
            "tool_count": 0,
        },
    ]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(TURN_USAGE_DDL)
        cols = (
            "turn_id",
            "session_id",
            "ts",
            "provider",
            "model",
            "prompt_version",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "cost_status",
            "cost_source",
            "latency_ms",
            "retry_count",
            "tool_count",
        )
        conn.executemany(
            f"INSERT INTO turn_usage ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [tuple(row[col] for col in cols) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()
    return {"now": now, "old": old}


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def test_is_metered_leak_truth_table() -> None:
    assert dc._is_metered_leak("anthropic", "actual", "paid_api") is True
    assert dc._is_metered_leak("anthropic", "included", "subscription") is False
    assert dc._is_metered_leak("openrouter", "estimated", "none") is True
    assert dc._is_metered_leak("anthropic", "estimated", "") is True
    assert dc._is_metered_leak("anthropic", None, "") is False
    assert dc._is_metered_leak("claude-cli-subprocess", "actual", "paid_api") is False
    assert dc._is_metered_leak(None, None, None) is False


def test_rollup_and_metered_leaks_from_temp_state_db(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    stamps = _seed_state_db(db_path)
    week_ago = stamps["now"] - 7 * 86_400

    conn = _connect_readonly(db_path)
    try:
        rollup = dc._rollup(conn, week_ago)
        assert rollup["totalTurns"] == 4
        assert rollup["totalCostUsd"] == 1.65

        anthropic_group = next(
            group
            for group in rollup["groups"]
            if group["provider"] == "anthropic" and group["promptVersion"] == "pv-cost"
        )
        assert anthropic_group["model"] == "claude-sonnet-4"
        assert anthropic_group["turns"] == 2
        assert anthropic_group["costUsd"] == 0.45
        assert anthropic_group["totalTokens"] == 285

        leaks = dc._metered_leaks(conn, week_ago)
        assert [leak["turnId"] for leak in leaks] == [
            "anthropic-paid-recent",
            "openrouter-estimated-recent",
        ]
        assert len(leaks) == 2
        assert all(leak["turnId"] != "anthropic-included-recent" for leak in leaks)
        assert all(leak["provider"] != "claude-cli-subprocess" for leak in leaks)
        assert all(leak["turnId"] != "anthropic-paid-old" for leak in leaks)
        assert leaks[0]["costUsd"] >= leaks[-1]["costUsd"]
    finally:
        conn.close()


def test_get_cost_uses_hermes_home_temp_state_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_state_db(tmp_path / "state.db")

    snapshot = dc.get_cost()

    for key in ("today", "last7d", "meteredLeak", "meteredLeakCount", "dailySeries", "cacheLatency7d"):
        assert key in snapshot
    assert snapshot["meteredLeakCount"] == len(snapshot["meteredLeak"])
    assert snapshot["meteredLeakCount"] == 2
    assert snapshot["today"]["totalTurns"] >= 1
    assert snapshot["last7d"]["totalTurns"] == 4
    assert snapshot["dailySeries"]
    assert snapshot["cacheLatency7d"]["cacheHitRatio"] > 0.0


def test_get_cost_returns_zero_snapshot_when_state_db_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    snapshot = dc.get_cost()

    assert snapshot["today"]["totalCostUsd"] == 0.0
    assert snapshot["today"]["totalTurns"] == 0
    assert snapshot["meteredLeak"] == []
    assert snapshot["meteredLeakCount"] == 0
    assert snapshot["last7d"]["groups"] == []
