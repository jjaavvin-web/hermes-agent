"""Dashboard Mission Control API — live data layer (Hive 2).

Mount point: add to hermes_cli/web_server.py:
    from hermes_cli.dashboard_health import router as mission_router
    app.include_router(mission_router)

All endpoints require X-Hermes-Session-Token validated by existing middleware.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-mission"])

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"

# ---------------------------------------------------------------------------
# Pricing per million tokens (estimated API-equivalent; user is on Max plan)
# ---------------------------------------------------------------------------
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6":        {"in": 3.0,  "out": 15.0,  "cr": 0.30,  "cw": 3.75},
    "claude-opus-4-7":          {"in": 15.0, "out": 75.0,  "cr": 1.50,  "cw": 18.75},
    "claude-haiku-4-5-20251001":{"in": 0.80, "out": 4.0,   "cr": 0.08,  "cw": 1.0},
    "claude-haiku-4-5":         {"in": 0.80, "out": 4.0,   "cr": 0.08,  "cw": 1.0},
}
_DEFAULT_PRICING = {"in": 3.0, "out": 15.0, "cr": 0.30, "cw": 3.75}

# Server-side cache: (value, expires_at)
_SNAPSHOT_CACHE: tuple[dict, float] | None = None
_SNAPSHOT_TTL = 30.0
_SPEND_CACHE: dict[str, tuple[dict, float]] = {}
_SPEND_TTL = 60.0
# Nexus Health graph snapshot + infrastructure probe caches (30 s cadence).
_NEXUS_CACHE: tuple[dict, float] | None = None
_NEXUS_TTL = 30.0
_INFRA_CACHE: tuple[dict, float] | None = None
_INFRA_TTL = 30.0
# Hives snapshot cache (15 s cadence — matches the frontend poll interval).
_HIVES_CACHE: tuple[dict, float] | None = None
_HIVES_TTL = 15.0

# Single-flight locks — prevent cache stampedes on cold start.
_SNAPSHOT_LOCK = threading.Lock()
_NEXUS_LOCK = threading.Lock()
_INFRA_LOCK = threading.Lock()
_HIVES_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Runtime health probes
# ---------------------------------------------------------------------------

def _tcp_latency(host: str, port: int, timeout: float = 1.0) -> tuple[str, Optional[float]]:
    """Return (status, latency_ms). status is 'online'|'offline'."""
    try:
        t0 = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return "online", round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return "offline", None


def _process_alive(name: str) -> tuple[str, Optional[float]]:
    """Check if a process name is running. Returns (status, approx_latency_ms)."""
    try:
        t0 = time.monotonic()
        result = subprocess.run(
            ["pgrep", "-x", name],
            capture_output=True, timeout=2
        )
        latency = round((time.monotonic() - t0) * 1000, 1)
        return ("online" if result.returncode == 0 else "offline", latency)
    except Exception:
        return ("unknown", None)


def _probe_codex() -> dict:
    status, latency = _process_alive("codex")
    # Also accept 'node' processes that may host codex
    if status == "offline":
        try:
            result = subprocess.run(
                ["pgrep", "-f", "codex"],
                capture_output=True, timeout=2
            )
            if result.returncode == 0:
                status = "online"
        except Exception:
            pass
    return {"name": "codex", "label": "Codex", "status": status,
            "latencyMs": latency, "lastChecked": _now()}


def _probe_claude_code() -> dict:
    status, latency = _process_alive("claude")
    return {"name": "claude-code", "label": "Claude Code", "status": status,
            "latencyMs": latency, "lastChecked": _now()}


def _probe_ruflo() -> dict:
    try:
        t0 = time.monotonic()
        result = subprocess.run(
            ["ruflo", "status"],
            capture_output=True, timeout=3
        )
        latency = round((time.monotonic() - t0) * 1000, 1)
        status = "online" if result.returncode == 0 else "degraded"
    except FileNotFoundError:
        status, latency = "offline", None
    except Exception:
        status, latency = "unknown", None
    return {"name": "ruflo", "label": "Ruflo", "status": status,
            "latencyMs": latency, "lastChecked": _now()}


def _probe_hermes() -> dict:
    """Probe gateway via gateway_state.json (gateway runs as a subprocess, no TCP server)."""
    try:
        t0 = time.monotonic()
        state_path = HERMES_HOME / "gateway_state.json"
        data = json.loads(state_path.read_text())
        latency = round((time.monotonic() - t0) * 1000, 1)
        gw_state = data.get("gateway_state", "unknown")
        pid = data.get("pid")
        # Verify PID is actually alive (psutil works on Windows; os.kill(pid, 0) doesn't)
        from gateway.status import _pid_exists

        if pid and _pid_exists(int(pid)):
            status = "online" if gw_state == "running" else "degraded"
        else:
            status = "offline"
        platforms = data.get("platforms", {})
        detail = ", ".join(f"{p}:{v.get('state','?')}" for p, v in platforms.items())
    except Exception:
        status, latency, detail = "unknown", None, None
    return {"name": "hermes", "label": "Hermes Subagents", "status": status,
            "latencyMs": latency, "port": 8642, "detail": detail,
            "lastChecked": _now()}


def _probe_kanban() -> dict:
    """Check kanban state by reading the kanban DB directly.

    The previous implementation called back into our own HTTP server
    (http://127.0.0.1:9119/...) which always self-diagnoses as healthy.  We
    now read the SQLite database directly — the same path the HTTP handler uses
    — so the probe is independent of whether the event-loop is healthy.
    """
    import glob, sqlite3
    t0 = time.monotonic()
    open_total = 0
    found_db = False

    # Try the new-style per-board DBs first.
    for path in glob.glob(str(HERMES_HOME / "kanban/boards/*/kanban.db")):
        found_db = True
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
            c = conn.cursor()
            c.execute(
                "SELECT count(*) FROM tasks "
                "WHERE status NOT IN ('done','archived','complete')"
            )
            open_total += c.fetchone()[0]
            conn.close()
        except Exception:
            continue

    # Legacy single-file DB fallback.
    if not found_db:
        kanban_db = HERMES_HOME / "kanban.db"
        if kanban_db.exists():
            found_db = True
            try:
                conn = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True, timeout=1.0)
                c = conn.cursor()
                c.execute(
                    "SELECT count(*) FROM tasks "
                    "WHERE status NOT IN ('done','archived','complete')"
                )
                open_total += c.fetchone()[0]
                conn.close()
            except Exception:
                pass

    latency = round((time.monotonic() - t0) * 1000, 1)
    if found_db:
        status = "online"
        detail = f"{open_total} open task{'s' if open_total != 1 else ''}"
    else:
        status = "unknown"
        detail = None

    return {"name": "kanban", "label": "Kanban Dispatcher", "status": status,
            "latencyMs": latency, "port": 9119, "detail": detail,
            "lastChecked": _now()}


def _probe_cron() -> dict:
    """Check if cron has ≥1 enabled job."""
    try:
        t0 = time.monotonic()
        jobs_path = HERMES_HOME / "cron" / "jobs.json"
        data = json.loads(jobs_path.read_text())
        latency = round((time.monotonic() - t0) * 1000, 1)
        jobs = data.get("jobs", [])
        enabled = [j for j in jobs if j.get("enabled")]
        total = len(jobs)
        status = "online" if enabled else "degraded"
        detail = f"{len(enabled)}/{total} jobs enabled"
    except Exception:
        status, latency, detail = "unknown", None, None
    return {"name": "cron", "label": "Cron", "status": status,
            "latencyMs": latency, "detail": detail, "lastChecked": _now()}


_PROBE_MAP = {
    "codex": _probe_codex,
    "claude-code": _probe_claude_code,
    "ruflo": _probe_ruflo,
    "hermes": _probe_hermes,
    "kanban": _probe_kanban,
    "cron": _probe_cron,
}

_RUNTIME_ORDER = ["codex", "claude-code", "ruflo", "hermes", "kanban", "cron"]


def _probe_all() -> list[dict]:
    return [_PROBE_MAP[name]() for name in _RUNTIME_ORDER]


# ---------------------------------------------------------------------------
# Infrastructure probes — systemd --user units, docker containers, TCP ports.
# All reads are non-mutating: subprocess (read-only verbs) + socket connect.
# ---------------------------------------------------------------------------

def _run_capture(cmd: list[str], timeout: float = 4.0) -> tuple[int, str]:
    """Run a read-only command; return (returncode, stdout). Never raises."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout
    except Exception:
        return 1, ""


def _systemd_status(active: str, sub: str) -> str:
    """Map a systemd (active, sub) pair to a nexus status."""
    active = (active or "").lower()
    sub = (sub or "").lower()
    if active == "failed" or sub == "failed":
        return "error"
    if active == "active" and sub in {"running", "waiting", "exited", "listening", "mounted", "active"}:
        return "ok"
    if active in {"activating", "reloading", "deactivating"}:
        return "warn"
    if active == "inactive" or sub == "dead":
        return "warn"
    return "unknown"


def _probe_systemd_units() -> list[dict]:
    """List `systemctl --user` hermes-* units (services + timers). Read-only."""
    rc, out = _run_capture(
        ["systemctl", "--user", "list-units", "hermes-*",
         "--all", "--no-legend", "--no-pager", "--plain"],
        timeout=2.0,
    )
    units: list[dict] = []
    if rc != 0 and not out.strip():
        return units
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        toks = line.split()
        # A leading status bullet (●/○/×/*) is its own token for failed units.
        if toks and not toks[0].startswith("hermes-"):
            toks = toks[1:]
        if len(toks) < 4 or not toks[0].startswith("hermes-"):
            continue
        name, load, active, sub = toks[0], toks[1], toks[2], toks[3]
        description = " ".join(toks[4:])
        units.append({
            "name": name, "load": load, "active": active, "sub": sub,
            "description": description,
            "status": _systemd_status(active, sub),
        })
    return units


def _docker_status(state: str, status_text: str) -> str:
    """Map a docker (state, status-text) pair to a nexus status."""
    state = (state or "").lower()
    status_text = (status_text or "").lower()
    if state == "running":
        return "warn" if "unhealthy" in status_text else "ok"
    if state in {"restarting", "paused", "created"}:
        return "warn"
    if state in {"exited", "dead", "removing"}:
        return "error"
    return "unknown"


def _probe_docker_containers() -> list[dict]:
    """List MVMS Supabase docker containers via `docker ps -a`. Read-only."""
    rc, out = _run_capture(
        ["docker", "ps", "-a", "--no-trunc",
         "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"],
        timeout=2.0,
    )
    containers: list[dict] = []
    if rc != 0:
        return containers
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        # MVMS is backed by the local Supabase stack (supabase_* containers).
        if not name.startswith("supabase_"):
            continue
        state = parts[1].strip()
        status_text = parts[2].strip()
        image = parts[3].strip() if len(parts) > 3 else ""
        ports = parts[4].strip() if len(parts) > 4 else ""
        containers.append({
            "name": name, "state": state, "status_text": status_text,
            "image": image, "ports": ports,
            "status": _docker_status(state, status_text),
        })
    return containers


# Curated infrastructure ports — TCP-latency probed on localhost.
_KNOWN_PORTS: list[tuple[int, str, str]] = [
    (9119, "Dashboard API", "Hermes dashboard FastAPI server."),
    (4747, "GitNexus API", "GitNexus knowledge-graph backend (code-graph)."),
    (54321, "Supabase API", "MVMS Supabase Kong API gateway."),
    (54323, "Supabase Studio", "MVMS Supabase Studio UI."),
    (5434, "Supabase DB", "MVMS Supabase Postgres database."),
]


def _probe_port_9119_http() -> tuple[str, float | None]:
    """Application-level liveness check for port 9119 (Dashboard API).

    A raw TCP connect to the port we are serving from always succeeds while the
    process is running, even if the event-loop is hung.  Instead, issue a fast
    HTTP GET to a lightweight ping-style route so we actually exercise the
    request path.  The URL uses the auth-bypass flag (local loopback only).
    Falls back to TCP if the HTTP probe itself fails for environmental reasons.
    """
    import urllib.request
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:9119/api/dashboard/ping",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            _ = resp.read(64)
        return "online", round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        pass
    # Fallback to TCP; note in the description that this is less reliable.
    return _tcp_latency("127.0.0.1", 9119, timeout=0.4)


def _probe_ports() -> list[dict]:
    """Probe a curated set of infrastructure ports. Read-only.

    Port 9119 (Dashboard API) uses an application-level HTTP GET so the probe
    catches event-loop hangs that a raw TCP connect would mask.  All other
    ports use TCP-only probes since they have no common application-level route.
    """
    results: list[dict] = []
    for port, label, description in _KNOWN_PORTS:
        if port == 9119:
            state, latency = _probe_port_9119_http()
            detail_extra = " (application-level HTTP ping)"
        else:
            state, latency = _tcp_latency("127.0.0.1", port, timeout=0.4)
            detail_extra = ""
        results.append({
            "port": port, "label": label,
            "description": description + detail_extra,
            "online": state == "online", "latencyMs": latency,
            "status": "ok" if state == "online" else "error",
        })
    return results


def _build_infra_snapshot() -> dict:
    return {
        "services": _probe_systemd_units(),
        "containers": _probe_docker_containers(),
        "ports": _probe_ports(),
    }


def _get_infra_snapshot() -> dict:
    """30 s-cached infrastructure snapshot (systemd / docker / ports)."""
    global _INFRA_CACHE
    now = time.monotonic()
    if _INFRA_CACHE and now < _INFRA_CACHE[1]:
        return _INFRA_CACHE[0]
    with _INFRA_LOCK:
        # Re-check after acquiring lock (another thread may have rebuilt it).
        now = time.monotonic()
        if _INFRA_CACHE and now < _INFRA_CACHE[1]:
            return _INFRA_CACHE[0]
        snapshot = _build_infra_snapshot()
        _INFRA_CACHE = (snapshot, now + _INFRA_TTL)
    return snapshot


# ---------------------------------------------------------------------------
# Hives snapshot — Ruflo hive run discovery and status
# ---------------------------------------------------------------------------

RUFLO_WORK_DIR = HERMES_HOME / "ruflo-work"


def _tmux_sessions() -> set[str]:
    """Return set of live tmux session names. Empty if tmux is unavailable."""
    try:
        result = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except Exception:
        pass
    return set()


def _iso(ts: float) -> str:
    """Convert a POSIX timestamp to an ISO-8601 string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _probe_hive(workdir: Path, tmux_alive_sessions: set[str]) -> dict:
    """Build one hive snapshot entry from a workdir. Read-only."""
    hive_id = workdir.name

    # --- Read .ruflo-status.json if present ---
    status_path = workdir / ".ruflo-status.json"
    status_data: dict = {}
    if status_path.exists():
        try:
            status_data = json.loads(status_path.read_text())
        except Exception:
            pass

    session_name: Optional[str] = status_data.get("session")
    tracking_card: Optional[str] = status_data.get("tracking_card")
    updated_at: Optional[str] = status_data.get("updated_at")

    # --- LAUNCH.sh mtime as started_at; also grep TRACK_TITLE ---
    launch_path = workdir / "LAUNCH.sh"
    started_at: Optional[str] = None
    track_title: Optional[str] = None
    if launch_path.exists():
        try:
            started_at = _iso(launch_path.stat().st_mtime)
        except Exception:
            pass
        try:
            text = launch_path.read_text(errors="replace")
            for line in text.splitlines():
                if line.strip().startswith("TRACK_TITLE="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if raw:
                        track_title = raw
                    break
        except Exception:
            pass

    # Fall back: use status_path mtime if no LAUNCH.sh
    if started_at is None and status_path.exists():
        try:
            started_at = _iso(status_path.stat().st_mtime)
        except Exception:
            pass

    # --- Objective summary (first ~200 chars of objective.md, single line) ---
    objective_summary: Optional[str] = None
    obj_path = workdir / "objective.md"
    if obj_path.exists():
        try:
            raw_obj = obj_path.read_text(errors="replace")
            lines = [ln.strip() for ln in raw_obj.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if lines:
                objective_summary = " ".join(lines)[:200]
        except Exception:
            pass

    # --- FINAL-REPORT.md ---
    final_report_path: Optional[str] = None
    final_report_status: Optional[str] = None
    report_file = workdir / "FINAL-REPORT.md"
    if report_file.exists():
        final_report_path = str(report_file)
        try:
            first_lines = report_file.read_text(errors="replace")[:500]
            for ln in first_lines.splitlines():
                ln = ln.strip()
                if ln.startswith("Status:"):
                    val = ln.split(":", 1)[1].strip().upper()
                    if "BLOCKED" in val:
                        final_report_status = "BLOCKED"
                    elif "COMPLETE" in val:
                        final_report_status = "COMPLETE"
                    break
                # Also accept **Status:** markdown style
                if "**Status:**" in ln or "**status:**" in ln.lower():
                    if "BLOCKED" in ln.upper():
                        final_report_status = "BLOCKED"
                    elif "COMPLETE" in ln.upper():
                        final_report_status = "COMPLETE"
                    break
        except Exception:
            pass

    # --- Log file ---
    log_path: Optional[str] = None
    log_size: int = 0
    log_mtime: Optional[str] = None
    log_file = workdir / "hive-mind.log"
    if log_file.exists():
        log_path = str(log_file)
        try:
            st = log_file.stat()
            log_size = st.st_size
            log_mtime = _iso(st.st_mtime)
        except Exception:
            pass

    # --- tmux alive: check session name from status.json; also fallback search ---
    tmux_alive = False
    if session_name and session_name in tmux_alive_sessions:
        tmux_alive = True
    elif not session_name:
        # Heuristic: workdir name slug may match a tmux session
        slug = hive_id.split("-")[0]  # crude first-word match
        for sess in tmux_alive_sessions:
            if sess.startswith(slug):
                tmux_alive = True
                session_name = sess
                break

    # --- Status classification ---
    if final_report_path is not None:
        hive_status = "blocked" if final_report_status == "BLOCKED" else "completed"
    elif tmux_alive:
        hive_status = "running"
    else:
        hive_status = "stale"

    # --- Elapsed seconds ---
    elapsed_seconds: int = 0
    try:
        if started_at:
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            elapsed_seconds = int((now_dt - start_dt).total_seconds())
    except Exception:
        pass

    return {
        "id": hive_id,
        "workdir": str(workdir),
        "session": session_name,
        "status": hive_status,
        "tracking_card": tracking_card,
        "started_at": started_at,
        "updated_at": updated_at,
        "elapsed_seconds": elapsed_seconds,
        "final_report_status": final_report_status,
        "final_report_path": final_report_path,
        "log_path": log_path,
        "log_size_bytes": log_size,
        "log_mtime": log_mtime,
        "tmux_alive": tmux_alive,
        "track_title": track_title,
        "objective_summary": objective_summary,
    }


def _is_valid_hive_dir(workdir: Path) -> bool:
    """Return True if this looks like a real hive workdir (not junk)."""
    if workdir.name.startswith("."):
        return False
    has_launch = (workdir / "LAUNCH.sh").exists()
    has_objective = (workdir / "objective.md").exists()
    has_status = (workdir / ".ruflo-status.json").exists()
    return has_launch or has_objective or has_status


def _build_hives_snapshot() -> dict:
    """Scan ~/.hermes/ruflo-work for hive runs. Read-only. Thread-safe."""
    from itertools import groupby as _groupby

    scanned_at = _now()

    if not RUFLO_WORK_DIR.exists():
        return {
            "hives": [],
            "scanned_at": scanned_at,
            "active_count": 0,
            "completed_count": 0,
            "stale_count": 0,
        }

    workdirs = [d for d in RUFLO_WORK_DIR.iterdir() if d.is_dir() and _is_valid_hive_dir(d)]

    # Fan-out: get tmux sessions once (shared) then probe each hive in the pool.
    tmux_sessions = _tmux_sessions()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_probe_hive, wd, tmux_sessions): wd for wd in workdirs}
        hives: list[dict] = []
        for fut in concurrent.futures.as_completed(futures):
            try:
                hives.append(fut.result())
            except Exception:
                pass

    def _rank(h: dict) -> int:
        return {"running": 0, "completed": 1, "blocked": 1, "stale": 2}.get(h["status"], 2)

    # Sort all by rank asc, then within rank by updated_at/started_at desc.
    hives.sort(key=lambda h: (_rank(h), ""))

    sorted_hives: list[dict] = []
    for _, group in _groupby(hives, key=_rank):
        bucket = sorted(
            list(group),
            key=lambda h: h.get("updated_at") or h.get("started_at") or "",
            reverse=True,
        )
        sorted_hives.extend(bucket)

    active_count = sum(1 for h in sorted_hives if h["status"] == "running")
    completed_count = sum(1 for h in sorted_hives if h["status"] in {"completed", "blocked"})
    stale_count = sum(1 for h in sorted_hives if h["status"] == "stale")

    return {
        "hives": sorted_hives,
        "scanned_at": scanned_at,
        "active_count": active_count,
        "completed_count": completed_count,
        "stale_count": stale_count,
    }


def _get_hives_snapshot() -> dict:
    """15 s-cached hive runs snapshot. Thread-safe."""
    global _HIVES_CACHE
    now = time.monotonic()
    if _HIVES_CACHE and now < _HIVES_CACHE[1]:
        return _HIVES_CACHE[0]
    with _HIVES_LOCK:
        now = time.monotonic()
        if _HIVES_CACHE and now < _HIVES_CACHE[1]:
            return _HIVES_CACHE[0]
        data = _build_hives_snapshot()
        _HIVES_CACHE = (data, now + _HIVES_TTL)
    return data


def _get_hive_log_tail(hive_id: str, tail: int = 200) -> Optional[dict]:
    """Return last N lines of a hive's hive-mind.log. Read-only.

    Validates hive_id against the current snapshot whitelist to prevent
    path traversal. Hard cap: N <= 1000.
    """
    tail = max(1, min(tail, 1000))

    snapshot = _get_hives_snapshot()
    known = {h["id"]: h for h in snapshot["hives"]}
    if hive_id not in known:
        return None

    hive = known[hive_id]
    log_path_str = hive.get("log_path")
    if not log_path_str:
        return {
            "lines": [],
            "path": None,
            "mtime": None,
            "truncated_to": tail,
        }

    log_path = Path(log_path_str)
    try:
        mtime_val = _iso(log_path.stat().st_mtime)
        text = log_path.read_text(errors="replace")
        lines = text.splitlines()
        result_lines = lines[-tail:] if len(lines) > tail else lines
        return {
            "lines": result_lines,
            "path": log_path_str,
            "mtime": mtime_val,
            "truncated_to": tail,
        }
    except Exception as exc:
        return {
            "lines": [],
            "path": log_path_str,
            "mtime": None,
            "truncated_to": tail,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Spend data from claude JSONL session files
# ---------------------------------------------------------------------------

def _compute_spend_points(days: int) -> list[dict]:
    """Scan ~/.claude/projects/**/*.jsonl for usage data. Returns SpendPoint list."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    daily: dict[str, dict[str, Any]] = {}  # date -> {model: {cost, tokens}}

    if not CLAUDE_PROJECTS_DIR.exists():
        return []

    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                with open(jsonl_file, encoding="utf-8", errors="replace") as fh:
                    for raw in fh:
                        try:
                            obj = json.loads(raw)
                            ts = obj.get("timestamp", "")
                            msg = obj.get("message", {})
                            usage = msg.get("usage", {})
                            if not usage or not ts:
                                continue
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if dt < cutoff:
                                continue
                            date_str = dt.strftime("%Y-%m-%d")
                            model = msg.get("model") or "claude-sonnet-4-6"
                            pricing = _MODEL_PRICING.get(model, _DEFAULT_PRICING)
                            cost = (
                                usage.get("input_tokens", 0) * pricing["in"] / 1e6 +
                                usage.get("output_tokens", 0) * pricing["out"] / 1e6 +
                                usage.get("cache_read_input_tokens", 0) * pricing["cr"] / 1e6 +
                                usage.get("cache_creation_input_tokens", 0) * pricing["cw"] / 1e6
                            )
                            tokens = (
                                usage.get("input_tokens", 0) +
                                usage.get("output_tokens", 0)
                            )
                            key = (date_str, model)
                            if key not in daily:
                                daily[key] = {"cost": 0.0, "tokens": 0}
                            daily[key]["cost"] += cost
                            daily[key]["tokens"] += tokens
                        except Exception:
                            continue
            except Exception:
                continue

    # Collapse by date, taking the dominant model per day
    by_date: dict[str, dict[str, Any]] = {}
    for (date_str, model), vals in daily.items():
        if date_str not in by_date:
            by_date[date_str] = {"model": model, "cost": 0.0, "tokens": 0}
        by_date[date_str]["cost"] += vals["cost"]
        if vals["cost"] > by_date[date_str]["cost"]:
            by_date[date_str]["model"] = model
        by_date[date_str]["tokens"] += vals["tokens"]

    points = []
    for date_str in sorted(by_date.keys()):
        d = by_date[date_str]
        points.append({
            "date": date_str,
            "model": d["model"],
            "amountUsd": round(d["cost"], 4),
            "tokenCount": d["tokens"],
        })
    return points


def _get_spend(range_str: str) -> dict:
    global _SPEND_CACHE
    now = time.monotonic()
    cached = _SPEND_CACHE.get(range_str)
    if cached and now < cached[1]:
        return cached[0]

    days = {"1d": 1, "7d": 7, "30d": 30}.get(range_str, 7)
    points = _compute_spend_points(days)
    result = {"range": range_str, "points": points}
    _SPEND_CACHE[range_str] = (result, now + _SPEND_TTL)
    return result


# ---------------------------------------------------------------------------
# Session data from ~/.hermes/sessions/sessions.json
# ---------------------------------------------------------------------------

def _get_recent_sessions(limit: int = 5) -> list[dict]:
    sessions_path = HERMES_HOME / "sessions" / "sessions.json"
    try:
        raw = json.loads(sessions_path.read_text())
        if isinstance(raw, dict):
            entries = list(raw.values())
        elif isinstance(raw, list):
            entries = raw
        else:
            return []

        # Sort by updated_at descending
        entries.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
        result = []
        for e in entries[:limit]:
            preview = e.get("display_name") or e.get("platform") or "Session"
            preview = str(preview)[:80]
            result.append({
                "id": e.get("session_id", "unknown"),
                "preview": preview,
                "createdAt": e.get("created_at", _now()),
                "modelUsed": e.get("model"),
            })
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Spend summary (today / week)
# ---------------------------------------------------------------------------

def _get_spend_summary() -> tuple[float, float]:
    spend_7d = _get_spend("7d")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    today_total = 0.0
    week_total = 0.0
    for p in spend_7d["points"]:
        week_total += p["amountUsd"]
        if p["date"] == today_str:
            today_total += p["amountUsd"]
    return round(today_total, 4), round(week_total, 4)


# ---------------------------------------------------------------------------
# Streak calculation
# ---------------------------------------------------------------------------

def _compute_streak() -> int:
    """Count consecutive days with claude session activity, ending today."""
    try:
        proj_dir = CLAUDE_PROJECTS_DIR
        if not proj_dir.exists():
            return 0
        active_dates: set[str] = set()
        for project_dir in proj_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl_file in project_dir.glob("*.jsonl"):
                try:
                    mtime = datetime.fromtimestamp(
                        jsonl_file.stat().st_mtime, tz=timezone.utc
                    )
                    active_dates.add(mtime.strftime("%Y-%m-%d"))
                except Exception:
                    continue

        if not active_dates:
            return 0

        streak = 0
        check_date = datetime.now(timezone.utc).date()
        while check_date.isoformat() in active_dates:
            streak += 1
            check_date -= timedelta(days=1)
        return streak
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Active model
# ---------------------------------------------------------------------------

def _get_active_model() -> str:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        model_cfg = cfg.get("model", "")
        if isinstance(model_cfg, dict):
            return (model_cfg.get("default") or model_cfg.get("name") or "claude-sonnet-4-6")
        if isinstance(model_cfg, str) and model_cfg.strip():
            return model_cfg.strip()
    except Exception:
        pass
    return "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Cron data
# ---------------------------------------------------------------------------

def _get_next_cron() -> Optional[dict]:
    try:
        jobs_path = HERMES_HOME / "cron" / "jobs.json"
        data = json.loads(jobs_path.read_text())
        jobs = data.get("jobs", [])
        now_str = datetime.now(timezone.utc).isoformat()
        enabled_with_next = [
            j for j in jobs
            if j.get("enabled") and j.get("next_run_at")
        ]
        if not enabled_with_next:
            # Fall back to staged dream-reflect.cron
            staged_path = HERMES_HOME / "cron.d" / "dream-reflect.cron"
            if staged_path.exists():
                content = staged_path.read_text()
                for line in content.splitlines():
                    if line.strip() and not line.strip().startswith("#"):
                        parts = line.split()
                        if len(parts) >= 6:
                            expr = " ".join(parts[:5])
                            return {
                                "name": "dream-reflect (staged)",
                                "schedule": expr,
                                "nextRun": now_str,
                            }
            return None
        # Pick soonest
        soonest = min(enabled_with_next, key=lambda j: j["next_run_at"])
        return {
            "name": soonest.get("name", "unnamed"),
            "schedule": soonest.get("schedule_display", soonest.get("schedule", {}).get("expr", "")),
            "nextRun": soonest["next_run_at"],
        }
    except Exception:
        return None


def _get_all_cron_jobs() -> list[dict]:
    try:
        jobs_path = HERMES_HOME / "cron" / "jobs.json"
        data = json.loads(jobs_path.read_text())
        jobs = data.get("jobs", [])
        result = []
        for j in jobs:
            result.append({
                "id": j.get("id"),
                "name": j.get("name"),
                "schedule": j.get("schedule_display") or j.get("schedule", {}).get("expr", ""),
                "enabled": j.get("enabled", False),
                "state": j.get("state", "unknown"),
                "nextRun": j.get("next_run_at"),
                "lastRun": j.get("last_run_at"),
                "lastStatus": j.get("last_status"),
                "staged": False,
            })

        # Check for staged dream-reflect
        staged_path = HERMES_HOME / "cron.d" / "dream-reflect.cron"
        if staged_path.exists():
            content = staged_path.read_text()
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") and "dream-reflect" in stripped.lower():
                    # Extract the commented cron expression
                    inner = stripped.lstrip("#").strip()
                    parts = inner.split(None, 5)
                    if len(parts) >= 6:
                        result.append({
                            "id": "dream-reflect-staged",
                            "name": "dream-reflect (staged — click to activate)",
                            "schedule": " ".join(parts[:5]),
                            "enabled": False,
                            "state": "staged",
                            "nextRun": None,
                            "lastRun": None,
                            "lastStatus": None,
                            "staged": True,
                            "activateCmd": f"crontab -l | sed 's/^#\\s*{parts[:5][0]}//' | crontab -",
                        })
                    break
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Dream data
# ---------------------------------------------------------------------------

def _get_last_dream() -> Optional[str]:
    dreams_dir = HERMES_HOME / "dreams"
    if not dreams_dir.exists():
        return None
    try:
        dream_files = sorted(dreams_dir.glob("*.md"), reverse=True)
        if not dream_files:
            return None
        content = dream_files[0].read_text(errors="replace")
        # Return first ~300 chars as the brief
        lines = [ln for ln in content.splitlines() if ln.strip() and not ln.startswith("#")]
        brief = " ".join(lines)[:300].strip()
        return brief or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Swarm status
# ---------------------------------------------------------------------------

def _get_swarm_status() -> Optional[dict]:
    """Best-effort swarm status from ruflo."""
    try:
        result = subprocess.run(
            ["ruflo", "swarm", "status", "--json"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return data
    except Exception:
        pass

    # Fall back to hive process scan — look for hive-* working dirs
    try:
        hive_dirs = list((HERMES_HOME / "ruflo-work").glob("*hive*")) if (HERMES_HOME / "ruflo-work").exists() else []
        active = [d for d in hive_dirs if d.is_dir()]
        if active:
            return {
                "id": "hive-local",
                "name": "Hive Mind Swarm",
                "topology": "hierarchical-mesh",
                "workerCount": len(active),
                "activeWorkers": 0,
                "queueDepth": 0,
                "lastActivity": _now(),
            }
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Snapshot cache
# ---------------------------------------------------------------------------

def _build_snapshot() -> dict:
    runtimes = _probe_all()
    today_spend, week_spend = _get_spend_summary()
    return {
        "model": _get_active_model(),
        "spendToday": today_spend,
        "spendWeek": week_spend,
        "streakDays": _compute_streak(),
        "runtimes": runtimes,
        "swarm": _get_swarm_status(),
        "recentSessions": _get_recent_sessions(),
        "lastDream": _get_last_dream(),
        "nextCron": _get_next_cron(),
    }


def _get_snapshot() -> dict:
    global _SNAPSHOT_CACHE
    now = time.monotonic()
    if _SNAPSHOT_CACHE and now < _SNAPSHOT_CACHE[1]:
        return _SNAPSHOT_CACHE[0]
    with _SNAPSHOT_LOCK:
        # Re-check after acquiring lock (another thread may have rebuilt it).
        now = time.monotonic()
        if _SNAPSHOT_CACHE and now < _SNAPSHOT_CACHE[1]:
            return _SNAPSHOT_CACHE[0]
        snapshot = _build_snapshot()
        _SNAPSHOT_CACHE = (snapshot, now + _SNAPSHOT_TTL)
    return snapshot


# ---------------------------------------------------------------------------
# Nexus Health graph
# ---------------------------------------------------------------------------

def _get_gitnexus_runtime_snapshot() -> dict:
    """Read GitNexus topology from the read-only runtime collector."""
    try:
        from hermes_cli.gitnexus_runtime_collector import snapshot

        return dict(snapshot())
    except Exception as exc:
        return {
            "agents": [],
            "swarms": [],
            "hives": [],
            "mcp": [],
            "gateways": [],
            "cron": [],
            "edges": [],
            "_error": str(exc),
        }


def _runtime_by_name(mission: dict) -> dict[str, dict]:
    return {
        str(item.get("name")): item
        for item in mission.get("runtimes", [])
        if isinstance(item, dict) and item.get("name")
    }


def _nexus_status(runtime_status: Any) -> str:
    return {
        "online": "ok",
        "running": "ok",
        "active": "ok",
        "enabled": "ok",
        "degraded": "warn",
        "stopped": "warn",
        "offline": "error",
        "error": "error",
        "auth_gated": "auth_gated",
    }.get(str(runtime_status or "unknown").lower(), "unknown")


def _provenance(source: str, detail: str) -> list[dict[str, str]]:
    return [{"source": source, "detail": detail}]


def _nexus_node(
    *,
    node_id: str,
    label: str,
    kind: str,
    group: str,
    status: str,
    summary: str,
    details: str,
    metrics: Optional[dict] = None,
    provenance: Optional[list[dict[str, str]]] = None,
    safe_next_check: str,
    needs_joseph: bool = False,
) -> dict:
    return {
        "id": node_id,
        "label": label,
        "kind": kind,
        "group": group,
        "status": status,
        "summary": summary,
        "details": details,
        "metrics": metrics or {},
        "provenance": provenance or [],
        "safe_next_check": safe_next_check,
        "needs_joseph": needs_joseph,
    }


def _rollup_status(statuses: list) -> str:
    """Aggregate child statuses into a single hub status."""
    present = {str(s) for s in statuses if s}
    if not present:
        return "unknown"
    if "error" in present:
        return "error"
    if present & {"warn", "auth_gated"}:
        return "warn"
    if "unknown" in present:
        return "warn" if present - {"unknown"} else "unknown"
    return "ok"


def _nexus_edge(
    edge_id: str,
    source: str,
    target: str,
    label: str,
    status: str,
    summary: str,
    provenance: Optional[list[dict[str, str]]] = None,
) -> dict:
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "label": label,
        "status": status,
        "summary": summary,
        "provenance": provenance or [],
    }


def _build_nexus_health() -> dict:
    """Compose the read-only infrastructure health graph.

    Covers the Hermes core, the messaging gateway, the control plane
    (kanban / cron / agent lanes), every ``systemctl --user`` hermes-* unit,
    the MVMS Supabase container stack, curated listening ports, and each
    individual MCP server. Every input comes from a read-only probe.
    """
    generated_at = _now()
    # Fan-out the three independent snapshot builders concurrently so cold time ≈
    # max(single-group latency) rather than the sum of all three groups.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        fut_mission  = pool.submit(_get_snapshot)
        fut_topology = pool.submit(_get_gitnexus_runtime_snapshot)
        fut_infra    = pool.submit(_get_infra_snapshot)
        mission  = fut_mission.result()
        topology = fut_topology.result()
        infra    = fut_infra.result()
    runtimes = _runtime_by_name(mission)

    hermes_rt = runtimes.get("hermes", {})
    kanban_rt = runtimes.get("kanban", {})
    cron_rt = runtimes.get("cron", {})
    codex_rt = runtimes.get("codex", {})
    ruflo_rt = runtimes.get("ruflo", {})
    claude_rt = runtimes.get("claude-code", {})

    gateways = topology.get("gateways", []) if isinstance(topology, dict) else []
    agents = topology.get("agents", []) if isinstance(topology, dict) else []
    mcp = topology.get("mcp", []) if isinstance(topology, dict) else []
    cron = topology.get("cron", []) if isinstance(topology, dict) else []
    hives = topology.get("hives", []) if isinstance(topology, dict) else []

    services = infra.get("services", []) if isinstance(infra, dict) else []
    containers = infra.get("containers", []) if isinstance(infra, dict) else []
    ports = infra.get("ports", []) if isinstance(infra, dict) else []

    source_root = Path(__file__).resolve().parents[1]

    gateway_status = _nexus_status(
        gateways[0].get("status") if gateways else hermes_rt.get("status")
    )
    kanban_status = _nexus_status(kanban_rt.get("status"))
    cron_status = _nexus_status(cron_rt.get("status"))
    agent_lane_statuses = [
        _nexus_status(codex_rt.get("status")),
        _nexus_status(ruflo_rt.get("status")),
        _nexus_status(claude_rt.get("status")),
    ]
    if "error" in agent_lane_statuses:
        lane_status = "error"
    elif "warn" in agent_lane_statuses or "unknown" in agent_lane_statuses:
        lane_status = "warn"
    else:
        lane_status = "ok"

    nodes: list[dict] = []
    edges: list[dict] = []

    # --- Core spine: dashboard -> hermes -> gateway ----------------------
    nodes.append(_nexus_node(
        node_id="dashboard", label="Dashboard", kind="dashboard", group="core",
        status="ok",
        summary="Dashboard API is serving this read-only health map.",
        details="Reached through the dashboard FastAPI router; the System Health "
                "tab renders this document.",
        metrics={"port": 9119, "endpoint": "/api/dashboard/nexus-health"},
        provenance=_provenance("dashboard-health", "GET /api/dashboard/nexus-health returned this document."),
        safe_next_check="Refresh this page or open the browser console for fetch errors.",
    ))
    nodes.append(_nexus_node(
        node_id="hermes", label="Hermes Core", kind="runtime", group="core",
        status=_nexus_status(hermes_rt.get("status")),
        summary=hermes_rt.get("label") or "Core agent runtime",
        details=hermes_rt.get("detail") or "Mission Control runtime probe for the Hermes core.",
        metrics={
            "model": mission.get("model"),
            "recent_sessions": len(mission.get("recentSessions", [])),
            "latency_ms": hermes_rt.get("latencyMs"),
            "spend_today_usd": mission.get("spendToday"),
            "spend_week_usd": mission.get("spendWeek"),
            "streak_days": mission.get("streakDays"),
        },
        provenance=_provenance("mission-control", "Runtime probe from /api/dashboard/mission."),
        safe_next_check="Open logs or read the Mission Control runtime chip.",
    ))
    nodes.append(_nexus_node(
        node_id="gateway", label="Gateway", kind="gateway", group="core",
        status=gateway_status,
        summary="Messaging gateway state from runtime topology.",
        details=f"{len(gateways)} gateway record(s); platforms: "
                f"{', '.join(gateways[0].get('platforms', [])) if gateways else 'unknown'}.",
        metrics={
            "gateways": len(gateways),
            "platforms": gateways[0].get("platforms", []) if gateways else [],
            "active_agents": gateways[0].get("active_agents") if gateways else None,
        },
        provenance=_provenance("gitnexus-runtime-collector", "Read gateway_state.json without mutating indexes."),
        safe_next_check="Read gateway logs or gateway_state.json; do not restart from this page.",
        needs_joseph=gateway_status in {"error", "auth_gated"},
    ))
    edges.append(_nexus_edge("dashboard->hermes", "dashboard", "hermes", "mission api", "ok", "Mission Control snapshot feeds core runtime claims.", _provenance("dashboard-health", "Composed from _get_snapshot().")))
    edges.append(_nexus_edge("hermes->gateway", "hermes", "gateway", "gateway_state.json", gateway_status, "Hermes core drives the messaging platform adapters.", _provenance("gitnexus-runtime-collector", "Gateway records.")))

    # --- Control plane: kanban / cron / agent lanes ----------------------
    nodes.append(_nexus_node(
        node_id="kanban", label="Kanban", kind="kanban", group="control",
        status=kanban_status,
        summary="Dispatcher and board state visibility.",
        details=kanban_rt.get("detail") or "Kanban runtime probe is unknown or unavailable.",
        metrics={
            "active_tasks": kanban_rt.get("active_tasks"),
            "queue_port": kanban_rt.get("port"),
            "latency_ms": kanban_rt.get("latencyMs"),
        },
        provenance=_provenance("mission-control", "Kanban probe uses status endpoint or read-only DB fallback."),
        safe_next_check="Open the Kanban board status; do not dispatch or reclaim here.",
        needs_joseph=kanban_status == "error",
    ))
    next_cron = mission.get("nextCron")
    nodes.append(_nexus_node(
        node_id="cron-watchdogs", label="Cron / Watchdogs", kind="control-plane", group="control",
        status=cron_status,
        summary="Scheduled jobs and watchdog posture.",
        details=cron_rt.get("detail") or "Cron job state is unknown.",
        metrics={
            "collector_jobs": len(cron),
            "next_cron": next_cron.get("name") if isinstance(next_cron, dict) else next_cron,
        },
        provenance=_provenance("mission-control", "Cron probe and read-only runtime collector cron list."),
        safe_next_check="Read cron job listings and last run status before changing schedules.",
    ))
    nodes.append(_nexus_node(
        node_id="agent-lanes", label="Codex / Ruflo / Claude Lanes", kind="agent-lane", group="control",
        status=lane_status,
        summary="Implementation lane readiness across local agent surfaces.",
        details=(f"Codex={codex_rt.get('status', 'unknown')}, "
                 f"Ruflo={ruflo_rt.get('status', 'unknown')}, "
                 f"Claude={claude_rt.get('status', 'unknown')}."),
        metrics={
            "collector_agents": len(agents),
            "codex": codex_rt.get("status"),
            "ruflo": ruflo_rt.get("status"),
            "claude_code": claude_rt.get("status"),
            "spend_today_usd": mission.get("spendToday"),
        },
        provenance=_provenance("mission-control", "Process probes for codex, ruflo, and claude-code."),
        safe_next_check="Read lane status and logs; do not launch workers from this page.",
    ))
    edges.append(_nexus_edge("hermes->kanban", "hermes", "kanban", "task queue", kanban_status, "Hermes uses Kanban for work coordination.", _provenance("mission-control", "Kanban runtime probe.")))
    edges.append(_nexus_edge("cron->hermes", "cron-watchdogs", "hermes", "scheduled prompts", cron_status, "Cron and watchdog jobs invoke Hermes workflows.", _provenance("mission-control", "Cron runtime probe.")))
    edges.append(_nexus_edge("hermes->agent-lanes", "hermes", "agent-lanes", "dispatch", lane_status, "Hermes dispatches work to the agent lanes.", _provenance("mission-control", "Lane process probes.")))

    # --- systemd --user hermes-* units -----------------------------------
    svc_hub_status = _rollup_status([s.get("status") for s in services])
    nodes.append(_nexus_node(
        node_id="systemd-units", label="systemd --user", kind="service-group", group="services",
        status=svc_hub_status,
        summary=f"{len(services)} hermes-* user unit(s) under systemd.",
        details=("Enumerated via `systemctl --user list-units hermes-*`."
                 if services else
                 "No hermes-* user units found, or systemctl is unavailable."),
        metrics={
            "units": len(services),
            "failed": sum(1 for s in services if s.get("status") == "error"),
            "active": sum(1 for s in services if s.get("status") == "ok"),
        },
        provenance=_provenance("systemd", "systemctl --user list-units hermes-* (read-only)."),
        safe_next_check="Inspect a unit with journalctl --user -u <unit>.",
    ))
    edges.append(_nexus_edge("hermes->systemd", "hermes", "systemd-units", "process supervision", svc_hub_status, "Hermes runs as a set of systemd --user units.", _provenance("systemd", "list-units")))
    for svc in services:
        unit = str(svc.get("name", ""))
        if not unit:
            continue
        node_id = f"svc:{unit}"
        st = svc.get("status", "unknown")
        short = unit.replace("hermes-", "").replace(".service", "").replace(".timer", "")
        nodes.append(_nexus_node(
            node_id=node_id,
            label=short + (" (timer)" if unit.endswith(".timer") else ""),
            kind="service", group="services", status=st,
            summary=svc.get("description") or unit,
            details=f"active={svc.get('active', '?')}, sub={svc.get('sub', '?')}, "
                    f"load={svc.get('load', '?')}.",
            metrics={
                "unit": unit, "active": svc.get("active"),
                "sub": svc.get("sub"), "load": svc.get("load"),
            },
            provenance=_provenance("systemd", f"systemctl --user list-units row for {unit}."),
            safe_next_check=f"journalctl --user -u {unit} -n 80 --no-pager",
        ))
        edges.append(_nexus_edge(f"systemd->{node_id}", "systemd-units", node_id, "unit", st, short, _provenance("systemd", "list-units")))
        if "dashboard" in unit:
            edges.append(_nexus_edge(f"{node_id}->dashboard", node_id, "dashboard", "serves", st, "This unit runs the dashboard process.", _provenance("systemd", unit)))
        elif "gateway" in unit:
            edges.append(_nexus_edge(f"{node_id}->gateway", node_id, "gateway", "serves", st, "This unit runs the gateway process.", _provenance("systemd", unit)))
        elif "gitnexus" in unit:
            edges.append(_nexus_edge(f"{node_id}->gitnexus-explorer", node_id, "gitnexus-explorer", "serves", st, "This unit feeds GitNexus topology.", _provenance("systemd", unit)))

    # --- Curated listening ports -----------------------------------------
    port_hub_status = _rollup_status([p.get("status") for p in ports])
    nodes.append(_nexus_node(
        node_id="ports", label="Listening Ports", kind="network-group", group="network",
        status=port_hub_status,
        summary=f"{len(ports)} infrastructure port(s) probed over TCP.",
        details="Each port is checked with a localhost TCP connect; no payload is sent.",
        metrics={
            "probed": len(ports),
            "online": sum(1 for p in ports if p.get("online")),
            "offline": sum(1 for p in ports if not p.get("online")),
        },
        provenance=_provenance("tcp-probe", "socket.create_connection to 127.0.0.1:<port>."),
        safe_next_check="Confirm the owning process is listening before changing config.",
    ))
    edges.append(_nexus_edge("hermes->ports", "hermes", "ports", "tcp surface", port_hub_status, "Infrastructure services expose localhost ports.", _provenance("tcp-probe", "create_connection")))
    for p in ports:
        port_num = p.get("port")
        node_id = f"port:{port_num}"
        st = p.get("status", "unknown")
        latency = p.get("latencyMs")
        nodes.append(_nexus_node(
            node_id=node_id, label=f"{p.get('label', 'port')} :{port_num}",
            kind="port", group="network", status=st,
            summary=p.get("description") or f"TCP port {port_num}",
            details=(f"Listening — TCP connect succeeded in {latency} ms."
                     if p.get("online") else
                     f"Not listening — TCP connect to 127.0.0.1:{port_num} failed."),
            metrics={"port": port_num, "latency_ms": latency, "online": p.get("online")},
            provenance=_provenance("tcp-probe", f"socket.create_connection(127.0.0.1, {port_num})."),
            safe_next_check=f"ss -tlnp | grep :{port_num}",
        ))
        edges.append(_nexus_edge(f"ports->{node_id}", "ports", node_id, "probe", st, p.get("label", ""), _provenance("tcp-probe", "create_connection")))
        if port_num == 9119:
            edges.append(_nexus_edge("port9119->dashboard", node_id, "dashboard", "binds", st, "The dashboard listens on :9119.", _provenance("tcp-probe", "9119")))
        elif port_num == 4747:
            edges.append(_nexus_edge("port4747->gitnexus-explorer", node_id, "gitnexus-explorer", "binds", st, "The GitNexus API listens on :4747.", _provenance("tcp-probe", "4747")))

    # --- MVMS Supabase container stack -----------------------------------
    ctr_hub_status = _rollup_status([c.get("status") for c in containers])
    nodes.append(_nexus_node(
        node_id="containers", label="MVMS Containers", kind="container-group", group="containers",
        status=ctr_hub_status,
        summary=f"{len(containers)} MVMS Supabase container(s).",
        details=("Enumerated via `docker ps -a`, filtered to supabase_* names."
                 if containers else
                 "No supabase_* containers found, or docker is unavailable."),
        metrics={
            "containers": len(containers),
            "running": sum(1 for c in containers if c.get("status") == "ok"),
            "down": sum(1 for c in containers if c.get("status") == "error"),
        },
        provenance=_provenance("docker", "docker ps -a (read-only)."),
        safe_next_check="docker inspect <name> for container detail.",
    ))
    for c in containers:
        cname = str(c.get("name", ""))
        if not cname:
            continue
        node_id = f"ctr:{cname}"
        st = c.get("status", "unknown")
        short = cname.replace("supabase_", "").replace("_goattrade-system", "")
        nodes.append(_nexus_node(
            node_id=node_id, label=short, kind="container", group="containers", status=st,
            summary=c.get("status_text") or cname,
            details=f"image={c.get('image', '?')}; docker state={c.get('state', '?')}.",
            metrics={
                "container": cname, "state": c.get("state"),
                "image": c.get("image"), "ports": c.get("ports"),
            },
            provenance=_provenance("docker", f"docker ps -a row for {cname}."),
            safe_next_check=f"docker logs --tail 80 {cname}",
        ))
        edges.append(_nexus_edge(f"containers->{node_id}", "containers", node_id, "container", st, short, _provenance("docker", "ps")))

    # --- Integrations: GitNexus + MCP servers ----------------------------
    nodes.append(_nexus_node(
        node_id="gitnexus-explorer", label="GitNexus / Explorer", kind="gitnexus", group="integrations",
        status="warn" if topology.get("_error") else "ok",
        summary="Topology collector available without index ingestion.",
        details=topology.get("_error") or "Read-only collector returned runtime graph ingredients.",
        metrics={"agents": len(agents), "mcp_servers": len(mcp), "hives": len(hives)},
        provenance=_provenance("gitnexus-runtime-collector", "Used snapshot(); ingest/index mutation path is not imported."),
        safe_next_check="Open Explorer read-only; avoid any ingest or rebuild operation.",
    ))
    mcp_statuses = [_nexus_status(m.get("status")) for m in mcp if isinstance(m, dict)]
    mcp_hub_status = _rollup_status(mcp_statuses) if mcp else "unknown"
    has_auth_gated_mcp = any(s == "auth_gated" for s in mcp_statuses)
    nodes.append(_nexus_node(
        node_id="mcp-memory", label="MCP / Memory", kind="memory", group="integrations",
        status=mcp_hub_status,
        summary="MCP and memory-adjacent tool surface.",
        details=f"{len(mcp)} MCP server record(s) visible to the collector.",
        metrics={"servers": len(mcp)},
        provenance=_provenance("gitnexus-runtime-collector", "Parsed hermes mcp list or ~/.hermes/mcp directory names."),
        safe_next_check="Read MCP server list and auth status; do not change provider config here.",
        needs_joseph=has_auth_gated_mcp,
    ))
    edges.append(_nexus_edge("hermes->gitnexus-explorer", "hermes", "gitnexus-explorer", "topology", "warn" if topology.get("_error") else "ok", "Hermes feeds the read-only topology collector.", _provenance("gitnexus-runtime-collector", "snapshot()")))
    edges.append(_nexus_edge("hermes->mcp-memory", "hermes", "mcp-memory", "tool context", mcp_hub_status, "MCP and memory providers extend runtime context.", _provenance("gitnexus-runtime-collector", "MCP server list.")))
    edges.append(_nexus_edge("mcp-memory->containers", "mcp-memory", "containers", "MVMS backend", ctr_hub_status, "MVMS memory is backed by the Supabase container stack.", _provenance("docker", "ps")))
    for m in mcp:
        if not isinstance(m, dict):
            continue
        mname = str(m.get("name") or m.get("id") or "")
        if not mname:
            continue
        node_id = f"mcp:{mname}"
        st = _nexus_status(m.get("status"))
        nodes.append(_nexus_node(
            node_id=node_id, label=mname, kind="mcp", group="integrations", status=st,
            summary=f"MCP server '{mname}'.",
            details=f"Collector status: {m.get('status', 'unknown')}.",
            metrics={"server": mname, "raw_status": m.get("status")},
            provenance=_provenance("gitnexus-runtime-collector", "hermes mcp list row."),
            safe_next_check="hermes mcp list to confirm tools and auth state.",
            needs_joseph=st == "auth_gated",
        ))
        edges.append(_nexus_edge(f"mcp->{node_id}", "mcp-memory", node_id, "mcp server", st, mname, _provenance("gitnexus-runtime-collector", "mcp list")))

    # --- Data plane: source tree + audit store ---------------------------
    nodes.append(_nexus_node(
        node_id="source-tree", label="Source Tree", kind="source", group="data",
        status="ok" if source_root.exists() else "unknown",
        summary="Hermes source tree is available for read-only inspection.",
        details=str(source_root),
        metrics={"path": str(source_root)},
        provenance=_provenance("filesystem", "Resolved from hermes_cli/dashboard_health.py."),
        safe_next_check="Use local tests and static inspection before changing shared runtime state.",
    ))
    nodes.append(_nexus_node(
        node_id="audit-store", label="Audit Store", kind="audit", group="data",
        status="ok" if source_root.exists() else "unknown",
        summary="Current audit worktree is readable.",
        details=str(source_root),
        metrics={"path": str(source_root)},
        provenance=_provenance("filesystem", "Resolved current dashboard_health.py repository root."),
        safe_next_check="Read files in this worktree only.",
    ))
    edges.append(_nexus_edge("agent-lanes->source-tree", "agent-lanes", "source-tree", "workspace", lane_status, "Agent lanes operate on the source worktree.", _provenance("mission-control", "Lane process probes.")))
    edges.append(_nexus_edge("source-tree->audit-store", "source-tree", "audit-store", "verification trail", "ok", "Local tests and artifacts stay in the audit worktree.", _provenance("filesystem", str(source_root))))

    # --- Posture, gating, summary ----------------------------------------
    needs_joseph = [
        {
            "id": node["id"],
            "label": node["label"],
            "reason": node["summary"],
            "gate": "Human review required before state-changing recovery.",
        }
        for node in nodes
        if node["needs_joseph"]
    ]

    safe_actions = [
        {"id": "copy-summary", "label": "Copy health summary", "kind": "copy",
         "payload": "Hermes System Health is read-only; inspect degraded nodes before changing runtime state."},
        {"id": "open-explorer", "label": "Open Explorer", "kind": "open", "payload": "/explorer"},
        {"id": "open-logs", "label": "Read logs", "kind": "open", "payload": "/logs"},
        {"id": "open-cron", "label": "Open Cron", "kind": "open", "payload": "/cron"},
    ]
    locked_actions = [
        {"id": "restart-gateway", "label": "Restart gateway", "gate": "disabled", "reason": "State-changing service control is intentionally unavailable here."},
        {"id": "kanban-dispatch", "label": "Dispatch / reclaim Kanban work", "gate": "copy-only", "reason": "Requires explicit operator intent outside System Health."},
        {"id": "gitnexus-ingest", "label": "Rebuild GitNexus indexes", "gate": "disabled", "reason": "This endpoint only uses the read-only runtime collector."},
        {"id": "provider-auth", "label": "Change provider or MCP auth", "gate": "disabled", "reason": "Auth and billing configuration stay outside this page."},
    ]

    counts = {
        st: sum(1 for node in nodes if node["status"] == st)
        for st in ("ok", "warn", "error", "unknown", "auth_gated")
    }
    node_statuses = {node["status"] for node in nodes}
    if needs_joseph:
        posture = "stop"
    elif node_statuses & {"warn", "error", "unknown", "auth_gated"}:
        posture = "caution"
    else:
        posture = "safe"

    degraded = [node["label"] for node in nodes
                if node["status"] in {"warn", "error", "unknown", "auth_gated"}]
    if posture == "safe":
        summary = "All observed systems are safe for read-only inspection."
    elif posture == "stop":
        summary = (f"{len(needs_joseph)} node(s) need Joseph: "
                   f"{', '.join(g['label'] for g in needs_joseph[:4])}.")
    else:
        summary = (f"{len(degraded)} of {len(nodes)} node(s) need attention: "
                   f"{', '.join(degraded[:4])}{'…' if len(degraded) > 4 else ''}.")

    evidence = [
        {"source": "mission-control", "detail": "Runtime probes feed core status."},
        {"source": "gitnexus-runtime-collector", "detail": "Read-only topology snapshot; no ingest or index rebuild."},
        {"source": "systemd", "detail": "systemctl --user list-units hermes-* (read-only)."},
        {"source": "docker", "detail": "docker ps -a for MVMS Supabase containers (read-only)."},
        {"source": "tcp-probe", "detail": "localhost TCP connects to infrastructure ports."},
        {"source": "filesystem", "detail": "Source and audit paths resolved from this worktree."},
    ]

    return {
        "generated_at": generated_at,
        "posture": posture,
        "summary": summary,
        "counts": counts,
        "nodes": nodes,
        "edges": edges,
        "needs_joseph": needs_joseph,
        "safe_actions": safe_actions,
        "locked_actions": locked_actions,
        "evidence": evidence,
    }


def _get_nexus_health() -> dict:
    """30 s-cached System Health graph (matches the mission snapshot cadence)."""
    global _NEXUS_CACHE
    now = time.monotonic()
    if _NEXUS_CACHE and now < _NEXUS_CACHE[1]:
        return _NEXUS_CACHE[0]
    with _NEXUS_LOCK:
        # Re-check after acquiring lock (another thread may have rebuilt it).
        now = time.monotonic()
        if _NEXUS_CACHE and now < _NEXUS_CACHE[1]:
            return _NEXUS_CACHE[0]
        data = _build_nexus_health()
        _NEXUS_CACHE = (data, now + _NEXUS_TTL)
    return data


# ---------------------------------------------------------------------------
# Per-node detail: metric cards, history sparklines, recommendations
# ---------------------------------------------------------------------------

def _node_metric_cards(node: dict) -> list[dict]:
    """Format a node's raw metrics dict into display-ready cards."""
    cards: list[dict] = []
    for key, value in (node.get("metrics") or {}).items():
        if value is None or value == "" or value == []:
            continue
        label = key.replace("_", " ").replace("usd", "USD").strip().title()
        if isinstance(value, bool):
            display = "yes" if value else "no"
        elif isinstance(value, float):
            display = f"{value:.2f}"
        elif isinstance(value, list):
            display = ", ".join(str(v) for v in value[:4]) or "—"
        else:
            display = str(value)
        cards.append({"label": label, "value": display})
    return cards


def _node_history(node: dict) -> list[dict]:
    """Attach real time-series history where it exists for this node."""
    history: list[dict] = []
    node_id = node["id"]
    if node_id == "kanban":
        queue = _get_queue_depth("7d")
        if queue.get("points"):
            history.append({
                "label": "Tasks created / day (7d)", "kind": "queue",
                "openNow": queue.get("openNow", 0), "points": queue["points"],
            })
    if node_id in {"agent-lanes", "hermes"}:
        spend = _get_spend("7d")
        if spend.get("points"):
            history.append({
                "label": "Spend / day (7d, est. USD)", "kind": "spend",
                "points": spend["points"],
            })
    return history


def _node_recommendations(node: dict) -> list[dict]:
    """Concrete fix (unhealthy) or optimization (healthy) recommendations."""
    status = node["status"]
    kind = node["kind"]
    healthy = status == "ok"
    metrics = node.get("metrics") or {}
    recs: list[dict] = []

    def fix(title: str, detail: str, command: Optional[str] = None) -> None:
        recs.append({"kind": "fix", "title": title, "detail": detail, "command": command})

    def opt(title: str, detail: str, command: Optional[str] = None) -> None:
        recs.append({"kind": "optimization", "title": title, "detail": detail, "command": command})

    if not healthy:
        if kind == "service":
            unit = metrics.get("unit", node["label"])
            fix("Inspect the unit journal",
                "Read recent logs to find why the unit is not active. This is read-only.",
                f"journalctl --user -u {unit} -n 120 --no-pager")
            fix("Recover only after diagnosis",
                "Once the cause is understood the operator can recover the unit; "
                "System Health never controls services itself.",
                f"systemctl --user status {unit}")
        elif kind == "container":
            cname = metrics.get("container", node["label"])
            fix("Inspect container logs",
                "Check the container's recent output for a crash or failed health probe.",
                f"docker logs --tail 120 {cname}")
            fix("Confirm intended state",
                "Verify the MVMS Supabase stack is meant to be running before any recovery.",
                f"docker inspect {cname}")
        elif kind == "port":
            port = metrics.get("port")
            fix("Find the owning process",
                f"Nothing is accepting TCP connections on port {port}; the owning "
                "service is likely down.",
                f"ss -tlnp | grep :{port}")
        elif kind == "gateway":
            fix("Read gateway state",
                "Inspect gateway_state.json and gateway logs to find which platform adapter degraded.")
        elif kind == "kanban":
            fix("Check the dispatcher",
                "The Kanban probe failed. Confirm the board DB is reachable; do not reclaim work here.")
        elif kind in {"mcp", "memory"}:
            fix("Re-check MCP auth",
                "An MCP server is offline or auth-gated. Confirm tokens and selection.",
                "hermes mcp list")
        elif kind.endswith("-group"):
            fix("Open a degraded child node",
                "One or more members of this group need attention — click a red or "
                "amber child node for its specific fix.")
        else:
            fix("Inspect logs for this node",
                "Open the related logs to understand the degraded state before any change.")
        if node.get("needs_joseph"):
            fix("Human gate is active",
                "Recovery for this node changes runtime state and requires Joseph's explicit review.")
    else:
        if kind == "service":
            opt("Healthy — keep it observable",
                "Unit is active. Review journald rate-limits if it logs heavily.")
        elif kind == "container":
            opt("Healthy — watch resource headroom",
                "Container is up. Spot-check memory use on the Supabase stack.",
                f"docker stats --no-stream {metrics.get('container', '')}".rstrip())
        elif kind == "port":
            latency = metrics.get("latency_ms") or 0
            opt("Reachable — latency is healthy" if latency < 50 else "Reachable — latency is elevated",
                f"TCP connect succeeded in {metrics.get('latency_ms')} ms. "
                "Sub-50 ms localhost latency is healthy.")
        elif kind == "kanban":
            opt("Drain the backlog steadily",
                "Dispatcher is healthy — keep the open-task count trending down.")
        elif kind in {"mcp", "memory"}:
            opt("Tool surface healthy",
                "All MCP servers are reachable. Prune unused servers to cut context overhead.")
        elif kind in {"runtime", "agent-lane"}:
            opt("Runtime healthy — watch spend",
                "Lanes are ready. Track daily spend so cost stays predictable.")
        elif kind == "gateway":
            opt("Gateway healthy",
                "All platform adapters are connected. Keep an eye on per-platform latency.")
        elif kind.endswith("-group"):
            opt("Group healthy",
                "Every member of this group is reporting OK — no action needed.")
        else:
            opt("Healthy — no action needed",
                "This node is operating normally; keep it under periodic observation.")
    return recs


def _build_node_detail(node_id: str) -> Optional[dict]:
    """Return summary + metrics + history + recommendations for one node."""
    health = _get_nexus_health()
    node = next((n for n in health["nodes"] if n["id"] == node_id), None)
    if node is None:
        return None
    detail = dict(node)
    detail["generated_at"] = health["generated_at"]
    detail["metric_cards"] = _node_metric_cards(node)
    detail["history"] = _node_history(node)
    detail["recommendations"] = _node_recommendations(node)
    detail["connections"] = [
        {
            "id": edge["id"],
            "label": edge["label"],
            "status": edge["status"],
            "direction": "out" if edge["source"] == node_id else "in",
            "peer": edge["target"] if edge["source"] == node_id else edge["source"],
        }
        for edge in health["edges"]
        if edge["source"] == node_id or edge["target"] == node_id
    ]
    return detail


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# Hard timeout (seconds) for the nexus-health cold-build path.
_NEXUS_HEALTH_TIMEOUT = 12.0


@router.get("/mission", summary="Mission Control snapshot")
async def get_mission_snapshot() -> dict:
    """Combined MissionSnapshot with real live data. 30 s server-side cache."""
    return await asyncio.get_running_loop().run_in_executor(None, _get_snapshot)


@router.get("/nexus-health", summary="System Health read-only graph")
async def get_nexus_health() -> dict:
    """Read-only command-center health map for the whole Hermes infrastructure."""
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _get_nexus_health),
            timeout=_NEXUS_HEALTH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="System Health build timed out — try again in a moment.",
        )


