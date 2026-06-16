"""Learning loop dashboard API (staged 2026-06-15).

GET /api/dashboard/learning

Reads JSON state files only; no database access in the dashboard request path.
Never raises a 500 for missing/malformed learning-loop artifacts.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from hermes_constants import get_hermes_home

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-learning"])

_TTL_SECONDS = 20.0
_CACHE: tuple[dict[str, Any], float] | None = None
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths() -> dict[str, Path]:
    home = get_hermes_home()
    return {
        "index_snapshot": home / "state" / "learning-index" / "snapshot-latest.json",
        "index_history": home / "state" / "learning-index" / "history.jsonl",
        "canary_result": home / "state" / "learning-canary" / "result-latest.json",
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

        payload: dict[str, Any] = {
            "generated_at": _now(),
            "cache_ttl_seconds": _TTL_SECONDS,
            "status": _status(snapshot, result, errors),
            "files": {name: str(path) for name, path in paths.items()},
            "snapshot_latest": snapshot,
            "result_latest": result,
            "history_tail": history,
            "history_tail_count": len(history),
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
            "errors": [f"learning snapshot build failed: {exc}"],
        }
