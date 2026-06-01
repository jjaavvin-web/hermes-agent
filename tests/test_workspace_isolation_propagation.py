from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return path


def test_board_repo_root_anchors_scratch_workspaces_and_cleanup_allows_them(fresh_home, tmp_path):
    repo = _git_repo(tmp_path / "project")
    kb.write_board_metadata("project", repo_root=str(repo), base_branch="fork/project-main")
    conn = kb.connect(board="project")
    try:
        task_id = kb.create_task(conn, title="scratch", assignee="default", board="project")
        task = kb.get_task(conn, task_id)
        assert task is not None

        workspace = kb.resolve_workspace(task, board="project")
        kb.set_workspace_path(conn, task_id, workspace)

        assert workspace == repo / ".hermes" / "kanban" / "workspaces" / task_id
        assert workspace.is_dir()
        assert kb._is_managed_scratch_path(workspace) is True

        kb.complete_task(conn, task_id, result="done")
        assert not workspace.exists()
    finally:
        conn.close()


def test_legacy_board_without_repo_root_keeps_existing_workspace_root(fresh_home):
    conn = kb.connect(board="legacy")
    try:
        task_id = kb.create_task(conn, title="legacy scratch", assignee="default", board="legacy")
        task = kb.get_task(conn, task_id)
        assert task is not None

        workspace = kb.resolve_workspace(task, board="legacy")

        assert workspace == fresh_home / "kanban" / "boards" / "legacy" / "workspaces" / task_id
        assert kb._is_managed_scratch_path(workspace) is True
    finally:
        conn.close()


def test_dispatch_threads_board_base_branch_into_worktree_spawn(fresh_home, tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "project")
    kb.write_board_metadata("project", repo_root=str(repo), base_branch="fork/project-main")
    conn = kb.connect(board="project")
    captured: list[tuple[str, str, str | None]] = []

    def fake_profile_exists(name: str) -> bool:
        return name == "default"

    def fake_spawn(task: kb.Task, workspace: str, *, board: str | None = None, base_branch: str | None = None):
        captured.append((task.id, workspace, base_branch))
        return 12345

    try:
        monkeypatch.setattr("hermes_cli.profiles.profile_exists", fake_profile_exists)
        task_id = kb.create_task(
            conn,
            title="worktree task",
            assignee="default",
            workspace_kind="worktree",
            branch_name="feature/test",
            board="project",
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=fake_spawn,
            board="project",
            base_branch=kb.read_board_metadata("project")["base_branch"],
        )

        expected_workspace = repo / ".hermes" / "kanban" / "worktrees" / task_id
        assert captured == [(task_id, str(expected_workspace), "fork/project-main")]
        assert result.spawned == [(task_id, "default", str(expected_workspace))]
    finally:
        conn.close()


def test_gateway_dispatch_reads_board_base_branch_and_passes_it_to_dispatch():
    import ast

    source = Path("gateway/run.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    calls = [node for node in ast.walk(module) if isinstance(node, ast.Call)]
    dispatch_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "dispatch_once"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_kb"
    ]
    assert dispatch_calls, "gateway dispatcher should call _kb.dispatch_once"
    assert any(
        any(keyword.arg == "base_branch" for keyword in call.keywords)
        for call in dispatch_calls
    ), "gateway dispatch_once call must pass board base_branch"
    assert "board_meta = _kb.read_board_metadata(slug)" in source


def test_board_without_base_branch_preserves_existing_worktree_fallback(fresh_home, monkeypatch):
    conn = kb.connect(board="legacy")
    captured: list[tuple[str, str, str | None]] = []

    def fake_profile_exists(name: str) -> bool:
        return name == "default"

    def fake_spawn(task: kb.Task, workspace: str, *, board: str | None = None, base_branch: str | None = None):
        captured.append((task.id, workspace, base_branch))
        return 12345

    try:
        monkeypatch.setattr("hermes_cli.profiles.profile_exists", fake_profile_exists)
        task_id = kb.create_task(
            conn,
            title="legacy worktree task",
            assignee="default",
            workspace_kind="worktree",
            branch_name="feature/test",
            board="legacy",
        )

        kb.dispatch_once(conn, spawn_fn=fake_spawn, board="legacy")

        assert captured == [(task_id, str(Path.cwd() / ".worktrees" / task_id), None)]
    finally:
        conn.close()
