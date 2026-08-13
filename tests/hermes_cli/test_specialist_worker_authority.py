from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _guard(profile: str, workspace: Path, tool: str, args: dict):
    from hermes_cli.worker_authority import authorize_worker_tool

    return authorize_worker_tool(
        tool,
        args,
        authority=profile,
        workspace=str(workspace),
    )


def test_verifier_can_read_and_search_assigned_workspace(tmp_path):
    assert _guard("sol-verifier", tmp_path, "read_file", {"path": "fixture.txt"}).allowed
    assert _guard("sol-verifier", tmp_path, "search_files", {"path": ".", "pattern": "x"}).allowed


def test_verifier_can_run_only_specialist_test(tmp_path):
    assert _guard("sol-verifier", tmp_path, "specialist_test", {"targets": ["tests/test_x.py"]}).allowed
    denied = _guard("sol-verifier", tmp_path, "terminal", {"command": "pytest -q"})
    assert not denied.allowed


@pytest.mark.parametrize("tool", ["write_file", "patch", "execute_code", "process", "delegate_task", "memory", "cronjob", "send_message"])
def test_verifier_denies_mutation_execution_and_side_effect_tools(tmp_path, tool):
    result = _guard("sol-verifier", tmp_path, tool, {"path": "x"})
    assert not result.allowed


@pytest.mark.parametrize("tool", ["kanban_create", "kanban_link", "kanban_unblock"])
def test_specialists_deny_non_lifecycle_kanban_mutation(tmp_path, tool):
    assert not _guard("sol-verifier", tmp_path, tool, {}).allowed
    assert not _guard("sol-builder", tmp_path, tool, {}).allowed


def test_builder_file_mutation_is_confined_to_workspace(tmp_path):
    assert _guard("sol-builder", tmp_path, "write_file", {"path": "src/x.py"}).allowed
    # Patch remains denied for specialists because its multi-step read/replace
    # implementation cannot provide a descriptor-stable no-symlink boundary.
    assert not _guard("sol-builder", tmp_path, "patch", {
        "mode": "replace", "path": "src/x.py", "old_string": "a", "new_string": "b",
    }).allowed
    assert not _guard("sol-builder", tmp_path, "write_file", {"path": "../escape.py"}).allowed
    assert not _guard("sol-builder", tmp_path, "read_file", {"path": "/etc/passwd"}).allowed


def test_builder_denies_v4a_bulk_patch_embedded_escape(tmp_path):
    result = _guard("sol-builder", tmp_path, "patch", {
        "mode": "patch",
        "patch": "*** Begin Patch\n*** Add File: /tmp/escape\n+x\n*** End Patch",
    })
    assert not result.allowed


def test_builder_symlink_escape_is_denied(tmp_path):
    outside = tmp_path.parent / "outside-authority"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    result = _guard("sol-builder", tmp_path, "write_file", {"path": "link/escape.py"})
    assert not result.allowed


def test_builder_denies_arbitrary_terminal_and_execute_code(tmp_path):
    assert not _guard("sol-builder", tmp_path, "terminal", {"command": "curl https://example.com"}).allowed
    assert not _guard("sol-builder", tmp_path, "execute_code", {"code": "import os"}).allowed
    assert _guard("sol-builder", tmp_path, "specialist_test", {"targets": ["tests"]}).allowed


def test_missing_or_relative_workspace_fails_closed(tmp_path):
    from hermes_cli.worker_authority import authorize_worker_tool

    assert not authorize_worker_tool("read_file", {"path": "x"}, authority="sol-builder", workspace="").allowed
    assert not authorize_worker_tool("read_file", {"path": "x"}, authority="sol-builder", workspace="relative").allowed


