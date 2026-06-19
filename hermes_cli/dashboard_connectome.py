"""Neural Connectome dashboard API skeleton.

Stub-only L1 backend for the Hermes OS Neural Connectome view.
Real probes land in later lanes; this module keeps the endpoint shape,
cache behavior, and never-500 discipline in place.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-connectome"])

# ---------------------------------------------------------------------------
# Cache (20 s TTL, single-flight; mirrors dashboard_os.py's _OS_LOCK pattern)
# ---------------------------------------------------------------------------
_CONNECTOME_CACHE: tuple[dict[str, Any], float] | None = None
_CONNECTOME_TTL = 20.0
_CONNECTOME_LOCK = threading.Lock()

_HUBS: tuple[tuple[str, str], ...] = (
    ("projects", "Kanban work"),
    ("brain", "MVMS agent-engineering memory"),
    ("code", "GitNexus-indexed source"),
    ("infra", "Live systemd services + timers"),
    ("learning", "Recall flywheel"),
    ("lanes", "Delegation workforce"),
    ("config", "AI-config spine"),
    ("programs_deploy", "Active flagship programs"),
    ("deploy", "Live deploy state"),
)

_STUB_EDGES: tuple[tuple[str, str, str, str], ...] = (
    ("projects-brain", "projects", "brain", "kanban-mvms-bridge.timer"),
    ("brain-lanes", "brain", "lanes", "recall-to-dispatch"),
    ("code-deploy", "code", "deploy", "reindex-on-commit"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stub_provenance() -> dict[str, str]:
    return {"source": "", "query": "", "field": ""}


def _hub_node(hub_id: str, label: str) -> dict[str, Any]:
    return {
        "id": hub_id,
        "label": label,
        "kind": "hub",
        "count": 0,
        "status": "unknown",
        "provenance": _stub_provenance(),
    }


def _edge(edge_id: str, source: str, target: str, label: str) -> dict[str, str]:
    return {"id": edge_id, "source": source, "target": target, "label": label, "kind": "bridge"}


def _safe_envelope(error: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "generated_at": _now(),
        "stub": True,
        "hub_count": 0,
        "cache_ttl_seconds": _CONNECTOME_TTL,
    }
    if error:
        meta["status"] = "degraded"
        meta["error"] = error
    else:
        meta["status"] = "ok"
    return {"nodes": [], "edges": [], "meta": meta}


def _build_connectome_snapshot() -> dict[str, Any]:
    """Build the stub summary graph.

    Later lanes replace the stub probe bodies with real, per-cluster guarded
    probes. L1 intentionally emits stable placeholder hub nodes only.
    """
    nodes = [_hub_node(hub_id, label) for hub_id, label in _HUBS]
    edges = [_edge(edge_id, source, target, label) for edge_id, source, target, label in _STUB_EDGES]
    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "generated_at": _now(),
            "stub": True,
            "hub_count": len(nodes),
            "edge_count": len(edges),
            "cache_ttl_seconds": _CONNECTOME_TTL,
            "status": "ok",
        },
    }


def get_connectome_snapshot() -> dict[str, Any]:
    """20s-cached Connectome snapshot. Thread-safe single-flight."""
    global _CONNECTOME_CACHE
    now = time.monotonic()
    if _CONNECTOME_CACHE and now < _CONNECTOME_CACHE[1]:
        return _CONNECTOME_CACHE[0]
    with _CONNECTOME_LOCK:
        now = time.monotonic()
        if _CONNECTOME_CACHE and now < _CONNECTOME_CACHE[1]:
            return _CONNECTOME_CACHE[0]
        try:
            data = _build_connectome_snapshot()
        except Exception as exc:
            data = _safe_envelope(str(exc))
        _CONNECTOME_CACHE = (data, now + _CONNECTOME_TTL)
    return data


@router.get("/connectome", summary="Neural Connectome summary graph")
def get_connectome() -> dict[str, Any]:
    """Return the cached stub Connectome graph envelope; never 500."""
    try:
        return get_connectome_snapshot()
    except Exception as exc:
        return _safe_envelope(str(exc))


@router.get("/connectome/cluster/{cluster_id}", summary="Neural Connectome cluster leaves")
def get_connectome_cluster(
    cluster_id: str,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=300)] = 80,
) -> dict[str, Any]:
    """Return a cursor-paginated stub leaf page for a cluster; never 500."""
    try:
        _ = (cursor, min(max(int(limit), 1), 300))
        return {"cluster_id": cluster_id, "leaves": [], "next_cursor": None}
    except Exception:
        return {"cluster_id": cluster_id, "leaves": [], "next_cursor": None}


def register(app: Any) -> None:
    """Register the Connectome router on a FastAPI app."""
    app.include_router(router)
