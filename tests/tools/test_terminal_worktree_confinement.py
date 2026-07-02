"""Regression tests for terminal tool worktree confinement."""

import json
import time
from types import SimpleNamespace

import pytest

from agent.codex_session_context import (
    require_confinement_without_worktree,
    reset_active_worktree,
    set_active_worktree,
)
import tools.file_tools as file_tools
import tools.terminal_tool as terminal_tool
from tools.environments.local import LocalEnvironment


@pytest.fixture(autouse=True)
def _clean_terminal_state(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_resolve_container_task_id", lambda value: value or "default")
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {"approved": True},
    )


def _bind_worktree(path):
    token = set_active_worktree(str(path))
    return token


def _fake_protected_roots(monkeypatch, protected_root):
    monkeypatch.setattr(
        file_tools,
        "_codex_protected_code_roots",
        lambda wt_real: (str(protected_root),),
    )


def test_bound_worktree_without_workdir_resolves_to_bound_worktree(tmp_path):
    wt = tmp_path / "lane-wt"
    wt.mkdir()
    env = SimpleNamespace(cwd="/legacy/live")
    token = _bind_worktree(wt)
    try:
        assert terminal_tool._resolve_command_cwd(
            workdir=None,
            env=env,
            default_cwd="/legacy/default",
        ) == str(wt.resolve())
    finally:
        reset_active_worktree(token)


def test_bound_worktree_rejects_explicit_protected_workdir(monkeypatch, tmp_path):
    wt = tmp_path / "lane-wt"
    protected = tmp_path / "protected-live-tree"
    wt.mkdir()
    (protected / "subdir").mkdir(parents=True)
    _fake_protected_roots(monkeypatch, protected)

    calls = []

    class FakeEnv:
        env = {}
        cwd = str(wt)

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "SHOULD_NOT_RUN", "returncode": 0}

    token = _bind_worktree(wt)
    try:
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": FakeEnv()})
        result = json.loads(
            terminal_tool.terminal_tool(
                command="echo hi",
                workdir=str(protected / "subdir"),
            )
        )
    finally:
        reset_active_worktree(token)

    assert result["status"] == "blocked"
    assert result["error"].startswith("WORKTREE_CONFINEMENT:")
    assert str((protected / "subdir").resolve()) in result["error"]
    assert str(wt.resolve()) in result["error"]
    assert calls == []


def test_bound_worktree_rejects_absolute_cd_into_protected_root(monkeypatch, tmp_path):
    wt = tmp_path / "lane-wt"
    protected = tmp_path / "protected-live-tree"
    wt.mkdir()
    (protected / "sub").mkdir(parents=True)
    _fake_protected_roots(monkeypatch, protected)

    calls = []

    class FakeEnv:
        env = {}
        cwd = str(wt)

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "SHOULD_NOT_RUN", "returncode": 0}

    token = _bind_worktree(wt)
    try:
        monkeypatch.setattr(terminal_tool, "_active_environments", {"default": FakeEnv()})
        result = json.loads(
            terminal_tool.terminal_tool(
                command=f"cd {protected / 'sub'} && echo hi",
            )
        )
    finally:
        reset_active_worktree(token)

    assert result["status"] == "blocked"
    assert result["error"].startswith("WORKTREE_CONFINEMENT:")
    assert str((protected / "sub").resolve()) in result["error"]
    assert calls == []


def test_no_bound_worktree_preserves_legacy_cwd_resolution():
    env = SimpleNamespace(cwd="/legacy/live")

    assert terminal_tool._resolve_command_cwd(
        workdir="/explicit/workdir",
        env=env,
        default_cwd="/legacy/default",
    ) == "/explicit/workdir"
    assert terminal_tool._resolve_command_cwd(
        workdir=None,
        env=env,
        default_cwd="/legacy/default",
    ) == "/legacy/live"
    assert terminal_tool._resolve_command_cwd(
        workdir=None,
        env=SimpleNamespace(cwd=""),
        default_cwd="/legacy/default",
    ) == "/legacy/default"


