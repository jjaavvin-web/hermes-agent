from __future__ import annotations

import importlib
import time
from pathlib import Path

from hermes_cli import kanban_db as kb


def _setup_home(tmp_path: Path, monkeypatch) -> Path:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    return hermes_home


def _import_module(monkeypatch):
    import hermes_cli.dashboard_get_some as get_some

    module = importlib.reload(get_some)
    module._PROJECTS_CACHE = None
    module._NEXUS_CACHE = None
    return module


def _create_board(slug: str, *, name: str, icon: str, color: str, archived: bool = False) -> None:
    kb.create_board(slug, name=name, icon=icon, color=color)
    if archived:
        kb.write_board_metadata(slug, archived=True)


def _add_task(
    slug: str,
    title: str,
    *,
    status: str,
    priority: int = 0,
    branch_name: str | None = None,
    created_at: int | None = None,
    started_at: int | None = None,
    completed_at: int | None = None,
) -> str:
    conn = kb.connect(board=slug)
    try:
        task_id = kb.create_task(
            conn,
            title=title,
            priority=priority,
            branch_name=branch_name,
            board=slug,
            triage=status == "triage",
        )
        ts = int(time.time()) if created_at is None else created_at
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, created_at = ?, started_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, ts, started_at, completed_at, task_id),
        )
        return task_id
    finally:
        conn.close()


def _link_tasks(slug: str, parent_id: str, child_id: str) -> None:
    conn = kb.connect(board=slug)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO task_links(parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id),
        )
    finally:
        conn.close()


def test_projects_weighted_pct_and_excludes_archived_boards(monkeypatch, tmp_path):
    _setup_home(tmp_path, monkeypatch)
    _create_board("alpha", name="Alpha Ship", icon="🚀", color="#76e4f7")
    _create_board("old-ghost", name="Old Ghost", icon="🪦", color="#4a5568", archived=True)
    _add_task("alpha", "done task", status="done", completed_at=1_700_001_000)
    _add_task("alpha", "review task", status="review", started_at=1_700_000_900)
    _add_task("alpha", "running task", status="running", started_at=1_700_000_800)
    _add_task("alpha", "blocked task", status="blocked", created_at=1_700_000_700)
    _add_task("alpha", "todo task", status="todo", created_at=1_700_000_600)
    _add_task("old-ghost", "archived board task", status="done", completed_at=1_700_002_000)
    get_some = _import_module(monkeypatch)

    payload = get_some.get_projects()

    assert [p["slug"] for p in payload["projects"]] == ["alpha"]
    project = payload["projects"][0]
    assert project["name"] == "Alpha Ship"
    assert project["icon"] == "🚀"
    assert project["color"] == "#76e4f7"
    assert project["archived"] is False
    assert project["total"] == 5
    assert project["by_status"] == {
        "done": 1,
        "review": 1,
        "running": 1,
        "blocked": 1,
        "todo": 1,
    }
    assert project["completion_pct"] == 51
    assert project["active"] == 2
    assert project["blocked"] == 1
    assert project["last_activity"] == 1_700_001_000
    assert project["remaining_count"] == 4
    assert project["remaining_by_status"] == {
        "review": 1,
        "running": 1,
        "blocked": 1,
        "todo": 1,
    }
    assert project["remaining_more"] == 0
    assert {item["title"] for item in project["remaining"]} == {
        "review task",
        "running task",
        "blocked task",
        "todo task",
    }
    assert all(item["status"] != "done" for item in project["remaining"])


def test_projects_remaining_work_is_capped_and_reports_more(monkeypatch, tmp_path):
    _setup_home(tmp_path, monkeypatch)
    _create_board("alpha", name="Alpha Ship", icon="🚀", color="#76e4f7")
    for idx in range(24):
        _add_task(
            "alpha",
            f"ready task {idx:02d}",
            status="ready",
            priority=idx % 3,
            created_at=1_700_000_000 + idx,
        )
    _add_task("alpha", "already shipped", status="done", completed_at=1_700_001_000)
    get_some = _import_module(monkeypatch)

    project = get_some.get_projects()["projects"][0]

    assert project["remaining_count"] == 24
    assert project["remaining_by_status"] == {"ready": 24}
    assert len(project["remaining"]) == 20
    assert project["remaining_more"] == 4
    assert all(item["status"] == "ready" for item in project["remaining"])
    assert "already shipped" not in {item["title"] for item in project["remaining"]}


