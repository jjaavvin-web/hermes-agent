"""Read-only dashboard dangerous-action preflight reports.

This module intentionally does not issue action tickets and does not enforce
checks on existing mutating routes. It only computes a best-effort blast-radius
report for a requested dashboard action.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

ACTION_CLASSES: dict[str, str] = {
    "status-read": "read",
    "profile-config-write": "write-low",
    "cron-trigger": "write-high",
    "env-reveal": "write-high",
    "gateway-restart": "write-high",
    "hermes-update": "write-high",
    "sessions-bulk-delete": "destructive",
    "memory-reset": "destructive",
    "gateway-start": "write-high",
    "gateway-stop": "destructive",
    "codex-kill": "destructive",
    "codex-force-merge": "exec",
    "hook-create": "exec",
    "plugin-install": "exec",
    "mcp-stdio-add": "exec",
}

MACHINE_GLOBAL_ACTIONS = {
    "gateway-restart",
    "hermes-update",
    "gateway-start",
    "gateway-stop",
    "memory-reset",
}

GATEWAY_IMPACT_ACTIONS = {
    "gateway-restart",
    "gateway-start",
    "gateway-stop",
}

ROLLBACK_HINTS: dict[str, str] = {
    "status-read": "read-only status check; no rollback needed",
    "profile-config-write": "restore the previous profile config from backup or git/audit diff",
    "cron-trigger": "triggered runs cannot be un-fired; pause the job or inspect run logs if needed",
    "env-reveal": "secret reveal is non-reversible; rotate the credential if exposed",
    "gateway-restart": "gateway auto-restarts; sessions resume",
    "hermes-update": "rollback by resetting the source checkout to the previous commit, e.g. git reset --hard <prev>",
    "sessions-bulk-delete": "no automatic rollback — destructive",
    "memory-reset": "no automatic rollback — destructive",
    "gateway-start": "stop the gateway again if this was accidental",
    "gateway-stop": "start or restart the gateway to recover service",
    "codex-kill": "killed Codex sessions cannot be resumed automatically",
    "codex-force-merge": "review git history and revert/reset the merge commit if necessary",
    "hook-create": "remove the hook file/config entry and audit spawned commands",
    "plugin-install": "remove the plugin directory/config entry and restart only after approval",
    "mcp-stdio-add": "remove the MCP server entry and audit command provenance before reconnecting",
}

REQUIRED_KEYS = (
    "action",
    "target",
    "action_class",
    "scope",
    "active_sessions",
    "dirty_source",
    "cron_impact",
    "rollback_hint",
)

router = APIRouter(tags=["dashboard-action-preflight"])


def _count_active_sessions() -> int:
    """Return the same recent-active session count used by /api/status."""
    active_sessions = 0
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=50)
            now = time.time()
            active_sessions = sum(
                1
                for s in sessions
                if s.get("ended_at") is None
                and (now - s.get("last_active", s.get("started_at", 0))) < 300
            )
        finally:
            db.close()
    except Exception:
        pass
    return int(active_sessions)


def _has_tracked_dirty_source() -> bool:
    """Return True when tracked files have git status changes.

    Untracked files are deliberately ignored so screenshots, backups, and local
    scratch artifacts do not make the source tree look rollback-risky.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        if not line.startswith("??") and line[:2].strip():
            return True
    return False


def _cron_impact_for_gateway_action(action: str) -> int:
    """Best-effort count of enabled cron jobs affected by gateway stop/restart."""
    if action not in GATEWAY_IMPACT_ACTIONS:
        return 0
    try:
        from hermes_cli import web_server

        count = 0
        for item in web_server._cron_profile_dicts():  # type: ignore[attr-defined]
            name = str(item.get("name") or "")
            if not name:
                continue
            jobs = web_server._call_cron_for_profile(name, "list_jobs", True)  # type: ignore[attr-defined]
            for job in jobs or []:
                if isinstance(job, dict) and job.get("enabled", True):
                    count += 1
        return int(count)
    except Exception:
        return 0


def _scope_for_action(action: str, profile: str | None) -> str:
    if action in MACHINE_GLOBAL_ACTIONS:
        return "machine-global"
    selected = (profile or "default").strip() or "default"
    return f"profile:{selected}"


def compute_preflight(action: str, target: str | None, profile: str | None) -> dict[str, Any]:
    """Compute a read-only blast-radius report for a dashboard action."""
    normalized_action = (action or "unknown").strip() or "unknown"
    action_class = ACTION_CLASSES.get(normalized_action, "write-high")
    payload: dict[str, Any] = {
        "action": normalized_action,
        "target": target,
        "action_class": action_class,
        "scope": _scope_for_action(normalized_action, profile),
        "active_sessions": _count_active_sessions(),
        "dirty_source": _has_tracked_dirty_source(),
        "cron_impact": _cron_impact_for_gateway_action(normalized_action),
        "rollback_hint": ROLLBACK_HINTS.get(
            normalized_action,
            "unknown action; require review before mutation",
        ),
    }
    return payload


@router.get("/api/dashboard/action-preflight")
def action_preflight(action: str, target: str | None = None, profile: str | None = None) -> dict[str, Any]:
    """Return a read-only dangerous-action blast-radius report."""
    return compute_preflight(action, target, profile)


def register(app: Any) -> None:
    """Mount the read-only dashboard action preflight router."""
    app.include_router(router)