@router.get("/nexus-health/node/{node_id}", summary="Per-node System Health detail")
async def get_nexus_health_node(node_id: str) -> dict:
    """Summary, metrics, history sparklines and fix/optimization recommendations
    for a single node in the System Health graph. Read-only."""
    detail = await asyncio.get_running_loop().run_in_executor(
        None, _build_node_detail, node_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unknown System Health node: {node_id}")
    return detail


@router.get("/health/runtime/{name}", summary="Single runtime health probe")
async def get_runtime_health(name: str) -> dict:
    """Live probe for one named runtime."""
    probe = _PROBE_MAP.get(name)
    if probe is None:
        return {"name": name, "label": name, "status": "unknown", "lastChecked": _now()}
    return await asyncio.get_running_loop().run_in_executor(None, probe)


@router.get("/spend", summary="Spend history sparkline")
async def get_spend(range: str = "7d") -> dict:
    """Spend history from claude session JSONL files. range=1d|7d|30d."""
    if range not in ("1d", "7d", "30d"):
        range = "7d"
    return await asyncio.get_running_loop().run_in_executor(None, _get_spend, range)


def _get_queue_depth(range_str: str) -> dict:
    """Daily task creation counts across all hermes kanban boards.
    Proxy for 'workload'; each task represents a unit of dispatch.
    """
    import glob, sqlite3
    days = {"1d": 1, "7d": 7, "30d": 30}.get(range_str, 7)
    aggregated: dict[str, int] = {}
    open_total = 0
    for path in glob.glob(str(HERMES_HOME / "kanban/boards/*/kanban.db")):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            c = conn.cursor()
            c.execute(
                "SELECT date(created_at,'unixepoch') d, count(*) "
                "FROM tasks "
                "WHERE created_at >= strftime('%s','now',?) "
                "GROUP BY d",
                (f"-{days} days",),
            )
            for d, n in c.fetchall():
                aggregated[d] = aggregated.get(d, 0) + n
            c.execute(
                "SELECT count(*) FROM tasks "
                "WHERE status NOT IN ('done','archived','complete')"
            )
            open_total += c.fetchone()[0]
            conn.close()
        except Exception:
            continue
    points = [{"date": d, "count": n} for d, n in sorted(aggregated.items())]
    return {"range": range_str, "points": points, "openNow": open_total}


@router.get("/queue", summary="Queue depth — kanban task creation per day")
async def get_queue(range: str = "7d") -> dict:
    """Daily task creation across all kanban boards + current open count."""
    if range not in ("1d", "7d", "30d"):
        range = "7d"
    return await asyncio.get_running_loop().run_in_executor(None, _get_queue_depth, range)


@router.get("/swarm", summary="Swarm status")
async def get_swarm() -> dict:
    """Ruflo + Hermes subagents + Kanban dispatcher swarm status."""
    swarm = await asyncio.get_running_loop().run_in_executor(None, _get_swarm_status)
    if swarm is None:
        return {"active": False, "message": "No active swarm detected"}
    return {"active": True, **swarm}


@router.get("/cron", summary="Cron jobs — next firings and last runs")
async def get_cron() -> dict:
    """All cron jobs with next firing times and last run status."""
    jobs = await asyncio.get_running_loop().run_in_executor(None, _get_all_cron_jobs)
    return {"jobs": jobs, "count": len(jobs)}


@router.get("/dreams/latest", summary="Last overnight reflection brief")
async def get_latest_dream() -> dict:
    """Return the most recent dream-reflect brief from ~/.hermes/dreams/."""
    dreams_dir = HERMES_HOME / "dreams"
    if not dreams_dir.exists():
        return {"dream": None, "date": None, "message": "No dreams directory yet"}

    dream_files = sorted(dreams_dir.glob("*.md"), reverse=True)
    if not dream_files:
        return {"dream": None, "date": None, "message": "No dream files yet"}

    try:
        latest = dream_files[0]
        content = latest.read_text(errors="replace")
        date_str = latest.stem  # filename is YYYY-MM-DD.md
        return {"dream": content, "date": date_str, "filename": latest.name}
    except Exception as e:
        return {"dream": None, "date": None, "error": str(e)}


@router.get("/stream", summary="SSE runtime health stream (1 Hz chip updates)")
async def stream_health() -> StreamingResponse:
    """Server-Sent Events stream. One SSEHealthEvent per runtime per 10 s cycle.
    Heartbeat every 15 s to keep proxies alive. id: field for Last-Event-ID resume.
    """
    async def _generate():
        event_id = 0
        loop = asyncio.get_running_loop()
        last_heartbeat = time.monotonic()

        while True:
            # Probe all runtimes in executor to avoid blocking the event loop
            chips = await loop.run_in_executor(None, _probe_all)
            for chip in chips:
                event = {**chip, "eventType": "health"}
                yield f"id: {event_id}\ndata: {json.dumps(event)}\n\n"
                event_id += 1
                await asyncio.sleep(0.1)  # small gap between chips

            # Heartbeat: emit a comment line every 15 s
            now = time.monotonic()
            if now - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = now

            # Wait for the next cycle (10 s total, accounting for probe time)
            await asyncio.sleep(10)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Hives endpoints — read-only observability for Ruflo hive runs
# ---------------------------------------------------------------------------

_HIVES_TIMEOUT = 10.0


@router.get("/hives", summary="Read-only snapshot of all Ruflo hive runs")
async def get_hives_snapshot() -> dict:
    """Scans ~/.hermes/ruflo-work for hive run directories.

    Returns a cached (15 s TTL) snapshot sorted active-first.  Strictly
    read-only — no subprocess that mutates state.
    """
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _get_hives_snapshot),
            timeout=_HIVES_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Hives snapshot build timed out — try again in a moment.",
        )


@router.get("/hives/{hive_id}/log", summary="Tail hive-mind.log for one hive run")
async def get_hive_log(hive_id: str, tail: int = 200) -> dict:
    """Return the last N lines (default 200, max 1000) of hive-mind.log.

    hive_id is validated against the live snapshot whitelist — no path
    traversal is possible.  Read-only.
    """
    tail = max(1, min(tail, 1000))
    result = await asyncio.get_running_loop().run_in_executor(
        None, _get_hive_log_tail, hive_id, tail,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown hive id: {hive_id}")
    return result
