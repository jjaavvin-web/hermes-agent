from __future__ import annotations

import subprocess
from typing import Any

import pytest


def _make_task(kb, *, assignee: str):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def _capture_spawn(monkeypatch, kb):
    captured: dict[str, Any] = {"popen_calls": 0}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["popen_calls"] += 1
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return captured


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's explicit CLI tools."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
    - kanban
    - no_mcp
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    captured = _capture_spawn(monkeypatch, kb)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation", "kanban"):
        assert required in pinned


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - memory\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
    - kanban
    - no_mcp
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved
    assert "memory" not in resolved


@pytest.mark.parametrize(
    ("config_text", "match"),
    [
        ("toolsets:\n  - hermes-cli\n", "platform_toolsets.cli"),
        ("platform_toolsets: []\n", "platform_toolsets"),
        ("platform_toolsets:\n  cli: terminal\n", "must be a non-empty list"),
        ("platform_toolsets:\n  cli: []\n", "must be a non-empty list"),
        ("platform_toolsets:\n  cli:\n    - definitely-not-a-toolset\n", "unknown CLI toolset"),
        ("platform_toolsets:\n  cli:\n    - all\n", "broad CLI toolset"),
        ("platform_toolsets:\n  cli:\n    - '*'\n", "broad CLI toolset"),
        ("platform_toolsets: [broken\n", "malformed config"),
    ],
)
def test_resolve_worker_cli_toolsets_fails_closed_on_missing_malformed_or_unknown(
    monkeypatch, tmp_path, config_text, match
):
    profile = tmp_path / "profile"
    profile.mkdir()
    profile.joinpath("config.yaml").write_text(config_text, encoding="utf-8")

    from hermes_cli import kanban_db as kb

    with pytest.raises(kb.WorkerAuthorityResolutionError, match=match):
        kb._resolve_worker_cli_toolsets(str(profile))


def test_resolve_worker_cli_toolsets_does_not_inject_enabled_mcp(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli: [terminal, kanban]
mcp_servers:
  high_authority:
    enabled: true
    command: python
    args: [server.py]
""".lstrip(),
        encoding="utf-8",
    )

    from hermes_cli import kanban_db as kb

    assert kb._resolve_worker_cli_toolsets(str(profile)) == ["kanban", "terminal"]


def test_resolve_worker_cli_toolsets_does_not_inject_context_engine(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli: [terminal, kanban, no_mcp]
context:
  engine: lcm
""".lstrip(),
        encoding="utf-8",
    )

    from hermes_cli import kanban_db as kb

    assert kb._resolve_worker_cli_toolsets(str(profile)) == ["kanban", "terminal"]


def test_resolve_worker_cli_toolsets_allows_only_explicit_enabled_mcp(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli: [terminal, kanban, bounded_mcp]
mcp_servers:
  bounded_mcp:
    enabled: true
    command: python
    args: [server.py]
  undeclared_mcp:
    enabled: true
    command: python
    args: [other.py]
""".lstrip(),
        encoding="utf-8",
    )

    from hermes_cli import kanban_db as kb

    assert kb._resolve_worker_cli_toolsets(str(profile)) == [
        "bounded_mcp",
        "kanban",
        "terminal",
    ]


def test_resolve_worker_cli_toolsets_rejects_process_global_alias_leak(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli: [terminal, leaked_mcp]\n",
        encoding="utf-8",
    )
    from hermes_cli import kanban_db as kb
    from tools.registry import registry

    monkeypatch.setitem(registry._toolset_aliases, "leaked_mcp", "mcp-leaked")
    with pytest.raises(kb.WorkerAuthorityResolutionError, match="unknown CLI toolset"):
        kb._resolve_worker_cli_toolsets(str(profile))


def test_resolve_worker_cli_toolsets_fails_closed_on_resolver_exception(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - terminal\n    - kanban\n    - no_mcp\n",
        encoding="utf-8",
    )

    from hermes_cli import kanban_db as kb
    import hermes_cli.plugins as plugins

    monkeypatch.setattr(plugins, "discover_plugins", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(kb.WorkerAuthorityResolutionError, match="could not resolve"):
        kb._resolve_worker_cli_toolsets(str(profile))


def test_default_spawn_never_calls_popen_when_profile_home_missing(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    root.joinpath("config.yaml").write_text("toolsets:\n  - hermes-cli\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    captured = _capture_spawn(monkeypatch, kb)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(kb.WorkerAuthorityResolutionError, match="profile home"):
        kb._default_spawn(_make_task(kb, assignee="missing-profile"), str(workspace))
    assert captured["popen_calls"] == 0


def test_default_spawn_never_calls_popen_when_toolsets_unresolved(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - definitely-not-a-toolset\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    captured = _capture_spawn(monkeypatch, kb)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(kb.WorkerAuthorityResolutionError, match="unknown CLI toolset"):
        kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))
    assert captured["popen_calls"] == 0
