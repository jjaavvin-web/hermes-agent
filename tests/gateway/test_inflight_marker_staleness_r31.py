from __future__ import annotations

import json
import time


def test_stale_marker_ignored_and_removed(tmp_path, monkeypatch, caplog):
    from gateway import inflight_crash_markers as markers

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    marker_dir = tmp_path / "gateway" / "inflight"
    marker_dir.mkdir(parents=True)
    path = marker_dir / "old.json"
    path.write_text(
        json.dumps({"session_key": "s1", "started_at": time.time() - 48 * 60 * 60}),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="gateway.run"):
        loaded = markers.load_markers()

    assert loaded == []
    assert not path.exists()
    assert "Ignoring stale in-flight crash marker" in caplog.text


def test_missing_session_marker_swept(tmp_path, monkeypatch, caplog):
    from gateway import inflight_crash_markers as markers

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    marker_dir = tmp_path / "gateway" / "inflight"
    marker_dir.mkdir(parents=True)
    path = marker_dir / "missing.json"
    path.write_text(json.dumps({"session_key": "gone", "started_at": time.time()}), encoding="utf-8")

    with caplog.at_level("WARNING", logger="gateway.run"):
        loaded = markers.load_markers(live_session_keys=["alive"])

    assert loaded == []
    assert not path.exists()
    assert "missing session" in caplog.text