def test_confinement_required_no_worktree_blocks_command(tmp_path):
    configured = tmp_path / "configured"
    configured.mkdir()
    env = LocalEnvironment(cwd=str(configured), timeout=10)

    token = require_confinement_without_worktree()
    try:
        with pytest.raises(PermissionError) as excinfo:
            env.execute("echo hi", timeout=5)
    finally:
        reset_active_worktree(token)
        env.cleanup()

    message = str(excinfo.value)
    assert message.startswith("WORKTREE_CONFINEMENT:")
    assert "requires an active worktree but none is bound" in message
    assert "refusing to run against the process cwd" in message


def test_foreground_command_still_blocked_when_confinement_required():
    token = require_confinement_without_worktree()
    try:
        result = json.loads(terminal_tool.terminal_tool(command="echo hi", timeout=5))
    finally:
        reset_active_worktree(token)

    assert result["status"] == "blocked"
    assert result["error"].startswith("WORKTREE_CONFINEMENT:")
    assert "requires an active worktree but none is bound" in result["error"]


def test_background_command_blocked_when_confinement_required_no_worktree(tmp_path):
    live_tree = tmp_path / "live-tree"
    live_tree.mkdir()
    canary = live_tree / "bg-canary.txt"

    token = require_confinement_without_worktree()
    try:
        result = json.loads(
            terminal_tool.terminal_tool(
                command=f"echo pwned > {canary}",
                background=True,
                timeout=10,
            )
        )
        time.sleep(0.1)
    finally:
        reset_active_worktree(token)

    assert result["status"] == "blocked"
    assert result["error"].startswith("WORKTREE_CONFINEMENT:")
    assert "requires an active worktree but none is bound" in result["error"]
    assert not canary.exists()


def test_unbound_no_confinement_still_runs(tmp_path):
    configured = tmp_path / "configured"
    configured.mkdir()
    env = LocalEnvironment(cwd=str(configured), timeout=10)
    try:
        result = env.execute("echo hi", timeout=5)
    finally:
        env.cleanup()

    assert result["returncode"] == 0
    assert result["output"].strip() == "hi"


def test_background_command_runs_normally_when_unbound_no_confinement(monkeypatch, tmp_path):
    import tools.process_registry as process_registry_module

    calls = []

    def fake_spawn_local(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id="bg-test-session", pid=12345)

    monkeypatch.setattr(
        process_registry_module.process_registry,
        "spawn_local",
        fake_spawn_local,
    )

    result = json.loads(
        terminal_tool.terminal_tool(
            command="echo legacy",
            background=True,
            workdir=str(tmp_path),
            timeout=10,
        )
    )

    assert result.get("status") != "blocked"
    assert result["exit_code"] == 0
    assert result["session_id"] == "bg-test-session"
    assert calls and calls[0]["cwd"] == str(tmp_path)


def test_bound_worktree_relative_workdir_resolves_under_bound_worktree(tmp_path):
    wt = tmp_path / "lane-wt"
    wt.mkdir()
    token = _bind_worktree(wt)
    try:
        resolved = terminal_tool._resolve_command_cwd(
            workdir="sub/dir",
            env=SimpleNamespace(cwd="/legacy/live"),
            default_cwd="/legacy/default",
        )
    finally:
        reset_active_worktree(token)

    assert resolved == str((wt / "sub" / "dir").resolve())


def test_env_config_seeds_cwd_from_bound_worktree(monkeypatch, tmp_path):
    wt = tmp_path / "lane-wt"
    terminal_cwd = tmp_path / "terminal-cwd"
    wt.mkdir()
    terminal_cwd.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(terminal_cwd))

    token = _bind_worktree(wt)
    try:
        config = terminal_tool._get_env_config()
    finally:
        reset_active_worktree(token)

    assert config["cwd"] == str(wt.resolve())
