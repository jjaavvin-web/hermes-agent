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

    # The close-on-disconnect claim is resume-race-sensitive (#39591): it pops
    # under _session_resume_lock itself and calls _teardown_popped_session
    # directly, not the _close_session_by_id convenience wrapper (that
    # wrapper is for callers with no resume race to guard against). Mock the
    # function actually on this path -- the real (unmocked) _pop_session_by_id
    # already removes the session from server._sessions and stamps "_sid"
    # onto it before handing it to teardown.
    def teardown_popped(session, *, end_reason):
        sid = (session or {}).get("_sid")
        closed.append((sid, end_reason))
        return True

    def schedule_reap(sid):
        scheduled.append(sid)
        if sid == "detached-raises":
            raise RuntimeError("timer setup failed")

    monkeypatch.setattr(server, "_teardown_popped_session", teardown_popped)
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

    # The reap closure is resume-race-sensitive (#39591), so it pops under
    # _session_resume_lock itself and calls _teardown_popped_session directly
    # rather than the _close_session_by_id convenience wrapper (that wrapper
    # is for callers with no resume race to guard against). Mock the function
    # actually on this path -- the real (unmocked) _pop_session_by_id already
    # removes the session from server._sessions and stamps "_sid" onto it.
    def teardown_popped(session, *, end_reason):
        sid = (session or {}).get("_sid")
        closed.append((sid, end_reason))
        return True

    monkeypatch.setattr(server, "_teardown_popped_session", teardown_popped)
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
    server._schedule_ws_orphan_reap("reattached")
    assert closed == [("orphan", "ws_orphan_reap")]


def _running_detached_session(server, *, run_thread=None, session_key="running-key"):
    """Mid-turn session already pointed at the drop sentinel: the shape
    ``_interrupt_session_turn`` needs (#85578) -- a real lock, a session_key
    for the approval-resolve step, and an agent stub with neither
    ``interrupt`` nor ``hard_interrupt`` (so the legacy-ABI fallback in
    ``agent.interrupt_compat.request_hard_interrupt`` is a safe no-op).

    ``server`` is the ``tui_gateway.server`` module -- pass the fixture value
    explicitly since fixture injection only applies to test functions, not
    plain helpers sharing the fixture's parameter name."""
    return {
        "transport": server._detached_ws_transport,
        "running": True,
        "session_key": session_key,
        "agent": types.SimpleNamespace(model="test/model", provider="test-provider"),
        "history_lock": threading.Lock(),
        "_run_thread": run_thread,
    }


def test_scheduled_orphan_reap_interrupts_running_session_once_then_reaps_after_it_settles(
    server, monkeypatch
):
    """A mid-turn detached session (#85578) is interrupted exactly once and
    is NOT reaped on that same pass; only once the run thread's liveness
    check reports the turn has settled does the next poll reap it."""

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
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda session, *, end_reason: closed.append(((session or {}).get("_sid"), end_reason))
        or True,
    )

    session = _running_detached_session(server)

    class SettlesOnFirstLivenessCheck:
        """Stand-in for the real background turn-runner thread: reports
        alive for the interrupt's own liveness snapshot (so
        ``_interrupt_session_turn`` does not itself clear ``running``), then
        -- mirroring how the real thread's own finalization clears the flag
        as it exits -- the session settles before the reaper's next poll."""

        def is_alive(self):
            session["running"] = False
            return True

    session["_run_thread"] = SettlesOnFirstLivenessCheck()
    server._sessions["running"] = session

    server._schedule_ws_orphan_reap("running")

    # Exactly two polls fire: the reconnect-grace poll (interrupt requested,
    # turn still running so not reaped) and one interrupt-poll-interval
    # later (turn now settled -> reaped).
    assert fired_callbacks == [1.25, server._WS_ORPHAN_INTERRUPT_REAP_POLL_S]
    assert session["_client_gone_interrupt_requested"] is True
    assert session["_client_gone_interrupt_polls"] == 1
    assert session["_turn_cancel_requested"] is True
    assert closed == [("running", "ws_orphan_reap")]
    assert "running" not in server._sessions


def test_scheduled_orphan_reap_force_reaps_running_session_after_interrupt_poll_budget_exhausted(
    server, monkeypatch
):
    """A mid-turn detached session whose turn never settles is force-reaped
    once the interrupt-poll budget (#85578) is exhausted rather than parked
    behind a timer chain forever."""

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
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda session, *, end_reason: closed.append(((session or {}).get("_sid"), end_reason))
        or True,
    )

    class NeverSettles:
        def is_alive(self):
            return True

    session = _running_detached_session(server, run_thread=NeverSettles())
    server._sessions["running"] = session

    server._schedule_ws_orphan_reap("running")

    max_polls = server._WS_ORPHAN_INTERRUPT_REAP_MAX_POLLS
    poll_s = server._WS_ORPHAN_INTERRUPT_REAP_POLL_S
    # One reconnect-grace poll plus max_polls interrupt-interval polls before
    # the budget is exhausted and the still-running session is force-reaped.
    assert fired_callbacks == [1.25] + [poll_s] * max_polls
    assert session["_client_gone_interrupt_requested"] is True
    assert session["_client_gone_interrupt_polls"] == max_polls + 1
    assert closed == [("running", "ws_orphan_reap")]
    assert "running" not in server._sessions


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
        def get_session(self, session_id):
            # _finalize_session checks the row's source before ending it, so
            # a TUI viewer never ends a gateway-owned session's lifecycle
            # (#60609). No row -> source "" -> not gateway-owned -> proceeds
            # to end_session below, matching this test's plain local session.
            return None

        def end_session(self, session_id, end_reason):
            calls.append(("db.end_session", f"{session_id}:{end_reason}"))

    approval_module = types.SimpleNamespace(
        unregister_gateway_notify=lambda key: calls.append(("approval.unregister", key))
    )
    monkeypatch.setitem(sys.modules, "tools.approval", approval_module)
    monkeypatch.setattr(
        server,
        "_notify_session_boundary",
        # _notify_session_boundary now takes a positional `platform` too (the
        # session's source, for CLI-parity lifecycle hooks) -- accept and
        # ignore it here since this test only cares about the event/session
        # pairing.
        lambda event, session_id, platform=None: calls.append((event, session_id)),
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
