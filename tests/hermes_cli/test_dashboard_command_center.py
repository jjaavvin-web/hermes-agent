from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

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


def _import_module():
    import hermes_cli.dashboard_command_center as command_center

    module = importlib.reload(command_center)
    module._COMMAND_CENTER_CACHE = None
    return module


def _create_board(slug: str, *, name: str = "Alpha Ship") -> None:
    kb.create_board(slug, name=name, icon="🚀", color="#76e4f7")


def _add_task(
    slug: str,
    title: str,
    *,
    status: str,
    now: int,
    worker_pid: int | None = None,
    last_heartbeat_at: int | None = None,
    consecutive_failures: int = 0,
) -> str:
    conn = kb.connect(board=slug)
    try:
        task_id = kb.create_task(conn, title=title, priority=1, board=slug)
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, started_at = ?, last_heartbeat_at = ?, worker_pid = ?, consecutive_failures = ?
            WHERE id = ?
            """,
            (status, now - 100, last_heartbeat_at, worker_pid, consecutive_failures, task_id),
        )
        return task_id
    finally:
        conn.close()


def test_command_center_endpoint_aggregates_existing_layers(monkeypatch, tmp_path):
    _setup_home(tmp_path, monkeypatch)
    now = int(time.time())
    _create_board("alpha")
    blocked_id = _add_task("alpha", "Blocked deploy decision", status="blocked", now=now)
    _add_task("alpha", "Needs operator review", status="review", now=now)
    _add_task(
        "alpha",
        "Worker stopped heartbeating",
        status="running",
        now=now,
        worker_pid=12345,
        last_heartbeat_at=now - (3 * 3600),
    )

    mod = _import_module()
    monkeypatch.setattr(
        mod,
        "_mission_snapshot",
        lambda: {
            "runtimes": [{"name": "codex", "label": "Codex", "status": "online"}],
            "recentSessions": [{"id": "s1", "preview": "Continue dashboard polish"}],
            "swarm": {"workerCount": 2},
            "nextCron": {"name": "Daily brief"},
        },
    )
    monkeypatch.setattr(
        mod,
        "_git_health_snapshot",
        lambda: {
            "rows": [
                {
                    "slug": "command-center",
                    "thread_id": "thread-1",
                    "stage": "pr",
                    "pr_number": 12,
                    "pr_url": "https://example.test/pull/12",
                    "pr_state": "OPEN",
                    "merged": False,
                    "recommendation": "PR is open — review before merge",
                }
            ]
        },
    )
    monkeypatch.setattr(
        mod,
        "_codex_sessions_snapshot",
        lambda: {
            "sessions": [
                {
                    "session_id": "sid-1",
                    "pr_number": 12,
                    "pr_url": "https://example.test/pull/12",
                    "pr_state": "OPEN",
                }
            ]
        },
    )
    monkeypatch.setattr(mod, "_resume_snapshot", lambda: {"summary": "Resume Alpha Ship from review", "session_id": "s1"})
    monkeypatch.setattr(mod, "_now_epoch", lambda: now)
    monkeypatch.setattr(mod, "_pid_alive", lambda pid: True)

    app = FastAPI()
    app.include_router(mod.router)
    payload = TestClient(app).get("/api/dashboard/command-center").json()

    assert set(payload) == {"projects", "live", "decisions", "stalled", "resume"}
    assert payload["projects"][0]["slug"] == "alpha"
    assert payload["projects"][0]["completion_pct"] >= 0
    assert payload["live"]["runtimes"][0]["name"] == "codex"
    assert payload["live"]["active_sessions"][0]["id"] == "s1"
    assert payload["resume"]["summary"] == "Resume Alpha Ship from review"

    decisions = {(item["title"], item["source"]) for item in payload["decisions"]}
    assert ("Blocked deploy decision", "kanban:alpha") in decisions
    assert ("Needs operator review", "kanban:alpha") in decisions
    assert ("command-center PR #12", "github") in decisions
    blocked = next(item for item in payload["decisions"] if item["title"] == "Blocked deploy decision")
    assert blocked["link_or_id"].endswith(blocked_id)
    assert blocked["reason"] == "blocked"

    assert payload["stalled"] == [
        {
            "title": "Worker stopped heartbeating",
            "project": "alpha",
            "status": "running",
            "idle_for": "3h",
            "why": "heartbeat stale",
        }
    ]


def test_resume_snapshot_degrades_to_null(monkeypatch, tmp_path):
    _setup_home(tmp_path, monkeypatch)
    mod = _import_module()

    def boom(*_args, **_kwargs):
        raise RuntimeError("honcho unavailable")

    monkeypatch.setattr(mod.subprocess, "run", boom)

    assert mod._resume_snapshot() is None


def test_resume_snapshot_parses_json(monkeypatch, tmp_path):
    _setup_home(tmp_path, monkeypatch)
    mod = _import_module()

    completed = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"summary": "Pick up the dashboard thread"}),
        stderr="",
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert mod._resume_snapshot() == {"summary": "Pick up the dashboard thread"}
