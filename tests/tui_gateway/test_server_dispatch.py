"""Focused coverage for tui_gateway.server dispatch/error/disconnect cleanup paths."""

from __future__ import annotations

import importlib
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test_server_dispatch")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        methods = mod._methods.copy()
        try:
            yield mod
        finally:
            mod._sessions.clear()
            mod._pending.clear()
            mod._pending_prompt_payloads.clear()
            mod._answers.clear()
            mod._methods.clear()
            mod._methods.update(methods)


class RecordingTransport:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.closed = False

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        self.closed = True


class InlinePool:
    """Run submitted RPC work immediately but through the same callable seam."""

    def submit(self, fn):
        fn()
        return types.SimpleNamespace(done=lambda: True)


def test_protocol_errors_reject_malformed_messages_before_method_dispatch(server):
    assert server.dispatch("not-a-dict")["error"] == {
        "code": -32600,
        "message": "invalid request: expected an object",
    }
    assert server.dispatch({"id": "missing-method"})["error"] == {
        "code": -32600,
        "message": "invalid request: method must be a non-empty string",
    }
    assert server.dispatch({"id": "bad-method", "method": 123})["error"]["code"] == -32600
    assert server.dispatch({"id": "bad-params", "method": "anything", "params": []})[
        "error"
    ] == {"code": -32602, "message": "invalid params: expected an object"}

    unknown = server.dispatch({"id": "unknown", "method": "no.such.method"})
    assert unknown["error"]["code"] == -32601
    assert unknown["error"]["message"] == "unknown method: no.such.method"


    params_none = server.dispatch({"id": "none-params", "method": "no.such.method", "params": None})
    assert params_none["error"]["code"] == -32601


def test_dispatch_fast_path_binds_transport_and_returns_inline_response(server):
    seen = {}

    def fast_handler(rid, params):
        seen["transport"] = server.current_transport()
        seen["params"] = params
        return server._ok(rid, {"ok": True, "value": params["value"]})

    transport = RecordingTransport()
    server._methods["dispatch.fast.test"] = fast_handler

    response = server.dispatch(
        {"id": "fast-1", "method": "dispatch.fast.test", "params": {"value": 42}},
        transport=transport,
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": "fast-1",
        "result": {"ok": True, "value": 42},
    }
    assert seen == {"transport": transport, "params": {"value": 42}}
    assert transport.frames == []
    assert server.current_transport() is None


def test_dispatch_long_handler_writes_response_on_bound_transport(server, monkeypatch):
    seen = {}

    def long_handler(rid, params):
        seen["transport"] = server.current_transport()
        seen["params"] = params
        return server._ok(rid, {"ran": params["payload"]})

    monkeypatch.setattr(server, "_pool", InlinePool())
    server._methods["shell.exec"] = long_handler
    transport = RecordingTransport()

    response = server.dispatch(
        {"id": "long-1", "method": "shell.exec", "params": {"payload": "slow"}},
        transport=transport,
    )

    assert response is None
    assert seen == {"transport": transport, "params": {"payload": "slow"}}
    assert transport.frames == [
        {"jsonrpc": "2.0", "id": "long-1", "result": {"ran": "slow"}}
    ]
    assert server.current_transport() is None


def test_dispatch_long_handler_exceptions_become_protocol_error_frames(server, monkeypatch):
    def broken_handler(_rid, _params):
        raise RuntimeError("boom from handler")

    monkeypatch.setattr(server, "_pool", InlinePool())
    server._methods["slash.exec"] = broken_handler
    transport = RecordingTransport()

    response = server.dispatch(
        {"id": "long-err", "method": "slash.exec", "params": {}},
        transport=transport,
    )

    assert response is None
    assert transport.frames == [
        {
            "jsonrpc": "2.0",
            "id": "long-err",
            "error": {"code": -32000, "message": "handler error: boom from handler"},
        }
    ]
    assert server.current_transport() is None


def test_close_sessions_for_transport_reaps_flagged_and_detaches_remainder(server, monkeypatch):
    disconnecting = RecordingTransport()
    unrelated = RecordingTransport()
    closed: list[tuple[str, str]] = []
    scheduled: list[str] = []

    def close_session(sid, *, end_reason):
        closed.append((sid, end_reason))
        server._sessions.pop(sid, None)
        return True

    def schedule_reap(sid):
        scheduled.append(sid)
        if sid == "detached-raises":
            raise RuntimeError("timer setup failed")

    monkeypatch.setattr(server, "_close_session_by_id", close_session)
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", schedule_reap)
    server._sessions.update(
        {
            "close-me": {"transport": disconnecting, "close_on_disconnect": True},
            "detach-me": {"transport": disconnecting, "close_on_disconnect": False},
            "detached-raises": {"transport": disconnecting, "close_on_disconnect": False},
            "unrelated": {"transport": unrelated, "close_on_disconnect": True},
        }
    )

    assert server._close_sessions_for_transport(disconnecting, end_reason="ws_disconnect") == (1, 2)

    assert closed == [("close-me", "ws_disconnect")]
    assert scheduled == ["detach-me", "detached-raises"]
    assert "close-me" not in server._sessions
    assert server._sessions["detach-me"]["transport"] is server._detached_ws_transport
    assert server._sessions["detached-raises"]["transport"] is server._detached_ws_transport
    assert server._sessions["unrelated"]["transport"] is unrelated


