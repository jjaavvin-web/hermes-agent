"""Durable in-flight gateway turn crash markers.

Markers are written when a gateway turn is claimed and removed only when the
turn slot is released.  On unclean boot they let recovery mark exactly the
sessions that were truly in-flight instead of approximating from "recent"
sessions.json activity.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

MARKER_VERSION = 1


def markers_dir() -> Path:
    return get_hermes_home() / "gateway" / "inflight"


def _safe_name(session_key: str) -> str:
    return session_key.encode("utf-8").hex()


def marker_path(session_key: str) -> Path:
    return markers_dir() / f"{_safe_name(session_key)}.json"


def _fsync_directory(path: Path) -> None:
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


def load_markers() -> list[dict[str, Any]]:
    root = markers_dir()
    if not root.is_dir():
        return []
    markers: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("session_key"):
            data["marker_path"] = str(path)
            markers.append(data)
    return markers
