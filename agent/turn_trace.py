"""Per-turn cost/latency trace — the measurement-layer keystone (audit WC-3/GWR-4).

emit_turn_trace() is called once from turn_finalizer after the result dict is built.
It appends one line to ~/.hermes/traces/turns-YYYYMMDD.jsonl (the SOURCE OF TRUTH)
and best-effort inserts one turn_usage row into state.db. The whole body is fail-open
(mirrors recall_at_dispatch) so a trace failure can NEVER break a turn. Records tool
NAMES/counts and token/cost numbers only — never prompt/response bodies or secrets.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

_TRACE_DIR = Path(os.environ.get("HERMES_TRACE_DIR", str(Path.home() / ".hermes" / "traces")))
_STATE_DB = Path(os.environ.get("HERMES_STATE_DB", str(Path.home() / ".hermes" / "state.db")))
_VERSION_FILE = Path(os.environ.get("HERMES_PROMPT_VERSION_FILE", str(Path.home() / ".hermes" / "prompts" / "VERSION")))

_TURN_USAGE_DDL = """CREATE TABLE IF NOT EXISTS turn_usage (
    turn_id TEXT PRIMARY KEY, session_id TEXT, ts REAL NOT NULL, provider TEXT, model TEXT,
    prompt_version TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0, estimated_cost_usd REAL,
    cost_status TEXT, cost_source TEXT, latency_ms REAL, retry_count INTEGER DEFAULT 0,
    tool_count INTEGER DEFAULT 0)"""


def _read_prompt_version() -> str | None:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def _tool_count(messages: list | None) -> int:
    if not messages:
        return 0
    n = 0
    for m in messages:
        try:
            if m.get("role") == "tool" or m.get("tool_calls"):
                n += 1
        except AttributeError:
            continue
    return n


def build_row(result: dict, turn_id: str, latency_ms: float | None) -> dict:
    """Pure: extract the trace row from the finalize_turn result dict."""
    return {
        "turn_id": turn_id,
        "session_id": result.get("session_id"),
        "ts": round(time.time(), 3),
        "iso": datetime.now(timezone.utc).isoformat(),
        "provider": result.get("provider"),
        "model": result.get("model"),
        "prompt_version": _read_prompt_version(),
        "input_tokens": int(result.get("input_tokens") or 0),
        "output_tokens": int(result.get("output_tokens") or 0),
        "cache_read_tokens": int(result.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(result.get("cache_write_tokens") or 0),
        "reasoning_tokens": int(result.get("reasoning_tokens") or 0),
        "total_tokens": int(result.get("total_tokens") or 0),
        "estimated_cost_usd": result.get("estimated_cost_usd"),
        "cost_status": result.get("cost_status"),
        "cost_source": result.get("cost_source"),
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "retry_count": int(result.get("api_calls") or 0),
        "tool_count": _tool_count(result.get("messages")),
        "completed": bool(result.get("completed")),
        "failed": bool(result.get("failed")),
    }


def _write_jsonl(row: dict) -> None:
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_TRACE_DIR, 0o700)
    except OSError:
        pass
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = _TRACE_DIR / f"turns-{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _insert_sqlite(row: dict) -> None:
    # Best-effort: a busy/locked single-writer state.db must not fail the turn.
    conn = sqlite3.connect(str(_STATE_DB), timeout=1.0)
    try:
        conn.execute(_TURN_USAGE_DDL)
        cols = ("turn_id", "session_id", "ts", "provider", "model", "prompt_version",
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "reasoning_tokens", "total_tokens", "estimated_cost_usd", "cost_status",
                "cost_source", "latency_ms", "retry_count", "tool_count")
        conn.execute(
            f"INSERT OR REPLACE INTO turn_usage ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        conn.commit()
    finally:
        conn.close()


def emit_turn_trace(result: dict, turn_id: str | None, latency_ms: float | None = None) -> None:
    """Fail-open per-turn trace. Never raises. JSONL is source of truth; SQLite best-effort."""
    if not turn_id:
        return
    try:
        row = build_row(result, turn_id, latency_ms)
    except Exception:
        return
    try:
        _write_jsonl(row)
    except Exception:
        pass
    try:
        _insert_sqlite(row)
    except Exception:
        pass
