from __future__ import annotations

from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from gateway.run import GatewayRunner


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_marker_write_is_offloaded_after_claim(monkeypatch):
    runner = _runner()
    runner.session_store.lookup_by_session_key.return_value = SimpleNamespace(session_id="sid-1")
    calls: list[tuple[str, object]] = []

    class Loop:
        def run_in_executor(self, executor, fn):
            calls.append(("executor", executor))
            return object()

    monkeypatch.setattr("asyncio.get_running_loop", lambda: Loop())
    write_marker = MagicMock()
    monkeypatch.setattr("gateway.inflight_crash_markers.write_marker", write_marker)

    runner._write_inflight_crash_marker("s1", started_at=123.0)

    assert len(calls) == 1
    assert calls[0][0] == "executor"
    assert isinstance(calls[0][1], ThreadPoolExecutor)
    assert calls[0][1]._max_workers == 1  # noqa: SLF001
    write_marker.assert_not_called()


def test_marker_remove_is_offloaded(monkeypatch):
    runner = _runner()
    calls: list[tuple[str, object]] = []

    class Loop:
        def run_in_executor(self, executor, fn):
            calls.append(("executor", executor))
            return object()

    monkeypatch.setattr("asyncio.get_running_loop", lambda: Loop())
    runner._running_agents = {"s1": object()}
    runner._running_agents_ts = {"s1": 1.0}
    runner._active_session_leases = {}
    runner._persist_active_agents = MagicMock()
    monkeypatch.setattr("gateway.inflight_crash_markers.remove_marker", MagicMock())

    assert runner._release_running_agent_state("s1") is True
    assert len(calls) == 1
    assert calls[0][0] == "executor"
    assert isinstance(calls[0][1], ThreadPoolExecutor)
    assert calls[0][1]._max_workers == 1  # noqa: SLF001
