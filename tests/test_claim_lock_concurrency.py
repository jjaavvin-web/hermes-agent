from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
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
    board = "claims"
    kb.connect(board=board).close()
    return board


def _connect(board: str) -> sqlite3.Connection:
    return kb.connect(board=board)


def test_concurrent_dispatch_claims_same_card_once(isolated_board, monkeypatch):
    setup_conn = _connect(isolated_board)
    try:
        task_id = kb.create_task(setup_conn, title="one winner", assignee="default", board=isolated_board)
    finally:
        setup_conn.close()

    gate = threading.Barrier(2)
    spawned: list[tuple[str, str]] = []
    lock = threading.Lock()

    def fake_profile_exists(name: str) -> bool:
        return name == "default"

    def fake_spawn(task: kb.Task, workspace: str, **kwargs):
        with lock:
            spawned.append((task.id, task.claim_lock or ""))
        return 12345

    def contender() -> kb.DispatchResult:
        conn = _connect(isolated_board)
        try:
            gate.wait(timeout=5)
            return kb.dispatch_once(conn, spawn_fn=fake_spawn, board=isolated_board, max_spawn=1)
        finally:
            conn.close()

    monkeypatch.setattr("hermes_cli.profiles.profile_exists", fake_profile_exists)
    results: list[kb.DispatchResult] = []
    errors: list[BaseException] = []

    def run_contender():
        try:
            results.append(contender())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run_contender) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert sum(1 for result in results if result.spawned) == 1
    assert spawned and [task for task, _ in spawned] == [task_id]
    assert len({claim_lock for _, claim_lock in spawned}) == 1

    check_conn = _connect(isolated_board)
    try:
        row = check_conn.execute(
            "SELECT status, claim_lock, claim_expires, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["claim_lock"]
        assert row["claim_expires"] is not None
        assert row["current_run_id"] is not None
        assert check_conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ? AND status = 'running'",
            (task_id,),
        ).fetchone()[0] == 1
    finally:
        check_conn.close()


def test_expired_claim_reclaim_then_concurrent_dispatch_claims_once(isolated_board, monkeypatch):
    setup_conn = _connect(isolated_board)
    try:
        task_id = kb.create_task(setup_conn, title="stale winner", assignee="default", board=isolated_board)
        claimed = kb.claim_task(setup_conn, task_id, ttl_seconds=60, claimer="stale-worker")
        assert claimed is not None
        setup_conn.execute(
            "UPDATE tasks SET claim_expires = 1, worker_pid = NULL WHERE id = ?",
            (task_id,),
        )
        setup_conn.execute(
            "UPDATE task_runs SET claim_expires = 1 WHERE task_id = ? AND ended_at IS NULL",
            (task_id,),
        )
        setup_conn.commit()
    finally:
        setup_conn.close()

    release_conn = _connect(isolated_board)
    try:
        assert kb.release_stale_claims(release_conn) == 1
    finally:
        release_conn.close()

    gate = threading.Barrier(2)
    spawned: list[tuple[str, str]] = []
    lock = threading.Lock()

    def fake_profile_exists(name: str) -> bool:
        return name == "default"

    def fake_spawn(task: kb.Task, workspace: str, **kwargs):
        with lock:
            spawned.append((task.id, task.claim_lock or ""))
        return 23456

    def contender() -> kb.DispatchResult:
        conn = _connect(isolated_board)
        try:
            gate.wait(timeout=5)
            return kb.dispatch_once(conn, spawn_fn=fake_spawn, board=isolated_board, max_spawn=1)
        finally:
            conn.close()

    monkeypatch.setattr("hermes_cli.profiles.profile_exists", fake_profile_exists)
    results: list[kb.DispatchResult] = []
    errors: list[BaseException] = []

    def run_contender():
        try:
            results.append(contender())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run_contender) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert sum(1 for result in results if result.spawned) == 1
    assert spawned and [task for task, _ in spawned] == [task_id]
    assert len({claim_lock for _, claim_lock in spawned}) == 1

    check_conn = _connect(isolated_board)
    try:
        task = kb.get_task(check_conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.claim_lock == spawned[0][1]
        assert task.claim_expires is not None
        runs = check_conn.execute(
            "SELECT status, outcome, claim_lock, claim_expires, ended_at "
            "FROM task_runs WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        assert [(row["status"], row["outcome"]) for row in runs] == [
            ("reclaimed", "reclaimed"),
            ("running", None),
        ]
        assert runs[0]["claim_lock"] is None
        assert runs[0]["claim_expires"] is None
        assert runs[0]["ended_at"] is not None
        assert runs[1]["claim_lock"] == spawned[0][1]
        assert runs[1]["claim_expires"] is not None
    finally:
        check_conn.close()
