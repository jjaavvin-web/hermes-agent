from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hermes_cli import git_janitor as gj


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _write_lock(home: Path, name: str, payload) -> Path:
    registry = home / "run-registry"
    registry.mkdir(parents=True, exist_ok=True)
    path = registry / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_missing_run_registry_returns_empty(hermes_home):
    assert gj._read_run_registry() == []


def test_seeded_lease_round_trips_full_schema(hermes_home):
    lease = {
        "branch": "worker/h01",
        "worktree_path": "/repo/.worktrees/h01",
        "spawner": "pytest",
        "tmux_session": "h01-session",
        "kanban_card_id": "card-h01",
        "repo_root": "/repo",
        "created_at": "2026-05-29T07:02:01Z",
    }
    lock_path = _write_lock(hermes_home, "h01.lock", lease)

    locks = gj._read_run_registry()

    assert len(locks) == 1
    lock = locks[0]
    for field in gj.RUN_REGISTRY_LEASE_FIELDS:
        assert lock[field] == lease[field]
    assert lock["_path"] == str(lock_path)


def test_malformed_lease_is_skipped_with_warning(hermes_home, caplog):
    _write_lock(hermes_home, "bad.lock", "{definitely not json")
    _write_lock(
        hermes_home,
        "good.lock",
        {
            "branch": "worker/good",
            "worktree_path": "/repo/.worktrees/good",
            "spawner": "pytest",
            "tmux_session": "good-session",
            "kanban_card_id": "card-good",
            "repo_root": "/repo",
            "created_at": "2026-05-29T07:02:01Z",
        },
    )

    with caplog.at_level(logging.WARNING):
        locks = gj._read_run_registry()

    assert [lock["branch"] for lock in locks] == ["worker/good"]
    assert "Skipping malformed run-registry lease" in caplog.text
    assert "bad.lock" in caplog.text


def test_live_tmux_lease_keeps_matching_worktree_active(monkeypatch):
    lock = {"branch": "worker/live", "tmux_session": "live-session"}
    worktree = {"branch": "worker/live", "path": "/repo/.worktrees/live"}

    assert gj._lock_for_branch([lock], "worker/live") is lock
    monkeypatch.setattr(gj, "_tmux_alive", lambda session: session == "live-session")

    klass = gj.classify_worktree(
        worktree,
        lock=gj._lock_for_branch([lock], worktree["branch"]),
        is_merged=False,
        card_status=None,
        tmux_alive=gj._tmux_alive(lock["tmux_session"]),
        age_days=30,
    )

    assert klass == "ACTIVE"
