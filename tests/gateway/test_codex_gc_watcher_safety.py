"""Fail-closed registry and open-PR contracts for CodexGcWatcher."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway import codex_gc_watcher as watcher_mod
from gateway import codex_session_reaper as reaper_mod


class _Dispatcher:
    def __init__(
        self,
        hermes_home: Path,
        *,
        payload: object | None = None,
        raw: str | None = None,
        absent: bool = False,
        recovery_payload: object | None = None,
    ) -> None:
        self.hermes_home = hermes_home
        self._sessions_path = hermes_home / "codex_sessions.json"
        hermes_home.mkdir(parents=True, exist_ok=True)
        if not absent:
            if raw is not None:
                self._sessions_path.write_text(raw, encoding="utf-8")
            else:
                self._sessions_path.write_text(json.dumps(payload), encoding="utf-8")
        self._recovery_payload = recovery_payload

    def _load_state(self) -> dict:
        if self._recovery_payload is not None:
            return self._recovery_payload  # type: ignore[return-value]
        return json.loads(self._sessions_path.read_text(encoding="utf-8"))


def _valid_state(*sids: str) -> dict:
    return {
        "version": 1,
        "sessions": {
            f"thread-{index}": {
                "session_id": sid,
                "state": "EXECUTING",
                "thread_id": f"thread-{index}",
            }
            for index, sid in enumerate(sids)
        },
    }


def _broker(tmp_path: Path) -> MagicMock:
    broker = MagicMock()
    broker.repo_root = tmp_path / "repo"
    broker.repo_root.mkdir(exist_ok=True)
    broker.hermes_home = tmp_path / "broker-home"
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    return broker


def _install_reaper_probe(monkeypatch) -> MagicMock:
    reaper_class = MagicMock()
    reaper_class.return_value.reap.return_value = []
    monkeypatch.setattr(reaper_mod, "CodexSessionReaper", reaper_class)
    return reaper_class


def _assert_no_destructive_phase(broker: MagicMock, reaper_class: MagicMock) -> None:
    broker.gc.assert_not_called()
    broker.reap_deleted.assert_not_called()
    reaper_class.assert_not_called()


@pytest.mark.asyncio
async def test_checked_registry_corrupt_json_skips_all_destructive_phases(
    tmp_path, monkeypatch
):
    dispatcher = _Dispatcher(tmp_path / "home", raw="{not-json")
    broker = _broker(tmp_path)
    reaper_class = _install_reaper_probe(monkeypatch)
    watcher = watcher_mod.CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=lambda: set(),
    )

    await watcher._tick()

    _assert_no_destructive_phase(broker, reaper_class)


@pytest.mark.asyncio
async def test_checked_registry_absent_skips_all_destructive_phases(tmp_path, monkeypatch):
    recovery = _valid_state()
    dispatcher = _Dispatcher(
        tmp_path / "home",
        absent=True,
        recovery_payload=recovery,
    )
    broker = _broker(tmp_path)
    reaper_class = _install_reaper_probe(monkeypatch)
    watcher = watcher_mod.CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=lambda: set(),
    )

    await watcher._tick()

    _assert_no_destructive_phase(broker, reaper_class)


@pytest.mark.asyncio
async def test_checked_registry_io_failure_skips_all_destructive_phases(
    tmp_path, monkeypatch
):
    payload = _valid_state("sid-live")
    dispatcher = _Dispatcher(tmp_path / "home", payload=payload)
    broker = _broker(tmp_path)
    reaper_class = _install_reaper_probe(monkeypatch)

    def fail_locked_read(path: Path):
        raise OSError("simulated shared-lock read failure")

    monkeypatch.setattr(watcher_mod, "load_locked_json", fail_locked_read, raising=False)
    watcher = watcher_mod.CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=lambda: set(),
    )

    await watcher._tick()

    _assert_no_destructive_phase(broker, reaper_class)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"version": 2, "sessions": {}},
        {"version": 1, "sessions": []},
        {"version": 1, "sessions": {"thread": "not-a-row"}},
        {"version": 1, "sessions": {"thread": {"state": "EXECUTING"}}},
        {"version": 1, "sessions": {"thread": {"session_id": ""}}},
        {"sessions": {}},
    ],
    ids=[
        "malformed-top",
        "unsupported-version",
        "malformed-sessions",
        "non-object-row",
        "missing-session-id",
        "empty-session-id",
        "missing-version",
    ],
)
@pytest.mark.asyncio
async def test_checked_registry_malformed_state_skips_all_destructive_phases(
    tmp_path, monkeypatch, payload
):
    dispatcher = _Dispatcher(tmp_path / "home", payload=payload)
    broker = _broker(tmp_path)
    reaper_class = _install_reaper_probe(monkeypatch)
    watcher = watcher_mod.CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=lambda: set(),
    )

    await watcher._tick()

    _assert_no_destructive_phase(broker, reaper_class)


def _scripted_run(
    root: Path,
    *,
    remote_url: str = "git@github.com:owner/repo.git",
    gh_stdout: str = "[]",
    scenario: str = "success",
):
    calls: list[tuple[list[str], dict]] = []
    other_root = root.parent / "different-repo"
    other_root.mkdir(exist_ok=True)

    def run(argv, **kwargs):
        argv = list(argv)
        calls.append((argv, dict(kwargs)))
        if argv == ["git", "-C", str(root), "rev-parse", "--show-toplevel"]:
            if scenario == "root-timeout":
                raise subprocess.TimeoutExpired(argv, timeout=30)
            if scenario == "root-nonzero":
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="not a repo")
            stdout = str(other_root if scenario == "root-mismatch" else root) + "\n"
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
        if argv == ["git", "-C", str(root), "remote", "get-url", "fork"]:
            if scenario == "remote-nonzero":
                return subprocess.CompletedProcess(argv, 2, stdout="", stderr="missing")
            url = "https://example.com/owner/repo.git" if scenario == "remote-unsupported" else remote_url
            return subprocess.CompletedProcess(argv, 0, stdout=url + "\n", stderr="")
        if argv and argv[0] == "gh":
            if scenario == "gh-missing":
                raise FileNotFoundError("gh absent")
            if scenario == "gh-timeout":
                raise subprocess.TimeoutExpired(argv, timeout=30)
            if scenario == "gh-nonzero":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="auth failure")
            return subprocess.CompletedProcess(argv, 0, stdout=gh_stdout, stderr="")
        raise AssertionError(f"unexpected subprocess argv: {argv!r}")

    return run, calls


@pytest.mark.parametrize(
    "remote_url",
    [
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "https://github.com/owner/repo.git",
    ],
    ids=["scp-ssh", "url-ssh", "https"],
)
def test_pr_lookup_accepts_github_fork_urls_and_uses_exact_argv(
    tmp_path, monkeypatch, remote_url
):
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    run, calls = _scripted_run(root, remote_url=remote_url)
    monkeypatch.setattr(watcher_mod.subprocess, "run", run)

    assert watcher_mod._gh_list_open_branches(root) == set()

    assert [argv for argv, _ in calls] == [
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        ["git", "-C", str(root), "remote", "get-url", "fork"],
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "open",
            "--json",
            "headRefName",
            "--limit",
            "200",
        ],
    ]
    assert calls[-1][1]["cwd"] == root


def test_pr_lookup_parses_only_well_formed_rows(tmp_path, monkeypatch):
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    payload = json.dumps(
        [
            {"headRefName": "codex/sid-one/task"},
            {"headRefName": "codex/sid-two/task"},
        ]
    )
    run, _ = _scripted_run(root, gh_stdout=payload)
    monkeypatch.setattr(watcher_mod.subprocess, "run", run)

    assert watcher_mod._gh_list_open_branches(root) == {
        "codex/sid-one/task",
        "codex/sid-two/task",
    }


@pytest.mark.parametrize(
    ("scenario", "gh_stdout", "create_root"),
    [
        ("root-missing", "[]", False),
        ("root-nonzero", "[]", True),
        ("root-mismatch", "[]", True),
        ("root-timeout", "[]", True),
        ("remote-nonzero", "[]", True),
        ("remote-unsupported", "[]", True),
        ("gh-missing", "[]", True),
        ("gh-timeout", "[]", True),
        ("gh-nonzero", "[]", True),
        ("success", "{bad-json", True),
        ("success", "{}", True),
        ("success", '["not-a-row"]', True),
        ("success", '[{}]', True),
        ("success", '[{"headRefName": ""}]', True),
    ],
    ids=[
        "missing-root",
        "root-nonzero",
        "root-mismatch",
        "root-timeout",
        "remote-nonzero",
        "unsupported-remote",
        "gh-missing",
        "gh-timeout",
        "gh-nonzero",
        "malformed-json",
        "malformed-top",
        "row-not-object",
        "row-missing-ref",
        "row-empty-ref",
    ],
)
def test_pr_lookup_failures_are_typed(
    tmp_path, monkeypatch, scenario, gh_stdout, create_root
):
    root = (tmp_path / "repo").resolve()
    if create_root:
        root.mkdir()
    run, _ = _scripted_run(root, gh_stdout=gh_stdout, scenario=scenario)
    monkeypatch.setattr(watcher_mod.subprocess, "run", run)

    error: BaseException | None = None
    try:
        watcher_mod._gh_list_open_branches(root)
    except BaseException as exc:
        error = exc

    assert type(error).__name__ == "OpenPrLookupError"
    assert isinstance(getattr(error, "fingerprint", None), str)
    assert error.fingerprint


@pytest.mark.asyncio
async def test_watcher_default_pr_lookup_is_bound_to_broker_repo_root(
    tmp_path, monkeypatch
):
    dispatcher = _Dispatcher(tmp_path / "home", payload=_valid_state("sid-live"))
    broker = _broker(tmp_path)
    helper = MagicMock(return_value=set())
    monkeypatch.setattr(watcher_mod, "_gh_list_open_branches", helper)
    _install_reaper_probe(monkeypatch)
    watcher = watcher_mod.CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
    )

    await watcher._tick()

    helper.assert_called_once_with(broker.repo_root)


@pytest.mark.asyncio
async def test_pr_lookup_failure_skips_gc_reap_and_session_reaper(
    tmp_path, monkeypatch
):
    dispatcher = _Dispatcher(tmp_path / "home", payload=_valid_state("sid-live"))
    broker = _broker(tmp_path)
    reaper_class = _install_reaper_probe(monkeypatch)

    def fail_lookup():
        raise TimeoutError("offline test lookup")

    watcher = watcher_mod.CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=fail_lookup,
    )

    await watcher._tick()

    _assert_no_destructive_phase(broker, reaper_class)


@pytest.mark.asyncio
async def test_pr_lookup_warning_dedupe_changed_failure_and_recovery(
    tmp_path, monkeypatch, caplog
):
    dispatcher = _Dispatcher(tmp_path / "home", payload=_valid_state("sid-live"))
    broker = _broker(tmp_path)
    reaper_class = _install_reaper_probe(monkeypatch)
    outcomes = iter(
        [
            TimeoutError("first"),
            TimeoutError("first"),
            OSError("changed class"),
            {"codex/sid-open/task"},
        ]
    )

    def lookup():
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    watcher = watcher_mod.CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=lookup,
    )
    caplog.set_level(logging.DEBUG, logger=watcher_mod.__name__)

    for _ in range(4):
        await watcher._tick()

    pr_records = [
        record
        for record in caplog.records
        if record.name == watcher_mod.__name__ and "open-PR lookup" in record.message
    ]
    assert [record.levelno for record in pr_records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.WARNING,
        logging.INFO,
    ]
    assert "recovered" in pr_records[-1].message
    assert broker.gc.call_count == 1
    assert broker.reap_deleted.call_count == 1
    assert reaper_class.call_count == 1


@pytest.mark.asyncio
async def test_gc_and_reaper_share_one_verified_snapshot_and_tmp_ledger(
    tmp_path, monkeypatch
):
    dispatcher = _Dispatcher(tmp_path / "home", payload=_valid_state("sid-live"))
    broker = _broker(tmp_path)
    calls = 0
    branches = {"codex/sid-open/task"}
    captured: dict[str, object] = {}

    def lookup():
        nonlocal calls
        calls += 1
        return set(branches)

    def probe_reap(self, *, reap_idle_days=10, dry_run=True):
        captured["ledger"] = self._ledger_path
        captured["branches"] = self._gh_open_branches_fn()
        return []

    monkeypatch.setattr(reaper_mod.CodexSessionReaper, "reap", probe_reap)
    watcher = watcher_mod.CodexGcWatcher(
        dispatcher=dispatcher,
        worktree_broker=broker,
        gh_list_open_branches=lookup,
    )

    await watcher._tick()

    assert calls == 1
    assert broker.gc.call_args.kwargs["live_branches"] == branches
    assert captured["branches"] == branches
    expected_ledger = tmp_path / "home" / "state" / "codex-reaper" / "reap-ledger.jsonl"
    assert captured["ledger"] == expected_ledger
    assert not expected_ledger.is_relative_to(Path.home() / ".hermes")
