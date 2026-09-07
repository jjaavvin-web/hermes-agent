from __future__ import annotations

import json
from unittest.mock import MagicMock

from gateway.run import GatewayRunner


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    return runner


def test_unclean_shutdown_empty_markers_uses_legacy_fallback_and_warns(monkeypatch, caplog):
    runner = _runner()
    runner.session_store.session_keys.return_value = ["s1"]
    runner.session_store.suspend_recently_active.return_value = 2
    load = MagicMock(return_value=[])
    monkeypatch.setattr("gateway.inflight_crash_markers.load_markers", load)

    with caplog.at_level("WARNING", logger="gateway.run"):
        result = runner._recover_inflight_sessions_after_unclean_shutdown()

    assert result == (0, 2, True)
    load.assert_called_once_with(live_session_keys=["s1"])
    runner.session_store.suspend_recently_active.assert_called_once_with()
    runner.session_store.mark_inflight_sessions_from_markers.assert_not_called()
    assert "zero in-flight crash markers" in caplog.text


def test_unclean_shutdown_markers_use_marker_path_without_legacy_fallback(monkeypatch):
    runner = _runner()
    runner.session_store.session_keys.return_value = ["s1"]
    runner.session_store.mark_inflight_sessions_from_markers.return_value = 1
    markers = [{"session_key": "s1", "session_id": "sid-1"}]
    load = MagicMock(return_value=markers)
    monkeypatch.setattr("gateway.inflight_crash_markers.load_markers", load)

    result = runner._recover_inflight_sessions_after_unclean_shutdown()

    assert result == (1, 1, False)
    runner.session_store.mark_inflight_sessions_from_markers.assert_called_once_with(markers)
    runner.session_store.suspend_recently_active.assert_not_called()


def test_corrupt_marker_warns_and_empty_set_triggers_fallback(tmp_path, monkeypatch, caplog):
    from gateway import inflight_crash_markers as markers

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    marker_dir = tmp_path / "gateway" / "inflight"
    marker_dir.mkdir(parents=True)
    (marker_dir / "corrupt.json").write_text('{"session_key":', encoding="utf-8")
    runner = _runner()
    runner.session_store.session_keys.return_value = ["s1"]
    runner.session_store.suspend_recently_active.return_value = 1

    with caplog.at_level("WARNING", logger="gateway.run"):
        loaded = markers.load_markers(live_session_keys=["s1"])
        result = runner._recover_inflight_sessions_after_unclean_shutdown()

    assert loaded == []
    assert result == (0, 1, True)
    assert "Ignoring unreadable in-flight crash marker" in caplog.text
    assert "zero in-flight crash markers" in caplog.text