def test_ws_session_is_orphaned_rejects_empty_finalized_and_running_sessions(server):
    assert server._ws_session_is_orphaned(None) is False
    assert server._ws_session_is_orphaned({}) is False
    assert server._ws_session_is_orphaned({"_finalized": True}) is False
    assert server._ws_session_is_orphaned(
        {"transport": server._detached_ws_transport, "running": True}
    ) is False
    assert server._ws_session_is_orphaned(
        {"transport": server._detached_ws_transport, "running": False}
    ) is True


def test_scheduled_orphan_reap_closes_only_still_detached_sessions(server, monkeypatch):
    fired_callbacks = []

    class ImmediateTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False

        def start(self):
            fired_callbacks.append(self.delay)
            self.callback()

    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 1.25)
    monkeypatch.setattr(server.threading, "Timer", ImmediateTimer)

    closed: list[tuple[str, str]] = []

    def close_session(sid, *, end_reason):
        closed.append((sid, end_reason))
        server._sessions.pop(sid, None)
        return True

    monkeypatch.setattr(server, "_close_session_by_id", close_session)
    server._sessions["orphan"] = {
        "transport": server._detached_ws_transport,
        "running": False,
        "session_key": "orphan-key",
    }

    server._schedule_ws_orphan_reap("orphan")

    assert fired_callbacks == [1.25]
    assert closed == [("orphan", "ws_orphan_reap")]
    assert "orphan" not in server._sessions

    live = RecordingTransport()
    server._sessions["reattached"] = {"transport": live, "running": False}
    server._sessions["running"] = {"transport": server._detached_ws_transport, "running": True}
    server._schedule_ws_orphan_reap("reattached")
    server._schedule_ws_orphan_reap("running")
    assert closed == [("orphan", "ws_orphan_reap")]


def test_close_session_by_id_runs_idempotent_teardown_cleanup(server, monkeypatch):
    calls: list[tuple[str, str | None]] = []
    stop_event = threading.Event()

    class FakeLease:
        def release(self):
            calls.append(("lease.release", None))

    class FakeAgent:
        session_id = "agent-session-id"

        def __init__(self) -> None:
            self.committed_history = None

        def commit_memory_session(self, history):
            self.committed_history = history
            calls.append(("agent.commit_memory_session", str(len(history))))

        def close(self):
            calls.append(("agent.close", None))

    class FakeWorker:
        def __init__(self) -> None:
            self.closed = 0

        def close(self):
            self.closed += 1
            calls.append(("worker.close", str(self.closed)))

    class FakeDB:
        def end_session(self, session_id, end_reason):
            calls.append(("db.end_session", f"{session_id}:{end_reason}"))

    approval_module = types.SimpleNamespace(
        unregister_gateway_notify=lambda key: calls.append(("approval.unregister", key))
    )
    monkeypatch.setitem(sys.modules, "tools.approval", approval_module)
    monkeypatch.setattr(
        server,
        "_notify_session_boundary",
        lambda event, session_id: calls.append((event, session_id)),
    )
    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())

    worker = FakeWorker()
    agent = FakeAgent()
    session = {
        "active_session_lease": FakeLease(),
        "agent": agent,
        "history": [{"role": "user", "content": "hi"}],
        "history_lock": threading.RLock(),
        "session_key": "session-key",
        "slash_worker": worker,
        "_notif_stop": stop_event,
    }
    server._sessions["sid"] = session

    assert server._close_session_by_id("missing", end_reason="nope") is False
    assert server._close_session_by_id("sid", end_reason="disconnect-test") is True

    assert "sid" not in server._sessions
    assert session["_finalized"] is True
    assert stop_event.is_set()
    assert agent.committed_history == [{"role": "user", "content": "hi"}]
    assert calls == [
        ("lease.release", None),
        ("agent.commit_memory_session", "1"),
        ("on_session_finalize", "agent-session-id"),
        ("db.end_session", "agent-session-id:disconnect-test"),
        ("worker.close", "1"),
        ("approval.unregister", "session-key"),
        ("agent.close", None),
    ]

    # A direct finalize/teardown after close is harmless: the finalized guard
    # prevents a second worker close or duplicate memory commit.
    server._finalize_session(session, end_reason="again")
    server._teardown_session(session, end_reason="again")
    assert worker.closed == 1
