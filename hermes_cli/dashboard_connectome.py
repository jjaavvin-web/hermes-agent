"""Neural Connectome dashboard API.

L2 real-data probes for the Hermes OS Neural Connectome view. Cheap probes
(Kanban projects, config, programs, deploy) read live local state; infra and
learning bind to the cached OS snapshot so liveness cannot drift from /os.
Every source is try/except guarded and degrades to a provenance-backed
``source unreachable`` hub instead of raising through the API.
"""
from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Query

from hermes_constants import get_hermes_home

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-connectome"])

# ---------------------------------------------------------------------------
# Cache (20 s TTL, single-flight; mirrors dashboard_os.py's _OS_LOCK pattern)
# ---------------------------------------------------------------------------
_CONNECTOME_CACHE: tuple[dict[str, Any], float] | None = None
_CLUSTER_CACHE: dict[tuple[str, str | None, int], tuple[dict[str, Any], float]] = {}
_CONNECTOME_TTL = 20.0
_CLUSTER_TTL = 30.0
_CONNECTOME_LOCK = threading.Lock()
_CLUSTER_LOCK = threading.Lock()

HERMES_HOME = get_hermes_home()
REPO_ROOT = Path(__file__).resolve().parents[1]
KANBAN_DB = HERMES_HOME / "kanban" / "boards" / "hermes" / "kanban.db"
CONFIG_PATH = HERMES_HOME / "config.yaml"
AUDITS_DIR = HERMES_HOME / "audits"
SERVING_DEPLOY_BRANCH = "deploy/dashboard-both"

CANONICAL_HUBS: tuple[str, ...] = (
    "projects",
    "brain",
    "code",
    "infra",
    "learning",
    "lanes",
    "config",
    "programs",
    "deploy",
)

HUB_LABELS: dict[str, str] = {
    "projects": "Kanban work",
    "brain": "MVMS agent-engineering memory",
    "code": "GitNexus-indexed source",
    "infra": "Live infra",
    "learning": "Recall flywheel",
    "lanes": "Delegation workforce",
    "config": "AI-config spine",
    "programs": "Active flagship programs",
    "deploy": "Live deploy state",
}

BRIDGES: tuple[tuple[str, str, str, str], ...] = (
    ("projects-brain", "projects", "brain", "kanban-mvms-bridge.timer"),
    ("brain-lanes", "brain", "lanes", "recall-to-dispatch"),
    ("code-deploy", "code", "deploy", "reindex-on-commit"),
    ("learning-brain", "learning", "brain", "write-back/reflect-gate"),
    ("config-infra", "config", "infra", "gateway runs model"),
)

SECRET_MARKERS = (
    "api_key",
    "apikey",
    "auth",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "jwt",
    "key",
    "password",
    "secret",
    "token",
)

STATUS_RANK = {
    "source unreachable": 0,
    "blocked": 1,
    "red": 1,
    "in-progress": 2,
    "running": 2,
    "amber": 2,
    "queued": 3,
    "stale": 3,
    "not-serving": 3,
    "pending": 3,
    "unknown": 3,
    "ok": 4,
    "green": 4,
    "completed": 5,
    "active": 5,
    "serving": 5,
    "info": 5,
}


