from __future__ import annotations

import json
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
import hermes_cli.sol_morning_ready_gate as gate


def _load_gate():
    return gate


def _packet(evidence: str) -> str:
    return textwrap.dedent(
        f"""\
        GOAL_PACKET_V1
        DECISION
        Hermes verdict: APPROVE
        Fable verdict: APPROVE
        OBJECTIVE
        Prove one bounded executable card.
        STARTING STATE
        One selected Sol Triage recommendation.
        SCOPE
        Safe local lifecycle pilot only.
        WORKSPACE
        scratch
        EXECUTION STEPS
        1. Claim once.
        2. Record progress.
        3. Verify and close.
        SUCCESS CRITERIA
        1. Exactly one claim succeeds.
        2. Verification exits zero.
        3. Evidence exists before DONE.
        FAILURE CRITERIA
        1. More than one claim succeeds.
        2. Verification exits non-zero.
        3. Evidence is absent.
        FAILURE RESPONSE
        Stop with exactly one answerable question.
        VALIDATION
        Rerun the canonical verifier before DONE.
        BUDGET
        - Max turns: 8
        - Max runtime seconds: 600
        - Max failures before BLOCKED: 2
        STOP GATES
        No config, service, provider, credential, public, or dispatch activation changes.
        DISPATCH HANDOFF
        Goal mode: true
        Worker/profile selection: DEFERRED TO MOTHERSHIP
        Dispatch authorization: NOT ***
        Expected terminal state: DONE or BLOCKED
        - Evidence directory: {evidence}
        - Required final report: {evidence}/FINAL-REPORT.md

        ```ready-spec
        scope: prove one bounded Sol executable card lifecycle
        allowed_workspace: scratch
        evidence_path: {evidence}/
        stop_gates: [config, auth, service, git-push]
        verifier: default
        ```
        """
    )


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    home.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(board="sol")
    gate = _load_gate()
    setattr(gate, "HERMES_HOME", home)
    setattr(gate, "SOL_DB", kb.kanban_db_path(board="sol"))
    return gate, home


def _row(conn: sqlite3.Connection, tid: str):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def test_valid_packet_archives_recommendation_and_creates_fresh_ready_with_provenance(gate_env):
    gate, home = gate_env
    evidence = home / "audits" / "pilot-valid"
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn,
            title="selected recommendation",
            body=_packet(str(evidence)),
            triage=True,
            created_by="Sol",
            board="sol",
        )
        source = _row(conn, source_id)
        result = gate.validate(source, conn)
        assert result["ok"] is True, result["errors"]
        executable_id = gate.apply(conn, source, result)

        assert executable_id != source_id
        assert _row(conn, source_id)["status"] == "archived"
        executable = _row(conn, executable_id)
        assert executable["status"] == "ready"
        assert executable["goal_mode"] == 1
        assert executable["assignee"] is None
        assert executable["max_retries"] == 2  # failure threshold: one retry allowed
        created = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='created'",
            (executable_id,),
        ).fetchone()
        provenance = json.loads(created["payload"])["provenance"]
        assert provenance["source_task_id"] == source_id
        assert provenance["packet_sha256"] == result["packet_sha256"]


def test_invalid_packet_never_archives_or_creates_ready(gate_env):
    gate, _home = gate_env
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn,
            title="underspecified recommendation",
            body="GOAL_PACKET_V1\nOBJECTIVE\nToo vague.",
            triage=True,
            created_by="Sol",
            board="sol",
        )
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        result = gate.validate(_row(conn, source_id), conn)
        assert result["ok"] is False
        assert any(e["code"] == "missing_section" for e in result["errors"])
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert after == before
        assert _row(conn, source_id)["status"] == "triage"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE status='ready'").fetchone()[0] == 0


