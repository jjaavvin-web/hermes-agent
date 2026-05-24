"""Pulse tab data fusion — hives + nexus + kanban into Pulse-ready shapes."""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import sqlite3
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

_QUEUE_STATUSES = frozenset({"ready", "running", "triage"})
_GRAPH_CARD_STATUSES = frozenset({"ready", "running", "blocked", "triage"})
_HIVE_BLOCKED_STATUSES = frozenset({"blocked", "failed", "stale"})
_HIVE_IDLE_WINDOW_SECONDS = 3600
_RECENT_HANDOFF_WINDOW_SECONDS = 6 * 3600
_LOG_POLL_INTERVAL = 1.0
_LOG_RATE_CAP_PER_SECOND = 20
_HERMES_REPO_ENV = "HERMES_REPO_PATH"
_DEFAULT_HERMES_REPO = "/home/josep/.local/share/hermes-agent"
_HERMES_HOME = Path.home() / ".hermes"


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _hive_group(hive: dict, now: float) -> str:
    status = hive.get("status", "")
    if status == "running" and hive.get("tmux_alive"):
        return "hive-active"
    if status == "completed":
        val = hive.get("updated_at") or hive.get("log_mtime")
        if val:
            try:
                ts = (datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
                      if isinstance(val, str) else float(val))
                if now - ts <= _HIVE_IDLE_WINDOW_SECONDS:
                    return "hive-idle"
            except Exception:
                pass
        return "hive-idle"
    if status in _HIVE_BLOCKED_STATUSES:
        return "hive-blocked"
    return "hive-idle"


def _card_group(status: str) -> Optional[str]:
    return {
        "ready": "card-ready",
        "running": "card-running",
        "blocked": "card-blocked",
        "triage": "card-triage",
    }.get(status)


def _swarm_worker_count() -> int:
    try:
        from hermes_cli.dashboard_health import _build_snapshot
        snap = _build_snapshot()
        return int((snap.get("swarm") or {}).get("workerCount", 0) or 0)
    except Exception as exc:
        logger.warning("_swarm_worker_count failed: %s", exc)
        return 0


def _iter_kanban_dbs() -> list[tuple[str, Path]]:
    hermes_home = Path.home() / ".hermes"
    result: list[tuple[str, Path]] = []
    legacy = hermes_home / "kanban.db"
    if legacy.exists():
        result.append(("default", legacy))
    for path in sorted(glob.glob(str(hermes_home / "kanban/boards/*/kanban.db"))):
        p = Path(path)
        result.append((p.parent.name, p))
    return result