class ProbeResult(dict[str, Any]):
    """Small typed alias for probe dictionaries."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_timestamp(ts: float | int | None) -> str:
    if ts is None:
        return _now()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _redact_value(value: Any, key_path: str = "") -> Any:
    """Return a display-safe copy with secrets redacted by key-path heuristics."""
    path_lower = key_path.lower()
    if any(marker in path_lower for marker in SECRET_MARKERS):
        if value not in (None, "", [], {}):
            return "[REDACTED]"
        return value
    if isinstance(value, dict):
        return {str(k): _redact_value(v, f"{key_path}.{k}" if key_path else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, key_path) for v in value]
    if isinstance(value, str) and len(value) > 120:
        return f"{value[:117]}..."
    return value


def _prov(source: str, query: str, field: str, value: Any | None = None) -> dict[str, str]:
    val = _redact_value(value, field)
    if val is None:
        val = "n/a"
    return {
        "source": str(source),
        "query": str(query),
        "field": str(field),
        "value": str(val),
        "lastSeen": _now(),
    }


def _with_flat_prov(node: dict[str, Any]) -> dict[str, Any]:
    """Expose both nested and flat provenance fields for frontend compatibility."""
    provenance = node.get("provenance") or {}
    node.setdefault("real", True)
    node["provSource"] = provenance.get("source", "")
    node["provQuery"] = provenance.get("query", "")
    node["provField"] = provenance.get("field", "")
    node["provValue"] = provenance.get("value", "")
    node["lastSeen"] = provenance.get("lastSeen", _now())
    return node


def _node(
    node_id: str,
    label: str,
    cluster: str,
    kind: str,
    status: str,
    provenance: dict[str, str],
    *,
    count: int | None = None,
    detail: str | None = None,
    metric: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": node_id,
        "label": label,
        "cluster": cluster,
        "kind": kind,
        "status": status,
        "provenance": provenance,
    }
    if count is not None:
        payload["count"] = count
    if detail is not None:
        payload["detail"] = detail
    if metric is not None:
        payload["metric"] = metric
    if extra:
        payload.update(extra)
    return _with_flat_prov(payload)


def _hub_node(
    hub_id: str,
    count: int,
    status: str,
    provenance: dict[str, str],
    *,
    detail: str | None = None,
    metric: Any | None = None,
) -> dict[str, Any]:
    return _node(
        hub_id,
        HUB_LABELS[hub_id],
        hub_id,
        "hub",
        status,
        provenance,
        count=count,
        detail=detail,
        metric=metric,
    )


def _unreachable_hub(hub_id: str, source: str, query: str, error: Exception | str) -> ProbeResult:
    message = str(error) or "source unreachable"
    hub = _hub_node(
        hub_id,
        0,
        "source unreachable",
        _prov(source, query, "error", message),
        detail=message,
    )
    return ProbeResult({"hub": hub, "leaves": [], "edges": []})


def _edge(edge_id: str, source: str, target: str, label: str, *, kind: str = "bridge") -> dict[str, str]:
    return {"id": edge_id, "source": source, "target": target, "label": label, "kind": kind}


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "unknown"
    return min(statuses, key=lambda status: STATUS_RANK.get(status, 3))


def _safe_envelope(error: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "generated_at": _now(),
        "stub": False,
        "hub_count": 0,
        "cache_ttl_seconds": _CONNECTOME_TTL,
    }
    if error:
        meta["status"] = "degraded"
        meta["error"] = error
    else:
        meta["status"] = "ok"
    return {"nodes": [], "edges": [], "meta": meta}


def _paginate(leaves: list[dict[str, Any]], cursor: str | None, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    offset = 0
    if cursor:
        try:
            offset = max(int(cursor), 0)
        except ValueError:
            offset = 0
    page = leaves[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = str(next_offset) if next_offset < len(leaves) else None
    return page, next_cursor


# ---------------------------------------------------------------------------
# Projects probe — Kanban SQLite read-only
# ---------------------------------------------------------------------------

def _project_status(row: sqlite3.Row) -> str:
    raw = str(row["status"] or "unknown")
    if raw in {"done", "archived"}:
        return "completed"
    if raw == "blocked":
        return "blocked"
    if row["started_at"] and raw not in {"done", "archived"}:
        return "in-progress"
    if raw in {"ready", "scheduled", "todo", "triage"}:
        return "queued"
    return raw


def _projects_probe(db_path: Path = KANBAN_DB) -> ProbeResult:
    query = (
        "SELECT id,title,status,priority,started_at,completed_at,branch_name "
        "FROM tasks WHERE status!='archived' ORDER BY priority DESC, created_at DESC"
    )
    try:
        uri = f"file:{db_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(query).fetchall()
        finally:
            con.close()
        leaves: list[dict[str, Any]] = []
        statuses: list[str] = []
        for row in rows:
            raw_status = str(row["status"] or "unknown")
            status = _project_status(row)
            statuses.append(status)
            task_id = str(row["id"])
            leaves.append(
                _node(
                    f"projects:{task_id}",
                    str(row["title"]),
                    "projects",
                    "task",
                    status,
                    _prov(str(db_path), query, "tasks.status", raw_status),
                    metric={
                        "priority": row["priority"],
                        "started_at": row["started_at"],
                        "completed_at": row["completed_at"],
                        "branch_name": row["branch_name"],
                    },
                    extra={"task_id": task_id, "raw_status": raw_status},
                )
            )
        count = len(rows)
        hub = _hub_node(
            "projects",
            count,
            _worst_status(statuses) if statuses else "completed",
            _prov(str(db_path), query, "tasks.status", f"{count} non-archived rows"),
            metric={status: statuses.count(status) for status in sorted(set(statuses))},
        )
        return ProbeResult({"hub": hub, "leaves": leaves, "edges": []})
    except Exception as exc:
        return _unreachable_hub("projects", str(db_path), query, exc)


# ---------------------------------------------------------------------------
# Config probe — sanitized config.yaml + profiles/personality surfaces
# ---------------------------------------------------------------------------

def _active_profile() -> str:
    env_profile = os.environ.get("HERMES_PROFILE", "").strip()
    if env_profile:
        return env_profile
    active_file = HERMES_HOME / "active_profile"
    if active_file.exists():
        try:
            return active_file.read_text(encoding="utf-8").strip() or "default"
        except OSError:
            return "default"
    return "default"


def _add_config_leaf(
    leaves: list[dict[str, Any]],
    leaf_id: str,
    label: str,
    kind: str,
    key_path: str,
    value: Any,
    config_path: Path,
    *,
    status: str = "ok",
) -> None:
    leaves.append(
        _node(
            f"config:{leaf_id}",
            label,
            "config",
            kind,
            status,
            _prov(str(config_path), f"yaml.safe_load({config_path})", key_path, _redact_value(value, key_path)),
            metric=_redact_value(value, key_path),
        )
    )


def _config_probe(config_path: Path = CONFIG_PATH) -> ProbeResult:
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        leaves: list[dict[str, Any]] = []

        model_cfg = config.get("model", {}) if isinstance(config.get("model"), dict) else {}
        _add_config_leaf(leaves, "model.default", f"model.default: {model_cfg.get('default', 'unset')}", "model", "model.default", model_cfg.get("default", "unset"), config_path)
        _add_config_leaf(leaves, "model.provider", f"model.provider: {model_cfg.get('provider', 'unset')}", "provider", "model.provider", model_cfg.get("provider", "unset"), config_path)

        providers = config.get("providers", {})
        if isinstance(providers, dict):
            for name, value in sorted(providers.items()):
                _add_config_leaf(leaves, f"providers.{name}", f"provider: {name}", "provider", f"providers.{name}", value, config_path)

        toolsets = config.get("toolsets", [])
        if isinstance(toolsets, list):
            _add_config_leaf(leaves, "toolsets", f"toolsets: {len(toolsets)}", "toolset", "toolsets", toolsets, config_path)
            for idx, value in enumerate(toolsets):
                _add_config_leaf(leaves, f"toolsets.{idx}", f"toolset: {value}", "toolset", f"toolsets[{idx}]", value, config_path)

        auxiliary = config.get("auxiliary", {})
        if isinstance(auxiliary, dict):
            for name, value in sorted(auxiliary.items()):
                _add_config_leaf(leaves, f"auxiliary.{name}", f"aux: {name}", "auxiliary", f"auxiliary.{name}", value, config_path)

        mcp_servers = config.get("mcp_servers", {})
        if isinstance(mcp_servers, dict):
            for name, value in sorted(mcp_servers.items()):
                _add_config_leaf(leaves, f"mcp_servers.{name}", f"mcp: {name}", "mcp", f"mcp_servers.{name}", value, config_path)

        profile = _active_profile()
        _add_config_leaf(leaves, "active_profile", f"active profile: {profile}", "profile", "active_profile", profile, config_path, status="active")

        profiles_dir = HERMES_HOME / "profiles"
        if profiles_dir.exists():
            for path in sorted(p for p in profiles_dir.iterdir() if p.is_dir()):
                status = "active" if path.name == profile else "ok"
                leaves.append(
                    _node(
                        f"config:profile:{path.name}",
                        f"profile: {path.name}",
                        "config",
                        "profile",
                        status,
                        _prov(str(profiles_dir), "profiles directory listing", "profiles[].name", path.name),
                    )
                )

        personalities = config.get("personalities", {})
        if isinstance(personalities, dict):
            for name, value in sorted(personalities.items()):
                _add_config_leaf(leaves, f"personalities.{name}", f"personality: {name}", "personality", f"personalities.{name}", value, config_path)

        hub = _hub_node(
            "config",
            len(leaves),
            "ok",
            _prov(str(config_path), f"yaml.safe_load({config_path})", "model.default", model_cfg.get("default", "unset")),
            metric={
                "providers": len(providers) if isinstance(providers, dict) else 0,
                "toolsets": len(toolsets) if isinstance(toolsets, list) else 0,
                "auxiliary": len(auxiliary) if isinstance(auxiliary, dict) else 0,
                "mcp_servers": len(mcp_servers) if isinstance(mcp_servers, dict) else 0,
                "active_profile": profile,
            },
        )
        return ProbeResult({"hub": hub, "leaves": leaves, "edges": []})
    except Exception as exc:
        return _unreachable_hub("config", str(config_path), f"yaml.safe_load({config_path})", exc)


# ---------------------------------------------------------------------------
# Programs probe — audit MASTER-GAMEPLAN.md files
# ---------------------------------------------------------------------------

def _first_markdown_heading(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("#"):
                    return stripped.lstrip("#").strip() or path.parent.name
    except OSError:
        return path.parent.name
    return path.parent.name


def _programs_probe(audits_dir: Path = AUDITS_DIR) -> ProbeResult:
    try:
        file_map = {path.resolve(): path for path in audits_dir.glob("*/MASTER-GAMEPLAN.md")}
        # Active flagship packets may use BUILD-SPEC.md rather than MASTER-GAMEPLAN.md.
        for path in audits_dir.glob("*/BUILD-SPEC.md"):
            try:
                if (time.time() - path.stat().st_mtime) / 86400 < 14:
                    file_map.setdefault(path.resolve(), path)
            except OSError:
                continue
        files = sorted(file_map.values(), key=lambda path: path.stat().st_mtime, reverse=True)
        query = f"glob {audits_dir}/*/MASTER-GAMEPLAN.md + active */BUILD-SPEC.md (<14d)"
        leaves: list[dict[str, Any]] = []
        statuses: list[str] = []
        now = time.time()
        for path in files:
            stat = path.stat()
            age_days = (now - stat.st_mtime) / 86400
            status = "active" if age_days < 14 else "stale"
            statuses.append(status)
            label = _first_markdown_heading(path)
            leaves.append(
                _node(
                    f"programs:{path.parent.name}",
                    label,
                    "programs",
                    "gameplan",
                    status,
                    _prov(str(path), query, "mtime", _iso_from_timestamp(stat.st_mtime)),
                    metric={"mtime": _iso_from_timestamp(stat.st_mtime), "age_days": round(age_days, 2)},
                    extra={"path": str(path)},
                )
            )
        hub_status = _worst_status(statuses) if statuses else "source unreachable"
        hub = _hub_node(
            "programs",
            len(leaves),
            hub_status,
            _prov(str(audits_dir), query, "mtime", f"{len(leaves)} gameplans"),
            metric={status: statuses.count(status) for status in sorted(set(statuses))},
        )
        return ProbeResult({"hub": hub, "leaves": leaves, "edges": []})
    except Exception as exc:
        return _unreachable_hub("programs", str(audits_dir), f"glob {audits_dir}/*/MASTER-GAMEPLAN.md + active */BUILD-SPEC.md", exc)


# ---------------------------------------------------------------------------
# Deploy probe — git branch/rev vs serving deploy branch
# ---------------------------------------------------------------------------

def _git(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    return result.stdout.strip()


def _deploy_probe(repo_dir: Path = REPO_ROOT) -> ProbeResult:
    try:
        head = _git(repo_dir, "rev-parse", "HEAD")
        short_head = _git(repo_dir, "rev-parse", "--short", "HEAD")
        branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        serving_rev = _git(repo_dir, "rev-parse", SERVING_DEPLOY_BRANCH)
        serving_short = serving_rev[:12]
        is_serving = head == serving_rev
        status = "serving" if is_serving else "not-serving"
        query = f"git rev-parse HEAD && git rev-parse {SERVING_DEPLOY_BRANCH}"
        leaf = _node(
            "deploy:head",
            f"{branch}@{short_head}",
            "deploy",
            "git-ref",
            status,
            _prov(str(repo_dir), query, "HEAD", f"{branch} {short_head}; serving {SERVING_DEPLOY_BRANCH} {serving_short}"),
            metric={
                "branch": branch,
                "head": short_head,
                "serving_branch": SERVING_DEPLOY_BRANCH,
                "serving_head": serving_short,
                "head_equals_serving": is_serving,
            },
        )
        hub = _hub_node(
            "deploy",
            1,
            status,
            _prov(str(repo_dir), query, "HEAD", f"{branch} {short_head}"),
            metric=leaf["metric"],
        )
        return ProbeResult({"hub": hub, "leaves": [leaf], "edges": []})
    except Exception as exc:
        return _unreachable_hub("deploy", str(repo_dir), f"git rev-parse HEAD && git rev-parse {SERVING_DEPLOY_BRANCH}", exc)


# ---------------------------------------------------------------------------
# Infra + learning — bind to already-computed dashboard_os snapshot/graph
# ---------------------------------------------------------------------------

def _translate_os_status(status: str) -> str:
    if status == "green":
        return "ok"
    if status == "red":
        return "blocked"
    if status == "amber":
        return "queued"
    return status or "unknown"


def _os_graph_snapshot(snapshot_getter: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    if snapshot_getter is not None:
        return snapshot_getter()
    from hermes_cli.dashboard_os import get_os_snapshot

    return get_os_snapshot()


def _os_bound_probe(
    hub_id: str,
    group_filter: Callable[[dict[str, Any]], bool],
    snapshot_getter: Callable[[], dict[str, Any]] | None = None,
) -> ProbeResult:
    try:
        snapshot = _os_graph_snapshot(snapshot_getter)
        sections = snapshot.get("sections", [])
        graph = snapshot.get("graph") or {}
        if not graph.get("nodes") and sections:
            from hermes_cli.dashboard_os import _build_os_graph

            graph = _build_os_graph(sections)
        graph_nodes = [node for node in graph.get("nodes", []) if group_filter(node)]
        leaves: list[dict[str, Any]] = []
        statuses: list[str] = []
        for raw in graph_nodes:
            status = _translate_os_status(str(raw.get("status", "unknown")))
            statuses.append(status)
            node_id = str(raw.get("id", "unknown"))
            field = str(raw.get("section_ref") or raw.get("group") or "dashboard_os.graph.nodes[].status")
            leaves.append(
                _node(
                    f"{hub_id}:{node_id}",
                    str(raw.get("label") or node_id),
                    hub_id,
                    str(raw.get("kind") or "os-node"),
                    status,
                    _prov("hermes_cli.dashboard_os.get_os_snapshot()", "snapshot['graph']['nodes']", field, raw.get("status", "unknown")),
                    detail=raw.get("detail") or raw.get("reason"),
                    metric={k: v for k, v in raw.items() if k not in {"id", "label"}},
                )
            )
        source_field = "graph.nodes[group=='learning']" if hub_id == "learning" else "graph.nodes[group!='learning']"
        hub = _hub_node(
            hub_id,
            len(leaves),
            _worst_status(statuses) if statuses else "source unreachable",
            _prov("hermes_cli.dashboard_os.get_os_snapshot()", "snapshot['graph']['nodes']", source_field, len(leaves)),
            metric={"os_snapshot_generated_at": snapshot.get("generated_at"), "os_overall": snapshot.get("overall")},
        )
        return ProbeResult({"hub": hub, "leaves": leaves, "edges": []})
    except Exception as exc:
        return _unreachable_hub(hub_id, "hermes_cli.dashboard_os.get_os_snapshot()", "snapshot['graph']", exc)


def _infra_probe(snapshot_getter: Callable[[], dict[str, Any]] | None = None) -> ProbeResult:
    return _os_bound_probe("infra", lambda node: node.get("group") != "learning", snapshot_getter)


def _learning_probe(snapshot_getter: Callable[[], dict[str, Any]] | None = None) -> ProbeResult:
    return _os_bound_probe("learning", lambda node: node.get("group") == "learning", snapshot_getter)


# ---------------------------------------------------------------------------
# Deferred hubs for later lanes — provenance-backed, not empty stubs
# ---------------------------------------------------------------------------

def _deferred_probe(hub_id: str, source: str, query: str, field: str) -> ProbeResult:
    hub = _hub_node(
        hub_id,
        0,
        "pending",
        _prov(source, query, field, "pending later workstream"),
        detail="probe deferred to a later Neural Connectome workstream",
    )
    return ProbeResult({"hub": hub, "leaves": [], "edges": []})


def _brain_probe() -> ProbeResult:
    return _deferred_probe("brain", "MVMS", "L3 _memory_probe", "memory.observations")


def _code_probe() -> ProbeResult:
    return _deferred_probe("code", "GitNexus", "L3 _code_probe", "/api/repos")


def _lanes_probe() -> ProbeResult:
    return _deferred_probe("lanes", "Kanban task_runs + git refs", "L4 _lanes_probe", "task_runs.status")


PROBES: dict[str, Callable[[], ProbeResult]] = {
    "projects": _projects_probe,
    "brain": _brain_probe,
    "code": _code_probe,
    "infra": _infra_probe,
    "learning": _learning_probe,
    "lanes": _lanes_probe,
    "config": _config_probe,
    "programs": _programs_probe,
    "deploy": _deploy_probe,
}


# ---------------------------------------------------------------------------
# Builders / routes
# ---------------------------------------------------------------------------

def _run_probe(hub_id: str) -> ProbeResult:
    try:
        return PROBES[hub_id]()
    except Exception as exc:
        return _unreachable_hub(hub_id, "connectome probe", hub_id, exc)


def _build_connectome_snapshot() -> dict[str, Any]:
    """Build the summary graph from the canonical nine hubs."""
    results: dict[str, ProbeResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(CANONICAL_HUBS)) as pool:
        futures = {hub_id: pool.submit(_run_probe, hub_id) for hub_id in CANONICAL_HUBS}
        for hub_id in CANONICAL_HUBS:
            try:
                results[hub_id] = futures[hub_id].result(timeout=15)
            except Exception as exc:
                results[hub_id] = _unreachable_hub(hub_id, "connectome probe", hub_id, exc)

    nodes = [results[hub_id]["hub"] for hub_id in CANONICAL_HUBS]
    edges = [_edge(edge_id, source, target, label) for edge_id, source, target, label in BRIDGES]
    status = "degraded" if any(node.get("status") == "source unreachable" for node in nodes) else "ok"
    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "generated_at": _now(),
            "stub": False,
            "canonical_hubs": list(CANONICAL_HUBS),
            "hub_count": len(nodes),
            "edge_count": len(edges),
            "cache_ttl_seconds": _CONNECTOME_TTL,
            "status": status,
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
    """Return the cached Connectome graph envelope; never 500."""
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
    """Return a cursor-paginated leaf page for a cluster; never 500."""
    try:
        safe_limit = min(max(int(limit), 1), 300)
        cache_key = (cluster_id, cursor, safe_limit)
        now = time.monotonic()
        with _CLUSTER_LOCK:
            cached = _CLUSTER_CACHE.get(cache_key)
            if cached and now < cached[1]:
                return cached[0]
        if cluster_id not in PROBES:
            result = {
                "cluster_id": cluster_id,
                "leaves": [],
                "next_cursor": None,
                "status": "source unreachable",
                "error": "unknown cluster",
            }
        else:
            probe = _run_probe(cluster_id)
            leaves, next_cursor = _paginate(list(probe.get("leaves", [])), cursor, safe_limit)
            result = {
                "cluster_id": cluster_id,
                "hub": probe.get("hub"),
                "leaves": leaves,
                "next_cursor": next_cursor,
                "generated_at": _now(),
            }
        with _CLUSTER_LOCK:
            _CLUSTER_CACHE[cache_key] = (result, time.monotonic() + _CLUSTER_TTL)
        return result
    except Exception as exc:
        return {
            "cluster_id": cluster_id,
            "leaves": [],
            "next_cursor": None,
            "status": "source unreachable",
            "error": str(exc),
        }


def register(app: Any) -> None:
    """Register the Connectome router on a FastAPI app."""
    app.include_router(router)
