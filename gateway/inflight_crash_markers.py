"""Durable in-flight gateway turn crash markers.

Markers are written when a gateway turn is claimed and removed only when the
turn slot is released.  On unclean boot they let recovery mark exactly the
sessions that were truly in-flight instead of approximating from "recent"
sessions.json activity.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home

logger = logging.getLogger("gateway.run")

MARKER_VERSION = 1
MARKER_MAX_AGE_SECONDS = 24 * 60 * 60
DIR_FSYNC_MIN_INTERVAL_SECONDS = 0.25
_DIR_FSYNC_LOCK = threading.Lock()
_LAST_DIR_FSYNC: dict[str, float] = {}


def markers_dir() -> Path:
    return get_hermes_home() / "gateway" / "inflight"


def _safe_name(session_key: str) -> str:
    return session_key.encode("utf-8").hex()


def marker_path(session_key: str) -> Path:
    return markers_dir() / f"{_safe_name(session_key)}.json"


def _fsync_directory(path: Path, *, coalesce: bool = True) -> None:
    key = str(path)
    if coalesce:
        now = time.monotonic()
        with _DIR_FSYNC_LOCK:
            last = _LAST_DIR_FSYNC.get(key, 0.0)
            if now - last < DIR_FSYNC_MIN_INTERVAL_SECONDS:
                return
            _LAST_DIR_FSYNC[key] = now
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_marker(
    session_key: str,
    *,
    session_id: str | None = None,
    started_at: float | None = None,
    worktree: str | None = None,
    autonomous_dispatch: bool | None = None,
    approval_key: str | None = None,
    deny_patterns: list[Any] | None = None,
) -> Path | None:
    """Atomically persist the in-flight marker for ``session_key``."""
    if not session_key:
        return None
    root = markers_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = marker_path(session_key)
    payload = {
        "version": MARKER_VERSION,
        "session_key": session_key,
        "session_id": session_id,
        "started_at": started_at if started_at is not None else time.time(),
        "worktree": worktree,
        "autonomous_dispatch": autonomous_dispatch,
        "approval_key": approval_key,
        "deny_patterns": list(deny_patterns) if deny_patterns else None,
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_directory(root)
    return path


def remove_marker(session_key: str) -> None:
    if not session_key:
        return
    path = marker_path(session_key)
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError:
        pass


def _marker_age_seconds(path: Path, data: dict[str, Any], now: float) -> float:
    started_at = data.get("started_at")
    if isinstance(started_at, (int, float)):
        return max(0.0, now - float(started_at))
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return 0.0


def _remove_marker_path(path: Path, reason: str) -> None:
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError as exc:
        logger.warning("Failed to sweep in-flight crash marker %s (%s): %s", path, reason, exc)


def load_markers(
    *,
    max_age_seconds: int = MARKER_MAX_AGE_SECONDS,
    live_session_keys: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    root = markers_dir()
    if not root.is_dir():
        return []
    allowed = {str(key) for key in live_session_keys} if live_session_keys is not None else None
    now = time.time()
    markers: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Ignoring unreadable in-flight crash marker %s: %s", path, exc)
            continue
        if not isinstance(data, dict) or not data.get("session_key"):
            continue
        session_key = str(data.get("session_key"))
        if max_age_seconds > 0 and _marker_age_seconds(path, data, now) > max_age_seconds:
            logger.warning("Ignoring stale in-flight crash marker %s for %s", path, session_key)
            _remove_marker_path(path, "stale")
            continue
        if allowed is not None and session_key not in allowed:
            logger.warning("Sweeping in-flight crash marker %s for missing session %s", path, session_key)
            _remove_marker_path(path, "missing_session")
            continue
        data["marker_path"] = str(path)
        markers.append(data)
    return markers
