"""Operator surface for retained dirty relay delivery worktrees."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


def retained_delivery_root() -> Path:
    return get_hermes_home() / "relay-wt" / "deliveries"


def scan_retained_dirty_deliveries(root: Path | None = None) -> dict[str, Any]:
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
