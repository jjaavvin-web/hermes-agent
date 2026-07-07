from __future__ import annotations

import json
from unittest.mock import MagicMock


def test_runtime_status_surfaces_restart_loop_and_codex_diagnostics(tmp_path, monkeypatch):
    from gateway import status

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 123)
    monkeypatch.setattr(
        "gateway.restart_loop_guard.invalid_status",
        lambda: {"status": "invalid", "reason": "unreadable_state"},
    )
    monkeypatch.setattr(
        "gateway.codex_session_dispatcher.CodexSessionDispatcher._load_state",
        lambda _self: {"version": 1, "sessions": {}, "quarantined_from": "/tmp/corrupt", "load_error": "boom"},
    )

    status.write_runtime_status(gateway_state="running")
    payload = status.read_runtime_status()

    assert payload is not None
    assert payload["restart_loop_guard_invalid"]["reason"] == "unreadable_state"
    assert payload["codex_sessions_diagnostics"] == {"quarantined_from": "/tmp/corrupt", "load_error": "boom"}
    assert "quarantined_from" not in payload
    assert "load_error" not in payload


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
