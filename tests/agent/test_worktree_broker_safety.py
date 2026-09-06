"""Regression contracts for the four broker-safety defects."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent import worktree_broker as broker_mod


_INVALID_SESSION_IDS = [
    "",
    ".",
    "..",
    "../escape",
    "bad/id",
    r"bad\id",
    "bad\ncontrol",
    "bad$id",
    "/",
]


def _proc(returncode: int = 0, *, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _bare_broker(tmp_path: Path) -> broker_mod.WorktreeBroker:
    broker = broker_mod.WorktreeBroker.__new__(broker_mod.WorktreeBroker)
    broker.repo_root = tmp_path / "repo"
    broker.repo_root.mkdir(exist_ok=True)
    broker.hermes_home = tmp_path / "home"
    broker.hermes_home.mkdir(exist_ok=True)
    broker.wt_dir_name = "codex-wt"
    broker.branch_prefix = "codex"
    broker.port_range = (50000, 50002)
    broker.ports_enabled = True
    broker.max_active_leases = None
    broker._wt_root = broker.hermes_home / broker.wt_dir_name
    broker._wt_root.mkdir(parents=True, exist_ok=True)
    broker._registry = {}
    broker._disk_free_bytes = MagicMock(return_value=16 * 1024**3)
    broker._git = MagicMock(return_value=_proc(1, stderr="blocked test git"))
    broker._allocate_port = MagicMock(return_value=50000)
    broker._free_port = MagicMock()
    broker._write_identity = MagicMock()
    broker._remove_identity = MagicMock()
    broker._worktree_is_clean_for_removal = MagicMock(return_value=False)
    return broker


def _capture_error(callable_) -> BaseException | None:
    try:
        callable_()
    except BaseException as exc:  # the exact typed contract is asserted below
        return exc
    return None


def _invoke(broker: broker_mod.WorktreeBroker, operation: str, session_id: str):
    if operation == "allocate":
        return broker.allocate(session_id, isa_slug="safety")
    if operation == "complete_lease":
        return broker.complete_lease(session_id, base_sha="base")
    if operation == "release":
        return broker.release(session_id)
    raise AssertionError(f"unknown operation: {operation}")


def _assert_invalid_without_side_effects(
    *,
    broker: broker_mod.WorktreeBroker,
    operation: str,
    session_id: str,
    subprocess_run: MagicMock,
    rmtree: MagicMock,
    canary: Path,
) -> None:
    before_registry = dict(broker._registry)
    error = _capture_error(lambda: _invoke(broker, operation, session_id))
    expected_error = getattr(broker_mod, "InvalidSessionIdError", None)
    contract_holds = all(
        (
            expected_error is not None,
            type(error) is expected_error,
            broker._registry == before_registry,
            broker._disk_free_bytes.call_count == 0,
            broker._git.call_count == 0,
            broker._allocate_port.call_count == 0,
            broker._free_port.call_count == 0,
            broker._write_identity.call_count == 0,
            broker._remove_identity.call_count == 0,
            broker._worktree_is_clean_for_removal.call_count == 0,
            subprocess_run.call_count == 0,
            rmtree.call_count == 0,
            canary.read_text(encoding="utf-8") == "survives",
        )
    )
    assert contract_holds, (
        f"{operation} accepted unsafe session/path state or performed a side effect; "
        f"error={error!r}"
    )


@pytest.mark.parametrize("operation", ["allocate", "complete_lease", "release"])
@pytest.mark.parametrize(
    "session_id",
    _INVALID_SESSION_IDS,
    ids=[
        "empty",
        "dot",
        "dotdot",
        "traversal",
        "slash",
        "backslash",
        "control",
        "outside-charset",
        "absolute",
    ],
)
def test_invalid_session_ids_fail_before_every_side_effect(
    tmp_path, monkeypatch, operation, session_id
):
    broker = _bare_broker(tmp_path)
    canary = tmp_path / "canary.txt"
    canary.write_text("survives", encoding="utf-8")

    # Make the base implementation walk as far as it can, but keep its unsafe
    # cleanup attempt observational and confined to mocks.
    try:
        candidate = broker._wt_root / session_id
        resolved = candidate.resolve(strict=False)
        if resolved.is_relative_to(tmp_path.resolve()) and not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, ValueError):
        pass

    run = MagicMock(return_value=_proc(1, stderr="no tmux"))
    remove_tree = MagicMock()
    monkeypatch.setattr(broker_mod.subprocess, "run", run)
    monkeypatch.setattr(shutil, "rmtree", remove_tree)

    _assert_invalid_without_side_effects(
        broker=broker,
        operation=operation,
        session_id=session_id,
        subprocess_run=run,
        rmtree=remove_tree,
        canary=canary,
    )


@pytest.mark.parametrize("operation", ["allocate", "complete_lease", "release"])
def test_hydrated_external_path_is_rejected_before_side_effects(
    tmp_path, monkeypatch, operation
):
    broker = _bare_broker(tmp_path)
    session_id = "sid-safe"
    external = tmp_path / "outside" / session_id
    external.mkdir(parents=True)
    canary = external / "canary.txt"
    canary.write_text("survives", encoding="utf-8")
    broker._registry[session_id] = broker_mod.Worktree(
        session_id=session_id,
        path=external,
        branch=f"codex/{session_id}/task",
        port=50000,
        created_at=datetime.now(timezone.utc),
        base_sha="base",
    )

    run = MagicMock(return_value=_proc(1, stderr="no tmux"))
    remove_tree = MagicMock()
    monkeypatch.setattr(broker_mod.subprocess, "run", run)
    monkeypatch.setattr(shutil, "rmtree", remove_tree)

    _assert_invalid_without_side_effects(
        broker=broker,
        operation=operation,
        session_id=session_id,
        subprocess_run=run,
        rmtree=remove_tree,
        canary=canary,
    )


@pytest.mark.parametrize("operation", ["allocate", "complete_lease", "release"])
def test_symlink_escape_is_rejected_before_side_effects(
    tmp_path, monkeypatch, operation
):
    broker = _bare_broker(tmp_path)
    session_id = "sid-symlink"
    external = tmp_path / "outside" / session_id
    external.mkdir(parents=True)
    canary = external / "canary.txt"
    canary.write_text("survives", encoding="utf-8")
    (broker._wt_root / session_id).symlink_to(external, target_is_directory=True)

    run = MagicMock(return_value=_proc(1, stderr="no tmux"))
    remove_tree = MagicMock()
    monkeypatch.setattr(broker_mod.subprocess, "run", run)
    monkeypatch.setattr(shutil, "rmtree", remove_tree)

    _assert_invalid_without_side_effects(
        broker=broker,
        operation=operation,
        session_id=session_id,
        subprocess_run=run,
        rmtree=remove_tree,
        canary=canary,
    )


def test_gc_empty_registry_floor_refuses_non_dot_worktrees(tmp_path):
    broker = _bare_broker(tmp_path)
    orphan = broker._wt_root / "sid-orphan"
    orphan.mkdir()
    (orphan / "canary.txt").write_text("survives", encoding="utf-8")

    actions = broker.gc(tracked_sids=set(), live_branches=set())

    assert actions == []
    assert (orphan / "canary.txt").read_text(encoding="utf-8") == "survives"
    broker._git.assert_not_called()


def test_gc_empty_registry_manual_override_is_explicit(tmp_path):
    broker = _bare_broker(tmp_path)
    orphan = broker._wt_root / "sid-orphan"
    orphan.mkdir()

    actions = broker.gc(
        tracked_sids=set(),
        live_branches=set(),
        allow_empty_tracked_sids=True,
    )

    assert [action.sid for action in actions] == ["sid-orphan"]
    assert not orphan.exists()
    broker._git.assert_called_once_with("worktree", "prune")


def _write_ports_home(tmp_path: Path, payload: object, *, label: str) -> tuple[Path, Path]:
    home = tmp_path / label
    home.mkdir()
    ports_path = home / "codex-ports.json"
    ports_path.write_text('{"50000": "sid-live", "50001": "sid-stale"}\n', encoding="utf-8")
    sessions_path = home / "codex_sessions.json"
    if isinstance(payload, str) and payload == "<CORRUPT>":
        sessions_path.write_text("{not-json", encoding="utf-8")
    else:
        sessions_path.write_text(json.dumps(payload), encoding="utf-8")
    return home, ports_path


@pytest.mark.parametrize(
    "payload",
    [
        "<CORRUPT>",
        {"version": 2, "sessions": {}},
        7,
        {"version": 1, "sessions": []},
        {"version": 1, "sessions": {"thread": {"session_id": 7}}},
        [{"session_id": 7}],
    ],
    ids=[
        "corrupt-json",
        "unsupported-version",
        "malformed-top",
        "malformed-sessions",
        "malformed-v1-row",
        "malformed-legacy-row",
    ],
)
def test_port_recovery_unknown_registry_leaves_ports_byte_unchanged(tmp_path, payload):
    home, ports_path = _write_ports_home(tmp_path, payload, label="home")
    before = ports_path.read_bytes()
    repo = tmp_path / "repo"
    repo.mkdir()

    broker_mod.WorktreeBroker(
        repo_root=repo,
        hermes_home=home,
        port_range=(50000, 50002),
    )

    assert ports_path.read_bytes() == before


def test_port_recovery_io_failure_leaves_ports_byte_unchanged(tmp_path, monkeypatch):
    payload = {"version": 1, "sessions": {"thread": {"session_id": "sid-live"}}}
    home, ports_path = _write_ports_home(tmp_path, payload, label="home")
    sessions_path = home / "codex_sessions.json"
    before = ports_path.read_bytes()
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        if path == sessions_path:
            raise OSError("simulated registry I/O failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    repo = tmp_path / "repo"
    repo.mkdir()
    broker_mod.WorktreeBroker(
        repo_root=repo,
        hermes_home=home,
        port_range=(50000, 50002),
    )

    assert ports_path.read_bytes() == before


def test_legacy_registry_shapes_require_positive_validation(tmp_path):
    valid_payloads = [
        {"sid-live": {"path": "/legacy/path"}},
        ["sid-live"],
        [{"session_id": "sid-live"}],
    ]
    repo = tmp_path / "repo"
    repo.mkdir()
    for index, payload in enumerate(valid_payloads):
        home, ports_path = _write_ports_home(tmp_path, payload, label=f"valid-{index}")
        broker_mod.WorktreeBroker(
            repo_root=repo,
            hermes_home=home,
            port_range=(50000, 50002),
        )
        recovered = json.loads(ports_path.read_text(encoding="utf-8"))
        assert recovered["50000"] == "sid-live"
        assert recovered["50001"] is None

    # bool is not an integer schema version; unknown v1-like data must leave
    # the exact ports bytes alone rather than being guessed as an empty registry.
    malformed = {"version": True, "sessions": {}}
    home, ports_path = _write_ports_home(tmp_path, malformed, label="malformed")
    before = ports_path.read_bytes()
    broker_mod.WorktreeBroker(
        repo_root=repo,
        hermes_home=home,
        port_range=(50000, 50002),
    )
    assert ports_path.read_bytes() == before


def test_two_broker_restart_preserves_port_from_real_v1_registry(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    broker_a = broker_mod.WorktreeBroker(
        repo_root=repo,
        hermes_home=home,
        port_range=(50000, 50002),
    )
    broker_a._disk_free_bytes = MagicMock(return_value=16 * 1024**3)
    broker_a._git = MagicMock(return_value=_proc())
    lease = broker_a.allocate("sid-live", isa_slug="restart")
    assert lease.port == 50000

    (home / "codex_sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "thread-live": {
                        "session_id": "sid-live",
                        "state": "EXECUTING",
                        "worktree_path": str(lease.path),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    broker_mod.WorktreeBroker(
        repo_root=repo,
        hermes_home=home,
        port_range=(50000, 50002),
    )
    recovered = json.loads((home / "codex-ports.json").read_text(encoding="utf-8"))
    assert recovered == {"50000": "sid-live", "50001": None}
