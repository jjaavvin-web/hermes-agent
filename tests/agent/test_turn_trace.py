"""Tests for the per-turn cost/latency trace (audit WC-3/GWR-4 measurement keystone).

Includes a structural INVARIANT test so an upstream merge that strips the
emit_turn_trace call from finalize_turn turns RED (mirrors the test_backup PR#70
survival pattern).
"""

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def trace_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("HERMES_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("HERMES_PROMPT_VERSION_FILE", str(tmp_path / "VERSION"))
    (tmp_path / "VERSION").write_text("test.1\n", encoding="utf-8")
    import importlib

    import agent.turn_trace as tt

    importlib.reload(tt)
    return tt, tmp_path


def _result():
    return {
        "session_id": "s1", "provider": "openai-codex", "model": "gpt-5.5",
        "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 20,
        "total_tokens": 170, "estimated_cost_usd": 0.0, "cost_status": "included",
        "cost_source": "subscription", "api_calls": 3,
        "messages": [{"role": "tool"}, {"role": "assistant", "tool_calls": [1]}, {"role": "user"}],
        "completed": True, "failed": False,
    }


def test_emit_writes_jsonl_and_turn_usage_row(trace_env):
    tt, tmp = trace_env
    tt.emit_turn_trace(_result(), "turn-abc", latency_ms=1234.5)

    jsonls = list((tmp / "traces").glob("turns-*.jsonl"))
    assert len(jsonls) == 1
    row = json.loads(jsonls[0].read_text(encoding="utf-8").strip())
    assert row["turn_id"] == "turn-abc"
    assert row["model"] == "gpt-5.5"
    assert row["total_tokens"] == 170
    assert row["tool_count"] == 2
    assert row["retry_count"] == 3
    assert row["latency_ms"] == 1234.5
    assert row["prompt_version"] == "test.1"

    db_rows = sqlite3.connect(str(tmp / "state.db")).execute(
        "SELECT turn_id, model, total_tokens, tool_count FROM turn_usage"
    ).fetchall()
    assert db_rows == [("turn-abc", "gpt-5.5", 170, 2)]


def test_emit_is_fail_open_on_bad_db(trace_env, monkeypatch):
    tt, _ = trace_env
    monkeypatch.setenv("HERMES_STATE_DB", "/nonexistent/dir/x.db")
    import importlib

    importlib.reload(tt)
    # Must not raise even when the DB path is unwritable.
    tt.emit_turn_trace(_result(), "turn-2")


def test_emit_noops_without_turn_id(trace_env):
    tt, tmp = trace_env
    tt.emit_turn_trace(_result(), None)
    assert not list((tmp / "traces").glob("turns-*.jsonl"))


def test_finalize_turn_calls_emit_turn_trace():
    """INVARIANT: finalize_turn must import and call emit_turn_trace.

    Guards against an upstream merge silently stripping the trace call.
    """
    src = Path(__file__).resolve().parents[2] / "agent" / "turn_finalizer.py"
    text = src.read_text(encoding="utf-8")
    assert "from agent.turn_trace import emit_turn_trace" in text
    # Tolerant of the latency_ms kwarg (WC-3/GWR-4 latency wiring); the call must
    # still exist with the turn_id positional.
    assert "emit_turn_trace(result, turn_id" in text
    assert "latency_ms=latency_ms" in text
