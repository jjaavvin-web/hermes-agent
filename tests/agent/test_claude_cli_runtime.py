"""Unit tests for the claude-cli-subprocess runtime.

These tests are intentionally hermetic: they do not require Claude Code, tmux,
or an OAuth login. Live Max/OAuth smoke coverage is performed separately by
running the real provider in the relay worktree.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_build_claude_command_is_interactive_not_print():
    from agent.claude_cli_runtime import build_claude_command

    cmd = build_claude_command(
        "/usr/bin/claude",
        model="claude-via-cli",
        add_dirs=["/tmp/handoff"],
    )

    assert cmd[0] == "/usr/bin/claude"
    assert "--print" not in cmd
    assert "-p" not in cmd
    assert "--output-format" not in cmd
    assert "--no-session-persistence" not in cmd
    assert "--model" not in cmd  # synthetic selector is not a real Claude model
    assert "--allowed-tools" in cmd
    assert "Read,Write" in cmd
    assert "--add-dir" in cmd


def test_build_claude_command_normalizes_real_anthropic_model():
    from agent.claude_cli_runtime import build_claude_command

    cmd = build_claude_command(
        "/usr/bin/claude",
        model="anthropic/claude-sonnet-4.6",
        add_dirs=[],
    )

    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_build_claude_command_supports_effort_and_permission_mode_without_print():
    from agent.claude_cli_runtime import build_claude_command

    cmd = build_claude_command(
        "/usr/bin/claude",
        model="anthropic/claude-opus-4.8",
        add_dirs=[],
        allowed_tools="Read,Write,Edit",
        effort="max",
        permission_mode="acceptEdits",
    )

    assert "--print" not in cmd
    assert "-p" not in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
    assert cmd[cmd.index("--effort") + 1] == "max"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert cmd[cmd.index("--allowed-tools") + 1] == "Read,Write,Edit"


def test_claude_subprocess_env_strips_paid_api_vars(monkeypatch):
    from agent.claude_cli_runtime import _claude_subprocess_env

    blocked = {
        "ANTHROPIC_API_KEY": "paid-key",
        "ANTHROPIC_TOKEN": "paid-token",
        "ANTHROPIC_AUTH_TOKEN": "paid-auth-token",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "ANTHROPIC_CUSTOM_HEADERS": "x-paid: true",
        "CLAUDE_CODE_OAUTH_TOKEN": "injected-oauth-token",
    }
    for key, value in blocked.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_SAFE_VAR", "kept")

    env = _claude_subprocess_env()

    for key in blocked:
        assert key not in env
    assert env["HERMES_SAFE_VAR"] == "kept"


def test_run_claude_cli_turn_uses_interactive_file_handoff(tmp_path, monkeypatch):
    import agent.claude_cli_runtime as runtime

    handoff_dir = tmp_path / "handoff"
    started = {}
    sent = {}
    killed = []

    monkeypatch.setattr(runtime, "_find_claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(runtime, "_find_tmux_binary", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(runtime.tempfile, "mkdtemp", lambda prefix: str(handoff_dir))
    monkeypatch.setattr(runtime, "_resolve_timeout", lambda: 30)
    monkeypatch.setattr(runtime, "_wait_for_claude_ready", lambda *args, **kwargs: None)

    def fake_start(tmux_bin, session_id, command, *, env, cwd):
        started["tmux_bin"] = tmux_bin
        started["session_id"] = session_id
        started["command"] = command
        started["env"] = env
        started["cwd"] = cwd
        assert "--print" not in command
        handoff_dir.mkdir(parents=True, exist_ok=True)

    def fake_send(tmux_bin, session_id, text):
        sent["text"] = text
        # The invocation should ask interactive Claude to write the result file.
        assert "Write" in text
        assert "result.md" in text
        (handoff_dir / "result.md").write_text("REAL CLAUDE RESULT\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "_start_tmux_session", fake_start)
    monkeypatch.setattr(runtime, "_send_tmux_text", fake_send)
    monkeypatch.setattr(runtime, "_kill_tmux_session", lambda tmux_bin, session_id: killed.append(session_id))

    agent = SimpleNamespace(
        model="claude-via-cli",
        session_cwd=str(tmp_path),
        _cached_system_prompt="SYSTEM RULES",
        _sync_external_memory_for_turn=lambda **kwargs: started.setdefault("memory", kwargs),
        _spawn_background_review=lambda **kwargs: started.setdefault("review", kwargs),
    )
    messages = [{"role": "user", "content": "Return a short answer."}]

    result = runtime.run_claude_cli_turn(
        agent,
        user_message="Return a short answer.",
        original_user_message="Return a short answer.",
        messages=messages,
        effective_task_id="task-1",
        should_review_memory=True,
    )

    assert result["completed"] is True
    assert result["final_response"] == "REAL CLAUDE RESULT"
    assert result["api_calls"] == 1
    assert messages[-1] == {"role": "assistant", "content": "REAL CLAUDE RESULT"}
    assert (handoff_dir / "turn.md").exists()
    payload = (handoff_dir / "turn.md").read_text(encoding="utf-8")
    assert "SYSTEM RULES" in payload
    assert "Return a short answer." in payload
    assert killed == [started["session_id"]]
    assert "memory" in started
    assert "review" in started


def test_run_claude_cli_turn_applies_effort_and_workflow_trigger(tmp_path, monkeypatch):
    import agent.claude_cli_runtime as runtime

    handoff_dir = tmp_path / "workflow-handoff"
    started = {}
    sent = {}

    monkeypatch.setattr(runtime, "_find_claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(runtime, "_find_tmux_binary", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(runtime.tempfile, "mkdtemp", lambda prefix: str(handoff_dir))
    monkeypatch.setattr(runtime, "_resolve_timeout", lambda configured=None: 30)
    monkeypatch.setattr(runtime, "_wait_for_claude_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_load_claude_cli_options",
        lambda: runtime.ClaudeCliOptions(
            effort="max",
            permission_mode="acceptEdits",
            allowed_tools="Read,Write,Edit",
            workflow_mode="on_request",
            timeout_seconds=30,
            ready_timeout_seconds=10,
        ),
    )

    def fake_start(tmux_bin, session_id, command, *, env, cwd):
        started["command"] = command
        assert "--print" not in command
        assert command[command.index("--model") + 1] == "claude-opus-4-8"
        assert command[command.index("--effort") + 1] == "max"
        assert command[command.index("--permission-mode") + 1] == "acceptEdits"
        handoff_dir.mkdir(parents=True, exist_ok=True)

    def fake_send(tmux_bin, session_id, text):
        sent["text"] = text
        assert text.startswith("ultracode:")
        assert "Dynamic Workflows" in text
        (handoff_dir / "result.md").write_text("WORKFLOW RESULT\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "_start_tmux_session", fake_start)
    monkeypatch.setattr(runtime, "_send_tmux_text", fake_send)
    monkeypatch.setattr(runtime, "_kill_tmux_session", lambda *args, **kwargs: None)

    agent = SimpleNamespace(
        model="claude-opus-4.8",
        session_cwd=str(tmp_path),
        _cached_system_prompt="SYSTEM RULES",
        _sync_external_memory_for_turn=lambda **kwargs: None,
        _spawn_background_review=lambda **kwargs: None,
    )
    messages = [{"role": "user", "content": "Use a workflow to design this agent."}]

    result = runtime.run_claude_cli_turn(
        agent,
        user_message="Use a workflow to design this agent.",
        original_user_message="Use a workflow to design this agent.",
        messages=messages,
        effective_task_id="task-workflow",
        should_review_memory=False,
    )

    assert result["completed"] is True
    assert result["final_response"] == "WORKFLOW RESULT"
    assert "ultracode:" in sent["text"]


def test_run_claude_oneshot_uses_interactive_file_handoff(tmp_path, monkeypatch):
    import agent.claude_cli_runtime as runtime

    handoff_dir = tmp_path / "oneshot"
    started = {}
    killed = []

    monkeypatch.setattr(runtime, "_find_claude_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(runtime, "_find_tmux_binary", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(runtime.tempfile, "mkdtemp", lambda prefix: str(handoff_dir))
    monkeypatch.setattr(runtime, "_resolve_timeout", lambda: 30)
    monkeypatch.setattr(runtime, "_wait_for_claude_ready", lambda *args, **kwargs: None)

    def fake_start(tmux_bin, session_id, command, *, env, cwd):
        started["command"] = command
        started["cwd"] = cwd
        # Must stay on the interactive Max path — never headless print mode.
        assert "--print" not in command
        assert "-p" not in command
        handoff_dir.mkdir(parents=True, exist_ok=True)

    def fake_send(tmux_bin, session_id, text):
        assert "Write" in text
        assert "result.md" in text
        (handoff_dir / "result.md").write_text("ONESHOT OUTPUT\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "_start_tmux_session", fake_start)
    monkeypatch.setattr(runtime, "_send_tmux_text", fake_send)
    monkeypatch.setattr(runtime, "_kill_tmux_session", lambda tmux_bin, session_id: killed.append(session_id))

    out = runtime.run_claude_oneshot(
        "Extract X as JSON.", model="claude-via-cli", cwd=str(tmp_path)
    )

    assert out == "ONESHOT OUTPUT"
    # The raw prompt is handed off verbatim (no agent transcript wrapping).
    assert (handoff_dir / "turn.md").read_text(encoding="utf-8") == "Extract X as JSON."
    assert started["cwd"] == str(tmp_path)
    assert "--print" not in started["command"]
    assert len(killed) == 1
