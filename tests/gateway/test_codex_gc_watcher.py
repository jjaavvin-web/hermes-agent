"""Tests for gateway.codex_gc_watcher (P5.1)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.codex_gc_watcher import CodexGcWatcher


class _FakeDispatcher:
    def __init__(self, hermes_home: Path, rows: dict) -> None:
        self.hermes_home = hermes_home
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
async def test_tick_fails_closed_when_gh_callable_crashes(tmp_path):
    """A failed PR lookup skips every destructive phase for this tick."""
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
    broker.reap_deleted.assert_not_called()


def _script_repo_lookup(root: Path, gh_result):
    calls = []

    def run(argv, **kwargs):
        argv = list(argv)
        calls.append((argv, kwargs))
        if argv == ["git", "-C", str(root), "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(root) + "\n", stderr="")
        if argv == ["git", "-C", str(root), "remote", "get-url", "fork"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="git@github.com:owner/repo.git\n", stderr=""
            )
        if argv and argv[0] == "gh":
            if isinstance(gh_result, BaseException):
                raise gh_result
            return gh_result
        raise AssertionError(f"unexpected command: {argv!r}")

    return run, calls


def test_default_gh_helper_raises_typed_failure_for_missing_gh(tmp_path, monkeypatch):
    """Missing gh is unknown state, never a verified-empty result."""
    from gateway import codex_gc_watcher as mod

    root = (tmp_path / "repo").resolve()
    root.mkdir()
    run, _ = _script_repo_lookup(root, FileNotFoundError("gh not on PATH"))
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(mod.OpenPrLookupError):
        mod._gh_list_open_branches(root)


def test_default_gh_helper_runs_exact_gh_command_from_bound_repo(tmp_path, monkeypatch):
    """gh is repo-qualified and runs from the verified broker root."""
    from gateway import codex_gc_watcher as mod

    root = (tmp_path / "repo").resolve()
    root.mkdir()
    gh_result = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    run, calls = _script_repo_lookup(root, gh_result)
    monkeypatch.setattr(mod.subprocess, "run", run)

    assert mod._gh_list_open_branches(root) == set()
    gh_argv, gh_kwargs = calls[-1]
    assert gh_argv == [
        "gh", "pr", "list", "--repo", "owner/repo", "--state", "open",
        "--json", "headRefName", "--limit", "200",
    ]
    assert gh_kwargs["cwd"] == root


def test_default_gh_helper_raises_typed_failure_for_timeout(tmp_path, monkeypatch):
    from gateway import codex_gc_watcher as mod

    root = (tmp_path / "repo").resolve()
    root.mkdir()
    timeout = mod.subprocess.TimeoutExpired(cmd=["gh"], timeout=30)
    run, _ = _script_repo_lookup(root, timeout)
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(mod.OpenPrLookupError):
        mod._gh_list_open_branches(root)


def test_default_gh_helper_raises_typed_failure_for_nonzero(tmp_path, monkeypatch):
    from gateway import codex_gc_watcher as mod

    root = (tmp_path / "repo").resolve()
    root.mkdir()
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="auth needed")
    run, _ = _script_repo_lookup(root, failed)
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(mod.OpenPrLookupError):
        mod._gh_list_open_branches(root)


def test_default_gh_helper_parses_branch_names(tmp_path, monkeypatch):
    """Happy path: gh returns JSON; we extract headRefName values."""
    from gateway import codex_gc_watcher as mod

    root = (tmp_path / "repo").resolve()
    root.mkdir()
    payload = [
        {"headRefName": "codex/sid-aaa/task"},
        {"headRefName": "codex/sid-bbb/feat"},
    ]
    success = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
    run, _ = _script_repo_lookup(root, success)
    monkeypatch.setattr(mod.subprocess, "run", run)
    result = mod._gh_list_open_branches(root)
    assert result == {"codex/sid-aaa/task", "codex/sid-bbb/feat"}


@pytest.mark.parametrize("stdout", ["{not valid json", "{}", '[{}]'])
def test_default_gh_helper_rejects_malformed_output(tmp_path, monkeypatch, stdout):
    """Malformed JSON, top-level shape, or rows are typed failures."""
    from gateway import codex_gc_watcher as mod

    root = (tmp_path / "repo").resolve()
    root.mkdir()
    malformed = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    run, _ = _script_repo_lookup(root, malformed)
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(mod.OpenPrLookupError):
        mod._gh_list_open_branches(root)


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
