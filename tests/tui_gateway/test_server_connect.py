"""Focused tests for TUI gateway connect/reconnect session handshakes."""

import sys
import threading
import types

import pytest

_original_stdout = sys.stdout
import tui_gateway.server as server_mod
sys.stdout = _original_stdout


class DummyTransport:
    def __init__(self, name="transport"):
        self.name = name
        self.writes = []

    def write(self, obj):
        self.writes.append(obj)
        return True


def _base_session(session_key="stored-session", transport=None, history=None):
    return {
        "agent": types.SimpleNamespace(model="test/model", provider="test-provider"),
        "created_at": 100.0,
        "history": history if history is not None else [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "last_active": 100.0,
        "running": False,
        "session_key": session_key,
        "transport": transport or DummyTransport("old"),
    }


@pytest.fixture(autouse=True)
def clean_server_state(monkeypatch, tmp_path):
    server_mod._sessions.clear()
    server_mod._pending.clear()
    server_mod._pending_prompt_payloads.clear()
    server_mod._answers.clear()
    monkeypatch.setattr(sys, "stdout", _original_stdout)
    monkeypatch.setattr(server_mod, "_get_db", lambda: None)
    monkeypatch.setattr(server_mod, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server_mod, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server_mod, "_load_tool_progress_mode", lambda: "new")
    monkeypatch.setattr(server_mod, "_resolve_model", lambda: "global/default")
    monkeypatch.setattr(server_mod, "_git_branch_for_cwd", lambda _cwd: "test-branch")
    monkeypatch.setattr(server_mod, "_completion_cwd", lambda _params=None: str(tmp_path))
    monkeypatch.setattr(server_mod, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server_mod, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server_mod, "_profile_home", lambda _profile: None)
    yield
    server_mod._sessions.clear()
    server_mod._pending.clear()
    server_mod._pending_prompt_payloads.clear()
    server_mod._answers.clear()
    sys.stdout = _original_stdout


def test_session_create_returns_lightweight_handshake_without_building_agent(monkeypatch, tmp_path):
    """session.create is the connection handshake: return immediately and defer build."""

    created_timers = []

    class FakeTimer:
        daemon = False

        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            self.started = False
            created_timers.append(self)

        def start(self):
            self.started = True

    transport = DummyTransport("ws-connect")
    monkeypatch.setattr(server_mod.uuid, "uuid4", lambda: types.SimpleNamespace(hex="abcdef1234567890"))
    monkeypatch.setattr(server_mod, "_new_session_key", lambda: "20260628_010203_handsh")
    monkeypatch.setattr(server_mod, "current_transport", lambda: transport)
    monkeypatch.setattr(server_mod.threading, "Timer", FakeTimer)
    monkeypatch.setattr(server_mod, "_claim_active_session_slot", lambda *_a, **_k: ("lease-token", None))

    def fail_build(*_args, **_kwargs):  # pragma: no cover - assertion path
        raise AssertionError("session.create must not synchronously build an agent")

    monkeypatch.setattr(server_mod, "_start_agent_build", fail_build)

    resp = server_mod.handle_request(
        {
            "id": "connect-1",
            "method": "session.create",
            "params": {
                "cols": 132,
                "messages": [{"role": "user", "text": "seed hello"}],
                "title": "seed title",
                "model": "chosen/model",
                "provider": "custom:chosen",
                "fast": True,
                "source": "desktop-ws",
                "close_on_disconnect": True,
                "cwd": str(tmp_path),
            },
        }
    )

    assert "error" not in resp
    result = resp["result"]
    assert result["session_id"] == "abcdef12"
    assert result["stored_session_id"] == "20260628_010203_handsh"
    assert result["message_count"] == 1
    assert result["messages"] == [{"role": "user", "text": "seed hello"}]
    assert result["info"]["cwd"] == str(tmp_path)
    assert result["info"]["branch"] == "test-branch"
    assert result["info"]["model"] == "chosen/model"
    assert result["info"]["provider"] == "custom:chosen"
    assert result["info"]["lazy"] is True
    assert result["info"]["desktop_contract"] == server_mod.DESKTOP_BACKEND_CONTRACT

    session = server_mod._sessions["abcdef12"]
    assert session["transport"] is transport
    assert session["close_on_disconnect"] is True
    assert session["source"] == "desktop-ws"
    assert session["cols"] == 132
    assert session["pending_title"] == "seed title"
    assert session["model_override"] == {"model": "chosen/model", "provider": "custom:chosen"}
    assert session["create_service_tier_override"] == "priority"
    assert session["active_session_lease"] == "lease-token"
    assert session["agent"] is None
    assert session["agent_ready"] is not None and not session["agent_ready"].is_set()
    assert len(created_timers) == 1
    assert created_timers[0].interval == pytest.approx(0.05)
    assert created_timers[0].started is True


def test_disconnect_reaps_close_on_disconnect_and_parks_reconnectable_sessions(monkeypatch):
    """WS disconnect should close sidecar sessions and park reconnectable ones."""

    shared = DummyTransport("shared-ws")
    unrelated = DummyTransport("other-ws")
    server_mod._sessions.update(
        {
            "close-me": _base_session("close-key", shared) | {"close_on_disconnect": True},
            "park-me": _base_session("park-key", shared) | {"close_on_disconnect": False},
            "ignore-me": _base_session("ignore-key", unrelated) | {"close_on_disconnect": True},
        }
    )
    closed = []
    scheduled = []
    monkeypatch.setattr(server_mod, "_close_session_by_id", lambda sid, **_k: closed.append(sid) or True)
    monkeypatch.setattr(server_mod, "_schedule_ws_orphan_reap", lambda sid: scheduled.append(sid))

    assert server_mod._close_sessions_for_transport(shared, end_reason="ws_disconnect") == (1, 1)

    assert closed == ["close-me"]
    assert scheduled == ["park-me"]
    assert server_mod._sessions["park-me"]["transport"] is server_mod._detached_ws_transport
    assert server_mod._sessions["ignore-me"]["transport"] is unrelated


def test_orphan_reap_timer_closes_only_still_detached_sessions(monkeypatch):
    """The reconnect grace timer must be cancelled by a live transport reattach."""

    callbacks = []

    class FakeTimer:
        daemon = False

        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            callbacks.append(callback)

        def start(self):
            return None

    closed = []
    monkeypatch.setattr(server_mod.threading, "Timer", FakeTimer)
    monkeypatch.setattr(server_mod, "_WS_ORPHAN_REAP_GRACE_S", 0.25)
    monkeypatch.setattr(server_mod, "_close_session_by_id", lambda sid, **_k: closed.append((sid, _k.get("end_reason"))) or True)

    live_transport = DummyTransport("reattached")
    server_mod._sessions["sid"] = _base_session("key", server_mod._detached_ws_transport)
    server_mod._schedule_ws_orphan_reap("sid")
    server_mod._sessions["sid"]["transport"] = live_transport
    callbacks.pop()()
    assert closed == []

    server_mod._sessions["sid"]["transport"] = server_mod._detached_ws_transport
    server_mod._schedule_ws_orphan_reap("sid")
    callbacks.pop()()
    assert closed == [("sid", "ws_orphan_reap")]


def test_session_activate_rebinds_live_session_to_current_transport(monkeypatch):
    """session.activate is a reconnect path for an already-live in-memory session."""

    old_transport = DummyTransport("old")
    new_transport = DummyTransport("new")
    session = _base_session("live-key", old_transport)
    session["inflight_turn"] = {"user": "question", "assistant": "partial", "streaming": True}
    server_mod._sessions["live-sid"] = session
    monkeypatch.setattr(server_mod, "current_transport", lambda: new_transport)
    monkeypatch.setattr(server_mod, "_fallback_session_info", lambda _session: {"model": "test/model", "lazy": False})

    before = session["last_active"]
    resp = server_mod.handle_request(
        {"id": "activate", "method": "session.activate", "params": {"session_id": "live-sid"}}
    )

    assert "error" not in resp
    result = resp["result"]
    assert result["session_id"] == "live-sid"
    assert result["session_key"] == "live-key"
    assert result["messages"] == [{"role": "user", "text": "hello"}]
    assert result["inflight"] == {"user": "question", "assistant": "partial", "streaming": True}
    assert result["status"] == "idle"
    assert session["transport"] is new_transport
    assert session["last_active"] >= before


def test_session_resume_fast_path_reuses_live_session_and_reattaches_transport(monkeypatch):
    """session.resume reconnects to an existing live session instead of building a duplicate."""

    class FakeDB:
        def get_session(self, session_id):
            assert session_id == "stored-key"
            return {"id": "stored-key", "cwd": "/tmp"}

        def get_session_by_title(self, _title):  # pragma: no cover - should not be needed
            raise AssertionError("id lookup should find the session")

        def resolve_resume_session_id(self, session_id):
            return session_id

    old_transport = DummyTransport("detached")
    new_transport = DummyTransport("reconnected")
    session = _base_session("stored-key", old_transport, history=[{"role": "assistant", "content": "welcome"}])
    server_mod._sessions["runtime-sid"] = session
    monkeypatch.setattr(server_mod, "_get_db", lambda: FakeDB())
    monkeypatch.setattr(server_mod, "current_transport", lambda: new_transport)
    monkeypatch.setattr(server_mod, "_fallback_session_info", lambda _session: {"model": "test/model", "lazy": False})

    resp = server_mod.handle_request(
        {"id": "resume", "method": "session.resume", "params": {"session_id": "stored-key", "cols": 101}}
    )

    assert "error" not in resp
    result = resp["result"]
    assert result["session_id"] == "runtime-sid"
    assert result["session_key"] == "stored-key"
    assert result["resumed"] == "stored-key"
    assert result["messages"] == [{"role": "assistant", "text": "welcome"}]
    assert result["status"] == "idle"
    assert session["transport"] is new_transport
    assert session["cols"] == 101


def test_wait_agent_reports_timeout_and_cached_initialization_error():
    ready = threading.Event()
    timeout_resp = server_mod._wait_agent({"agent_ready": ready}, "rid", timeout=0.001)
    assert timeout_resp["error"] == {"code": 5032, "message": "agent initialization timed out"}

    ready.set()
    error_resp = server_mod._wait_agent({"agent_ready": ready, "agent_error": "bad handshake"}, "rid")
    assert error_resp["error"] == {"code": 5032, "message": "bad handshake"}

    assert server_mod._wait_agent({"agent_ready": ready}, "rid") is None