def _open_kanban_ro(p: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_cards(statuses: frozenset[str]) -> tuple[list[tuple[str, sqlite3.Row]], bool]:
    """Return ([(board_slug, row), ...], opened_at_least_one_db)."""
    db_paths = _iter_kanban_dbs()
    results: list[tuple[str, sqlite3.Row]] = []
    opened_any = False
    ph = ",".join("?" * len(statuses))
    for board_slug, p in db_paths:
        try:
            conn = _open_kanban_ro(p)
            opened_any = True
            try:
                for row in conn.execute(
                    f"SELECT id,title,status,priority,assignee,created_at"
                    f" FROM cards WHERE status IN ({ph})",
                    tuple(statuses),
                ).fetchall():
                    try:
                        results.append((board_slug, row))
                    except Exception as exc:
                        logger.warning("skipping malformed kanban row: %s", exc)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("could not open kanban db %s: %s", p, exc)
    return results, opened_any


def _hive_ts(h: dict) -> float:
    val = h.get("updated_at") or h.get("log_mtime")
    if not val:
        return 0.0
    try:
        return (datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
                if isinstance(val, str) else float(val))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_pulse_graph(*, now: float | None = None) -> dict:
    """Returns {'nodes': [...], 'edges': [...], 'degraded_mode': [...]}."""
    if now is None:
        now = _now_ts()

    degraded_mode: list[str] = []

    hives: list[dict] = []
    try:
        from hermes_cli.dashboard_health import _get_hives_snapshot
        hives = _get_hives_snapshot().get("hives", [])
    except Exception as exc:
        logger.warning("_get_hives_snapshot failed: %s", exc)

    try:
        from hermes_cli.dashboard_health import _get_gitnexus_runtime_snapshot
        if "_error" in _get_gitnexus_runtime_snapshot():
            degraded_mode.append("gitnexus_unreachable")
    except Exception as exc:
        logger.warning("_get_gitnexus_runtime_snapshot failed: %s", exc)
        degraded_mode.append("gitnexus_unreachable")

    try:
        from hermes_cli.dashboard_health import _get_active_model
        active_model = _get_active_model() or "unknown"
    except Exception as exc:
        logger.warning("_get_active_model failed: %s", exc)
        active_model = "unknown"

    workers = _swarm_worker_count()

    nodes: list[dict] = []
    hive_ids: set[str] = set()
    for hive in hives:
        try:
            hid = hive["id"]
            nodes.append({
                "id": f"hive:{hid}",
                "label": hive.get("track_title") or hid,
                "group": _hive_group(hive, now),
                "status": hive.get("status", ""),
                "last_activity": hive.get("log_mtime"),
                "model": active_model,
                "workers": workers,
                "kind": "hive",
            })
            hive_ids.add(hid)
        except Exception as exc:
            logger.warning("skipping malformed hive node: %s", exc)

    card_ids: set[str] = set()
    kanban_rows, opened_any = _read_cards(_GRAPH_CARD_STATUSES)
    db_paths = _iter_kanban_dbs()
    if db_paths and not opened_any:
        degraded_mode.append("kanban_unreachable")

    for board_slug, row in kanban_rows:
        try:
            status = row["status"]
            group = _card_group(status)
            if group is None:
                continue
            cid = row["id"]
            created_at = row["created_at"]
            nodes.append({
                "id": f"card:{cid}",
                "label": (row["title"] or "")[:60],
                "group": group,
                "status": status,
                "priority": int(row["priority"] or 0),
                "age_seconds": max(0, int(now - (created_at or now))),
                "board": board_slug,
                "assignee": row["assignee"] or "unassigned",
                "kind": "card",
            })
            card_ids.add(cid)
        except Exception as exc:
            logger.warning("skipping malformed kanban row: %s", exc)

    edges: list[dict] = []

    for hive in hives:
        try:
            hid = hive.get("id")
            tracking = hive.get("tracking_card")
            if tracking and tracking in card_ids and hid in hive_ids:
                edges.append({"id": f"track:{hid}", "source": f"hive:{hid}",
                              "target": f"card:{tracking}", "kind": "tracking"})
        except Exception as exc:
            logger.warning("skipping tracking edge: %s", exc)

    for board_slug, p in db_paths:
        try:
            conn = _open_kanban_ro(p)
            try:
                try:
                    links = conn.execute(
                        "SELECT parent_id,child_id FROM task_links"
                        " WHERE link_type='blocks' OR link_type IS NULL"
                    ).fetchall()
                except sqlite3.OperationalError:
                    links = []
                for lnk in links:
                    try:
                        pid, cid = lnk["parent_id"], lnk["child_id"]
                        if pid in card_ids and cid in card_ids:
                            edges.append({"id": f"block:{cid}:{pid}",
                                         "source": f"card:{pid}",
                                         "target": f"card:{cid}",
                                         "kind": "blocked_by"})
                    except Exception as exc:
                        logger.warning("skipping task_link row: %s", exc)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("could not read task_links from %s: %s", p, exc)

    for hive in hives:
        try:
            if hive.get("status") != "completed":
                continue
            if now - _hive_ts(hive) > _RECENT_HANDOFF_WINDOW_SECONDS:
                continue
            rp_str = hive.get("final_report_path")
            if not rp_str or not Path(rp_str).exists():
                continue
            try:
                text = Path(rp_str).read_text(errors="replace")[:4096]
            except Exception as exc:
                logger.warning("could not read final_report %s: %s", rp_str, exc)
                continue
            this_id = hive["id"]
            for other in hives:
                try:
                    oid = other["id"]
                    if oid != this_id and oid in hive_ids and oid in text:
                        edges.append({"id": f"handoff:{this_id}:{oid}",
                                     "source": f"hive:{this_id}",
                                     "target": f"hive:{oid}",
                                     "kind": "handoff"})
                except Exception as exc:
                    logger.warning("skipping handoff edge: %s", exc)
        except Exception as exc:
            logger.warning("skipping hive handoff scan: %s", exc)

    return {"nodes": nodes, "edges": edges, "degraded_mode": degraded_mode}


def build_pulse_queue(*, limit: int = 50, now: float | None = None) -> dict:
    """Returns {'cards': [...]}."""
    if now is None:
        now = _now_ts()

    rows, _ = _read_cards(_QUEUE_STATUSES)
    cards: list[dict] = []
    for board_slug, row in rows:
        try:
            created_at = row["created_at"]
            cards.append({
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "board": board_slug,
                "priority": int(row["priority"] or 0),
                "assignee": row["assignee"] or "unassigned",
                "age_seconds": max(0, int(now - (created_at or now))),
                "created_at": created_at,
            })
        except Exception as exc:
            logger.warning("skipping malformed kanban row: %s", exc)

    _order = {"running": 0, "ready": 1, "triage": 2}
    cards.sort(key=lambda c: (_order.get(c["status"], 99), -c["priority"], c["age_seconds"]))
    return {"cards": cards[:limit]}


def build_pulse_kpis(*, now: float | None = None) -> dict:
    """Returns the KPI dict."""
    if now is None:
        now = _now_ts()

    hives: list[dict] = []
    try:
        from hermes_cli.dashboard_health import _get_hives_snapshot
        hives = _get_hives_snapshot().get("hives", [])
    except Exception as exc:
        logger.warning("_get_hives_snapshot failed in kpis: %s", exc)

    active_hives = sum(1 for h in hives if h.get("status") == "running")

    pending_cards = 0
    try:
        rows, _ = _read_cards(frozenset({"ready", "triage"}))
        pending_cards = len(rows)
    except Exception as exc:
        logger.warning("pending_cards count failed: %s", exc)

    today_spend_usd = 0.0
    try:
        from hermes_cli.dashboard_health import _build_snapshot
        today_spend_usd = float(_build_snapshot().get("spendToday") or 0.0)
    except Exception as exc:
        logger.warning("_build_snapshot failed in kpis: %s", exc)

    today_pr_merges = 0
    try:
        repo = os.environ.get(_HERMES_REPO_ENV, _DEFAULT_HERMES_REPO)
        since = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00 +0000")
        res = subprocess.run(
            ["git", "-C", repo, "log", "fork/main", f"--since={since}",
             "--merges", "--oneline"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            today_pr_merges = sum(1 for ln in res.stdout.splitlines() if ln.strip())
    except Exception as exc:
        logger.warning("git log for pr merges failed: %s", exc)

    last_completion = None
    try:
        completed = [h for h in hives if h.get("status") == "completed"]
        if completed:
            best = max(completed, key=_hive_ts)
            summary = ""
            try:
                rp = best.get("final_report_path")
                if rp and Path(rp).exists():
                    summary = Path(rp).read_text(errors="replace")[:200].strip()
            except Exception as exc:
                logger.warning("reading final_report for last_completion: %s", exc)
            if not summary:
                try:
                    wd = best.get("workdir")
                    if wd:
                        obj = Path(wd) / "objective.md"
                        if obj.exists():
                            summary = obj.read_text(errors="replace")[:200].strip()
                except Exception as exc:
                    logger.warning("reading objective.md: %s", exc)
            last_completion = {
                "slug": best["id"],
                "completed_at": best.get("updated_at") or best.get("log_mtime"),
                "summary": summary,
            }
    except Exception as exc:
        logger.warning("last_completion failed: %s", exc)

    return {
        "active_hives": active_hives,
        "pending_cards": pending_cards,
        "max_usage_pct": None,
        "today_spend_usd": today_spend_usd,
        "today_pr_merges": today_pr_merges,
        "last_completion": last_completion,
    }


async def pulse_activity_iter(
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict]:
    """Yields {'hive': str, 'line': str, 'ts': str} per new log line.

    Polls hive logs every 1s. Caps emission at 20 events/sec total (drop oldest).
    """
    offsets: dict[Path, int] = {}
    log_glob = str(Path.home() / ".hermes" / "ruflo-work" / "*" / "hive-mind.log")

    while True:
        if stop_event is not None and stop_event.is_set():
            return

        await asyncio.sleep(_LOG_POLL_INTERVAL)

        if stop_event is not None and stop_event.is_set():
            return

        pending: deque[dict] = deque()

        for path_str in sorted(glob.glob(log_glob)):
            p = Path(path_str)
            try:
                size = p.stat().st_size
            except Exception as exc:
                logger.warning("stat failed for %s: %s", p, exc)
                offsets.pop(p, None)
                continue

            if p not in offsets:
                offsets[p] = size
                continue

            if size <= offsets[p]:
                continue

            try:
                with open(p, "rb") as f:
                    f.seek(offsets[p])
                    new_bytes = f.read(size - offsets[p])
                offsets[p] = size
            except Exception as exc:
                logger.warning("read error for %s: %s", p, exc)
                offsets.pop(p, None)
                continue

            ts = datetime.now(timezone.utc).isoformat()
            hive_name = p.parent.name
            for raw_line in new_bytes.decode("utf-8", errors="replace").splitlines():
                stripped = raw_line.rstrip("\n")
                if stripped:
                    pending.append({"hive": hive_name, "line": stripped, "ts": ts})

        # drop oldest events if over the per-second cap
        while len(pending) > _LOG_RATE_CAP_PER_SECOND:
            pending.popleft()

        for event in pending:
            yield event
            await asyncio.sleep(0)
