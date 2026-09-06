from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


def test_runtime_status_surfaces_restart_loop_and_codex_diagnostics_without_mutating_corrupt_state(tmp_path, monkeypatch):
    from gateway import status

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 123)
    monkeypatch.setattr(
        "gateway.restart_loop_guard.invalid_status",
        lambda: {"status": "invalid", "reason": "unreadable_state"},
    )
    monkeypatch.setattr(
        "gateway.codex_session_dispatcher.CodexSessionDispatcher._load_state",
        MagicMock(side_effect=AssertionError("status must not call dispatcher _load_state")),
    )
    sessions_path = tmp_path / "codex_sessions.json"
    corrupt_bytes = b'{"version": 1, "sessions": {"t1":'
    sessions_path.write_bytes(corrupt_bytes)

    status.invalidate_codex_sessions_diagnostics_cache()
    status.write_runtime_status(gateway_state="running")
    payload = status.read_runtime_status()

    assert payload is not None
    assert payload["restart_loop_guard_invalid"]["reason"] == "unreadable_state"
    assert payload["codex_sessions_diagnostics"]["corrupt"] is True
    assert "load_error" in payload["codex_sessions_diagnostics"]
    assert sessions_path.exists()
    assert sessions_path.read_bytes() == corrupt_bytes
    assert not list(tmp_path.glob("codex_sessions.json.corrupt-*"))
    assert "quarantined_from" not in payload
    assert "load_error" not in payload


def test_codex_sessions_diagnostics_cache_expires_at_boundary(tmp_path, monkeypatch):
    from gateway import status

    sessions_path = tmp_path / "codex_sessions.json"
    sessions_path.write_text(json.dumps({"version": 1, "sessions": {}}), encoding="utf-8")
    clock = {"now": 300.0}
    calls = {"count": 0}
    real = status._peek_codex_sessions_diagnostics_uncached

    def wrapped(path: Path | None = None):
        calls["count"] += 1
        return real(path)

    monkeypatch.setattr(status, "_peek_codex_sessions_diagnostics_uncached", wrapped)
    monkeypatch.setattr(status.time, "monotonic", lambda: clock["now"])
    status.invalidate_codex_sessions_diagnostics_cache()

    assert status.peek_codex_sessions_diagnostics(sessions_path) == {}
    sessions_path.write_text('{"version": 1, "sessions": {"t1":', encoding="utf-8")
    assert status.peek_codex_sessions_diagnostics(sessions_path) == {}
    assert calls["count"] == 1
    clock["now"] = 300.0 + status.CODEX_SESSIONS_DIAGNOSTICS_CACHE_TTL_SECONDS

    diagnostics = status.peek_codex_sessions_diagnostics(sessions_path)
    assert diagnostics["corrupt"] is True
    assert calls["count"] == 2


def test_status_strips_transient_diagnostic_keys_before_persisting(tmp_path, monkeypatch):
    from gateway import status

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 123)
    path = tmp_path / "gateway_state.json"
    path.write_text(json.dumps({"quarantined_from": "old", "load_error": "old"}), encoding="utf-8")

    status.write_runtime_status(gateway_state="running")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert "quarantined_from" not in payload
    assert "load_error" not in payload
