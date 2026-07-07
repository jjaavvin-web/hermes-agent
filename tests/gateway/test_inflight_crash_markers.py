from datetime import datetime
import time
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform
from gateway.inflight_crash_markers import (
    MARKER_MAX_AGE_SECONDS,
    load_markers,
    remove_marker,
    write_marker,
)
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


def test_marker_at_exact_max_age_is_retained(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr("gateway.inflight_crash_markers.get_hermes_home", lambda: home)
    now = 2_000_000_000.0
    marker_started_at = now - MARKER_MAX_AGE_SECONDS
    monkeypatch.setattr("gateway.inflight_crash_markers.time.time", lambda: now)

    write_marker("agent:main:webhook:dm:edge", session_id="s-edge", started_at=marker_started_at)

    markers = load_markers()
    assert [marker["session_key"] for marker in markers] == ["agent:main:webhook:dm:edge"]


def test_gateway_marker_io_orders_slow_write_before_fast_remove_for_same_key(tmp_path, monkeypatch):
    from gateway import run as gateway_run
    from gateway.run import GatewayRunner

    home = tmp_path / "home"
    session_key = "agent:main:webhook:dm:ordered"
    monkeypatch.setattr("gateway.inflight_crash_markers.get_hermes_home", lambda: home)

    runner = object.__new__(GatewayRunner)
    slow_write_started = []

    def slow_write():
        slow_write_started.append(True)
        time.sleep(0.05)
        write_marker(session_key, session_id="s-ordered")

    write_future = runner._submit_inflight_marker_io(slow_write, "write", session_key)
    remove_future = runner._submit_inflight_marker_io(lambda: remove_marker(session_key), "remove", session_key)

    write_future.result(timeout=2)
    remove_future.result(timeout=2)
    assert slow_write_started == [True]
    assert load_markers() == []
    assert gateway_run._INFLIGHT_MARKER_IO_EXECUTOR._max_workers == 1  # noqa: SLF001
