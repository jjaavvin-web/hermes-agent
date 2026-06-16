"""Learning loop dashboard API (staged 2026-06-15).

GET /api/dashboard/learning

Reads JSON state files only; no database access in the dashboard request path.
Never raises a 500 for missing/malformed learning-loop artifacts.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

from fastapi import APIRouter

from hermes_constants import get_hermes_home

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-learning"])

_TTL_SECONDS = 20.0
_CACHE: tuple[dict[str, Any], float] | None = None
_LOCK = threading.Lock()


class RecallFiltersLatest(TypedDict, total=False):
    include_quarantine: bool
    exclude_auto_bridged: bool
    effective: str
    excluded_counts: dict[str, int | None]


class WeeklyHygieneLatest(TypedDict, total=False):
    lesson_completion_ratio: float | int | None
    embedding_coverage: Any
    stuck_ready_count: int | None
    ts: str | None
    dup_rate: float | int | None


class DistillerState(TypedDict, total=False):
    pending: int
    approved: int
    rejected: int
    oldest_pending_ts: str | None
    last_promotion_ts: str | None
    stale_count: int | None


class LoopCritic(TypedDict, total=False):
    status: Literal["PASS", "FAIL"] | str
    hard_failures: int | None
    checks: dict[str, bool]
    quarantine_count_last_7d: int | None
    next_run_ts: str | None


class CanaryHonesty(TypedDict, total=False):
    embed_present: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths() -> dict[str, Path]:
    home = get_hermes_home()
    state = home / "state"
    return {
        "index_snapshot": state / "learning-index" / "snapshot-latest.json",
        "index_history": state / "learning-index" / "history.jsonl",
        "canary_result": state / "learning-canary" / "result-latest.json",
        "distiller_queue": state / "distiller-queue.jsonl",
        "distiller_inbox": state / "distiller-inbox-latest.md",
        "weekly_hygiene_latest": state / "weekly-hygiene",
        "critic_latest": state / "learning-loop" / "critic-latest.md",
    }


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        if not path.exists():
            return None, f"missing: {path}"
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # never-500 file boundary
        return None, f"failed to read {path}: {exc}"


def _read_history_tail(path: Path, limit: int = 14) -> tuple[list[Any], list[str]]:
    errors: list[str] = []
    try:
        if not path.exists():
            return [], [f"missing: {path}"]
        all_lines = path.read_text(encoding="utf-8").splitlines()
        lines = all_lines[-limit:]
        first_line_no = max(1, len(all_lines) - len(lines) + 1)
    except Exception as exc:  # never-500 file boundary
        return [], [f"failed to read {path}: {exc}"]

    rows: list[Any] = []
    for idx, line in enumerate(lines, start=first_line_no):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception as exc:
            errors.append(f"history json parse failed line {idx}: {exc}")
    return rows, errors


def _read_distiller_state(paths: dict[str, Path] | None = None) -> tuple[DistillerState | None, list[str]]:
    paths = paths or _paths()
    queue_path = paths["distiller_queue"]
    inbox_path = paths["distiller_inbox"]
    errors: list[str] = []
    state: DistillerState = {"pending": 0, "approved": 0, "rejected": 0}
    saw_source = False

    try:
        if not queue_path.exists():
            errors.append(f"missing: {queue_path}")
        else:
            saw_source = True
            for idx, line in enumerate(queue_path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception as exc:
                    errors.append(f"distiller queue json parse failed line {idx}: {exc}")
                    continue
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status") or "pending").lower()
                if status == "pending":
                    state["pending"] = int(state.get("pending", 0) or 0) + 1
                    first_seen = row.get("first_seen_at") or row.get("ts") or row.get("created_at")
                    if isinstance(first_seen, str) and (
                        state.get("oldest_pending_ts") is None or first_seen < str(state.get("oldest_pending_ts"))
                    ):
                        state["oldest_pending_ts"] = first_seen
                elif status == "approved":
                    state["approved"] = int(state.get("approved", 0) or 0) + 1
                    decided_at = row.get("decided_at") or row.get("promoted_at")
                    if isinstance(decided_at, str) and (
                        state.get("last_promotion_ts") is None or decided_at > str(state.get("last_promotion_ts"))
                    ):
                        state["last_promotion_ts"] = decided_at
                elif status == "rejected":
                    state["rejected"] = int(state.get("rejected", 0) or 0) + 1
    except Exception as exc:  # never-500 file boundary
        errors.append(f"failed to read {queue_path}: {exc}")

    try:
        if not inbox_path.exists():
            errors.append(f"missing: {inbox_path}")
        else:
            saw_source = True
            text = inbox_path.read_text(encoding="utf-8")
            match = re.search(r"Stale pending candidates\s*\((\d+)\)", text, flags=re.IGNORECASE)
            if match:
                state["stale_count"] = int(match.group(1))
    except Exception as exc:  # never-500 file boundary
        errors.append(f"failed to read {inbox_path}: {exc}")

    return (state if saw_source else None), errors


def _read_weekly_hygiene_latest(paths: dict[str, Path] | None = None) -> tuple[WeeklyHygieneLatest | None, list[str]]:
    paths = paths or _paths()
    weekly_dir = paths["weekly_hygiene_latest"]
    errors: list[str] = []
    try:
        candidates = sorted(
            (path for path in weekly_dir.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception as exc:  # never-500 file boundary
        return None, [f"failed to glob {weekly_dir}: {exc}"]

    if not candidates:
        return None, [f"missing: {weekly_dir}/*.json"]

    latest = candidates[0]
    data, err = _read_json(latest)
    if err:
        return None, [err]
    if not isinstance(data, dict):
        return None, [f"weekly hygiene latest not an object: {latest}"]

    stores = data.get("stores") if isinstance(data.get("stores"), dict) else {}
    mvms = stores.get("4_mvms") if isinstance(stores.get("4_mvms"), dict) else {}
    kanban = stores.get("6_kanban") if isinstance(stores.get("6_kanban"), dict) else {}
    stuck_ready = data.get("stuck_ready")
    if stuck_ready is None and isinstance(kanban, dict):
        stuck_ready = 0
        for board in kanban.get("boards") or []:
            if isinstance(board, dict) and isinstance(board.get("stuck_ready"), list):
                stuck_ready += len(board["stuck_ready"])

    ts = data.get("generated_at_utc") or data.get("generated_at") or data.get("ts")
    try:
        age_seconds = time.time() - latest.stat().st_mtime
        if age_seconds > 7 * 24 * 60 * 60:
            errors.append(f"weekly hygiene latest stale >7d: {latest}")
    except Exception as exc:
        errors.append(f"failed to stat {latest}: {exc}")

    return {
        "lesson_completion_ratio": data.get("lesson_completion_ratio") or mvms.get("lesson_completion_ratio"),
        "embedding_coverage": data.get("embedding_coverage") or mvms.get("embedding_coverage"),
        "stuck_ready_count": stuck_ready if isinstance(stuck_ready, int) else None,
        "ts": ts if isinstance(ts, str) else None,
        "dup_rate": data.get("dup_rate") or mvms.get("dup_rate"),
    }, errors


def _read_critic_latest(paths: dict[str, Path] | None = None) -> tuple[LoopCritic | None, list[str]]:
    paths = paths or _paths()
    path = paths["critic_latest"]
    if not path.exists():
        return None, [f"missing: {path}"]
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # never-500 file boundary
        return None, [f"failed to read {path}: {exc}"]

    checks: dict[str, bool] = {}
    for match in re.finditer(r"^- \*\*(PASS|FAIL)\*\*\s+`([^`]+)`", text, flags=re.MULTILINE):
        name = match.group(2)
        # The quarantine-candidate line is exposed as a count below, not as a hard grid check.
        if name == "recent agent quarantine candidate":
            continue
        checks[name] = match.group(1) == "PASS"

    status_match = re.search(r"^status:\s*(PASS|FAIL|\S+)", text, flags=re.MULTILINE)
    hard_failures_match = re.search(r"^hard_failures:\s*(\d+)", text, flags=re.MULTILINE)
    quarantine_match = re.search(r"agent_quarantine_candidates_last_7d:\s*(\d+)", text)
    if not quarantine_match:
        quarantine_match = re.search(r"quarantine_count_last_7d:\s*(\d+)", text)
    next_run_match = re.search(r"next[_ -]run(?:_ts)?:\s*`?([^`\n]+)`?", text, flags=re.IGNORECASE)

    return {
        "status": status_match.group(1) if status_match else ("FAIL" if any(v is False for v in checks.values()) else "PASS"),
        "hard_failures": int(hard_failures_match.group(1)) if hard_failures_match else None,
        "checks": checks,
        "quarantine_count_last_7d": int(quarantine_match.group(1)) if quarantine_match else None,
        "next_run_ts": next_run_match.group(1).strip() if next_run_match else None,
    }, []


def _build_recall_filters(snapshot: Any) -> RecallFiltersLatest:
    excluded_counts: dict[str, int | None] = {"auto_bridged": None, "quarantine": None}
    if isinstance(snapshot, dict):
        auto_bridged_count = snapshot.get("auto_bridged_count")
        quarantine_count = snapshot.get("quarantine_count")
        excluded_counts["auto_bridged"] = auto_bridged_count if isinstance(auto_bridged_count, int) else None
        excluded_counts["quarantine"] = quarantine_count if isinstance(quarantine_count, int) else None
    return {
        "include_quarantine": False,
        "exclude_auto_bridged": True,
        "effective": "current",
        "excluded_counts": excluded_counts,
    }


def _status(snapshot: Any, result: Any, errors: list[str]) -> str:
    if errors:
        return "amber"
    if not isinstance(snapshot, dict) or not isinstance(result, dict):
        return "unknown"
    if result.get("pass") is True:
        return "green"
    if result.get("pass") is False:
        return "red"
    return "unknown"


def get_learning_snapshot() -> dict[str, Any]:
    global _CACHE
    now = time.monotonic()
    with _LOCK:
        if _CACHE and now - _CACHE[1] < _TTL_SECONDS:
            return _CACHE[0]

        paths = _paths()
        errors: list[str] = []
        snapshot, err = _read_json(paths["index_snapshot"])
        if err:
            errors.append(err)
        result, err = _read_json(paths["canary_result"])
        if err:
            errors.append(err)
        history, history_errors = _read_history_tail(paths["index_history"], limit=14)
        errors.extend(history_errors)
        distiller, distiller_errors = _read_distiller_state(paths)
        errors.extend(distiller_errors)
        weekly_hygiene_latest, weekly_errors = _read_weekly_hygiene_latest(paths)
        errors.extend(weekly_errors)
        loop_critic, critic_errors = _read_critic_latest(paths)
        errors.extend(critic_errors)

        payload: dict[str, Any] = {
            "generated_at": _now(),
            "cache_ttl_seconds": _TTL_SECONDS,
            "status": _status(snapshot, result, errors),
            "files": {name: str(path) for name, path in paths.items()},
            "snapshot_latest": snapshot,
            "result_latest": result,
            "history_tail": history,
            "history_tail_count": len(history),
            "actionable_signal_score": snapshot.get("ACTIONABLE_SIGNAL_SCORE") if isinstance(snapshot, dict) else None,
            "actionable_lessons_total": snapshot.get("actionable_lessons_total") if isinstance(snapshot, dict) else None,
            "trusted_actionable_ratio": snapshot.get("trusted_actionable_ratio") if isinstance(snapshot, dict) else None,
            "recall_filters": _build_recall_filters(snapshot),
            "distiller": distiller,
            "weekly_hygiene_latest": weekly_hygiene_latest,
            "loop_critic": loop_critic,
            "canary_embed_present": result.get("embed_present", True) if isinstance(result, dict) else True,
            "errors": errors,
        }
        _CACHE = (payload, now)
        return payload


@router.get("/learning", summary="Learning-loop metric and canary latest file snapshot")
async def get_learning() -> dict[str, Any]:
    """Return the learning-loop latest JSON files with a 20 s cache.

    This endpoint intentionally reads files only. It must never query MVMS or any
    other database from the dashboard request path.
    """
    try:
        return get_learning_snapshot()
    except Exception as exc:  # final never-500 guard
        return {
            "generated_at": _now(),
            "cache_ttl_seconds": _TTL_SECONDS,
            "status": "unknown",
            "files": {name: str(path) for name, path in _paths().items()},
            "snapshot_latest": None,
            "result_latest": None,
            "history_tail": [],
            "history_tail_count": 0,
            "actionable_signal_score": None,
            "actionable_lessons_total": None,
            "trusted_actionable_ratio": None,
            "recall_filters": _build_recall_filters(None),
            "distiller": None,
            "weekly_hygiene_latest": None,
            "loop_critic": None,
            "canary_embed_present": True,
            "errors": [f"learning snapshot build failed: {exc}"],
        }
