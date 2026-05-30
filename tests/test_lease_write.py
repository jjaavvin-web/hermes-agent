from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent import worktree_broker as wb
from agent.worktree_broker import WorktreeBroker, write_lease
from hermes_cli import git_janitor as gj


def _ok_git_result() -> MagicMock:
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


def _make_broker(tmp_path: Path) -> WorktreeBroker:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    return WorktreeBroker(repo_root=repo_root, hermes_home=hermes_home)


def _allocated_broker(tmp_path: Path, monkeypatch, *, lease_enabled: bool) -> tuple[WorktreeBroker, str]:
    monkeypatch.setenv("HERMES_RUN_REGISTRY_WRITE", "1" if lease_enabled else "0")
    broker = _make_broker(tmp_path)
    sid = "sid-lease-on" if lease_enabled else "sid-lease-off"
    with (
        patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
        patch.object(broker, "_git", return_value=_ok_git_result()),
    ):
        broker.allocate(sid, isa_slug="Lease Write")
    return broker, sid


def test_default_off_does_not_write_lease(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_RUN_REGISTRY_WRITE", raising=False)
    monkeypatch.setattr(wb, "read_raw_config", lambda: {}, raising=False)
    broker = _make_broker(tmp_path)

    with (
        patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
        patch.object(broker, "_git", return_value=_ok_git_result()),
    ):
        broker.allocate("sid-default-off", isa_slug="Lease Write")

    assert not (broker.hermes_home / "run-registry" / "sid-default-off.lock").exists()


def test_allocate_writes_schema_matching_lease_when_enabled(tmp_path, monkeypatch):
    broker, sid = _allocated_broker(tmp_path, monkeypatch, lease_enabled=True)
    lease_path = broker.hermes_home / "run-registry" / f"{sid}.lock"

    lease = json.loads(lease_path.read_text(encoding="utf-8"))

    assert set(gj.RUN_REGISTRY_LEASE_FIELDS) <= set(lease)
    assert lease["branch"] == "codex/sid-lease-on/lease-write"
    assert lease["worktree_path"] == str(broker.hermes_home / "codex-wt" / sid)
    assert lease["spawner"] == "worktree_broker"
    assert lease["tmux_session"] == "codex-sess-sid-lease-on"
    assert lease["kanban_card_id"] is None
    assert lease["repo_root"] == str(broker.repo_root)
    assert lease["created_at"].endswith("Z")


def test_release_removes_lease_when_enabled(tmp_path, monkeypatch):
    broker, sid = _allocated_broker(tmp_path, monkeypatch, lease_enabled=True)
    lease_path = broker.hermes_home / "run-registry" / f"{sid}.lock"
    assert lease_path.exists()

    with (
        patch.object(subprocess, "run", return_value=_ok_git_result()),
        patch.object(broker, "_git", return_value=_ok_git_result()),
    ):
        broker.release(sid)

    assert not lease_path.exists()


def test_gateway_kanban_worktree_lease_carries_card_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_RUN_REGISTRY_WRITE", "1")
    home = tmp_path / "hermes_home"
    repo = tmp_path / "repo"
    workspace = tmp_path / "worktrees" / "t_card123"
    repo.mkdir()
    workspace.mkdir(parents=True)
    lease_wt = type("LeaseWorktree", (), {
        "session_id": "run-42",
        "branch": "feature/card123",
        "path": workspace,
        "created_at": wb.datetime.now(wb.timezone.utc),
    })()

    write_lease(
        home,
        lease_wt,
        repo_root=repo,
        spawner="gateway_kanban",
        tmux_session="swarm-default",
        kanban_card_id="t_card123",
    )

    lease = json.loads((home / "run-registry" / "run-42.lock").read_text(encoding="utf-8"))
    assert lease["spawner"] == "gateway_kanban"
    assert lease["tmux_session"] == "swarm-default"
    assert lease["kanban_card_id"] == "t_card123"
    assert lease["branch"] == "feature/card123"
    assert lease["worktree_path"] == str(workspace)


def test_gateway_kanban_lease_wrapper_preserves_dispatch_context():
    """The gateway wrapper must expose board/base_branch for dispatch_once introspection."""
    module = ast.parse(Path("gateway/run.py").read_text(encoding="utf-8"))
    wrapper = next(
        node for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_lease_wrapped_spawn"
    )
    kwonly_names = {arg.arg for arg in wrapper.args.kwonlyargs}
    assert {"board", "base_branch"} <= kwonly_names


def test_stale_and_active_lease_classification(monkeypatch):
    lock = {"branch": "codex/sid-lease-on/lease-write", "tmux_session": "codex-sess-sid-lease-on"}
    worktree = {"branch": "codex/sid-lease-on/lease-write", "path": "/tmp/sid-lease-on"}

    monkeypatch.setattr(gj, "_tmux_alive", lambda session: session == "codex-sess-sid-lease-on")
    assert gj.classify_worktree(
        worktree,
        lock=lock,
        is_merged=False,
        card_status=None,
        tmux_alive=gj._tmux_alive(lock["tmux_session"]),
        age_days=30,
    ) == "ACTIVE"

    monkeypatch.setattr(gj, "_tmux_alive", lambda session: False)
    assert gj.classify_worktree(
        worktree,
        lock=lock,
        is_merged=False,
        card_status="archived",
        tmux_alive=gj._tmux_alive(lock["tmux_session"]),
        age_days=30,
        stale_days=7,
    ) == "STALE"
