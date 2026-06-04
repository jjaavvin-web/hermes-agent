"""Command Center dashboard API — unified read-only operator snapshot.

Phase 1 aggregates existing dashboard layers instead of rebuilding their data
pipelines.  The endpoint is intentionally read-only: kanban boards remain the
source of truth and this module only projects them into a compact command
center shape.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from fastapi import APIRouter

from hermes_cli import kanban_db
from hermes_cli.pulse_data import _iter_kanban_dbs, _open_kanban_ro

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-command-center"])

_COMMAND_CENTER_TTL = 12.0
_STALE_HEARTBEAT_SECONDS = 2 * 60 * 60
_DECISION_STATUSES = {"blocked", "review"}
_STALLED_STATUSES = {"running", "in-progress", "in_progress"}
_COMMAND_CENTER_CACHE: tuple[dict, float] | None = None
_COMMAND_CENTER_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> int:
    return int(time.time())


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        return row[key]
    except Exception:
        return default


def _table_columns(conn) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    except Exception:
        return set()


def _select_task_rows(conn, *, statuses: set[str] | None = None) -> list[Any]:
    cols = _table_columns(conn)
    required = ["id", "title", "status"]
    optional = [
        "created_at",
        "started_at",
        "last_heartbeat_at",
        "worker_pid",
        "consecutive_failures",
        "last_failure_error",
    ]
    select_cols = [col for col in required if col in cols] + [col for col in optional if col in cols]
    if not all(col in cols for col in required):
        return []
    if statuses:
        ph = ",".join("?" * len(statuses))
        return conn.execute(
            f"SELECT {', '.join(select_cols)} FROM tasks WHERE status IN ({ph})",
            tuple(sorted(statuses)),
        ).fetchall()
    return conn.execute(f"SELECT {', '.join(select_cols)} FROM tasks").fetchall()


def _iter_task_rows(*, statuses: set[str] | None = None) -> Iterable[tuple[str, Any]]:
    for board_slug, db_path in _iter_kanban_dbs():
        try:
            conn = _open_kanban_ro(db_path)
        except Exception as exc:
            log.warning("Could not open kanban board %s at %s: %s", board_slug, db_path, exc)
            continue
        try:
            for row in _select_task_rows(conn, statuses=statuses):
                yield board_slug, row
        except Exception as exc:
            log.warning("Could not read kanban tasks for %s: %s", board_slug, exc)
        finally:
            conn.close()


def _projects_snapshot() -> list[dict]:
    from hermes_cli.dashboard_get_some import get_projects

    payload = get_projects()
    projects = payload.get("projects") if isinstance(payload, dict) else None
    return projects if isinstance(projects, list) else []


def _mission_snapshot() -> dict:
    from hermes_cli.dashboard_health import _get_snapshot

    payload = _get_snapshot()
    return payload if isinstance(payload, dict) else {}


def _live_snapshot() -> dict:
    try:
        mission = _mission_snapshot()
    except Exception as exc:
        log.warning("Mission Control snapshot unavailable: %s", exc)
        return {
            "scanned_at": _now_iso(),
            "runtimes": [],
            "active_sessions": [],
            "swarm": None,
            "nextCron": None,
            "spendToday": 0,
            "spendWeek": 0,
            "streakDays": 0,
            "model": None,
        }
    return {
        "scanned_at": _now_iso(),
        "runtimes": mission.get("runtimes") if isinstance(mission.get("runtimes"), list) else [],
        "active_sessions": mission.get("recentSessions") if isinstance(mission.get("recentSessions"), list) else [],
        "swarm": mission.get("swarm"),
        "nextCron": mission.get("nextCron"),
        "spendToday": mission.get("spendToday"),
        "spendWeek": mission.get("spendWeek"),
        "streakDays": mission.get("streakDays"),
        "model": mission.get("model"),
    }


def _git_health_snapshot() -> dict:
    from hermes_cli.dashboard_codex_sessions import git_health

    payload = git_health()
    return payload if isinstance(payload, dict) else {}


def _codex_sessions_snapshot() -> dict:
    from hermes_cli.dashboard_codex_sessions import _cached_snapshot

    payload = _cached_snapshot()
    return payload if isinstance(payload, dict) else {}


def _kanban_decisions() -> list[dict]:
    decisions: list[dict] = []
    for board_slug, row in _iter_task_rows(statuses=_DECISION_STATUSES):
        status = str(_row_get(row, "status") or "").strip() or "unknown"
        task_id = str(_row_get(row, "id") or "")
        title = str(_row_get(row, "title") or "Untitled").strip() or "Untitled"
        decisions.append({
            "title": title,
            "source": f"kanban:{board_slug}",
            "reason": status,
            "link_or_id": f"/kanban?board={quote(str(board_slug))}&task={quote(task_id)}" if task_id else str(board_slug),
        })
    return decisions


def _open_pr_state(state: Any, *, merged: bool = False) -> bool:
    if merged:
        return False
    normalized = str(state or "").strip().lower()
    if normalized in {"closed", "merged", "complete", "done"}:
        return False
    return True


def _pr_key(row: dict) -> str | None:
    number = row.get("pr_number")
    url = row.get("pr_url")
    if number:
        return f"number:{number}"
    if url:
        return f"url:{url}"
    return None


def _pr_decisions() -> list[dict]:
    try:
        git_health = _git_health_snapshot()
    except Exception as exc:
        log.warning("Git Health snapshot unavailable for Command Center: %s", exc)
        git_health = {}
    try:
        codex = _codex_sessions_snapshot()
    except Exception as exc:
        log.warning("Codex session snapshot unavailable for Command Center: %s", exc)
        codex = {}

    sessions_by_key: dict[str, dict] = {}
    for session in codex.get("sessions", []) if isinstance(codex.get("sessions"), list) else []:
        if not isinstance(session, dict):
            continue
        key = _pr_key(session)
        if key:
            sessions_by_key[key] = session

    seen: set[str] = set()
    decisions: list[dict] = []

    def add_pr(row: dict, fallback: dict | None = None) -> None:
        merged = bool(row.get("merged") or row.get("merged_at") or (fallback or {}).get("merged_at"))
        state = row.get("pr_state") or (fallback or {}).get("pr_state")
        if not _open_pr_state(state, merged=merged):
            return
        number = row.get("pr_number") or (fallback or {}).get("pr_number")
        url = row.get("pr_url") or (fallback or {}).get("pr_url")
        if not number and not url:
            return
        key = str(number or url)
        if key in seen:
            return
        seen.add(key)
        slug = row.get("slug") or (fallback or {}).get("isa_slug") or (fallback or {}).get("session_id") or "Tracked work"
        title = f"{slug} PR #{number}" if number else f"{slug} PR"
        decisions.append({
            "title": title,
            "source": "github",
            "reason": "pr awaiting review",
            "link_or_id": str(url or number),
        })

    for row in git_health.get("rows", []) if isinstance(git_health.get("rows"), list) else []:
        if not isinstance(row, dict):
            continue
        add_pr(row, sessions_by_key.get(_pr_key(row) or ""))

    for key, session in sessions_by_key.items():
        if key.replace("number:", "").replace("url:", "") in seen:
            continue
        add_pr(session)

    return decisions


def _decisions_snapshot() -> list[dict]:
    return _kanban_decisions() + _pr_decisions()


def _coerce_epoch(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _human_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "unknown"
    total = max(0, int(seconds))
    days, rem = divmod(total, 24 * 3600)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d" if hours == 0 else f"{days}d {hours}h"
    if hours:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    return f"{minutes}m"


def _pid_alive(pid: int | None) -> bool:
    return kanban_db._pid_alive(pid)


def _stalled_snapshot() -> list[dict]:
    now = _now_epoch()
    stalled: list[dict] = []
    for board_slug, row in _iter_task_rows(statuses=_STALLED_STATUSES):
        status = str(_row_get(row, "status") or "").strip() or "running"
        title = str(_row_get(row, "title") or "Untitled").strip() or "Untitled"
        heartbeat = _coerce_epoch(_row_get(row, "last_heartbeat_at"))
        started = _coerce_epoch(_row_get(row, "started_at")) or _coerce_epoch(_row_get(row, "created_at"))
        pid_raw = _row_get(row, "worker_pid")
        try:
            pid = int(pid_raw) if pid_raw not in (None, "") else None
        except (TypeError, ValueError):
            pid = None
        try:
            failures = int(_row_get(row, "consecutive_failures") or 0)
        except (TypeError, ValueError):
            failures = 0

        reasons: list[str] = []
        if heartbeat is not None and now - heartbeat > _STALE_HEARTBEAT_SECONDS:
            reasons.append("heartbeat stale")
        if pid is not None and not _pid_alive(pid):
            reasons.append("worker pid dead")
        elif pid is None:
            reasons.append("worker pid missing")
        if failures > 0:
            reasons.append(f"{failures} failure{'s' if failures != 1 else ''}")

        if not reasons:
            continue
        idle_since = heartbeat or started
        stalled.append({
            "title": title,
            "project": str(board_slug),
            "status": status,
            "idle_for": _human_duration(now - idle_since) if idle_since else "unknown",
            "why": "; ".join(reasons),
        })
    stalled.sort(key=lambda item: (item.get("project") or "", item.get("title") or ""))
    return stalled


def _hermes_binary() -> str:
    return shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")


def _resume_snapshot() -> dict | None:
    try:
        result = subprocess.run(
            [_hermes_binary(), "resume", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        log.warning("hermes resume --json unavailable for Command Center: %s", exc)
        return None
    if result.returncode != 0 or not (result.stdout or "").strip():
        if result.stderr:
            log.debug("hermes resume --json returned rc=%s: %s", result.returncode, result.stderr.strip())
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.warning("hermes resume --json returned invalid JSON: %s", exc)
        return None
    return payload if isinstance(payload, dict) else {"items": payload}


def _build_command_center_snapshot() -> dict:
    try:
        projects = _projects_snapshot()
    except Exception as exc:
        log.warning("Project roster unavailable for Command Center: %s", exc)
        projects = []
    return {
        "projects": projects,
        "live": _live_snapshot(),
        "decisions": _decisions_snapshot(),
        "stalled": _stalled_snapshot(),
        "resume": _resume_snapshot(),
    }


def _cached_command_center_snapshot() -> dict:
    global _COMMAND_CENTER_CACHE
    now = time.monotonic()
    with _COMMAND_CENTER_LOCK:
        if _COMMAND_CENTER_CACHE is not None:
            value, expires_at = _COMMAND_CENTER_CACHE
            if now < expires_at:
                return value
        snapshot = _build_command_center_snapshot()
        _COMMAND_CENTER_CACHE = (snapshot, now + _COMMAND_CENTER_TTL)
        return snapshot


@router.get("/command-center", summary="Unified Command Center dashboard snapshot")
def get_command_center() -> dict:
    return _cached_command_center_snapshot()
