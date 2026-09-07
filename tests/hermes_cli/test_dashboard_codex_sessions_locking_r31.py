from __future__ import annotations

import json
from unittest.mock import MagicMock


def test_dashboard_persist_uses_shared_locked_json_helper(tmp_path, monkeypatch):
    from hermes_cli import dashboard_codex_sessions as mod

    sessions_path = tmp_path / "codex_sessions.json"
    monkeypatch.setattr(mod, "_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(mod, "_invalidate_snapshot", MagicMock())
    writer = MagicMock()
    monkeypatch.setattr(mod, "write_locked_json", writer)

    state = {"version": 1, "sessions": {}}
    mod._persist_sessions(state)

    writer.assert_called_once_with(sessions_path, state, indent=2)
    mod._invalidate_snapshot.assert_called_once_with()


def test_dashboard_load_uses_shared_locked_json_helper(tmp_path, monkeypatch):
    from hermes_cli import dashboard_codex_sessions as mod

    sessions_path = tmp_path / "codex_sessions.json"
    sessions_path.write_text(json.dumps({"version": 1, "sessions": {}}), encoding="utf-8")
    loader = MagicMock(return_value={"version": 1, "sessions": {"t": {}}})
    monkeypatch.setattr(mod, "load_locked_json", loader)

    assert mod._load_json(sessions_path)["sessions"] == {"t": {}}
    loader.assert_called_once_with(sessions_path)
