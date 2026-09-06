"""Neural Connectome dashboard API.

L2 real-data probes for the Hermes OS Neural Connectome view. Cheap probes
(Kanban projects, config, programs, deploy) read live local state; infra and
learning bind to the cached OS snapshot so liveness cannot drift from /os.
Every source is try/except guarded and degrades to a provenance-backed
``source unreachable`` hub instead of raising through the API.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import asyncpg
import httpx
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
MVMS_ENV_FILE = Path("/home/josep/workspace/goattrade-system/.env")
GITNEXUS_BASE_URL = "http://127.0.0.1:4747"
_GITNEXUS_REPOS_URL = f"{GITNEXUS_BASE_URL}/api/repos"
_GITNEXUS_GRAPH_URL = f"{GITNEXUS_BASE_URL}/api/graph"
_HTTP_BYTE_CEILING = 2_000_000
_MEMORY_LEAF_LIMIT = 150
_CODE_COMMUNITY_LIMIT = 25
_LOKI_LANE_COUNT = 15
_CODEX_BRANCH_LIMIT = 25
_RECALL_HEALTH_URL = "http://127.0.0.1:8745/health"
_RECALL_MCP_DIR = HERMES_HOME / "mcp" / "recall"
_SUBCACHE_TTL = 60.0
_GITNEXUS_REPO_TTL = 300.0
_MVMS_CACHE: tuple[ProbeResult, float] | None = None
_MVMS_LOCK = threading.Lock()
_GITNEXUS_CACHE: tuple[ProbeResult, float] | None = None
_GITNEXUS_LOCK = threading.Lock()

# The one authoritative ICT/chatter exclusion for MVMS reads. Keep the literal
# IS DISTINCT FROM predicate: != silently drops NULL-source rows.
MEMORY_QUERY_WHERE = """source IS DISTINCT FROM 'ict-brain'
  AND source !~ ':(gave_up|crashed|blocked)$'
  AND source !~* 'compactor|superseder|reflect-promote|curator'
  AND source !~ '^kanban-mvms-bridge:'
  AND deprecated_at IS NULL"""

# Clean-total is the ICT-excluded pool used as the secondary hub metric. The
# chatter predicate owns materialized/default signal rows. It intentionally
# allows deprecated rows through so each node can expose live/stale status.
MEMORY_CLEAN_TOTAL_WHERE = """source IS DISTINCT FROM 'ict-brain'"""
MEMORY_SIGNAL_WHERE = """source IS DISTINCT FROM 'ict-brain'
  AND source !~ ':(gave_up|crashed|blocked)$'
  AND source !~* 'compactor|superseder|reflect-promote|curator'
  AND source !~ '^kanban-mvms-bridge:'"""

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
    ("projects-brain", "projects", "brain", "kanban-mvms-bridge"),
    ("brain-lanes", "brain", "lanes", "recall→dispatch"),
    ("code-deploy", "code", "deploy", "reindex-on-commit"),
    ("learning-brain", "learning", "brain", "write-back"),
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


def _edge(
    edge_id: str,
    source: str,
    target: str,
    label: str,
    *,
    kind: str = "bridge",
    provenance: dict[str, str] | None = None,
    verified: bool = True,
) -> dict[str, Any]:
    edge = {
        "id": edge_id,
        "source": source,
        "target": target,
        "label": label,
        "kind": kind,
        "mechanism": label,
        "verified": verified,
        "provenance": provenance or _prov("connectome", "edge construction", "mechanism", label),
    }
    prov = edge["provenance"]
    edge["provSource"] = prov.get("source", "")
    edge["provQuery"] = prov.get("query", "")
    edge["provField"] = prov.get("field", "")
    edge["provValue"] = prov.get("value", "")
    edge["lastSeen"] = prov.get("lastSeen", _now())
    return edge


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "unknown"
    return min(statuses, key=lambda status: STATUS_RANK.get(status, 3))


def _hub_rollup_status(statuses: list[str]) -> str:
    """Health-aware hub headline. A few blocked/queued leaves must NOT flip the whole
    hub to 'blocked' (e.g. 3 of 67 cards) — the per-status breakdown lives in the hub's
    `metric` histogram. Flag the headline only when a meaningful share is broken."""
    if not statuses:
        return "unknown"
    n = len(statuses)
    bad = sum(1 for s in statuses if s in {"blocked", "red", "degraded", "error", "source unreachable"})
    live = sum(1 for s in statuses if s in {"in-progress", "running", "amber", "active", "live", "serving"})
    if bad == n:
        return "source unreachable" if "source unreachable" in statuses else "blocked"
    if bad >= max(2, round(n * 0.4)):
        return "degraded"
    if live:
        return "active"
    return "ok"


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
            _hub_rollup_status(statuses) if statuses else "completed",
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
        hub_status = _hub_rollup_status(statuses) if statuses else "source unreachable"
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
            _hub_rollup_status(statuses) if statuses else "source unreachable",
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
# Brain probe — MVMS read-only, ICT/chatter filtered server-side
# ---------------------------------------------------------------------------

def _load_mvms_env_if_needed() -> None:
    """Mirror the recall wrapper: source DB URL locally without printing it."""
    if os.environ.get("MVMS_DATABASE_URL"):
        return
    if not MVMS_ENV_FILE.is_file():
        return
    try:
        for raw in MVMS_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in {"SUPABASE_DB_URL", "DATABASE_URL", "MVMS_DATABASE_URL"}:
                continue
            value = value.strip().strip('"').strip("'")
            if value and key not in os.environ:
                os.environ[key] = value
    except OSError:
        return


def _mvms_dsn() -> str:
    _load_mvms_env_if_needed()
    value = os.getenv("MVMS_DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("MVMS_DATABASE_URL/SUPABASE_DB_URL/DATABASE_URL not set")
    return value


async def _memory_query(limit: int = _MEMORY_LEAF_LIMIT) -> dict[str, Any]:
    """Single read-only MVMS helper; all memory.observations reads go through here."""
    safe_limit = max(1, min(int(limit), _MEMORY_LEAF_LIMIT))
    conn = await asyncpg.connect(_mvms_dsn(), command_timeout=10)
    try:
        async with conn.transaction(readonly=True):
            clean_total = int(
                await conn.fetchval(
                    f"SELECT count(*)::bigint FROM memory.observations WHERE {MEMORY_CLEAN_TOTAL_WHERE}"
                )
                or 0
            )
            signal_total = int(
                await conn.fetchval(
                    f"""
                    SELECT count(*)::bigint
                    FROM memory.observations
                    WHERE {MEMORY_QUERY_WHERE}
                      AND kind IN ('completion','lesson')
                    """
                )
                or 0
            )
            unfiltered_total = int(
                await conn.fetchval("SELECT count(*)::bigint FROM memory.observations") or 0
            )
            ict_total = int(
                await conn.fetchval("SELECT count(*)::bigint FROM memory.observations WHERE source = 'ict-brain'")
                or 0
            )
            rows = await conn.fetch(
                f"""
                SELECT id, kind, source, project, importance, created_at, deprecated_at, superseded_by
                FROM memory.observations
                WHERE {MEMORY_QUERY_WHERE}
                  AND kind IN ('completion','lesson')
                ORDER BY importance DESC NULLS LAST, id ASC
                LIMIT $1
                """,
                safe_limit,
            )
    finally:
        await conn.close()
    return {
        "clean_total": clean_total,
        "signal_total": signal_total,
        "unfiltered_total": unfiltered_total,
        "ict_total": ict_total,
        "rows": [dict(row) for row in rows],
        "where": MEMORY_QUERY_WHERE,
        "exact_live_where": MEMORY_QUERY_WHERE,
        "clean_where": MEMORY_CLEAN_TOTAL_WHERE,
    }


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # The dashboard routes are sync, but tests may call from an async context.
    # Use a private loop in a short helper thread rather than nesting loops.
    box: dict[str, Any] = {}
    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            box["error"] = exc
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _memory_probe() -> ProbeResult:
    global _MVMS_CACHE
    now = time.monotonic()
    if _MVMS_CACHE and now < _MVMS_CACHE[1]:
        return _MVMS_CACHE[0]
    with _MVMS_LOCK:
        now = time.monotonic()
        if _MVMS_CACHE and now < _MVMS_CACHE[1]:
            return _MVMS_CACHE[0]
        query = f"{MEMORY_SIGNAL_WHERE} AND kind IN ('completion','lesson') ORDER BY importance DESC, id ASC LIMIT {_MEMORY_LEAF_LIMIT}"
        try:
            data = _run_async(_memory_query(_MEMORY_LEAF_LIMIT))
            leaves: list[dict[str, Any]] = []
            for row in data["rows"]:
                row_id = str(row.get("id"))
                status = "live" if row.get("deprecated_at") is None and row.get("superseded_by") is None else "stale"
                kind = str(row.get("kind") or "observation")
                importance = int(row.get("importance") or 0)
                leaves.append(
                    _node(
                        f"brain:{row_id}",
                        f"{kind} · imp {importance}",
                        "brain",
                        kind,
                        status,
                        _prov("memory.observations", query, "source", row.get("source")),
                        metric={
                            "id": row_id,
                            "kind": kind,
                            "source": row.get("source"),
                            "project": row.get("project"),
                            "importance": importance,
                            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
                        },
                    )
                )
            signal_total = int(data["signal_total"])
            clean_total = int(data["clean_total"])
            hub = _hub_node(
                "brain",
                signal_total,
                "completed",
                _prov("memory.observations", query, "count", f"signal={signal_total}; clean_total={clean_total}"),
                detail=f"MVMS signal {signal_total}; ICT-excluded clean pool {clean_total}",
                metric={
                    "signal_count": signal_total,
                    "clean_total": clean_total,
                    "unfiltered_total": int(data["unfiltered_total"]),
                    "ict_excluded": int(data["ict_total"]),
                    "leaf_cap": _MEMORY_LEAF_LIMIT,
                    "where": data["where"],
                    "exact_live_where": data["exact_live_where"],
                    "clean_total_where": data["clean_where"],
                },
            )
            result = ProbeResult({"hub": hub, "leaves": leaves, "edges": []})
            _MVMS_CACHE = (result, now + _SUBCACHE_TTL)
            return result
        except Exception as exc:
            if _MVMS_CACHE:
                return _MVMS_CACHE[0]
            return _unreachable_hub("brain", "memory.observations", query, exc)


# Back-compatible alias for packet/tests that name _memory_probe explicitly.
_brain_probe = _memory_probe


# ---------------------------------------------------------------------------
# Code probe — GitNexus repo summary only; never materialize full graph
# ---------------------------------------------------------------------------

def _read_http_json(url: str, *, timeout: float = 4.0, byte_ceiling: int = _HTTP_BYTE_CEILING) -> Any:
    with httpx.Client(timeout=timeout) as client:
        with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > byte_ceiling:
                    raise RuntimeError(f"response exceeded byte ceiling {byte_ceiling} for {url}")
                chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _repo_rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("repos") or payload.get("items") or payload.get("data") or []
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _code_probe() -> ProbeResult:
    global _GITNEXUS_CACHE
    now = time.monotonic()
    if _GITNEXUS_CACHE and now < _GITNEXUS_CACHE[1]:
        return _GITNEXUS_CACHE[0]
    with _GITNEXUS_LOCK:
        now = time.monotonic()
        if _GITNEXUS_CACHE and now < _GITNEXUS_CACHE[1]:
            return _GITNEXUS_CACHE[0]
        try:
            payload = _read_http_json(_GITNEXUS_REPOS_URL, timeout=4.0, byte_ceiling=200_000)
            repos = _repo_rows_from_payload(payload)
            leaves: list[dict[str, Any]] = []
            total_nodes = 0
            total_files = 0
            indexed: list[str] = []
            for repo in repos:
                name = str(repo.get("name") or repo.get("id") or repo.get("path") or "unknown")
                stats = repo.get("stats") if isinstance(repo.get("stats"), dict) else {}
                node_count = int(stats.get("nodes") or repo.get("nodes") or repo.get("node_count") or 0)
                file_count = int(stats.get("files") or repo.get("files") or repo.get("file_count") or 0)
                community_count = int(stats.get("communities") or repo.get("communities") or 0)
                indexed_at = str(repo.get("indexedAt") or repo.get("indexed_at") or "")
                if indexed_at:
                    indexed.append(indexed_at)
                total_nodes += node_count
                total_files += file_count
                leaves.append(
                    _node(
                        f"code:{name}",
                        name,
                        "code",
                        "repo",
                        "ok",
                        _prov(_GITNEXUS_REPOS_URL, "GET /api/repos", "indexedAt", indexed_at),
                        count=node_count,
                        metric={
                            "nodes": node_count,
                            "files": file_count,
                            "edges": int(stats.get("edges") or 0),
                            "communities": community_count,
                            "indexedAt": indexed_at,
                            "path": repo.get("path"),
                        },
                        extra={"repo": name, "indexedAt": indexed_at},
                    )
                )
            hub = _hub_node(
                "code",
                len(repos),
                "ok" if repos else "source unreachable",
                _prov(_GITNEXUS_REPOS_URL, "GET /api/repos", "indexedAt", ", ".join(indexed) or "none"),
                detail=f"{len(repos)} repos; {total_nodes} GitNexus nodes summarized, not shipped as leaves",
                metric={
                    "repos": len(repos),
                    "node_total": total_nodes,
                    "file_total": total_files,
                    "repo_leaf_count": len(leaves),
                    "underlying_nodes_materialized": 0,
                    "byte_ceiling": 200_000,
                },
            )
            result = ProbeResult({"hub": hub, "leaves": leaves, "edges": []})
            _GITNEXUS_CACHE = (result, now + _GITNEXUS_REPO_TTL)
            return result
        except Exception as exc:
            if _GITNEXUS_CACHE:
                return _GITNEXUS_CACHE[0]
            return _unreachable_hub("code", _GITNEXUS_REPOS_URL, "GET /api/repos", exc)


def _code_cluster(repo: str | None = None) -> ProbeResult:
    """Return repo hubs plus, when available, ≤25 bounded graph communities."""
    base = _code_probe()
    if not repo:
        return base
    leaves = [leaf for leaf in base.get("leaves", []) if leaf.get("repo") == repo]
    query_url = f"{_GITNEXUS_GRAPH_URL}?repo={repo}"
    try:
        graph = _read_http_json(query_url, timeout=4.0, byte_ceiling=_HTTP_BYTE_CEILING)
        raw_nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        communities: dict[str, int] = {}
        if isinstance(raw_nodes, list):
            for node in raw_nodes:
                if not isinstance(node, dict):
                    continue
                community = str(node.get("community") or node.get("group") or node.get("kind") or "unknown")
                communities[community] = communities.get(community, 0) + 1
        for community, count in sorted(communities.items(), key=lambda kv: (-kv[1], kv[0]))[:_CODE_COMMUNITY_LIMIT]:
            leaves.append(
                _node(
                    f"code:{repo}:community:{community}",
                    f"{repo} · {community}",
                    "code",
                    "community",
                    "ok",
                    _prov(query_url, f"GET /api/graph?repo={repo} cap={_CODE_COMMUNITY_LIMIT}", "community", community),
                    count=count,
                    metric={"repo": repo, "community": community, "members": count},
                )
            )
    except Exception:
        # Repo counts are the contract-critical payload. Graph expansion is optional
        # and bounded; on GitNexus wedge, return the repo node instead of raising.
        pass
    return ProbeResult({"hub": base.get("hub"), "leaves": leaves[: _CODE_COMMUNITY_LIMIT + 1], "edges": []})


# ---------------------------------------------------------------------------
# Lanes probe — Kanban task_runs primary signal + stale codex refs secondary
# ---------------------------------------------------------------------------

def _task_run_status(raw_status: str, outcome: str | None = None) -> str:
    status = (raw_status or "unknown").lower()
    if status == "running":
        return "running"
    if status in {"completed", "done"} or outcome == "completed":
        return "completed"
    if status == "blocked" or outcome == "blocked":
        return "blocked"
    if status == "scheduled":
        return "queued"
    if status in {"reclaimed", "released"} or outcome == "reclaimed":
        return "stale"
    if status in {"crashed", "timed_out", "failed"}:
        return "blocked"
    return status or "unknown"


def _task_run_time(row: sqlite3.Row) -> int:
    return int(row["ended_at"] or row["last_heartbeat_at"] or row["started_at"] or 0)


def _read_task_runs(db_path: Path = KANBAN_DB) -> list[sqlite3.Row]:
    query = (
        "SELECT id, task_id, profile, step_key, status, outcome, worker_pid, "
        "last_heartbeat_at, started_at, ended_at, summary, error "
        "FROM task_runs "
        "ORDER BY COALESCE(ended_at,last_heartbeat_at,started_at) DESC, id DESC"
    )
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


def _codex_branch_rows(repo_dir: Path = REPO_ROOT, limit: int = _CODEX_BRANCH_LIMIT) -> list[dict[str, str]]:
    result = subprocess.run(
        [
            "git",
            "for-each-ref",
            f"--count={int(limit)}",
            "--sort=-committerdate",
            "--format=%(committerdate:iso8601)%09%(refname:short)%09%(objectname:short)",
            "refs/heads/codex/**",
        ],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    rows: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append({"committerdate": parts[0], "branch": parts[1], "sha": parts[2]})
    return rows


def _lanes_probe(db_path: Path = KANBAN_DB, repo_dir: Path = REPO_ROOT) -> ProbeResult:
    task_query = (
        "SELECT id,task_id,profile,step_key,status,outcome,last_heartbeat_at,started_at,ended_at "
        "FROM task_runs ORDER BY COALESCE(ended_at,last_heartbeat_at,started_at) DESC, id DESC"
    )
    branch_query = "git for-each-ref --sort=-committerdate --format=... refs/heads/codex/** --count=25"
    try:
        rows = _read_task_runs(db_path)
        status_counts: dict[str, int] = {}
        normalized_counts: dict[str, int] = {}
        recent_by_profile: dict[str, sqlite3.Row] = {}
        running_profiles: set[str] = set()
        blocked_profiles: set[str] = set()
        statuses: list[str] = []
        for row in rows:
            raw_status = str(row["status"] or "unknown")
            status_counts[raw_status] = status_counts.get(raw_status, 0) + 1
            normalized = _task_run_status(raw_status, row["outcome"])
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1
            statuses.append(normalized)
            profile = str(row["profile"] or "").strip()
            if profile and profile not in recent_by_profile:
                recent_by_profile[profile] = row
            if normalized == "running" and profile:
                running_profiles.add(profile)
            if normalized == "blocked" and profile:
                blocked_profiles.add(profile)

        leaves: list[dict[str, Any]] = []
        for lane_no in range(1, _LOKI_LANE_COUNT + 1):
            lane_id = f"loki{lane_no}"
            display = f"loki {lane_no}"
            aliases = {lane_id, f"loki-{lane_no}", f"loki_{lane_no}", f"loki {lane_no}"}
            matched = next((recent_by_profile[a] for a in aliases if a in recent_by_profile), None)
            if any(alias in running_profiles for alias in aliases):
                status = "running"
            elif any(alias in blocked_profiles for alias in aliases):
                status = "blocked"
            else:
                status = "idle"
            prov_value = "no matching task_runs.profile row"
            metric: dict[str, Any] = {"lane_number": lane_no, "task_runs_profile_aliases": sorted(aliases)}
            if matched:
                prov_value = matched["status"]
                metric.update(
                    {
                        "task_run_id": matched["id"],
                        "task_id": matched["task_id"],
                        "profile": matched["profile"],
                        "outcome": matched["outcome"],
                        "last_seen_epoch": _task_run_time(matched),
                    }
                )
            leaves.append(
                _node(
                    f"lanes:{lane_id}",
                    display,
                    "lanes",
                    "loki-lane",
                    status,
                    _prov(str(db_path), task_query, "task_runs.status", prov_value),
                    metric=metric,
                    extra={"lane_number": lane_no, "secondary": False},
                )
            )

        branches = _codex_branch_rows(repo_dir, _CODEX_BRANCH_LIMIT)
        for branch in branches:
            leaves.append(
                _node(
                    f"lanes:branch:{branch['branch']}",
                    branch["branch"],
                    "lanes",
                    "codex-branch",
                    "stale",
                    _prov(str(repo_dir), branch_query, "committerdate", branch["committerdate"]),
                    metric={"branch": branch["branch"], "sha": branch["sha"], "committerdate": branch["committerdate"], "secondary": True},
                    extra={"branch": branch["branch"], "secondary": True, "dimmed": True},
                )
            )

        live_focus = normalized_counts.get("running", 0) + normalized_counts.get("blocked", 0) + normalized_counts.get("queued", 0)
        hub_status = _hub_rollup_status(
            [s for s, c in normalized_counts.items() for _ in range(int(c))]
        )
        hub = _hub_node(
            "lanes",
            len(rows),
            hub_status,
            _prov(str(db_path), task_query, "task_runs.status", f"{len(rows)} task_runs; statuses={status_counts}"),
            detail="Primary signal is live Kanban task_runs; codex/** branches are secondary stale refs only.",
            metric={
                "task_runs_total": len(rows),
                "task_runs_status_counts": status_counts,
                "task_runs_normalized_counts": normalized_counts,
                "live_focus_count": live_focus,
                "loki_lane_nodes": _LOKI_LANE_COUNT,
                "codex_branch_query": "refs/heads/codex/**",
                "codex_branch_nodes": len(branches),
                "codex_branch_cap": _CODEX_BRANCH_LIMIT,
                "codex_branches_secondary_stale": True,
                "projects_lanes_branch_name_edge": "dropped: branch_name has no live join key",
            },
        )
        return ProbeResult({"hub": hub, "leaves": leaves, "edges": []})
    except Exception as exc:
        return _unreachable_hub("lanes", f"{db_path} + {repo_dir}", f"{task_query}; {branch_query}", exc)


# ---------------------------------------------------------------------------
# Verified inter-hub bridges — emit only if the live mechanism is present
# ---------------------------------------------------------------------------

def _systemctl_user(*args: str) -> str:
    result = subprocess.run(
        ["systemctl", "--user", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    return result.stdout.strip()


def _verify_projects_brain_bridge() -> dict[str, str] | None:
    try:
        enabled = _systemctl_user("is-enabled", "kanban-mvms-bridge.timer")
    except Exception:
        return None
    if enabled != "enabled":
        return None
    return _prov("systemctl --user", "is-enabled kanban-mvms-bridge.timer", "unit.enabled", enabled)


def _verify_brain_lanes_bridge() -> dict[str, str] | None:
    try:
        payload = _read_http_json(_RECALL_HEALTH_URL, timeout=3.0, byte_ceiling=50_000)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    return _prov(_RECALL_HEALTH_URL, "GET /health", "ok", payload.get("ok"))


def _verify_code_deploy_bridge(repo_dir: Path = REPO_ROOT) -> dict[str, str] | None:
    hook = repo_dir / ".git" / "hooks" / "post-commit"
    try:
        text = hook.read_text(encoding="utf-8")
    except OSError:
        return None
    if "gitnexus-reindex" not in text or "hermes_cli.gitnexus_repo_manager" not in text:
        return None
    return _prov(str(hook), "read_text(encoding='utf-8')", "gitnexus hook marker", "gitnexus-reindex + hermes_cli.gitnexus_repo_manager")


def _verify_learning_brain_bridge() -> dict[str, str] | None:
    try:
        payload = _read_http_json(_RECALL_HEALTH_URL, timeout=3.0, byte_ceiling=50_000)
        close_loop = (_RECALL_MCP_DIR / "close_loop.py").read_text(encoding="utf-8")
        recorder = (_RECALL_MCP_DIR / "record_loop_lesson.py").read_text(encoding="utf-8")
    except Exception:
        return None
    recall_ready = isinstance(payload, dict) and payload.get("ok") is True and payload.get("model_warmed") is True
    helper_ready = "record_loop_lesson" in close_loop and "mvms_record_lesson" in recorder
    if not recall_ready or not helper_ready:
        return None
    return _prov(
        f"{_RECALL_HEALTH_URL} + {_RECALL_MCP_DIR}",
        "GET /health + read close_loop.py/record_loop_lesson.py",
        "recall close-loop write-back",
        "model_warmed=True; close_loop→record_loop_lesson→mvms_record_lesson",
    )


def _verify_config_infra_bridge(config_path: Path = CONFIG_PATH) -> dict[str, str] | None:
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        model_cfg = config.get("model", {}) if isinstance(config.get("model"), dict) else {}
        default_model = model_cfg.get("default") or model_cfg.get("model")
        provider = model_cfg.get("provider")
        gateway_state = _systemctl_user("is-active", "hermes-gateway.service")
    except Exception:
        return None
    if not default_model or gateway_state != "active":
        return None
    return _prov(str(config_path), "yaml.safe_load + systemctl --user is-active hermes-gateway.service", "model.default/provider", f"{default_model}/{provider}; gateway={gateway_state}")


def _verified_bridge_edges() -> list[dict[str, Any]]:
    verifiers: dict[str, Callable[[], dict[str, str] | None]] = {
        "projects-brain": _verify_projects_brain_bridge,
        "brain-lanes": _verify_brain_lanes_bridge,
        "code-deploy": _verify_code_deploy_bridge,
        "learning-brain": _verify_learning_brain_bridge,
        "config-infra": _verify_config_infra_bridge,
    }
    edges: list[dict[str, Any]] = []
    for edge_id, source, target, label in BRIDGES:
        provenance = verifiers[edge_id]()
        if provenance is None:
            continue
        edges.append(_edge(edge_id, source, target, label, kind="bridge", provenance=provenance, verified=True))
    return edges


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
    edges = _verified_bridge_edges()
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
    repo: str | None = None,
) -> dict[str, Any]:
    """Return a cursor-paginated leaf page for a cluster; never 500."""
    try:
        safe_limit = min(max(int(limit), 1), 300)
        cache_key = (cluster_id if not repo else f"{cluster_id}:{repo}", cursor, safe_limit)
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
            probe = _code_cluster(repo) if cluster_id == "code" else _run_probe(cluster_id)
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