def test_apply_rechecks_wip_and_rolls_back_if_active_card_appears(gate_env):
    gate, home = gate_env
    evidence = home / "audits" / "wip-race"
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn,
            title="selected recommendation",
            body=_packet(str(evidence)),
            triage=True,
            created_by="Sol",
            board="sol",
        )
        source = _row(conn, source_id)
        result = gate.validate(source, conn)
        assert result["ok"] is True
        active_id = kb.create_task(
            conn, title="raced active card", created_by="other", board="sol"
        )
        with pytest.raises(RuntimeError, match="WIP=1 gate closed"):
            gate.apply(conn, source, result)
        assert _row(conn, source_id)["status"] == "triage"
        assert _row(conn, active_id)["status"] == "ready"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_apply_rechecks_packet_hash_and_rolls_back_on_stale_validation(gate_env):
    gate, home = gate_env
    evidence = home / "audits" / "packet-race"
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn,
            title="selected recommendation",
            body=_packet(str(evidence)),
            triage=True,
            created_by="Sol",
            board="sol",
        )
        source = _row(conn, source_id)
        result = gate.validate(source, conn)
        assert result["ok"] is True
        conn.execute("UPDATE tasks SET body=body || '\nchanged' WHERE id=?", (source_id,))
        with pytest.raises(RuntimeError, match="packet changed after validation"):
            gate.apply(conn, source, result)
        assert _row(conn, source_id)["status"] == "triage"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "changed title"),
        ("priority", 9),
        ("workspace_kind", "dir"),
        ("workspace_path", "/tmp/changed"),
        ("branch_name", "changed/branch"),
        ("tenant", "changed-tenant"),
    ],
)
def test_apply_rejects_execution_metadata_races(gate_env, field, value):
    gate, home = gate_env
    evidence = home / "audits" / f"metadata-race-{field}"
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn,
            title="selected recommendation",
            body=_packet(str(evidence)),
            triage=True,
            created_by="Sol",
            workspace_kind="scratch",
            board="sol",
        )
        source = _row(conn, source_id)
        result = gate.validate(source, conn)
        assert result["ok"] is True
        conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, source_id))
        with pytest.raises(RuntimeError, match="execution metadata changed"):
            gate.apply(conn, source, result)
        assert _row(conn, source_id)["status"] == "triage"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_missing_default_verifier_config_fails_closed(gate_env):
    gate, home = gate_env
    home.joinpath("config.yaml").unlink()
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn,
            title="missing default verifier",
            body=_packet(str(home / "audits" / "missing-verifier")),
            triage=True,
            created_by="Sol",
            board="sol",
        )
        result = gate.validate(_row(conn, source_id), conn)
        assert result["ok"] is False
        assert any("verifier_unresolved" in e["code"] for e in result["errors"])


def test_verifier_resolver_exception_fails_closed(gate_env, monkeypatch):
    gate, home = gate_env
    monkeypatch.setattr(
        gate,
        "_resolve_verifier_profile",
        lambda _name: (_ for _ in ()).throw(RuntimeError("resolver boom")),
    )
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn,
            title="resolver exception",
            body=_packet(str(home / "audits" / "resolver-exception")),
            triage=True,
            created_by="Sol",
            board="sol",
        )
        result = gate.validate(_row(conn, source_id), conn)
        assert result["ok"] is False
        assert any("verifier_unresolved" in e["code"] for e in result["errors"])


def test_retry_vocabulary_matches_runtime_failure_threshold(gate_env):
    gate, home = gate_env
    body = _packet(str(home / "audits" / "retry-semantics")).replace(
        "Max failures before BLOCKED: 2",
        "Retry limit: 1",
    )
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn, title="old retry vocabulary", body=body, triage=True, created_by="Sol", board="sol"
        )
        result = gate.validate(_row(conn, source_id), conn)
        assert result["ok"] is False
        assert any(e["code"] == "invalid_max_failures" for e in result["errors"])


def test_unresolved_verifier_fails_closed(gate_env):
    gate, home = gate_env
    body = _packet(str(home / "audits" / "bad-verifier")).replace(
        "verifier: default", "verifier: ghost-profile"
    )
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn, title="bad verifier", body=body, triage=True, created_by="Sol", board="sol"
        )
        result = gate.validate(_row(conn, source_id), conn)
        assert result["ok"] is False
        assert any("verifier_unresolved" in e["code"] for e in result["errors"])
        assert _row(conn, source_id)["status"] == "triage"


def test_final_report_must_be_directly_under_declared_evidence(gate_env):
    gate, home = gate_env
    evidence = home / "audits" / "confined-report"
    body = _packet(str(evidence)).replace(
        f"{evidence}/FINAL-REPORT.md", f"{home}/.ssh/FINAL-REPORT.md"
    )
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn, title="unsafe report", body=body, triage=True, created_by="Sol", board="sol"
        )
        result = gate.validate(_row(conn, source_id), conn)
        assert result["ok"] is False
        assert any(e["code"] == "invalid_final_report" for e in result["errors"])


def test_main_rejects_non_sol_db_with_structured_json(gate_env, tmp_path, monkeypatch, capsys):
    gate, _home = gate_env
    wrong_db = tmp_path / "other.db"
    sqlite3.connect(wrong_db).close()
    monkeypatch.setattr(sys, "argv", ["gate", "t_missing", "--db", str(wrong_db), "--json"])
    assert gate.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "wrong_board_db"


def test_main_apply_race_returns_structured_json(gate_env, monkeypatch, capsys):
    gate, home = gate_env
    evidence = home / "audits" / "main-race"
    with kb.connect(board="sol") as conn:
        source_id = kb.create_task(
            conn, title="main race", body=_packet(str(evidence)), triage=True,
            created_by="Sol", board="sol",
        )
    monkeypatch.setattr(
        gate, "apply", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("race"))
    )
    monkeypatch.setattr(
        sys, "argv", ["gate", source_id, "--db", str(gate.SOL_DB), "--apply", "--json"]
    )
    assert gate.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["applied"] is False
    assert payload["errors"] == [{"code": "apply_error", "message": "race"}]

