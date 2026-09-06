from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb

    kb.init_db()
    return home


def _pin(monkeypatch, workspace: Path, *, role: str = "sol-builder", task: str = "t_self", run: str = "41", board: str = "sol") -> None:
    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", role)
    monkeypatch.setenv("HERMES_PROFILE", role)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))


def test_builder_has_only_task_scoped_lifecycle_authority(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path)
    from hermes_cli.worker_authority import authorize_current_worker

    assert authorize_current_worker(
        "kanban_heartbeat", {"task_id": "t_self", "board": "sol", "note": "phase=build artifact written"}
    ).allowed
    assert authorize_current_worker(
        "kanban_comment", {"task_id": "t_self", "board": "sol", "body": "Evidence: artifact hash verified."}
    ).allowed
    assert authorize_current_worker(
        "kanban_complete",
        {
            "task_id": "t_self",
            "board": "sol",
            "summary": "DONE_REVIEW_REQUIRED — artifact and focused test passed.",
            "metadata": {"verdict": "DONE_REVIEW_REQUIRED"},
        },
    ).allowed


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("kanban_heartbeat", {"task_id": "t_sibling", "board": "sol", "note": "real progress"}),
        ("kanban_comment", {"task_id": "t_sibling", "board": "sol", "body": "foreign"}),
        ("kanban_complete", {"task_id": "t_self", "board": "other", "summary": "DONE_REVIEW_REQUIRED"}),
        ("kanban_complete", {"task_id": "t_self", "board": "sol", "summary": "APPROVED"}),
        ("kanban_heartbeat", {"task_id": "t_self", "board": "sol", "note": "alive"}),
        ("kanban_block", {"task_id": "t_self", "board": "sol", "reason": "Need input."}),
        ("kanban_block", {"task_id": "t_self", "board": "sol", "reason": "Which fixture? Which hash?"}),
    ],
)
def test_builder_lifecycle_authority_denies_scope_and_semantic_violations(monkeypatch, tmp_path, tool, args):
    _pin(monkeypatch, tmp_path)
    from hermes_cli.worker_authority import authorize_current_worker

    assert not authorize_current_worker(tool, args).allowed


def test_lifecycle_authority_fails_closed_when_a_pin_is_missing(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    from hermes_cli.worker_authority import authorize_current_worker

    decision = authorize_current_worker(
        "kanban_heartbeat", {"task_id": "t_self", "board": "sol", "note": "phase=verify hash checked"}
    )
    assert not decision.allowed
    assert "pin" in decision.reason


def test_verifier_can_approve_but_cannot_use_builder_handoff(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path, role="sol-verifier")
    from hermes_cli.worker_authority import authorize_current_worker

    assert authorize_current_worker(
        "kanban_complete",
        {"task_id": "t_self", "board": "sol", "summary": "APPROVED — direct read and focused test passed."},
    ).allowed
    assert not authorize_current_worker(
        "kanban_complete",
        {"task_id": "t_self", "board": "sol", "summary": "DONE_REVIEW_REQUIRED"},
    ).allowed


@pytest.mark.parametrize("verdict", ["NOT APPROVED", "DISAPPROVED", "UNAPPROVED"])
def test_verifier_rejects_negative_approval_wording(monkeypatch, tmp_path, verdict):
    _pin(monkeypatch, tmp_path, role="sol-verifier")
    from hermes_cli.worker_authority import authorize_current_worker

    assert not authorize_current_worker(
        "kanban_complete",
        {"task_id": "t_self", "board": "sol", "summary": verdict},
    ).allowed


def test_only_noted_heartbeat_is_acceptance_grade(kanban_home):
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="progress semantics", assignee="sol-builder")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None
        assert kb.heartbeat_worker(conn, tid, note=None, expected_run_id=run_id)
        assert kb.heartbeat_worker(
            conn, tid, note="phase=build artifact hash verified", expected_run_id=run_id
        )
        events = kb.list_events(conn, tid)

    liveness = [event for event in events if event.kind == "liveness"]
    progress = [event for event in events if event.kind == "heartbeat"]
    assert len(liveness) == 1
    assert liveness[0].payload == {"source": "runtime_activity"}
    assert len(progress) == 1
    assert progress[0].payload == {"note": "phase=build artifact hash verified"}


