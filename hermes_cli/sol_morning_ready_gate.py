#!/usr/bin/env python3
"""Fail-closed Sol morning Triage -> READY gate for detailed /goal packets.

This is a local operator tool. It validates one selected Sol triage recommendation
and, only with --apply, atomically archives that source recommendation and creates
a fresh executable READY card with immutable provenance and goal-mode metadata.
It never dispatches a worker and refuses when another READY/RUNNING card exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
SOL_DB = HERMES_HOME / "kanban" / "boards" / "sol" / "kanban.db"
SOURCE_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_HEADINGS = (
    "DECISION",
    "OBJECTIVE",
    "STARTING STATE",
    "SCOPE",
    "WORKSPACE",
    "EXECUTION STEPS",
    "SUCCESS CRITERIA",
    "FAILURE CRITERIA",
    "FAILURE RESPONSE",
    "VALIDATION",
    "BUDGET",
    "STOP GATES",
    "DISPATCH HANDOFF",
)
REQUIRED_PHRASES = (
    "GOAL_PACKET_V1",
    "Hermes verdict: APPROVE",
    "Fable verdict: APPROVE",
    "Goal mode: true",
    "Worker/profile selection: DEFERRED TO MOTHERSHIP",
    "Dispatch authorization: NOT",
    "Expected terminal state: DONE or BLOCKED",
    "Required final report:",
)


def section(body: str, name: str) -> str:
    pattern = re.compile(
        rf"(?ms)^{re.escape(name)}\s*$\n(.*?)(?=^[A-Z][A-Z /_-]+\s*$|\Z)"
    )
    m = pattern.search(body)
    return m.group(1).strip() if m else ""


def int_field(body: str, label: str) -> int | None:
    m = re.search(rf"(?mi)^-\s*{re.escape(label)}:\s*(\d+)\s*$", body)
    return int(m.group(1)) if m else None


def _resolve_verifier_profile(name: str) -> bool:
    """Resolve a verifier through the production profile path, fail closed."""
    sys.path.insert(0, str(SOURCE_ROOT))
    from hermes_cli.profiles import profile_exists, resolve_profile_env

    if not profile_exists(name):
        return False
    profile_home = Path(resolve_profile_env(name)).expanduser()
    return profile_home.is_dir() and (profile_home / "config.yaml").is_file()


def validate(task: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    body = task["body"] or ""
    errors: list[dict[str, str]] = []

    def err(code: str, message: str) -> None:
        errors.append({"code": code, "message": message})

    if task["status"] != "triage":
        err("not_triage", f"card status is {task['status']!r}, expected 'triage'")
    if task["id"] == "t_53cbf8cc":
        err("root_card_forbidden", "the permanent Sol root ledger cannot enter READY")
    if task["assignee"]:
        err("assignee_must_be_deferred", "assignee must remain empty until MOTHERSHIP selects the worker/profile")

    for heading in REQUIRED_HEADINGS:
        content = section(body, heading)
        if not content:
            err("missing_section", heading)
    for phrase in REQUIRED_PHRASES:
        if phrase not in body:
            err("missing_phrase", phrase)
    if "<placeholder>" in body or re.search(r"<[^>]+>", body):
        err("unresolved_placeholder", "packet contains angle-bracket placeholders")

    success = section(body, "SUCCESS CRITERIA")
    failure = section(body, "FAILURE CRITERIA")
    if len(re.findall(r"(?m)^\d+\.\s+\S", success)) < 3:
        err("weak_success_criteria", "SUCCESS CRITERIA needs at least 3 numbered binary checks")
    if len(re.findall(r"(?m)^\d+\.\s+\S", failure)) < 3:
        err("weak_failure_criteria", "FAILURE CRITERIA needs at least 3 numbered stop conditions")
    if "exactly one answerable question" not in section(body, "FAILURE RESPONSE"):
        err("failure_response_not_bounded", "FAILURE RESPONSE must require exactly one answerable question")

    max_turns = int_field(body, "Max turns")
    max_runtime = int_field(body, "Max runtime seconds")
    # The runtime field is a failure threshold, not a retry count:
    # threshold=1 means zero retries, threshold=2 means one retry.
    max_failures = int_field(body, "Max failures before BLOCKED")
    if max_turns is None or not 1 <= max_turns <= 50:
        err("invalid_max_turns", "Max turns must be 1..50")
    if max_runtime is None or not 60 <= max_runtime <= 14400:
        err("invalid_max_runtime", "Max runtime seconds must be 60..14400")
    if max_failures is None or not 1 <= max_failures <= 3:
        err(
            "invalid_max_failures",
            "Max failures before BLOCKED must be 1..3 (1 means zero retries)",
        )

    evidence_match = re.search(r"(?mi)^-\s*Evidence directory:\s*(\S+)\s*$", body)
    report_match = re.search(r"(?mi)^-\s*Required final report:\s*(\S+)\s*$", body)
    if not evidence_match:
        err("missing_evidence_path", "Evidence directory is required")
    else:
        ev = Path(os.path.expanduser(evidence_match.group(1))).resolve()
        prefix = (HERMES_HOME / "audits").resolve()
        if prefix not in ev.parents or ev == prefix:
            err("unsafe_evidence_path", f"evidence path must be task-scoped under {prefix}")
    if not report_match:
        err("invalid_final_report", "Required final report is required")
    else:
        report = Path(os.path.expanduser(report_match.group(1))).resolve()
        if not evidence_match:
            err("invalid_final_report", "Required final report needs a valid Evidence directory")
        else:
            ev = Path(os.path.expanduser(evidence_match.group(1))).resolve()
            if report.name != "FINAL-REPORT.md" or report.parent != ev:
                err(
                    "invalid_final_report",
                    "Required final report must be FINAL-REPORT.md directly under the declared Evidence directory",
                )

    active = conn.execute(
        "SELECT id, status FROM tasks WHERE status IN ('ready','running') AND id != ? ORDER BY id",
        (task["id"],),
    ).fetchall()
    if active:
        err("wip_not_clear", "other READY/RUNNING cards exist: " + ", ".join(f"{r['id']}:{r['status']}" for r in active))

    # Reuse Hermes's native fail-closed ready-spec compiler.
    sys.path.insert(0, str(SOURCE_ROOT))
    try:
        from hermes_cli.ready_spec import validate_ready_spec
        workspace = task["workspace_path"] or task["workspace_kind"]
        native = validate_ready_spec(
            {"id": task["id"], "board": "sol", "body": body, "workspace": workspace},
            resolved_workspace=workspace,
            board_policy={
                "audits_prefix": str(HERMES_HOME / "audits"),
                "repo_root": str(SOURCE_ROOT),
                "default_verifier": "default",
                "verifier_resolver": _resolve_verifier_profile,
            },
        )
        if not native.ok:
            for e in native.errors:
                err("native_ready_spec:" + e["code"], e["message"])
    except Exception as exc:
        err("native_validator_error", repr(exc))

    return {
        "ok": not errors,
        "task_id": task["id"],
        "status": task["status"],
        "max_turns": max_turns,
        "max_runtime_seconds": max_runtime,
        "max_failures_before_blocked": max_failures,
        "packet_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "errors": errors,
    }


def apply(conn: sqlite3.Connection, task: sqlite3.Row, result: dict) -> str:
    """Archive the selected recommendation and create one fresh READY card."""
    sys.path.insert(0, str(SOURCE_ROOT))
    from hermes_cli import kanban_db as kb

    now = int(time.time())
    source_id = task["id"]
    provenance = {
        "source_board": "sol",
        "source_task_id": source_id,
        "source_status": "triage",
        "packet_sha256": result["packet_sha256"],
        "selected_by": "Hermes+Fable",
    }
    payload = {
        "actor": "MOTHERSHIP",
        "reason": "morning Hermes+Fable plan approved; detailed GOAL_PACKET_V1 passed fail-closed validation",
        "goal_mode": True,
        "goal_max_turns": result["max_turns"],
        "max_runtime_seconds": result["max_runtime_seconds"],
        "max_retries": result["max_failures_before_blocked"],
        "packet_sha256": result["packet_sha256"],
        "dispatch_authorized": False,
        "source_task_id": source_id,
    }
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute(
            "SELECT * FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()
        if current is None:
            raise RuntimeError("source card changed during promotion; no mutation committed")
        if current["body"] != task["body"]:
            raise RuntimeError("source packet changed after validation; no mutation committed")
        execution_fields = (
            "title",
            "assignee",
            "status",
            "priority",
            "workspace_kind",
            "workspace_path",
            "branch_name",
            "tenant",
        )
        if any(current[field] != task[field] for field in execution_fields):
            raise RuntimeError(
                "source execution metadata changed after validation; no mutation committed"
            )
        current_result = validate(current, conn)
        if not current_result["ok"]:
            codes = [error["code"] for error in current_result["errors"]]
            if "wip_not_clear" in codes:
                raise RuntimeError(
                    "WIP=1 gate closed after validation; no mutation committed"
                )
            raise RuntimeError(
                "source card no longer validates during promotion; no mutation committed: "
                + ", ".join(codes)
            )
        if current_result["packet_sha256"] != result["packet_sha256"]:
            raise RuntimeError("source packet changed after validation; no mutation committed")
        active = conn.execute(
            "SELECT id,status FROM tasks WHERE status IN ('ready','running') ORDER BY id"
        ).fetchall()
        if active:
            raise RuntimeError(
                "WIP=1 gate closed after validation; active card(s): "
                + ", ".join(f"{row['id']}:{row['status']}" for row in active)
            )
        archived = conn.execute(
            "UPDATE tasks SET status='archived' WHERE id=? AND status='triage' AND assignee IS NULL",
            (source_id,),
        )
        if archived.rowcount != 1:
            raise RuntimeError("source card changed during promotion; no mutation committed")
        conn.execute(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
            (
                source_id,
                "morning_recommendation_selected",
                json.dumps(payload, sort_keys=True),
                now,
            ),
        )

        executable_id = kb._new_task_id()
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, body, assignee, status, priority,
                created_by, created_at, workspace_kind, workspace_path,
                branch_name, tenant, max_runtime_seconds,
                max_retries, goal_mode, goal_max_turns
            ) VALUES (?, ?, ?, NULL, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                executable_id,
                current["title"],
                current["body"],
                int(current["priority"] or 0),
                "MOTHERSHIP",
                now,
                current["workspace_kind"],
                current["workspace_path"],
                current["branch_name"],
                current["tenant"],
                result["max_runtime_seconds"],
                result["max_failures_before_blocked"],
                result["max_turns"],
            ),
        )
        kb._append_event(
            conn,
            executable_id,
            "created",
            {
                "assignee": None,
                "status": "ready",
                "parents": [],
                "tenant": current["tenant"],
                "branch_name": current["branch_name"],
                "skills": None,
                "goal_mode": True,
                "provenance": provenance,
            },
        )
        conn.execute(
            "INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)",
            (
                executable_id,
                "MOTHERSHIP",
                "READY GATE PASS: fresh executable card created from archived Sol Triage "
                f"recommendation {source_id}; packet_sha256={result['packet_sha256']}; "
                f"goal turns={result['max_turns']}; runtime={result['max_runtime_seconds']}s; "
                f"failure threshold={result['max_failures_before_blocked']} "
                "(1 means zero retries). Worker/profile selection and dispatch "
                "remain deferred to MOTHERSHIP.",
                now,
            ),
        )
        conn.execute(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
            (
                executable_id,
                "morning_ready_gate_passed",
                json.dumps(payload, sort_keys=True),
                now,
            ),
        )
        conn.commit()
        return executable_id
    except Exception:
        conn.rollback()
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id")
    ap.add_argument("--db", default=str(SOL_DB))
    ap.add_argument("--apply", action="store_true", help="promote after validation; default is dry-run")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(SOURCE_ROOT))
    from hermes_cli import kanban_db as kb

    requested_db = Path(args.db).expanduser().resolve()
    expected_db = Path(SOL_DB).expanduser().resolve()
    if requested_db != expected_db:
        result = {
            "ok": False,
            "task_id": args.task_id,
            "applied": False,
            "errors": [{
                "code": "wrong_board_db",
                "message": f"--db must resolve to the Sol board database: {expected_db}",
            }],
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("FAIL", args.task_id)
            print(f"- wrong_board_db: {result['errors'][0]['message']}")
        return 1

    kb.init_db(board="sol")
    conn = kb._sqlite_connect(requested_db)
    conn.row_factory = sqlite3.Row
    try:
        try:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (args.task_id,)).fetchone()
            if task is None:
                result = {"ok": False, "task_id": args.task_id, "errors": [{"code": "not_found", "message": "task not found"}]}
            else:
                result = validate(task, conn)
                if args.apply and result["ok"]:
                    executable_id = apply(conn, task, result)
                    result["applied"] = True
                    result["source_status"] = "archived"
                    result["executable_task_id"] = executable_id
                    result["new_status"] = "ready"
                else:
                    result["applied"] = False
        except Exception as exc:
            result = {
                "ok": False,
                "task_id": args.task_id,
                "applied": False,
                "errors": [{"code": "apply_error", "message": str(exc)}],
            }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("PASS" if result["ok"] else "FAIL", args.task_id)
            for e in result.get("errors", []):
                print(f"- {e['code']}: {e['message']}")
        return 0 if result["ok"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
