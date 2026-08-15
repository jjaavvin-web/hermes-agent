"""Tests for gateway.codex_gc_watcher (P5.1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.codex_gc_watcher import CodexGcWatcher


class _FakeDispatcher:
    def __init__(self, hermes_home: Path, rows: dict) -> None:
        self._sessions_path = hermes_home / "codex_sessions.json"
        self._state = {"version": 1, "sessions": rows}
        self._sessions_path.write_text(json.dumps(self._state))

    def _load_state(self) -> dict:
        return json.loads(self._sessions_path.read_text(encoding="utf-8"))


def _row(sid: str) -> dict:
    return {"session_id": sid, "state": "EXECUTING", "thread_id": f"t-{sid}"}


@pytest.mark.asyncio
async def test_tick_derives_tracked_sids_from_rows(tmp_path):
    rows = {"t1": _row("sid-aaa"), "t2": _row("sid-bbb"), "t3": _row("sid-ccc")}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker, gh_list_open_branches=lambda: set())
    await w._tick()
    broker.gc.assert_called_once()
    sids = broker.gc.call_args.kwargs["tracked_sids"]
    assert sids == {"sid-aaa", "sid-bbb", "sid-ccc"}


@pytest.mark.asyncio
async def test_tick_handles_empty_sessions(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker, gh_list_open_branches=lambda: set())
    await w._tick()
    broker.gc.assert_called_once_with(tracked_sids=set(), live_branches=set())


@pytest.mark.asyncio
async def test_tick_calls_broker_reap_deleted(tmp_path):
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker, reap_max_age_days=14, gh_list_open_branches=lambda: set())
    await w._tick()
    broker.reap_deleted.assert_called_once_with(max_age_days=14)


@pytest.mark.asyncio
async def test_gc_exception_does_not_skip_reap(tmp_path):
    """ISA D-3: gc raising must not abort the tick or skip reap."""
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.side_effect = RuntimeError("disk full")
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker, gh_list_open_branches=lambda: set())
    # Must not raise.
    await w._tick()
    broker.reap_deleted.assert_called_once()


@pytest.mark.asyncio
async def test_reap_exception_does_not_kill_loop(tmp_path):
    """Symmetric: reap raising must not propagate."""
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.side_effect = OSError("read-only fs")
    w = CodexGcWatcher(dispatcher=disp, worktree_broker=broker, gh_list_open_branches=lambda: set())
    await w._tick()  # must not raise


@pytest.mark.asyncio
async def test_tick_passes_live_branches_to_gc(tmp_path):
    """P5.1+: live_branches set fetched from gh pr list and passed to broker.gc."""
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    fake_branches = {"codex/sid-bbb/task", "codex/sid-ccc/feat"}
    w = CodexGcWatcher(
        dispatcher=disp, worktree_broker=broker,
        gh_list_open_branches=lambda: fake_branches,
    )
    await w._tick()
    kw = broker.gc.call_args.kwargs
    assert kw["tracked_sids"] == {"sid-aaa"}
    assert kw["live_branches"] == fake_branches


@pytest.mark.asyncio
async def test_tick_tolerates_gh_callable_crashing(tmp_path):
    """C7 MED-1: a crashing lookup must NOT abort the tick, and must NOT
    degrade to ``live_branches=set()``.

    An empty set asserts "no open PR protects any of these worktrees" — the
    very claim the failed lookup could not verify.  gc is skipped instead;
    reap_deleted (which does not consult live_branches) still runs.
    """
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    def crashing_gh():
        raise RuntimeError("network gone")
    w = CodexGcWatcher(
        dispatcher=disp, worktree_broker=broker,
        gh_list_open_branches=crashing_gh,
    )
    await w._tick()  # must not raise
    broker.gc.assert_not_called()
    broker.reap_deleted.assert_called_once()


def test_default_gh_helper_raises_when_gh_is_missing(monkeypatch):
    """_gh_list_open_branches must FAIL, not return an empty set."""
    from gateway import codex_gc_watcher as mod

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh not on PATH")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    with pytest.raises(mod.GhLookupError):
        mod._gh_list_open_branches()


def test_default_gh_helper_runs_gh_from_repo_root(monkeypatch):
    """gh pr list must run from the Hermes repo root, not the gateway CWD."""
    from gateway import codex_gc_watcher as mod
    from unittest.mock import MagicMock

    fake_proc = MagicMock(returncode=0, stdout="[]", stderr="")
    run = MagicMock(return_value=fake_proc)
    monkeypatch.setattr(mod.subprocess, "run", run)

    assert mod._gh_list_open_branches() == set()
    assert run.call_args.kwargs["cwd"] == mod._REPO_ROOT


def test_default_gh_helper_raises_on_timeout(monkeypatch):
    """A timed-out lookup is an unknown, not an empty PR set."""
    from gateway import codex_gc_watcher as mod

    def fake_run(*args, **kwargs):
        raise mod.subprocess.TimeoutExpired(cmd=args[0], timeout=30)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    with pytest.raises(mod.GhLookupError):
        mod._gh_list_open_branches()


def test_default_gh_helper_raises_on_nonzero_exit(monkeypatch):
    """gh exiting non-zero (e.g. auth expired) must fail closed."""
    from gateway import codex_gc_watcher as mod
    from unittest.mock import MagicMock

    fake_proc = MagicMock(returncode=1, stdout="", stderr="auth needed")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: fake_proc)
    with pytest.raises(mod.GhLookupError, match="auth needed"):
        mod._gh_list_open_branches()


def test_default_gh_helper_reports_a_genuinely_empty_pr_list(monkeypatch):
    """The one case that IS an empty set: gh succeeded and there are no PRs.

    This is the pair to the raising tests — fail-closed must not mean
    "never returns empty", it means "only returns empty when it knows".
    """
    from gateway import codex_gc_watcher as mod
    from unittest.mock import MagicMock

    fake_proc = MagicMock(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: fake_proc)
    assert mod._gh_list_open_branches() == set()


@pytest.mark.asyncio
async def test_a_genuinely_empty_pr_set_still_runs_gc(tmp_path):
    """Fail-closed must not stall gc when the lookup actually succeeded."""
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(
        dispatcher=disp, worktree_broker=broker, gh_list_open_branches=lambda: set(),
    )
    await w._tick()
    broker.gc.assert_called_once()
    assert broker.gc.call_args.kwargs["live_branches"] == set()


def test_default_gh_helper_parses_branch_names(monkeypatch):
    """Happy path: gh returns JSON; we extract headRefName values."""
    from gateway import codex_gc_watcher as mod
    from unittest.mock import MagicMock
    import json as _json

    payload = [
        {"headRefName": "codex/sid-aaa/task"},
        {"headRefName": "codex/sid-bbb/feat"},
        {"headRefName": ""},  # empty, must be filtered
        {},  # no headRefName, must be skipped
    ]
    fake_proc = MagicMock(returncode=0, stdout=_json.dumps(payload), stderr="")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: fake_proc)
    result = mod._gh_list_open_branches()
    assert result == {"codex/sid-aaa/task", "codex/sid-bbb/feat"}


def test_default_gh_helper_raises_on_malformed_json(monkeypatch):
    """A response we cannot parse tells us nothing — fail closed."""
    from gateway import codex_gc_watcher as mod
    from unittest.mock import MagicMock

    fake_proc = MagicMock(returncode=0, stdout="{not valid json", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: fake_proc)
    with pytest.raises(mod.GhLookupError):
        mod._gh_list_open_branches()


@pytest.mark.asyncio
async def test_start_then_stop_clean(tmp_path):
    disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-aaa")})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    w = CodexGcWatcher(
        dispatcher=disp, worktree_broker=broker, poll_interval_sec=0.05,
        gh_list_open_branches=lambda: set(),
    )
    await w.start()
    await asyncio.sleep(0.18)
    await w.stop()
    # At least one tick should have run in 180ms with a 50ms interval.
    assert broker.gc.call_count >= 1
    assert broker.reap_deleted.call_count >= 1
    assert w._task is None