def test_auto_liveness_never_counts_as_meaningful_heartbeat(kanban_home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="automatic liveness", assignee="sol-builder")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        claim_lock = claimed.claim_lock

    _pin(
        monkeypatch,
        kanban_home,
        role="sol-builder",
        task=tid,
        run=str(run_id),
        board="default",
    )
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(claim_lock))
    kt._auto_heartbeat_last_attempt = 0.0
    assert kt.heartbeat_current_worker_from_env() is True
    with kb.connect() as conn:
        events = kb.list_events(conn, tid)
    assert len([event for event in events if event.kind == "liveness"]) == 1
    assert [event for event in events if event.kind == "heartbeat"] == []

    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    kt._auto_heartbeat_last_attempt = 0.0
    assert kt.heartbeat_current_worker_from_env() is False
    with kb.connect() as conn:
        after = kb.list_events(conn, tid)
    assert len(after) == len(events)


def test_stale_run_cannot_append_evidence_comment(kanban_home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stale evidence", assignee="sol-builder")
        first = kb.claim_task(conn, tid)
        assert first is not None and first.current_run_id is not None
        run1 = first.current_run_id
        kb._set_worker_pid(conn, tid, 98765)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        assert kb.detect_crashed_workers(conn) == [tid]
        second = kb.claim_task(conn, tid)
        assert second is not None and second.current_run_id != run1

    _pin(
        monkeypatch,
        kanban_home,
        role="sol-builder",
        task=tid,
        run=str(run1),
        board="default",
    )
    result = json.loads(
        kt._handle_comment({"task_id": tid, "body": "stale evidence must fail"})
    )
    assert "error" in result and "stale run" in result["error"]
    with kb.connect() as conn:
        assert kb.list_comments(conn, tid) == []


def test_specialist_test_is_a_known_narrow_toolset():
    from toolsets import resolve_toolset, validate_toolset

    assert validate_toolset("specialist_test")
    assert set(resolve_toolset("specialist_test")) == {"specialist_test"}


def test_unknown_specialist_toolset_fails_before_spawn_side_effects(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "sol-builder"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - file\n    - kanban\n    - specialist_test\n    - unknown_escape\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setattr(kb, "workspaces_root", lambda board=None: tmp_path / "workspaces")
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: log_dir)
    popen_calls: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = kb.Task(
        id="t_auth", title="x", body=None, assignee="sol-builder", status="running",
        priority=0, created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=str(workspace), claim_lock="lock",
        claim_expires=None, tenant=None, current_run_id=1,
    )

    with pytest.raises(RuntimeError, match="unknown_escape"):
        kb._default_spawn(task, str(workspace), board="sol")
    assert popen_calls == []
    assert not log_dir.exists()


def test_builder_specialist_test_leaves_workspace_byte_identical(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path)
    test_file = tmp_path / "test_artifact.py"
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("OK\n", encoding="utf-8")
    test_file.write_text(
        "from pathlib import Path\n\ndef test_artifact():\n    assert Path('artifact.txt').read_text() == 'OK\\n'\n",
        encoding="utf-8",
    )
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    from tools.specialist_test_tool import _handle_specialist_test

    result = _handle_specialist_test({"targets": ["test_artifact.py"], "timeout": 30})
    after = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert result["exit_code"] == 0, result
    assert before == after
    assert not any(p.name in {"__pycache__", ".pytest_cache"} for p in tmp_path.rglob("*"))
    assert not any(p.suffix == ".pyc" for p in tmp_path.rglob("*"))


def test_finalizer_reconciles_absolute_and_relative_same_expected_path(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path)
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._turn_failed_file_mutations = {}
    agent._turn_file_mutation_paths = set()
    target = tmp_path / "artifact.txt"
    agent._record_file_mutation_result(
        "write_file", {"path": str(target), "content": "OK\n"},
        json.dumps({"error": "absolute path denied"}), is_error=True,
    )
    agent._record_file_mutation_result(
        "write_file", {"path": "artifact.txt", "content": "OK\n"},
        json.dumps({"bytes_written": 3, "files_modified": [str(target)]}), is_error=False,
    )

    assert agent._turn_failed_file_mutations == {}
    assert agent._turn_file_mutation_paths == {str(target)}


def test_finalizer_does_not_launder_unrelated_denied_path(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path)
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._turn_failed_file_mutations = {}
    agent._turn_file_mutation_paths = set()
    outside = tmp_path.parent / "escape.txt"
    target = tmp_path / "artifact.txt"
    agent._record_file_mutation_result(
        "write_file", {"path": str(outside), "content": "NO"},
        json.dumps({"error": "outside path denied"}), is_error=True,
    )
    agent._record_file_mutation_result(
        "write_file", {"path": "artifact.txt", "content": "OK\n"},
        json.dumps({"bytes_written": 3, "files_modified": [str(target)]}), is_error=False,
    )

    assert str(outside) in agent._turn_failed_file_mutations
    assert str(target) not in agent._turn_failed_file_mutations

