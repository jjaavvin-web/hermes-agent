"""Get Some dashboard API — project roster + living work nexus graph.

All routes live under ``/api/dashboard`` and are protected by the existing
SPA/session middleware.  The handlers are intentionally read-only: Kanban board
state is the source of truth; this module only projects it into dashboard-ready
shapes.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import APIRouter

from hermes_cli import kanban_db
from hermes_cli.pulse_data import _iter_kanban_dbs, _open_kanban_ro

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-get-some"])

_PROJECTS_TTL = 10.0
_NEXUS_TTL = 10.0
_PROJECTS_CACHE: tuple[dict, float] | None = None
_NEXUS_CACHE: tuple[dict, float] | None = None
_PROJECTS_LOCK = threading.Lock()
_NEXUS_LOCK = threading.Lock()

WEIGHT: dict[str, float] = {
    "triage": 0.0,
    "todo": 0.0,
    "scheduled": 0.1,
    "ready": 0.1,
    "running": 0.5,
    "blocked": 0.25,
    "review": 0.8,
    "done": 1.0,
    "archived": 1.0,
}
_COMPLETED_STATUSES = {"done", "archived"}
_NEXUS_MAX_NODES = 150
_TASK_STATUS_SORT_RANK: dict[str, int] = {
    "running": 0,
    "review": 1,
    "blocked": 2,
    "ready": 3,
    "scheduled": 4,
    "triage": 5,
    "todo": 6,
    "done": 7,
    "archived": 8,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata_by_slug() -> dict[str, dict]:
    try:
        boards = kanban_db.list_boards(include_archived=True)
    except Exception as exc:  # pragma: no cover - defensive against malformed board dirs
        log.warning("Could not list kanban board metadata: %s", exc)
        boards = []
    return {str(board.get("slug") or ""): board for board in boards if board.get("slug")}


def _board_meta(slug: str, metadata: dict[str, dict]) -> dict:
    meta = dict(metadata.get(slug) or {})
    if not meta:
        try:
            meta = dict(kanban_db.read_board_metadata(slug))
        except Exception as exc:  # pragma: no cover - read_board_metadata should not raise
            log.warning("Could not read kanban board metadata for %s: %s", slug, exc)
            meta = {"slug": slug}
    return {
        "slug": slug,
        "name": meta.get("name") or slug,
        "icon": meta.get("icon") or "",
        "color": meta.get("color") or "",
        "archived": bool(meta.get("archived")),
    }


def _status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    return {str(row["status"]): int(row["n"] or 0) for row in rows}


def _last_activity(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(COALESCE(completed_at, started_at, created_at)) AS last_activity FROM tasks"
    ).fetchone()
    if row is None or row["last_activity"] is None:
        return None
    try:
        return int(row["last_activity"])
    except (TypeError, ValueError):
        return None


def _completion_pct(by_status: dict[str, int]) -> int:
    total = sum(by_status.values())
    if total <= 0:
        return 0
    weighted = sum(WEIGHT.get(status, 0.0) * count for status, count in by_status.items())
    return round(100 * weighted / total)


def _build_projects_snapshot() -> dict:
    metadata = _metadata_by_slug()
    projects: list[dict] = []
    for slug, db_path in _iter_kanban_dbs():
        meta = _board_meta(slug, metadata)
        try:
            conn = _open_kanban_ro(db_path)
        except Exception as exc:
            log.warning("Could not open kanban board %s at %s: %s", slug, db_path, exc)
            continue
        try:
            by_status = _status_counts(conn)
            total = sum(by_status.values())
            projects.append({
                "slug": slug,
                "name": meta["name"],
                "icon": meta["icon"],
                "color": meta["color"],
                "archived": meta["archived"],
                "total": total,
                "completion_pct": _completion_pct(by_status),
                "by_status": by_status,
                "active": int(by_status.get("running", 0))
                + int(by_status.get("ready", 0))
                + int(by_status.get("review", 0)),
                "blocked": int(by_status.get("blocked", 0)),
                "last_activity": _last_activity(conn),
            })
        except sqlite3.Error as exc:
            log.warning("Could not summarize kanban board %s: %s", slug, exc)
        finally:
            conn.close()
    projects.sort(key=lambda p: p.get("last_activity") or 0, reverse=True)
    return {"scanned_at": _now_iso(), "projects": projects}


def _cached_projects_snapshot() -> dict:
    global _PROJECTS_CACHE
    now = time.monotonic()
    with _PROJECTS_LOCK:
        if _PROJECTS_CACHE is not None:
            value, expires_at = _PROJECTS_CACHE
            if now < expires_at:
                return value
        snapshot = _build_projects_snapshot()
        _PROJECTS_CACHE = (snapshot, now + _PROJECTS_TTL)
        return snapshot


def _truncate_label(value: Any, *, limit: int = 40) -> str:
    text = str(value or "Untitled").strip() or "Untitled"
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _task_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    required = ["id", "title", "status"]
    optional = ["priority", "created_at", "started_at", "completed_at", "branch_name"]
    select_cols = required + [col for col in optional if col in cols]
    return conn.execute(f"SELECT {', '.join(select_cols)} FROM tasks").fetchall()


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _task_ts(row: sqlite3.Row) -> Any:
    return _row_get(row, "completed_at") or _row_get(row, "started_at") or _row_get(row, "created_at")


def _project_node(slug: str, meta: dict) -> dict:
    return {
        "id": f"project:{slug}",
        "kind": "project",
        "label": meta["name"],
        "color": meta["color"],
        "icon": meta["icon"],
        "board": slug,
    }


def _task_node(slug: str, row: sqlite3.Row) -> dict:
    status = str(_row_get(row, "status") or "")
    return {
        "id": f"task:{slug}:{row['id']}",
        "kind": "task",
        "label": _truncate_label(_row_get(row, "title")),
        "status": status,
        "board": slug,
        "priority": int(_row_get(row, "priority") or 0),
        "completed": status in _COMPLETED_STATUSES,
        "ts": _task_ts(row),
        "branch_name": _row_get(row, "branch_name"),
    }


def _task_sort_key(row: sqlite3.Row) -> tuple[int, int, int, str]:
    status = str(_row_get(row, "status") or "")
    priority = int(_row_get(row, "priority") or 0)
    ts_value = _task_ts(row) or 0
    try:
        ts = int(ts_value)
    except (TypeError, ValueError):
        ts = 0
    return (_TASK_STATUS_SORT_RANK.get(status, 99), -priority, -ts, str(row["id"]))


def _aggregate_task_node(slug: str, hidden_count: int) -> dict:
    return {
        "id": f"aggregate:{slug}:hidden-tasks",
        "kind": "task",
        "label": f"+{hidden_count} more",
        "status": "aggregated",
        "board": slug,
        "priority": 0,
        "completed": False,
        "aggregate": True,
        "hidden_count": hidden_count,
    }


def _selected_task_ids_by_board(
    rows_by_board: dict[str, list[sqlite3.Row]],
    *,
    max_nodes: int = _NEXUS_MAX_NODES,
) -> dict[str, set[str]]:
    board_count = len(rows_by_board)
    if board_count <= 0:
        return {}
    aggregate_count = sum(1 for rows in rows_by_board.values() if rows)
    task_budget = max(0, max_nodes - board_count - aggregate_count)
    selected: dict[str, set[str]] = {slug: set() for slug in rows_by_board}
    if task_budget <= 0:
        return selected

    sorted_rows_by_board = {
        slug: sorted(rows, key=_task_sort_key)
        for slug, rows in rows_by_board.items()
    }
    base_per_board = max(1, task_budget // board_count)
    remaining = task_budget
    for slug in sorted(sorted_rows_by_board):
        if remaining <= 0:
            break
        rows = sorted_rows_by_board[slug]
        take = min(len(rows), base_per_board, remaining)
        selected[slug].update(str(row["id"]) for row in rows[:take])
        remaining -= take

    next_index_by_board = {slug: len(ids) for slug, ids in selected.items()}
    while remaining > 0:
        added = False
        for slug in sorted(sorted_rows_by_board):
            rows = sorted_rows_by_board[slug]
            idx = next_index_by_board[slug]
            if idx >= len(rows):
                continue
            selected[slug].add(str(rows[idx]["id"]))
            next_index_by_board[slug] = idx + 1
            remaining -= 1
            added = True
            if remaining <= 0:
                break
        if not added:
            break
    return selected


def _read_core_nexus() -> tuple[list[dict], list[dict], dict[str, list[str]], dict[str, list[str]]]:
    metadata = _metadata_by_slug()
    nodes: list[dict] = []
    edges: list[dict] = []
    rows_by_board: dict[str, list[sqlite3.Row]] = {}
    links_by_board: dict[str, list[sqlite3.Row]] = {}
    meta_by_board: dict[str, dict] = {}
    tasks_by_branch: dict[str, list[str]] = {}
    tasks_by_session: dict[str, list[str]] = {}

    for slug, db_path in _iter_kanban_dbs():
        meta = _board_meta(slug, metadata)
        if meta["archived"]:
            continue
        try:
            conn = _open_kanban_ro(db_path)
        except Exception as exc:
            log.warning("Could not open kanban board %s at %s: %s", slug, db_path, exc)
            continue
        try:
            meta_by_board[slug] = meta
            rows_by_board[slug] = _task_rows(conn)
            try:
                links_by_board[slug] = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
            except sqlite3.OperationalError:
                links_by_board[slug] = []
        except sqlite3.Error as exc:
            log.warning("Could not build kanban nexus for board %s: %s", slug, exc)
        finally:
            conn.close()

    selected_ids_by_board = _selected_task_ids_by_board(rows_by_board)

    for slug in sorted(meta_by_board):
        meta = meta_by_board[slug]
        project_id = f"project:{slug}"
        nodes.append(_project_node(slug, meta))
        selected_ids = selected_ids_by_board.get(slug, set())
        emitted_task_ids: set[str] = set()
        rows = sorted(rows_by_board.get(slug, []), key=_task_sort_key)
        for row in rows:
            task_id = str(row["id"])
            if task_id not in selected_ids:
                continue
            emitted_task_ids.add(task_id)
            node = _task_node(slug, row)
            nodes.append(node)
            edges.append({
                "id": f"contains:{slug}:{task_id}",
                "kind": "contains",
                "source": project_id,
                "target": node["id"],
            })
            branch = node.get("branch_name")
            if branch:
                tasks_by_branch.setdefault(str(branch), []).append(node["id"])
            tasks_by_session.setdefault(task_id, []).append(node["id"])

        hidden_count = max(0, len(rows) - len(emitted_task_ids))
        if hidden_count > 0:
            aggregate = _aggregate_task_node(slug, hidden_count)
            nodes.append(aggregate)
            edges.append({
                "id": f"contains:{slug}:hidden-tasks",
                "kind": "contains",
                "source": project_id,
                "target": aggregate["id"],
            })

        for link in links_by_board.get(slug, []):
            parent_id = str(link["parent_id"])
            child_id = str(link["child_id"])
            if parent_id not in emitted_task_ids or child_id not in emitted_task_ids:
                continue
            edges.append({
                "id": f"blocks:{slug}:{parent_id}:{child_id}",
                "kind": "blocks",
                "source": f"task:{slug}:{parent_id}",
                "target": f"task:{slug}:{child_id}",
            })

    return nodes, edges, tasks_by_branch, tasks_by_session


def _iter_codex_sessions() -> Iterable[dict]:
    from hermes_cli.dashboard_codex_sessions import _cached_snapshot

    snapshot = _cached_snapshot()
    sessions = snapshot.get("sessions") or []
    if not isinstance(sessions, list):
        return []
    return sessions


def _codex_pr_overlay(
    tasks_by_branch: dict[str, list[str]],
    tasks_by_session: dict[str, list[str]],
) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_prs: set[str] = set()
    for session in _iter_codex_sessions():
        number = session.get("pr_number")
        url = session.get("pr_url")
        if not number and not url:
            continue
        pr_key = str(number or url)
        pr_id = f"pr:{pr_key}"
        if pr_id not in seen_prs:
            nodes.append({
                "id": pr_id,
                "kind": "pr",
                "label": f"PR #{pr_key}" if number else "PR",
                "state": session.get("pr_state") or session.get("state"),
                "url": url,
                "merged_at": session.get("merged_at"),
            })
            seen_prs.add(pr_id)
        task_node_ids: list[str] = []
        head_branch = session.get("head_branch")
        if head_branch:
            task_node_ids.extend(tasks_by_branch.get(str(head_branch), []))
        session_id = session.get("session_id")
        if session_id:
            task_node_ids.extend(tasks_by_session.get(str(session_id), []))
            task_node_ids.extend(tasks_by_branch.get(str(session_id), []))
        for task_node_id in sorted(set(task_node_ids)):
            edges.append({
                "id": f"delivered_by:{task_node_id}:{pr_id}",
                "kind": "delivered_by",
                "source": task_node_id,
                "target": pr_id,
            })
    return nodes, edges


def _git_river_pr_overlay() -> tuple[list[dict], list[dict]]:
    """Best-effort PR nodes from the optional git-river overlay if present."""
    try:
        import hermes_cli.dashboard_codex_sessions as codex_sessions
    except Exception:
        return [], []
    build_river = getattr(codex_sessions, "_build_river", None)
    if build_river is None:
        return [], []
    river = build_river()
    rows = river.get("items") or river.get("commits") or river.get("river") or []
    nodes: list[dict] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        subject = str(row.get("subject") or row.get("title") or "")
        marker = next((part for part in subject.split() if part.startswith("#") and part[1:].isdigit()), "")
        if not marker:
            continue
        pr_key = marker[1:]
        pr_id = f"pr:{pr_key}"
        if pr_id in seen:
            continue
        nodes.append({
            "id": pr_id,
            "kind": "pr",
            "label": f"PR #{pr_key}",
            "state": row.get("state") or row.get("status"),
            "url": row.get("url"),
            "merged_at": row.get("merged_at") or row.get("committed_at"),
        })
        seen.add(pr_id)
    return nodes, []


def _pr_sort_key(node: dict) -> tuple[int, str]:
    merged_at = node.get("merged_at") or ""
    return (0 if merged_at else 1, str(node.get("id") or ""))


def _enforce_nexus_node_cap(nodes: list[dict], edges: list[dict], max_nodes: int = _NEXUS_MAX_NODES) -> tuple[list[dict], list[dict]]:
    if len(nodes) <= max_nodes:
        return nodes, edges
    protected_nodes = [node for node in nodes if node.get("kind") == "project" or node.get("aggregate") is True]
    protected_object_ids = {id(node) for node in protected_nodes}
    optional_nodes = [node for node in nodes if id(node) not in protected_object_ids]
    remaining = max(0, max_nodes - len(protected_nodes))
    kept_object_ids = protected_object_ids | {id(node) for node in optional_nodes[:remaining]}
    kept_nodes = [node for node in nodes if id(node) in kept_object_ids]
    kept_ids = {str(node.get("id")) for node in kept_nodes}
    kept_edges = [
        edge for edge in edges
        if str(edge.get("source")) in kept_ids and str(edge.get("target")) in kept_ids
    ]
    return kept_nodes, kept_edges


def _build_nexus_snapshot() -> dict:
    nodes, edges, tasks_by_branch, tasks_by_session = _read_core_nexus()
    degraded_mode: list[str] = []

    try:
        pr_nodes, pr_edges = _codex_pr_overlay(tasks_by_branch, tasks_by_session)
        nodes.extend(sorted(pr_nodes, key=_pr_sort_key))
        edges.extend(pr_edges)
    except Exception as exc:
        log.warning("Codex PR overlay failed: %s", exc)
        degraded_mode.append("codex_pr_overlay")

    try:
        river_nodes, river_edges = _git_river_pr_overlay()
        existing_ids = {node["id"] for node in nodes}
        nodes.extend(sorted((node for node in river_nodes if node["id"] not in existing_ids), key=_pr_sort_key))
        edges.extend(river_edges)
    except Exception as exc:
        log.warning("Git river PR overlay failed: %s", exc)
        degraded_mode.append("git_river_overlay")

    nodes, edges = _enforce_nexus_node_cap(nodes, edges)

    return {
        "scanned_at": _now_iso(),
        "nodes": nodes,
        "edges": edges,
        "degraded_mode": degraded_mode,
    }


def _cached_nexus_snapshot() -> dict:
    global _NEXUS_CACHE
    now = time.monotonic()
    with _NEXUS_LOCK:
        if _NEXUS_CACHE is not None:
            value, expires_at = _NEXUS_CACHE
            if now < expires_at:
                return value
        snapshot = _build_nexus_snapshot()
        _NEXUS_CACHE = (snapshot, now + _NEXUS_TTL)
        return snapshot


@router.get("/projects", summary="Project roster across kanban boards")
def get_projects(include_archived: bool = False) -> dict:
    snapshot = _cached_projects_snapshot()
    projects = snapshot.get("projects", [])
    if not include_archived:
        projects = [project for project in projects if not project.get("archived")]
    return {"scanned_at": snapshot.get("scanned_at"), "projects": projects}


@router.get("/work-nexus", summary="Living work nexus graph")
def get_work_nexus() -> dict:
    return _cached_nexus_snapshot()
