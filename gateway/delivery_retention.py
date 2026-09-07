"""Operator surface for retained dirty relay delivery worktrees."""

from __future__ import annotations

import os
import time
import threading
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


def retained_delivery_root() -> Path:
    return get_hermes_home() / "relay-wt" / "deliveries"


SCAN_CACHE_TTL_SECONDS = 15.0
_SCAN_CACHE_LOCK = threading.Lock()
_SCAN_CACHE: tuple[Path, float, dict[str, Any]] | None = None


def _scan_retained_dirty_deliveries_uncached(root: Path | None = None) -> dict[str, Any]:
    """Return retained dirty ``wh-*`` delivery trees awaiting harvest."""
    base = root or retained_delivery_root()
    now = time.time()
    items: list[dict[str, Any]] = []
    if not base.is_dir():
        return {"count": 0, "paths": [], "items": []}
    for child in sorted(base.glob("wh-*")):
        if not child.is_dir():
            continue
        try:
            dirty = child / ".dirty"
            if dirty.exists():
                st = dirty.stat()
            elif (child / ".git").exists():
                # Delivery worktree exists and was not cleaned/harvested; this is
                # enough to surface it, but .dirty gets priority when present.
                st = child.stat()
            else:
                continue
            age_seconds = max(0, int(now - st.st_mtime))
        except OSError:
            continue
        items.append({"path": str(child), "age_seconds": age_seconds})
    return {"count": len(items), "paths": [i["path"] for i in items], "items": items}


def invalidate_scan_cache() -> None:
    global _SCAN_CACHE
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE = None


def scan_retained_dirty_deliveries(root: Path | None = None) -> dict[str, Any]:
    base = root or retained_delivery_root()
    now = time.monotonic()
    global _SCAN_CACHE
    with _SCAN_CACHE_LOCK:
        if _SCAN_CACHE is not None:
            cached_root, expires_at, value = _SCAN_CACHE
            if cached_root == base and now < expires_at:
                return value
        value = _scan_retained_dirty_deliveries_uncached(base)
        _SCAN_CACHE = (base, now + SCAN_CACHE_TTL_SECONDS, value)
        return value
