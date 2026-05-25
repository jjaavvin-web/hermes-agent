"""Codex sessions dashboard API — live read of the Codex parallel workflow.

Mount point: add to hermes_cli/web_server.py:

    from hermes_cli.dashboard_codex_sessions import router as codex_router
    app.include_router(codex_router)

All endpoints require ``X-Hermes-Session-Token`` validated by existing
middleware (same auth as ``dashboard_health.py``).

Routes (all under ``/api/dashboard/codex-sessions``):

- ``GET ./``                — snapshot of all tracked sessions + counts
- ``GET ./{sid}``           — detail (ISA verbatim, diff, review history)
- ``GET ./{sid}/log``       — agent.log tail filtered to this thread
- ``POST ./{sid}/pause``    — set ``paused`` flag (non-destructive)
- ``POST ./{sid}/resume``   — clear ``paused`` flag (non-destructive)
- ``POST ./{sid}/kill``     — release worktree + drop row.  Requires
                              ``{"confirm": "KILL_CODEX_SESSION"}``.
- ``POST ./{sid}/force-merge``  Requires ``{"confirm":
                              "FORCE_MERGE_CODEX_SESSION"}``.

Cache: 15s TTL on the snapshot endpoint, mirroring
``dashboard_health.py``'s ``_HIVES_TTL`` (same operator surface
shape).
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-codex-sessions"])

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
_SESSIONS_PATH = HERMES_HOME / "codex_sessions.json"
_REVIEW_STATE_PATH = HERMES_HOME / "codex-review-state.json"
_PORTS_PATH = HERMES_HOME / "codex-ports.json"
_AGENT_LOG_PATH = HERMES_HOME / "logs" / "agent.log"

_SNAPSHOT_TTL = 15.0
_SNAPSHOT_CACHE: tuple[dict, float] | None = None
_SNAPSHOT_LOCK = threading.Lock()

_KILL_TOKEN = "KILL_CODEX_SESSION"
_FORCE_MERGE_TOKEN = "FORCE_MERGE_CODEX_SESSION"
_LOG_TAIL_DEFAULT = 200
_DIFF_MAX_BYTES = 200 * 1024


# ── helpers ────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _build_snapshot() -> dict:
    """One snapshot of all tracked codex sessions + counts."""
    sessions_file = _load_json(_SESSIONS_PATH)
    rows = sessions_file.get("sessions", {})
    review_state = _load_json(_REVIEW_STATE_PATH).get("sessions", {})
    ports = _load_json(_PORTS_PATH)
    claimed_ports = sum(1 for v in ports.values() if v)

    sessions = []
    state_counts: dict[str, int] = {}
    for thread_id, row in rows.items():
        sid = row.get("session_id", "")
        review = review_state.get(sid, {})
        wt_path = row.get("worktree_path", "")
        wt_alive = Path(wt_path).is_dir() if wt_path else False
        sessions.append({
            "thread_id": thread_id,
            "session_id": sid,
            "state": row.get("state"),
            "paused": bool(row.get("paused")),
            "isa_id": row.get("isa_id"),
            "isa_phase": row.get("isa_phase"),
            "worktree_path": wt_path,
            "worktree_alive": wt_alive,
            "port": row.get("port"),
            "channel_id": row.get("channel_id"),
            "last_message_at": row.get("last_message_at"),
            "review_iterations": int(review.get("iterations", 0)),
            "reviews_today": int(review.get("reviews_today", 0)),
            "last_verdict": review.get("last_verdict"),
            "last_review_at": review.get("last_review_at"),
            "created_at": row.get("created_at"),
        })
        state = row.get("state") or "UNKNOWN"
        state_counts[state] = state_counts.get(state, 0) + 1

    return {
        "scanned_at": _now_iso(),
        "sessions": sessions,
        "counts": {
            "total": len(sessions),
            "by_state": state_counts,
            "ports_claimed": claimed_ports,
            "ports_free": 8 - claimed_ports,
        },
        "review_pool": {
            "size": 2,                # P2 default
            "daily_cap_per_sid": 10,  # P2 default
            "iteration_cap": 3,       # P2 default
        },
    }


def _cached_snapshot() -> dict:
    global _SNAPSHOT_CACHE
    now = time.monotonic()
    with _SNAPSHOT_LOCK:
        if _SNAPSHOT_CACHE is not None:
            value, expires_at = _SNAPSHOT_CACHE
            if now < expires_at:
                return value
        snap = _build_snapshot()
        _SNAPSHOT_CACHE = (snap, now + _SNAPSHOT_TTL)
        return snap


def _invalidate_snapshot() -> None:
    global _SNAPSHOT_CACHE
    with _SNAPSHOT_LOCK:
        _SNAPSHOT_CACHE = None


def _find_thread_for_sid(sid: str) -> tuple[Optional[str], Optional[dict]]:
    sessions_file = _load_json(_SESSIONS_PATH)
    for thread_id, row in sessions_file.get("sessions", {}).items():
        if row.get("session_id") == sid:
            return thread_id, row
    return None, None


def _persist_sessions(state: dict) -> None:
    _SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SESSIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_SESSIONS_PATH)
    _invalidate_snapshot()


def _collect_diff(worktree_path: str) -> tuple[str, bool]:
    """git diff origin/main...HEAD inside the worktree, truncated to 200KB."""
    if not worktree_path or not Path(worktree_path).is_dir():
        return "<worktree missing>", False
    try:
        result = subprocess.run(
            ["git", "-C", worktree_path, "diff", "origin/main...HEAD"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "<git diff timed out>", False
    out = result.stdout or ""
    truncated = False
    if len(out.encode("utf-8")) > _DIFF_MAX_BYTES:
        out = out.encode("utf-8")[:_DIFF_MAX_BYTES].decode("utf-8", errors="replace")
        truncated = True
    return out, truncated


# ── routes ─────────────────────────────────────────────────────────────


@router.get("/codex-sessions", summary="Snapshot of all codex sessions")
def get_snapshot():
    return _cached_snapshot()


@router.get("/codex-sessions/{sid}", summary="Detail for one codex session")
def get_detail(sid: str):
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    isa_path = Path(row.get("isa_path", ""))
    try:
        isa_text = isa_path.read_text(encoding="utf-8") if isa_path.exists() else ""
    except OSError as exc:
        isa_text = f"<could not read ISA: {exc}>"
    diff, diff_truncated = _collect_diff(row.get("worktree_path", ""))
    review_state = _load_json(_REVIEW_STATE_PATH).get("sessions", {}).get(sid, {})
    return {
        "thread_id": thread_id,
        "session_id": sid,
        "row": row,
        "isa_verbatim": isa_text,
        "current_diff": diff,
        "diff_truncated": diff_truncated,
        "review_state": review_state,
    }


@router.get("/codex-sessions/{sid}/log", summary="Recent agent.log lines for this thread")
def get_log(sid: str, tail: int = _LOG_TAIL_DEFAULT):
    thread_id, _ = _find_thread_for_sid(sid)
    if thread_id is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    if not _AGENT_LOG_PATH.exists():
        return {"sid": sid, "lines": []}
    needle = f"chat={thread_id}"
    try:
        with open(_AGENT_LOG_PATH, "r", encoding="utf-8", errors="replace") as fd:
            matching = [line.rstrip("\n") for line in fd if needle in line]
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"log read failed: {exc}") from exc
    return {"sid": sid, "lines": matching[-tail:]}


@router.post("/codex-sessions/{sid}/pause", summary="Pause a session (non-destructive)")
def post_pause(sid: str):
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    sessions_file = _load_json(_SESSIONS_PATH)
    sessions_file["sessions"][thread_id]["paused"] = True
    _persist_sessions(sessions_file)
    return {"ok": True, "sid": sid, "paused": True}


@router.post("/codex-sessions/{sid}/resume", summary="Resume a paused session")
def post_resume(sid: str):
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    sessions_file = _load_json(_SESSIONS_PATH)
    sessions_file["sessions"][thread_id]["paused"] = False
    _persist_sessions(sessions_file)
    return {"ok": True, "sid": sid, "paused": False}


@router.post("/codex-sessions/{sid}/kill", summary="Release worktree + drop row (destructive)")
def post_kill(sid: str, body: dict = Body(default_factory=dict)):
    confirm = (body or {}).get("confirm", "")
    if confirm != _KILL_TOKEN:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "confirm token required",
                "expected": _KILL_TOKEN,
                "example": {"confirm": _KILL_TOKEN},
            },
        )
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")

    # Best-effort worktree removal — use git worktree remove --force so
    # untracked files don't block the cleanup.
    wt_path = row.get("worktree_path", "")
    if wt_path and Path(wt_path).exists():
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt_path],
                capture_output=True, text=True, check=False, timeout=30,
            )
        except subprocess.TimeoutExpired:
            log.warning("post_kill: worktree remove timed out for %s", sid)

    sessions_file = _load_json(_SESSIONS_PATH)
    sessions_file["sessions"].pop(thread_id, None)
    _persist_sessions(sessions_file)
    return {"ok": True, "sid": sid, "released_worktree": wt_path}


@router.post("/codex-sessions/{sid}/force-merge", summary="Force-merge a session (destructive)")
def post_force_merge(sid: str, body: dict = Body(default_factory=dict)):
    confirm = (body or {}).get("confirm", "")
    if confirm != _FORCE_MERGE_TOKEN:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "confirm token required",
                "expected": _FORCE_MERGE_TOKEN,
                "example": {"confirm": _FORCE_MERGE_TOKEN},
            },
        )
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    # P4 emits an "intent" record; the actual merge runs out-of-band via
    # the same MergeBroker the dispatcher uses on APPROVE.  Putting the
    # merge in the request path would let a slow GitHub or rebase
    # conflict block the dashboard; instead the operator's intent is
    # logged + the row's state flips to MERGING for the dispatcher's
    # next tick to action.
    sessions_file = _load_json(_SESSIONS_PATH)
    sessions_file["sessions"][thread_id]["state"] = "MERGING"
    sessions_file["sessions"][thread_id]["force_merge_requested_at"] = _now_iso()
    _persist_sessions(sessions_file)
    return {
        "ok": True,
        "sid": sid,
        "state": "MERGING",
        "note": "dispatcher will pick up next tick",
    }
