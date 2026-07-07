import json

from tests.gateway.test_codex_session_dispatcher import _make_dispatcher


def test_corrupt_codex_sessions_json_is_quarantined_not_silently_emptied(tmp_path):
    dispatcher, _broker, _send = _make_dispatcher(tmp_path)
    sessions_path = tmp_path / "codex_sessions.json"
    sessions_path.write_text('{"version": 1, "sessions": {"t1":', encoding="utf-8")

    state = dispatcher._load_state()

    assert state["sessions"] == {}
    assert state.get("quarantined_from")
    assert not sessions_path.exists()
    quarantined = list(tmp_path.glob("codex_sessions.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8").startswith('{"version"')


def test_codex_sessions_write_uses_valid_json_after_atomic_replace(tmp_path):
    dispatcher, _broker, _send = _make_dispatcher(tmp_path)

    dispatcher._write_state({"version": 1, "sessions": {"t1": {"state": "EXECUTING"}}})

    data = json.loads((tmp_path / "codex_sessions.json").read_text(encoding="utf-8"))
    assert data["sessions"]["t1"]["state"] == "EXECUTING"
    assert not list(tmp_path.glob("*.tmp"))
