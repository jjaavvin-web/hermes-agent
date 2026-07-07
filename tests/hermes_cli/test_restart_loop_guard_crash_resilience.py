import json

from gateway import restart_loop_guard as guard


def test_corrupt_restart_loop_state_is_quarantined_and_marked_invalid(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(guard, "get_hermes_home", lambda: home)
    state = home / "gateway" / "restart_loop.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"boots": [1.0],', encoding="utf-8")

    assert guard._load_boots() == []

    marker = state.with_suffix(".invalid.json")
    status = json.loads(marker.read_text(encoding="utf-8"))
    assert status["status"] == "invalid"
    assert status["reason"] == "unreadable_state"
    assert "quarantined=" in status["detail"]
    assert not state.exists()
    assert list(state.parent.glob("restart_loop.json.corrupt-*"))


def test_atomic_restart_loop_write_clears_invalid_marker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(guard, "get_hermes_home", lambda: home)
    marker = home / "gateway" / "restart_loop.invalid.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"status": "invalid"}', encoding="utf-8")

    guard._save_boots([123.0])

    assert json.loads((home / "gateway" / "restart_loop.json").read_text(encoding="utf-8")) == {"boots": [123.0]}
    assert guard.invalid_status() is None
