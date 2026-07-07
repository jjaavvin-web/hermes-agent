from datetime import datetime
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform
from gateway.inflight_crash_markers import load_markers, remove_marker, write_marker
from gateway.session import SessionEntry, SessionStore


def _store(tmp_path):
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(tmp_path, GatewayConfig())
    store._db = None
    store._loaded = True
    return store


def test_unclean_boot_marks_only_marker_named_session_not_recent_idle(tmp_path):
    store = _store(tmp_path)
    now = datetime.now()
    store._entries = {
        "agent:main:discord:dm:marked": SessionEntry(
            session_key="agent:main:discord:dm:marked",
            session_id="s-marked",
            created_at=now,
            updated_at=now,
            platform=Platform.DISCORD,
        ),
        "agent:main:discord:dm:recent-completed": SessionEntry(
            session_key="agent:main:discord:dm:recent-completed",
            session_id="s-recent",
            created_at=now,
            updated_at=now,
            platform=Platform.DISCORD,
        ),
    }

    count = store.mark_inflight_sessions_from_markers([
        {"session_key": "agent:main:discord:dm:marked", "session_id": "s-marked"}
    ])

    assert count == 1
    assert store._entries["agent:main:discord:dm:marked"].resume_pending is True
    assert store._entries["agent:main:discord:dm:recent-completed"].resume_pending is False


def test_inflight_marker_roundtrip_and_release(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr("gateway.inflight_crash_markers.get_hermes_home", lambda: home)

    path = write_marker(
        "agent:main:webhook:dm:abc",
        session_id="s1",
        started_at=42.0,
        worktree="/tmp/wh-x",
        autonomous_dispatch=True,
    )

    assert path is not None and path.exists()
    markers = load_markers()
    assert markers[0]["session_key"] == "agent:main:webhook:dm:abc"
    assert markers[0]["worktree"] == "/tmp/wh-x"
    remove_marker("agent:main:webhook:dm:abc")
    assert load_markers() == []
