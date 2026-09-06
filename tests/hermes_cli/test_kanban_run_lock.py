"""Gate-2 collision-guard WRITE side (PREREQ-A): run-registry lock writer +
self-cleaning reader. Pairs with the existing read-side guard tests."""
import json
import os
from types import SimpleNamespace

from hermes_cli import kanban_db


def _task(tid, branch=None):
    return SimpleNamespace(id=tid, branch_name=branch)


def test_write_run_lock_creates_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    p = kanban_db._write_run_lock(_task("t_abc", "feat/x"), os.getpid(), board="hermes")
    assert p is not None and p.exists()
    data = json.loads(p.read_text())
    assert data["tracking_card"] == "t_abc"
    assert data["branch"] == "feat/x"
    assert data["pid"] == os.getpid()
    assert data["slug"] == "t_abc"


def test_conflict_detects_live_branch_collision(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kanban_db._write_run_lock(_task("t_running", "feat/x"), os.getpid(), board="hermes")
    # a different card that shares the live branch must be flagged
    conflict = kanban_db._hive_registry_conflict({"id": "t_new", "branch_name": "feat/x"})
    assert conflict == "t_running"


def test_conflict_detects_same_card_double_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kanban_db._write_run_lock(_task("t_dup", None), os.getpid(), board="hermes")
    assert kanban_db._hive_registry_conflict({"id": "t_dup"}) == "t_dup"


def test_dead_pid_lock_is_reaped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = kanban_db._run_registry_dir()
    reg.mkdir(parents=True, exist_ok=True)
    lock = reg / "t_dead.lock"
    lock.write_text(json.dumps({
        "slug": "t_dead", "tracking_card": "t_dead",
        "branch": "feat/dead", "pid": 2147480000,
    }))
    # dead pid → not a conflict AND the stale lock is unlinked
    assert kanban_db._hive_registry_conflict({"id": "t_o", "branch_name": "feat/dead"}) is None
    assert not lock.exists()


def test_no_conflict_when_branch_differs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kanban_db._write_run_lock(_task("t_a", "feat/a"), os.getpid(), board="hermes")
    assert kanban_db._hive_registry_conflict({"id": "t_b", "branch_name": "feat/b"}) is None


def test_missing_registry_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nonexistent"))
    assert kanban_db._hive_registry_conflict({"id": "t_x", "branch_name": "feat/x"}) is None


def test_pid_alive_classification():
    # canonical _pid_alive (zombie-aware) takes int/None — the only shapes the
    # reader/writer feed it (JSON pid is int; None is guarded before the call)
    assert kanban_db._pid_alive(os.getpid()) is True
    assert kanban_db._pid_alive(2147480000) is False
    assert kanban_db._pid_alive(0) is False
    assert kanban_db._pid_alive(None) is False