def test_work_nexus_returns_project_task_nodes_and_contains_blocks_edges(monkeypatch, tmp_path):
    _setup_home(tmp_path, monkeypatch)
    _create_board("alpha", name="Alpha Ship", icon="🚀", color="#76e4f7")
    parent = _add_task("alpha", "Parent blocker task with a long descriptive title", status="running")
    child = _add_task("alpha", "Child task", status="done", completed_at=1_700_001_000)
    _link_tasks("alpha", parent, child)
    get_some = _import_module(monkeypatch)

    payload = get_some.get_work_nexus()

    node_ids = {node["id"] for node in payload["nodes"]}
    assert "project:alpha" in node_ids
    assert f"task:alpha:{parent}" in node_ids
    assert f"task:alpha:{child}" in node_ids
    task = next(n for n in payload["nodes"] if n["id"] == f"task:alpha:{child}")
    assert task["kind"] == "task"
    assert task["status"] == "done"
    assert task["completed"] is True
    assert task["board"] == "alpha"
    assert len(task["label"]) <= 41
    edges = {(edge["kind"], edge["source"], edge["target"]) for edge in payload["edges"]}
    assert ("contains", "project:alpha", f"task:alpha:{parent}") in edges
    assert ("contains", "project:alpha", f"task:alpha:{child}") in edges
    assert ("blocks", f"task:alpha:{parent}", f"task:alpha:{child}") in edges


def test_work_nexus_bounds_task_nodes_and_rolls_up_backlog(monkeypatch, tmp_path):
    _setup_home(tmp_path, monkeypatch)
    statuses = ["todo", "ready", "running", "blocked", "review", "done"]
    slugs = [f"project-{idx:02d}" for idx in range(12)]
    total_tasks = 0
    for idx, slug in enumerate(slugs):
        _create_board(slug, name=f"Project {idx:02d}", icon="✦", color="#76e4f7")
        for task_idx in range(80):
            _add_task(
                slug,
                f"{slug} task {task_idx:02d}",
                status=statuses[task_idx % len(statuses)],
                priority=task_idx % 5,
                created_at=1_700_000_000 + task_idx,
            )
            total_tasks += 1
    overlay_pr_nodes = [
        {"id": f"pr:{idx}", "kind": "pr", "label": f"PR #{idx}"}
        for idx in range(25)
    ]
    get_some = _import_module(monkeypatch)
    monkeypatch.setattr(get_some, "_codex_pr_overlay", lambda *_args, **_kwargs: (overlay_pr_nodes, []))
    monkeypatch.setattr(get_some, "_git_river_pr_overlay", lambda: ([], []))

    payload = get_some.get_work_nexus()

    assert len(payload["nodes"]) <= 150
    project_nodes = [node for node in payload["nodes"] if node["kind"] == "project"]
    task_nodes = [node for node in payload["nodes"] if str(node["id"]).startswith("task:")]
    aggregate_nodes = [node for node in payload["nodes"] if node.get("aggregate") is True]
    assert {node["board"] for node in project_nodes} == set(slugs)
    assert len(project_nodes) == len(slugs)
    assert {node["board"] for node in aggregate_nodes} == set(slugs)
    assert all(node["label"] == f"+{node['hidden_count']} more" for node in aggregate_nodes)
    assert sum(int(node["hidden_count"]) for node in aggregate_nodes) == total_tasks - len(task_nodes)


def test_work_nexus_overlay_failures_degrade_without_raising(monkeypatch, tmp_path):
    _setup_home(tmp_path, monkeypatch)
    _create_board("alpha", name="Alpha Ship", icon="🚀", color="#76e4f7")
    _add_task("alpha", "Core task still visible", status="ready")
    get_some = _import_module(monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("codex sessions unavailable")

    monkeypatch.setattr(get_some, "_codex_pr_overlay", explode)

    payload = get_some.get_work_nexus()

    assert isinstance(payload["degraded_mode"], list)
    assert "codex_pr_overlay" in payload["degraded_mode"]
    assert any(node["id"] == "project:alpha" for node in payload["nodes"])
    assert any(node["id"].startswith("task:alpha:") for node in payload["nodes"])