def test_spawn_sets_authority_and_strips_provider_credentials(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "sol-builder"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - file\n    - kanban\n    - specialist_test\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "must-not-leak")
    monkeypatch.setenv("UNLISTED_PRIVATE_TOKEN", "must-not-leak")
    credentialed_http_proxy = "http://" + "synthetic-user" + ":" + "synthetic-password" + "@" + "proxy.invalid:8080"
    credentialed_https_proxy = "https://" + "synthetic-user" + ":" + "synthetic-password" + "@" + "proxy.invalid:8443"
    monkeypatch.setenv("HTTP_PROXY", credentialed_http_proxy)
    monkeypatch.setenv("HTTPS_PROXY", credentialed_https_proxy)

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setattr(kb, "workspaces_root", lambda board=None: tmp_path / "workspaces")
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")
    captured = {}

    class FakeProc:
        pid = 42

    def fake_popen(cmd, *args, **kwargs):
        captured.update(cmd=list(cmd), env=dict(kwargs["env"]), cwd=kwargs["cwd"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = kb.Task(
        id="t_auth", title="x", body=None, assignee="sol-builder", status="running",
        priority=0, created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=str(workspace), claim_lock="lock",
        claim_expires=None, tenant=None, current_run_id=1,
    )
    assert kb._default_spawn(task, str(workspace)) == 42
    assert captured["env"]["HERMES_WORKER_AUTHORITY"] == "sol-builder"
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(workspace.resolve())
    assert "--accept-hooks" not in captured["cmd"]
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "AWS_SESSION_TOKEN" not in captured["env"]
    assert "UNLISTED_PRIVATE_TOKEN" not in captured["env"]
    assert "HTTP_PROXY" not in captured["env"]
    assert "HTTPS_PROXY" not in captured["env"]


def test_specialist_test_runner_executes_only_workspace_pytest(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    test_file = tmp_path / "test_canary.py"
    test_file.write_text("def test_canary():\n    assert 2 + 2 == 4\n", encoding="utf-8")

    from tools.specialist_test_tool import _handle_specialist_test

    result = _handle_specialist_test({"targets": ["test_canary.py"], "timeout": 30})
    assert result["exit_code"] == 0
    assert "1 passed" in result["output"]
    assert _handle_specialist_test({"targets": ["../escape.py"]})["exit_code"] is None


def test_specialist_test_denies_workspace_symlinks(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    outside = tmp_path.parent / "outside-test-content"
    outside.mkdir(exist_ok=True)
    outside.joinpath("secret.txt").write_text("host-content", encoding="utf-8")
    tmp_path.joinpath("escape").symlink_to(outside, target_is_directory=True)
    test_file = tmp_path / "test_symlink.py"
    test_file.write_text("def test_x(): assert True\n", encoding="utf-8")
    from tools.specialist_test_tool import _handle_specialist_test
    result = _handle_specialist_test({"targets": ["test_symlink.py"], "timeout": 30})
    assert result["exit_code"] is None
    assert "symlink" in result["error"]


def test_specialist_test_sandbox_blocks_host_reads_network_writes_and_pytest_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    canary = tmp_path / "test_malicious.py"
    canary.write_text(
        """
import pathlib, socket, subprocess

def test_host_read_denied():
    try:
        pathlib.Path('/etc/passwd').read_text()
    except PermissionError:
        return
    raise AssertionError('host read escaped sandbox')

def test_outside_write_denied():
    try:
        pathlib.Path('/tmp/specialist-escape').write_text('x')
    except PermissionError:
        return
    raise AssertionError('outside write escaped sandbox')

def test_network_denied():
    s = None
    try:
        s = socket.socket()
        s.connect(('1.1.1.1', 80))
    except OSError:
        return
    finally:
        if s is not None:
            s.close()
    raise AssertionError('network escaped sandbox')

def test_make_dir_outside_denied():
    try:
        pathlib.Path('/tmp/specialist-escape-dir').mkdir()
    except PermissionError:
        return
    raise AssertionError('mkdir escaped sandbox')

def test_chmod_outside_denied():
    try:
        pathlib.Path('/tmp').chmod(0o777)
    except PermissionError:
        return
    raise AssertionError('chmod escaped sandbox')

def test_fchmodat2_outside_denied():
    import ctypes, os
    marker = b'/tmp'
    mode = os.stat('/tmp').st_mode & 0o7777
    result = ctypes.CDLL(None, use_errno=True).syscall(452, -100, marker, mode, 0)
    err = ctypes.get_errno()
    if result == -1 and err in (1, 13, 38):
        return
    raise AssertionError('fchmodat2 escaped sandbox')

def test_subprocess_denied():
    try:
        subprocess.run(['/bin/true'], check=True)
    except (PermissionError, OSError):
        return
    raise AssertionError('subprocess escaped sandbox')

def test_host_proc_read_denied():
    try:
        pathlib.Path('/proc/1/stat').read_text()
    except (PermissionError, FileNotFoundError):
        return
    raise AssertionError('host proc read escaped sandbox')
""".lstrip(), encoding="utf-8")
    from tools.specialist_test_tool import _handle_specialist_test
    result = _handle_specialist_test({"targets": ["test_malicious.py"], "timeout": 30})
    assert result["exit_code"] == 0
    assert "8 passed" in result["output"]


def test_specialist_test_sandbox_denies_host_unix_socket(monkeypatch, tmp_path):
    import socket
    import threading

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    socket_path = tmp_path.parent / "specialist-host.sock"
    socket_path.unlink(missing_ok=True)
    accepted: list[bool] = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    listener.settimeout(2)

    def accept_once():
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        accepted.append(True)
        connection.close()

    thread = threading.Thread(target=accept_once)
    thread.start()
    try:
        canary = tmp_path / "test_unix_socket.py"
        canary.write_text(
            "import socket, pytest\n"
            f"def test_unix_denied():\n    with pytest.raises((PermissionError, OSError)):\n        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n        try:\n            s.connect({str(socket_path)!r})\n        finally:\n            s.close()\n",
            encoding="utf-8",
        )
        from tools.specialist_test_tool import _handle_specialist_test
        result = _handle_specialist_test({"targets": ["test_unix_socket.py"], "timeout": 30})
        assert result["exit_code"] == 0
        assert "1 passed" in result["output"]
    finally:
        listener.close()
        thread.join(timeout=3)
        socket_path.unlink(missing_ok=True)
    assert accepted == []


def test_specialist_test_pid_namespace_blocks_host_signals(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    victim = subprocess.Popen(["sleep", "30"])
    try:
        canary = tmp_path / "test_signal.py"
        canary.write_text(
            "import os, signal, pytest\n"
            f"def test_signal_denied():\n    with pytest.raises((ProcessLookupError, PermissionError)):\n        os.kill({victim.pid}, signal.SIGTERM)\n",
            encoding="utf-8",
        )
        from tools.specialist_test_tool import _handle_specialist_test
        result = _handle_specialist_test({"targets": ["test_signal.py"], "timeout": 30})
        assert result["exit_code"] == 0
        assert "1 passed" in result["output"]
        assert victim.poll() is None
    finally:
        if victim.poll() is None:
            victim.terminate()
        victim.wait(timeout=5)


def test_specialist_startup_skips_shell_hook_registration(monkeypatch):
    import hermes_cli.main as main
    import hermes_cli.plugins as plugins
    import hermes_cli.mcp_startup as mcp_startup
    import agent.shell_hooks as shell_hooks

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    called = {"hooks": False, "plugins": False, "mcp": False}

    def forbidden_registration(*args, **kwargs):
        called["hooks"] = True
        raise AssertionError("specialist attempted shell hook registration")

    monkeypatch.setattr(shell_hooks, "register_from_config", forbidden_registration)
    monkeypatch.setattr(plugins, "discover_plugins", lambda: called.__setitem__("plugins", True))
    monkeypatch.setattr(mcp_startup, "start_background_mcp_discovery", lambda **kwargs: called.__setitem__("mcp", True))

    class Args:
        command = "chat"
        accept_hooks = True

    main._prepare_agent_startup(Args())
    assert called == {"hooks": False, "plugins": False, "mcp": False}


def test_specialist_main_guards_startup_side_effects():
    import hermes_cli.main as main

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert 'if not specialist_authority and _termux_should_prefetch_update_check()' in source
    # v0.20.1 moved the skills sync into a background thread; pin the GUARD
    # relationship (sync reachable only under `if not specialist_authority:`)
    # rather than the exact statement layout.
    import re as _re
    assert _re.search(
        r"if not specialist_authority:\n(?:[^\n]*\n){0,14}?[^\n]*_sync_bundled_skills_for_startup\(\)",
        source,
    ), "skills-sync no longer guarded by specialist_authority"
    assert 'if not specialist_authority:\n        _pin_kanban_board_env()' in source


def test_specialist_tui_import_guards_update_prefetch():
    source = (Path(__file__).resolve().parents[2] / "tui_gateway" / "server.py").read_text(encoding="utf-8")
    marker = 'if not os.environ.get("HERMES_WORKER_AUTHORITY", "").strip():\n    try:\n        from hermes_cli.banner import prefetch_update_check'
    assert marker in source


def test_specialist_parser_skips_plugin_cli_discovery(monkeypatch):
    import hermes_cli.main as main

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-builder")
    monkeypatch.setattr(main.sys, "argv", ["hermes", "unexpected-prompt-token"])
    assert main._plugin_cli_discovery_needed() is False


def test_cli_module_skips_dotenv_for_specialist(monkeypatch, tmp_path):
    import subprocess

    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("SPECIALIST_DOTENV_CANARY=loaded\n", encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "HERMES_HOME": str(home),
        "HERMES_WORKER_AUTHORITY": "sol-verifier",
    }
    probe = subprocess.run(
        [sys.executable, "-c", "import os,cli; print(os.getenv('SPECIALIST_DOTENV_CANARY','absent'))"],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert probe.stdout.strip() == "absent"


def test_run_agent_import_skips_dotenv_for_specialist(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("SPECIALIST_RUN_AGENT_CANARY=loaded\n", encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "HERMES_HOME": str(home),
        "HERMES_WORKER_AUTHORITY": "sol-verifier",
    }
    probe = subprocess.run(
        [sys.executable, "-c", "import os,run_agent; print(os.getenv('SPECIALIST_RUN_AGENT_CANARY','absent'))"],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert probe.stdout.strip() == "absent"


def test_specialist_lazy_plugin_commands_do_not_discover(monkeypatch):
    import cli
    import hermes_cli.plugins as plugins

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    called = False

    def forbidden_discovery(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("specialist attempted late plugin discovery")

    monkeypatch.setattr(plugins, "get_plugin_commands", forbidden_discovery)
    assert cli._get_plugin_cmd_handler_names() == set()
    assert called is False


def test_plugin_manager_discovery_fails_closed_for_specialist(monkeypatch):
    import hermes_cli.plugins as plugins

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-builder")
    manager = plugins.get_plugin_manager()
    called = False

    def forbidden_discovery(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("specialist attempted plugin manager discovery")

    monkeypatch.setattr(manager, "discover_and_load", forbidden_discovery)
    assert plugins._ensure_plugins_discovered() is manager
    assert called is False


def test_concurrent_specialist_denial_precedes_checkpoint_and_callbacks(monkeypatch, tmp_path):
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from agent.tool_executor import execute_tool_calls_concurrent

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    checkpoint = MagicMock()
    checkpoint.enabled = True
    checkpoint.get_working_dir_for_path.return_value = str(tmp_path)
    activity_calls = []
    agent = SimpleNamespace(
        _interrupt_requested=False,
        _tool_worker_threads_lock=threading.Lock(),
        _tool_worker_threads=set(),
        _checkpoint_mgr=checkpoint,
        _tool_guardrails=SimpleNamespace(before_call=lambda *a, **k: SimpleNamespace(allows_execution=True)),
        quiet_mode=True,
        tool_progress_mode="off",
        verbose_logging=False,
        log_prefix_chars=100,
        tool_progress_callback=MagicMock(),
        tool_start_callback=MagicMock(),
        tool_complete_callback=None,
        _current_tool="",
        _touch_activity=lambda *a, **k: activity_calls.append((a, k)),
        _budget_for_tool_outputs=None,
        _should_emit_quiet_tool_messages=lambda: False,
        _should_start_quiet_spinner=lambda: False,
        _subdirectory_hints=SimpleNamespace(check_tool_call=lambda *a, **k: ""),
        _tool_result_content_for_active_model=lambda name, result: result,
        _apply_pending_steer_to_tool_results=lambda *a, **k: None,
        session_id="test",
        _current_turn_id="",
        _current_api_request_id="",
    )
    call = SimpleNamespace(
        id="denied",
        function=SimpleNamespace(name="write_file", arguments=json.dumps({"path": "x", "content": "x"})),
    )
    assistant = SimpleNamespace(tool_calls=[call])
    messages = []
    execute_tool_calls_concurrent(agent, assistant, messages, "")
    checkpoint.ensure_checkpoint.assert_not_called()
    agent.tool_start_callback.assert_not_called()
    agent.tool_progress_callback.assert_not_called()
    assert activity_calls == []
    assert "authority" in messages[0]["content"].lower()


def test_builder_atomic_write_rejects_symlink_component(monkeypatch, tmp_path):
    from tools.file_tools import write_file_tool

    outside = tmp_path.parent / "outside-atomic"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-builder")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    result = json.loads(write_file_tool("link/pwn.txt", "blocked"))
    assert "error" in result
    assert not (outside / "pwn.txt").exists()


def test_builder_atomic_write_succeeds_in_workspace(monkeypatch, tmp_path):
    from tools.file_tools import write_file_tool

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-builder")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    result = json.loads(write_file_tool("src/ok.txt", "ok"))
    assert result["success"] is True
    assert (tmp_path / "src" / "ok.txt").read_text(encoding="utf-8") == "ok"


def test_builder_atomic_write_replaces_hardlink_without_mutating_outside(monkeypatch, tmp_path):
    from tools.file_tools import write_file_tool

    outside = tmp_path.parent / "outside-hardlink.txt"
    outside.write_text("outside", encoding="utf-8")
    inside = tmp_path / "linked.txt"
    os.link(outside, inside)
    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-builder")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    result = json.loads(write_file_tool("linked.txt", "inside"))
    assert result["success"] is True
    assert outside.read_text(encoding="utf-8") == "outside"
    assert inside.read_text(encoding="utf-8") == "inside"
    assert outside.stat().st_ino != inside.stat().st_ino


def test_direct_mcp_discovery_fails_closed_for_specialist(monkeypatch):
    from tools import mcp_tool

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("specialist attempted MCP config load")

    monkeypatch.setattr(mcp_tool, "_load_mcp_config", forbidden)
    assert mcp_tool.discover_mcp_tools() == []
    assert called is False


def test_direct_mcp_registration_fails_closed_for_specialist(monkeypatch):
    from tools import mcp_tool

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-builder")
    called = []
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_loop", lambda: called.append("loop"))
    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", lambda *args, **kwargs: called.append("run"))
    assert mcp_tool.register_mcp_servers({"synthetic": {"command": "never"}}) == []
    assert called == []


def test_direct_plugin_discovery_fails_closed_for_specialist(monkeypatch):
    import hermes_cli.plugins as plugins

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    manager = plugins.get_plugin_manager()
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("specialist attempted direct plugin discovery")

    monkeypatch.setattr(manager, "discover_and_load", forbidden)
    plugins.discover_plugins(force=True)
    assert called is False


def test_direct_plugin_manager_discovery_fails_closed_for_specialist(monkeypatch):
    import hermes_cli.plugins as plugins

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    manager = plugins.PluginManager()
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("specialist reached plugin discovery implementation")

    monkeypatch.setattr(manager, "_discover_and_load_inner", forbidden)
    manager.discover_and_load(force=True)
    assert called is False
    assert manager._discovered is True


def test_specialist_test_uses_isolated_python_bootstrap():
    from tools import specialist_test_tool

    source = Path(specialist_test_tool.__file__).read_text(encoding="utf-8")
    assert 'sys.executable, "-I", "-c"' in source
    assert 'sys.executable, "-m", "tools.specialist_test_sandbox"' not in source


def test_direct_agent_loop_todo_is_denied(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent.agent_runtime_helpers import invoke_tool

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    agent = SimpleNamespace(session_id="test", _current_turn_id="", _current_api_request_id="")
    result = json.loads(invoke_tool(agent, "todo", {}, ""))
    assert "authority" in result["error"].lower()


def test_model_tools_enforces_worker_authority_before_registry_dispatch(monkeypatch, tmp_path):
    import model_tools

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    called = False

    def forbidden_dispatch(*args, **kwargs):
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr(model_tools.registry, "dispatch", forbidden_dispatch)
    result = json.loads(model_tools.handle_function_call("write_file", {"path": "x", "content": "x"}))
    assert "authority" in result["error"].lower()
    assert called is False


def test_direct_registry_dispatch_is_also_denied(monkeypatch, tmp_path):
    from tools.registry import registry

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-verifier")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    result = json.loads(registry.dispatch("write_file", {"path": "x", "content": "x"}))
    assert "authority" in result["error"].lower()
