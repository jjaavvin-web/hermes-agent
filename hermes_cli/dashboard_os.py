"""OS Tab — Infrastructure Health Snapshot API (2026-06-12).

GET /api/dashboard/os  →  OSSnapshot

Sections (8):
  gateway, providers, containers, systemd, backups,
  memory_stores, cron, host

Auth: same HTTP middleware as all /api/dashboard/* routes.
Never 500: every probe is wrapped in try/except.
Cache: 20 s TTL with single-flight lock.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-os"])

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"
MEMORY_MD = HOME / ".claude" / "projects" / "-home-josep--local-share-hermes-agent" / "memory" / "MEMORY.md"

# ---------------------------------------------------------------------------
# Cache (20 s TTL, single-flight)
# ---------------------------------------------------------------------------
_OS_CACHE: tuple[dict, float] | None = None
_OS_TTL = 20.0
_OS_LOCK = threading.Lock()

# Sub-caches for slow docker-exec psql probes (60 s TTL each)
_MVMS_COUNT_CACHE: tuple[int | None, float] | None = None
_HONCHO_COUNT_CACHE: tuple[int | None, float] | None = None
_DB_CACHE_TTL = 60.0
_MVMS_LOCK = threading.Lock()
_HONCHO_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Type helpers (dicts matching the TypeScript interfaces in the spec)
# ---------------------------------------------------------------------------

Status = str  # "green" | "amber" | "red" | "unknown"


def _item(name: str, status: Status, detail: str, metric: Optional[str] = None) -> dict:
    d: dict = {"name": name, "status": status, "detail": detail}
    if metric is not None:
        d["metric"] = metric
    return d


def _section(id_: str, label: str, items: list[dict]) -> dict:
    worst = _worst_status([i["status"] for i in items])
    return {"id": id_, "label": label, "status": worst, "items": items}


def _worst_status(statuses: list[Status]) -> Status:
    order = {"red": 0, "amber": 1, "unknown": 2, "green": 3}
    if not statuses:
        return "unknown"
    return min(statuses, key=lambda s: order.get(s, 2))


def _run(cmd: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Graph helpers (Appendix B)
# ---------------------------------------------------------------------------

def _bind(sections: list[dict], section_id: str, item_name: str) -> tuple[Status, Optional[str]]:
    """Look up (status, detail) for a named item within a section.

    Falls back to ("unknown", None) when the section or item is absent.
    """
    for sec in sections:
        if sec.get("id") == section_id:
            for item in sec.get("items", []):
                if item.get("name") == item_name:
                    return item.get("status", "unknown"), item.get("detail")
            # Section found but item missing
            return "unknown", None
    # Section missing entirely
    return "unknown", None


def _bridge_edge_state() -> str:
    """Return 'live' if kanban-mvms-bridge.timer is enabled, else 'disabled'."""
    try:
        r = _run(
            ["systemctl", "--user", "is-enabled", "kanban-mvms-bridge.timer"],
            timeout=3.0,
        )
        if r.stdout.strip() == "enabled":
            return "live"
    except Exception:
        pass
    return "disabled"


def _build_os_graph(sections: list[dict]) -> dict:
    """Build the Nexus graph (24 nodes, ~23 edges) from Appendix B.

    Node status is BOUND from the already-computed sections; edges are static
    topology with one dynamic state (kanban-db→mvms bridge timer check).
    """
    # ------------------------------------------------------------------
    # Helper: worst status across a list of (section_id, item_name) bindings
    # ------------------------------------------------------------------
    def _bind_worst(bindings: list[tuple[str, str]]) -> tuple[Status, Optional[str]]:
        results = [_bind(sections, sid, iname) for sid, iname in bindings]
        worst_s = _worst_status([s for s, _ in results])
        # Use the detail from the worst-status binding (first match wins)
        detail = next((d for s, d in results if s == worst_s), None)
        return worst_s, detail

    def _node(
        id_: str,
        label: str,
        kind: str,
        group: str,
        status: Status,
        detail: Optional[str] = None,
        section_ref: Optional[str] = None,
    ) -> dict:
        n: dict = {
            "id": id_,
            "label": label,
            "kind": kind,
            "group": group,
            "status": status,
        }
        if detail is not None:
            n["detail"] = detail
        if section_ref is not None:
            n["section_ref"] = section_ref
        return n

    def _edge(
        id_: str,
        source: str,
        target: str,
        label: Optional[str] = None,
        state: str = "live",
    ) -> dict:
        e: dict = {"id": id_, "source": source, "target": target, "state": state}
        if label is not None:
            e["label"] = label
        return e

    # ------------------------------------------------------------------
    # NODES (24)
    # ------------------------------------------------------------------
    nodes: list[dict] = []

    # --- surfaces (4) ---
    # claude-code: bind memory_stores/memory_md (fresh→green)
    s, d = _bind(sections, "memory_stores", "memory_md")
    nodes.append(_node("claude-code", "Claude Code", "client", "surfaces", s, d, "memory_stores"))

    # discord: bind gateway state platforms (gateway section worst is the proxy)
    s, d = _bind(sections, "gateway", "gateway_state")
    nodes.append(_node("discord", "Discord", "surface", "surfaces", s, d, "gateway"))

    # dashboard: bind systemd / hermes-dashboard active — we look for the hermes-dashboard item
    # The systemd section probes "timers_keep" / "failed_units"; hermes-dashboard is a service
    # not directly probed by name.  Use section worst as proxy (timers section covers dashboard).
    s_sd, d_sd = _bind(sections, "systemd", "failed_units")
    # If failed_units is green, treat dashboard as green; otherwise surface the systemd status.
    dash_status: Status = "green" if s_sd == "green" else s_sd
    nodes.append(_node("dashboard", "Dashboard (:9119)", "ui", "surfaces", dash_status, d_sd, "systemd"))

    # codex-pipeline: bind providers/codex_pipeline
    s, d = _bind(sections, "providers", "codex_pipeline")
    nodes.append(_node("codex-pipeline", "Codex Pipeline", "pipeline", "surfaces", s, d, "providers"))

    # --- control (4) ---
    # gateway: worst of gateway section
    gw_statuses = [item.get("status", "unknown") for item in
                   next((sec["items"] for sec in sections if sec["id"] == "gateway"), [])]
    gw_status = _worst_status(gw_statuses) if gw_statuses else "unknown"
    gw_detail = next(
        (item.get("detail") for item in
         next((sec["items"] for sec in sections if sec["id"] == "gateway"), [])
         if item.get("status") == gw_status),
        None,
    )
    nodes.append(_node("gateway", "Gateway", "service", "control", gw_status, gw_detail, "gateway"))

    # watchdog: bind gateway/watchdog_events
    s, d = _bind(sections, "gateway", "watchdog_events")
    nodes.append(_node("watchdog", "Watchdog", "guard", "control", s, d, "gateway"))

    # hermes-cron: bind cron section worst
    cron_statuses = [item.get("status", "unknown") for item in
                     next((sec["items"] for sec in sections if sec["id"] == "cron"), [])]
    cron_status = _worst_status(cron_statuses) if cron_statuses else "unknown"
    cron_detail = next(
        (item.get("detail") for item in
         next((sec["items"] for sec in sections if sec["id"] == "cron"), [])
         if item.get("status") == cron_status),
        None,
    )
    nodes.append(_node("hermes-cron", "Hermes Cron", "scheduler", "control", cron_status, cron_detail, "cron"))

    # timers: bind systemd section worst
    sd_statuses = [item.get("status", "unknown") for item in
                   next((sec["items"] for sec in sections if sec["id"] == "systemd"), [])]
    sd_status = _worst_status(sd_statuses) if sd_statuses else "unknown"
    sd_detail = next(
        (item.get("detail") for item in
         next((sec["items"] for sec in sections if sec["id"] == "systemd"), [])
         if item.get("status") == sd_status),
        None,
    )
    nodes.append(_node("timers", "Timers", "scheduler", "control", sd_status, sd_detail, "systemd"))

    # --- providers (3) ---
    # chatgpt-backend: bind providers/codex_process
    s, d = _bind(sections, "providers", "codex_process")
    nodes.append(_node("chatgpt-backend", "ChatGPT Backend", "llm", "providers", s, d, "providers"))

    # claude-max: static green "Max cli-subprocess"
    nodes.append(_node("claude-max", "Claude Max", "llm", "providers",
                       "green", "Max cli-subprocess", "providers"))

    # openrouter: bind providers/openrouter_key
    s, d = _bind(sections, "providers", "openrouter_key")
    nodes.append(_node("openrouter", "OpenRouter", "llm", "providers", s, d, "providers"))

    # --- memory (9) ---
    # mvms: bind memory_stores/mvms_observations
    s, d = _bind(sections, "memory_stores", "mvms_observations")
    nodes.append(_node("mvms", "MVMS", "database", "memory", s, d, "memory_stores"))

    # supabase-db: bind containers item (supabase_db_goattrade-system)
    s, d = _bind(sections, "containers", "supabase_db_goattrade-system")
    nodes.append(_node("supabase-db", "Supabase DB", "database", "memory", s, d, "containers"))

    # honcho-api: bind containers item (honcho-api-1)
    s, d = _bind(sections, "containers", "honcho-api-1")
    nodes.append(_node("honcho-api", "Honcho API", "service", "memory", s, d, "containers"))

    # honcho-deriver: bind containers item (honcho-deriver-1)
    s, d = _bind(sections, "containers", "honcho-deriver-1")
    nodes.append(_node("honcho-deriver", "Honcho Deriver", "worker", "memory", s, d, "containers"))

    # honcho-db: bind containers item (honcho-database-1)
    s, d = _bind(sections, "containers", "honcho-database-1")
    nodes.append(_node("honcho-db", "Honcho DB", "database", "memory", s, d, "containers"))

    # honcho-redis: containers items — look for redis container; may not be in core set
    s, d = _bind(sections, "containers", "honcho-redis-1")
    if s == "unknown":
        # Try alternate name
        s, d = _bind(sections, "containers", "honcho-redis")
    nodes.append(_node("honcho-redis", "Honcho Redis", "cache", "memory", s, d, "containers"))

    # state-db: bind memory_stores/state_db
    s, d = _bind(sections, "memory_stores", "state_db")
    nodes.append(_node("state-db", "State DB", "database", "memory", s, d, "memory_stores"))

    # kanban-db: bind memory_stores/kanban_db
    s, d = _bind(sections, "memory_stores", "kanban_db")
    nodes.append(_node("kanban-db", "Kanban DB", "database", "memory", s, d, "memory_stores"))

    # claude-memory: bind memory_stores/memory_md
    s, d = _bind(sections, "memory_stores", "memory_md")
    nodes.append(_node("claude-memory", "Claude Memory", "store", "memory", s, d, "memory_stores"))

    # hermes-memories: static green
    nodes.append(_node("hermes-memories", "Hermes Memories", "store", "memory",
                       "green", "Honcho-managed memories", "memory_stores"))

    # --- protection (3) ---
    # nightly-backup: worst of mvms-canonical/honcho-live/app-state ages
    backup_items = [
        _bind(sections, "backups", "mvms-canonical-*.sql.gz"),
        _bind(sections, "backups", "honcho-live-store-*.sql.gz"),
        _bind(sections, "backups", "hermes-app-state-*.tar.gz"),
    ]
    backup_statuses = [s for s, _ in backup_items]
    nb_status = _worst_status(backup_statuses)
    nb_detail = next((d for s, d in backup_items if s == nb_status), None)
    nodes.append(_node("nightly-backup", "Nightly Backup", "backup", "protection",
                       nb_status, nb_detail, "backups"))

    # backups-dir: static green (directory existence is a precondition, not actively probed)
    nodes.append(_node("backups-dir", "Backups Dir", "storage", "protection",
                       "green", "~/.hermes/backups", "backups"))

    # veracrypt: bind backups/veracrypt_weekly
    s, d = _bind(sections, "backups", "veracrypt_weekly")
    nodes.append(_node("veracrypt", "VeraCrypt", "backup", "protection", s, d, "backups"))

    # --- host (1) ---
    # wsl-host: bind host section worst
    host_items = next((sec["items"] for sec in sections if sec["id"] == "host"), [])
    host_statuses = [item.get("status", "unknown") for item in host_items]
    host_status = _worst_status(host_statuses) if host_statuses else "unknown"
    host_detail = next(
        (item.get("detail") for item in host_items if item.get("status") == host_status),
        None,
    )
    nodes.append(_node("wsl-host", "WSL Host", "host", "host", host_status, host_detail, "host"))

    # ------------------------------------------------------------------
    # EDGES (~23)  state: live | disabled | broken | gated
    # ------------------------------------------------------------------
    edges: list[dict] = []

    # surfaces → control
    edges.append(_edge("e-discord-gateway",    "discord",       "gateway",        "chat",                  "live"))
    edges.append(_edge("e-dashboard-gateway",  "dashboard",     "gateway",        "state (ro)",            "live"))
    edges.append(_edge("e-watchdog-gateway",   "watchdog",      "gateway",        "dead-man",              "live"))
    edges.append(_edge("e-cron-gateway",       "hermes-cron",   "gateway",        "jobs",                  "live"))

    # gateway → providers
    edges.append(_edge("e-gw-chatgpt",         "gateway",       "chatgpt-backend","main lane gpt-5.5",     "live"))
    edges.append(_edge("e-gw-claudemax",       "gateway",       "claude-max",     "premium lane",          "live"))

    # codex-pipeline → provider
    edges.append(_edge("e-codex-chatgpt",      "codex-pipeline","chatgpt-backend","ChatGPT-Max",           "live"))

    # gateway + claude-code → honcho
    edges.append(_edge("e-claudecode-honcho",  "claude-code",   "honcho-api",     "SessionEnd bridge",     "live"))
    edges.append(_edge("e-gw-honcho",          "gateway",       "honcho-api",     "turns+dialectic",       "live"))
    edges.append(_edge("e-hapi-hdb",           "honcho-api",    "honcho-db",      None,                    "live"))
    edges.append(_edge("e-hapi-hredis",        "honcho-api",    "honcho-redis",   None,                    "live"))
    edges.append(_edge("e-hderiver-hdb",       "honcho-deriver","honcho-db",      None,                    "live"))
    edges.append(_edge("e-hderiver-or",        "honcho-deriver","openrouter",     "derivation",            "live"))

    # claude-code → memory
    edges.append(_edge("e-claudecode-mvms",    "claude-code",   "mvms",           "MCP /remember",         "live"))
    edges.append(_edge("e-gw-mvms",            "gateway",       "mvms",           "mvms_record_*",         "live"))
    edges.append(_edge("e-claudecode-cmem",    "claude-code",   "claude-memory",  "auto-memory",           "live"))

    # gateway → state-db / kanban-db
    edges.append(_edge("e-gw-statedb",         "gateway",       "state-db",       "sessions",              "live"))
    edges.append(_edge("e-gw-kanbandb",        "gateway",       "kanban-db",      "dispatcher",            "live"))

    # mvms → supabase-db
    edges.append(_edge("e-mvms-supadb",        "mvms",          "supabase-db",    "schema on",             "live"))

    # kanban-db → mvms (dynamic: live if timer enabled, else disabled)
    bridge_state = _bridge_edge_state()
    edges.append(_edge("e-kanban-mvms",        "kanban-db",     "mvms",           "bridge (staged)",       bridge_state))

    # claude-code → mvms (gated)
    edges.append(_edge("e-claudecode-mvms-gated","claude-code", "mvms",           "lesson promote (weekly, human-gated)", "gated"))

    # nightly-backup → stores
    edges.append(_edge("e-backup-mvms",        "nightly-backup","mvms",           "02:30 dump",            "live"))
    edges.append(_edge("e-backup-hdb",         "nightly-backup","honcho-db",      "02:30 dump",            "live"))
    edges.append(_edge("e-backup-cmem",        "nightly-backup","claude-memory",  "app-state tar",         "live"))

    # veracrypt → backups-dir  (edge stays live; node status carries the stale signal)
    edges.append(_edge("e-vera-bdir",          "veracrypt",     "backups-dir",    None,                    "live"))

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Section 1: gateway
# ---------------------------------------------------------------------------

def _probe_gateway() -> dict:
    state_path = HERMES_HOME / "gateway_state.json"
    data = json.loads(state_path.read_text())
    pid = data.get("pid")
    gw_state = data.get("gateway_state", "unknown")
    uptime_s = None
    start_time = data.get("start_time")
    if start_time:
        try:
            # start_time = field 22 of /proc/<pid>/stat = clock ticks since boot.
            # Convert to seconds using SC_CLK_TCK (typically 100 on Linux/WSL2).
            import os as _os
            hz = _os.sysconf("SC_CLK_TCK") or 100
            proc_uptime_s = float(Path("/proc/uptime").read_text().split()[0])
            proc_start_s = int(start_time) / hz
            candidate = int(proc_uptime_s - proc_start_s)
            # Accept only sane positive values under 90 days.
            if 0 < candidate < 90 * 86400:
                uptime_s = candidate
        except Exception:
            pass

    # PID alive check
    pid_alive = False
    if pid:
        try:
            os.kill(int(pid), 0)
            pid_alive = True
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if pid_alive and gw_state == "running":
        status: Status = "green"
    elif gw_state in ("running",):
        status = "amber"
    else:
        status = "red"

    uptime_str = f"{uptime_s // 3600}h{(uptime_s % 3600) // 60}m" if uptime_s and uptime_s >= 0 else "?"
    detail = f"pid={pid} state={gw_state} uptime={uptime_str}"
    return _item("gateway_state", status, detail)


def _probe_gateway_systemctl() -> dict:
    r = _run(["systemctl", "--user", "is-active", "hermes-gateway"])
    active = r.stdout.strip()
    status: Status = "green" if active == "active" else ("amber" if active == "activating" else "red")
    return _item("systemd_unit", status, f"hermes-gateway.service: {active}")


def _probe_gateway_watchdog() -> dict:
    # Silence file → red if present
    silence = HERMES_HOME / "state" / "gateway-watchdog" / "silence"
    if silence.exists():
        return _item("watchdog_silence", "red", "watchdog silenced — touch ~/.hermes/state/gateway-watchdog/silence present")

    # Last events.jsonl entry
    events_path = HERMES_HOME / "state" / "gateway-watchdog" / "events.jsonl"
    if events_path.exists():
        try:
            lines = events_path.read_text().splitlines()
            for line in reversed(lines):
                line = line.strip()
                if line:
                    ev = json.loads(line)
                    ev_status = ev.get("status", "unknown")
                    s: Status = "green" if ev_status == "ok" else ("amber" if ev_status in ("warn",) else "red")
                    return _item("watchdog_events", s, f"last event: {ev_status} at {ev.get('ts','?')}")
        except Exception as e:
            return _item("watchdog_events", "unknown", f"parse error: {e}")
    return _item("watchdog_events", "unknown", "no events.jsonl")


def _section_gateway() -> dict:
    items: list[dict] = []
    for probe in (_probe_gateway, _probe_gateway_systemctl, _probe_gateway_watchdog):
        try:
            items.append(probe())
        except Exception as e:
            items.append(_item(probe.__name__, "unknown", str(e)))
    return _section("gateway", "Gateway", items)


# ---------------------------------------------------------------------------
# Section 2: providers
# ---------------------------------------------------------------------------

def _probe_codex_process() -> dict:
    """Hermes uses the ChatGPT HTTP backend; no local codex process required.
    Green when the pipeline snapshot loads OK; amber only if the snapshot fails.
    """
    try:
        from hermes_cli.dashboard_codex_sessions import _cached_snapshot
        snap = _cached_snapshot()
        counts = snap.get("counts", {})
        total = counts.get("total", 0)
        return _item("codex_process", "green",
                     f"backend lane (HTTP); {total} sessions tracked",
                     metric=str(total))
    except Exception as e:
        return _item("codex_process", "amber", f"pipeline snapshot unavailable: {e}")


def _probe_codex_sessions() -> dict:
    """Import codex-sessions snapshot loader for counts by state."""
    try:
        from hermes_cli.dashboard_codex_sessions import _cached_snapshot
        snap = _cached_snapshot()
        counts = snap.get("counts", {})
        total = counts.get("total", 0)
        by_state = counts.get("by_state", {})
        parts = ", ".join(f"{k}={v}" for k, v in by_state.items()) if by_state else "none"
        s: Status = "green" if total >= 0 else "unknown"
        return _item("codex_pipeline", s, f"{total} sessions: {parts}", metric=str(total))
    except Exception as e:
        return _item("codex_pipeline", "unknown", f"codex-sessions import failed: {e}")


def _probe_claude_cli() -> dict:
    """claude binary exists + last cli-subprocess marker."""
    try:
        binary = shutil.which("claude")
        if not binary:
            return _item("claude_cli", "red", "claude binary not found in PATH")
        # Best-effort: check for cli-subprocess state file
        state_dir = HERMES_HOME / "state"
        # Look for any recent cli-subprocess or claude-cli-* artifact
        cli_markers = list(state_dir.glob("*cli*subprocess*")) + list(state_dir.glob("*claude-cli*"))
        detail = f"binary={binary}"
        if cli_markers:
            newest = max(cli_markers, key=lambda p: p.stat().st_mtime)
            age_h = (time.time() - newest.stat().st_mtime) / 3600
            detail += f", last turn ≈{age_h:.1f}h ago"
        return _item("claude_cli", "green", detail)
    except Exception as e:
        return _item("claude_cli", "unknown", str(e))


def _probe_openrouter_key() -> dict:
    """OpenRouter key is deliberately absent from the gateway env.
    The key lives in the Honcho deriver compose env.  Check that file for
    LLM_OPENROUTER_API_KEY — never read/print the value, only present/absent + length>20.
    """
    HONCHO_ENV = Path.home() / "workspace" / "honcho-selfhost" / "honcho" / ".env"
    try:
        if not HONCHO_ENV.exists():
            return _item("openrouter_key", "amber",
                         f"honcho .env not found ({HONCHO_ENV})")
        for line in HONCHO_ENV.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("LLM_OPENROUTER_API_KEY"):
                parts = stripped.split("=", 1)
                if len(parts) == 2:
                    val = parts[1].strip().strip("'\"")
                    if len(val) > 20:
                        return _item("openrouter_key", "green",
                                     "honcho-managed (capped)")
        return _item("openrouter_key", "amber", "honcho deriver key missing")
    except Exception as e:
        return _item("openrouter_key", "unknown", str(e))


def _section_providers() -> dict:
    items: list[dict] = []
    for probe in (
        _probe_codex_process,
        _probe_codex_sessions,
        _probe_claude_cli,
        _probe_openrouter_key,
    ):
        try:
            items.append(probe())
        except Exception as e:
            items.append(_item(probe.__name__, "unknown", str(e)))
    return _section("providers", "Providers", items)


# ---------------------------------------------------------------------------
# Section 3: containers
# ---------------------------------------------------------------------------

# Core containers that must be running; anything else is "extra"
_CORE_CONTAINERS = frozenset({
    "honcho-database-1",
    "honcho-api-1",
    "honcho-deriver-1",
    "supabase_db_goattrade-system",
    "supabase_kong_goattrade-system",
})


def _section_containers() -> dict:
    items: list[dict] = []
    try:
        r = _run(["docker", "ps", "-a", "--format", "{{json .}}"])
        if r.returncode != 0:
            return _section("containers", "Containers",
                            [_item("docker", "unknown", f"docker ps failed: {r.stderr.strip()}")])
        containers: list[dict] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    containers.append(json.loads(line))
                except Exception:
                    pass

        by_name = {c.get("Names", ""): c for c in containers}
        extra_count = 0

        for name in sorted(_CORE_CONTAINERS):
            c = by_name.get(name)
            if c is None:
                items.append(_item(name, "red", "container not found"))
                continue
            state = c.get("State", "unknown").lower()
            status_str = c.get("Status", "")
            if state == "running":
                s: Status = "green"
            elif state in ("created", "restarting"):
                s = "amber"
            else:
                s = "red"
            items.append(_item(name, s, f"{state} — {status_str}"))

        # Count extra containers (not in core set)
        for name, c in by_name.items():
            if name not in _CORE_CONTAINERS:
                extra_count += 1

        if extra_count:
            items.append(_item("extras", "green", f"{extra_count} non-core container(s) present",
                               metric=str(extra_count)))
    except Exception as e:
        items.append(_item("containers", "unknown", str(e)))

    return _section("containers", "Containers", items)


# ---------------------------------------------------------------------------
# Section 4: systemd
# ---------------------------------------------------------------------------

_KEEP_TIMERS = frozenset({
    "mvms-backup.timer",
    "mvms-watcher.timer",
    "weekly-hygiene.timer",
    "hermes-gateway-watchdog.timer",
    "hermes-cron-integrity-guard.timer",
    "hermes-user-logrotate.timer",
    "hermes-divergence-gate.timer",
})


def _section_systemd() -> dict:
    items: list[dict] = []
    try:
        # Failed units
        r_fail = _run(["systemctl", "--user", "--failed", "--no-legend"])
        if r_fail.returncode not in (0, 1):
            items.append(_item("failed_units", "unknown", f"systemctl --failed error: {r_fail.stderr.strip()}"))
        else:
            failed_lines = [l.strip() for l in r_fail.stdout.splitlines() if l.strip()]
            failed_names = [l.split()[0] for l in failed_lines if l]
            if failed_names:
                items.append(_item("failed_units", "red",
                                   f"{len(failed_names)} failed: {', '.join(failed_names)}",
                                   metric=str(len(failed_names))))
            else:
                items.append(_item("failed_units", "green", "no failed units"))
    except Exception as e:
        items.append(_item("failed_units", "unknown", str(e)))

    try:
        # Timers
        r_timers = _run(["systemctl", "--user", "list-timers", "--no-legend"])
        active_timers: set[str] = set()
        if r_timers.returncode == 0:
            for line in r_timers.stdout.splitlines():
                parts = line.split()
                # line format: NEXT ... LAST ... UNIT  ACTIVATES
                # timer name is in column -2 (second-to-last)
                if len(parts) >= 2:
                    # Find the timer name — it ends with .timer
                    for part in parts:
                        if part.endswith(".timer"):
                            active_timers.add(part)
                            break

        missing = _KEEP_TIMERS - active_timers
        if missing:
            items.append(_item("timers_missing", "amber",
                               f"missing/inactive KEEP timers: {', '.join(sorted(missing))}"))
        else:
            items.append(_item("timers_keep", "green",
                               f"all {len(_KEEP_TIMERS)} KEEP timers active",
                               metric=str(len(active_timers))))
    except Exception as e:
        items.append(_item("timers_keep", "unknown", str(e)))

    return _section("systemd", "Systemd", items)


# ---------------------------------------------------------------------------
# Section 5: backups
# ---------------------------------------------------------------------------

_AMBER_H = 26
_RED_H = 50
_VERA_AMBER_DAYS = 8
_VERA_RED_DAYS = 15


def _backup_age_status(path_glob_parent: Path, pattern: str) -> dict:
    """Find newest matching file; return item with age-based status."""
    try:
        matches = sorted(path_glob_parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return _item(pattern, "red", "no backup found")
        newest = matches[0]
        age_h = (time.time() - newest.stat().st_mtime) / 3600
        size_mb = newest.stat().st_size / (1024 * 1024)
        s: Status = "green" if age_h < _AMBER_H else ("amber" if age_h < _RED_H else "red")
        return _item(pattern, s,
                     f"{newest.name} age={age_h:.1f}h size={size_mb:.1f}MB",
                     metric=f"{age_h:.1f}h")
    except Exception as e:
        return _item(pattern, "unknown", str(e))


def _section_backups() -> dict:
    items: list[dict] = []
    mvms_dir = HERMES_HOME / "backups" / "mvms"

    # MVMS canonical SQL
    items.append(_backup_age_status(mvms_dir, "mvms-canonical-*.sql.gz"))
    # Honcho live store
    items.append(_backup_age_status(mvms_dir, "honcho-live-store-*.sql.gz"))
    # Hermes app state
    items.append(_backup_age_status(mvms_dir, "hermes-app-state-*.tar.gz"))

    # VeraCrypt weekly backup
    try:
        vera_dir = HERMES_HOME / "audits" / "veracrypt-backup"
        if vera_dir.exists():
            # Find newest weekly marker dir
            weekly = sorted(
                [d for d in vera_dir.iterdir() if d.is_dir()],
                key=lambda d: d.stat().st_mtime,
                reverse=True
            )
            if weekly:
                newest = weekly[0]
                age_days = (time.time() - newest.stat().st_mtime) / 86400
                s: Status = "green" if age_days < _VERA_AMBER_DAYS else ("amber" if age_days < _VERA_RED_DAYS else "red")
                items.append(_item("veracrypt_weekly", s,
                                   f"{newest.name} age={age_days:.1f}d",
                                   metric=f"{age_days:.1f}d"))
            else:
                items.append(_item("veracrypt_weekly", "amber", "no weekly backup dirs found"))
        else:
            items.append(_item("veracrypt_weekly", "amber", "veracrypt-backup dir missing"))
    except Exception as e:
        items.append(_item("veracrypt_weekly", "unknown", str(e)))

    return _section("backups", "Backups", items)


# ---------------------------------------------------------------------------
# Section 6: memory_stores
# ---------------------------------------------------------------------------

def _mvms_count() -> Optional[int]:
    """60s sub-cached count of memory.observations in MVMS."""
    global _MVMS_COUNT_CACHE
    now = time.monotonic()
    with _MVMS_LOCK:
        if _MVMS_COUNT_CACHE is not None and now < _MVMS_COUNT_CACHE[1]:
            return _MVMS_COUNT_CACHE[0]
        try:
            r = subprocess.run(
                ["docker", "exec", "supabase_db_goattrade-system",
                 "psql", "-U", "postgres", "-t", "-A",
                 "-c", "SELECT count(*) FROM memory.observations"],
                capture_output=True, text=True, timeout=3
            )
            val = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
        except Exception:
            val = None
        _MVMS_COUNT_CACHE = (val, now + _DB_CACHE_TTL)
    return val


def _honcho_count() -> Optional[int]:
    """60s sub-cached count of messages in Honcho."""
    global _HONCHO_COUNT_CACHE
    now = time.monotonic()
    with _HONCHO_LOCK:
        if _HONCHO_COUNT_CACHE is not None and now < _HONCHO_COUNT_CACHE[1]:
            return _HONCHO_COUNT_CACHE[0]
        try:
            r = subprocess.run(
                ["docker", "exec", "honcho-database-1",
                 "psql", "-U", "postgres", "-t", "-A",
                 "-c", "SELECT count(*) FROM messages"],
                capture_output=True, text=True, timeout=3
            )
            val = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
        except Exception:
            val = None
        _HONCHO_COUNT_CACHE = (val, now + _DB_CACHE_TTL)
    return val


def _section_memory_stores() -> dict:
    items: list[dict] = []

    # state.db
    try:
        state_db = HERMES_HOME / "state.db"
        if state_db.exists():
            st = state_db.stat()
            size_kb = st.st_size / 1024
            age_h = (time.time() - st.st_mtime) / 3600
            items.append(_item("state_db", "green",
                               f"size={size_kb:.0f}KB mtime={age_h:.1f}h ago",
                               metric=f"{size_kb:.0f}KB"))
        else:
            items.append(_item("state_db", "amber", "state.db not found"))
    except Exception as e:
        items.append(_item("state_db", "unknown", str(e)))

    # kanban.db counts (using _probe_kanban pattern from dashboard_health)
    try:
        import glob
        kanban_db = HERMES_HOME / "kanban" / "boards" / "hermes" / "kanban.db"
        if kanban_db.exists():
            conn = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True, timeout=1.0)
            c = conn.cursor()
            c.execute("SELECT status, count(*) FROM tasks GROUP BY status")
            rows = c.fetchall()
            conn.close()
            by_status = {r[0]: r[1] for r in rows}
            open_count = sum(v for k, v in by_status.items()
                             if k not in ("done", "archived", "complete"))
            total = sum(by_status.values())
            detail = f"{open_count} open / {total} total"
            s: Status = "green"
            items.append(_item("kanban_db", s, detail, metric=str(open_count)))
        else:
            items.append(_item("kanban_db", "amber", "kanban.db not found at expected path"))
    except Exception as e:
        items.append(_item("kanban_db", "unknown", str(e)))

    # MEMORY.md size
    try:
        if MEMORY_MD.exists():
            size = MEMORY_MD.stat().st_size
            s = "green" if size < 22000 else "amber"
            items.append(_item("memory_md", s,
                               f"size={size} bytes",
                               metric=f"{size}B"))
        else:
            items.append(_item("memory_md", "amber", "MEMORY.md not found"))
    except Exception as e:
        items.append(_item("memory_md", "unknown", str(e)))

    # MVMS count (docker exec, 60s sub-cache, degrade to unknown)
    try:
        count = _mvms_count()
        if count is None:
            items.append(_item("mvms_observations", "unknown",
                               "docker exec psql failed or timed out"))
        else:
            items.append(_item("mvms_observations", "green",
                               f"{count:,} observations in memory.observations",
                               metric=str(count)))
    except Exception as e:
        items.append(_item("mvms_observations", "unknown", str(e)))

    # Honcho messages count (docker exec, 60s sub-cache)
    try:
        count = _honcho_count()
        if count is None:
            items.append(_item("honcho_messages", "unknown",
                               "docker exec psql failed or timed out"))
        else:
            items.append(_item("honcho_messages", "green",
                               f"{count:,} messages in honcho.messages",
                               metric=str(count)))
    except Exception as e:
        items.append(_item("honcho_messages", "unknown", str(e)))

    return _section("memory_stores", "Memory Stores", items)


# ---------------------------------------------------------------------------
# Section 7: cron
# ---------------------------------------------------------------------------

def _section_cron() -> dict:
    items: list[dict] = []
    try:
        # Reuse the same loader as /api/dashboard/cron
        from hermes_cli.dashboard_health import _get_all_cron_jobs
        jobs = _get_all_cron_jobs()
        enabled = [j for j in jobs if j.get("enabled")]
        not_ok = [j for j in enabled if j.get("lastStatus") and j.get("lastStatus") != "ok"]

        if not jobs:
            items.append(_item("cron_jobs", "amber", "no cron jobs found"))
        else:
            items.append(_item("cron_enabled", "green" if enabled else "amber",
                               f"{len(enabled)}/{len(jobs)} jobs enabled",
                               metric=str(len(enabled))))
            if not_ok:
                names = ", ".join(j.get("name") or j.get("id") or "?" for j in not_ok)
                items.append(_item("cron_not_ok", "amber",
                                   f"{len(not_ok)} job(s) with last_status != ok: {names}",
                                   metric=str(len(not_ok))))
            else:
                items.append(_item("cron_last_status", "green",
                                   "all recent runs ok" if enabled else "no recent runs"))
    except Exception as e:
        items.append(_item("cron_jobs", "unknown", str(e)))

    return _section("cron", "Cron", items)


# ---------------------------------------------------------------------------
# Section 8: host
# ---------------------------------------------------------------------------

def _section_host() -> dict:
    items: list[dict] = []

    # df /
    try:
        r = _run(["df", "/", "--output=avail,size", "--block-size=1G"])
        lines = r.stdout.strip().splitlines()
        if len(lines) >= 2 and r.returncode == 0:
            parts = lines[-1].split()
            avail_gb = int(parts[0])
            total_gb = int(parts[1])
            s: Status = "green" if avail_gb >= 25 else ("amber" if avail_gb >= 10 else "red")
            items.append(_item("disk_free", s,
                               f"{avail_gb}G free / {total_gb}G total",
                               metric=f"{avail_gb}G"))
        else:
            items.append(_item("disk_free", "unknown", "df parse failed"))
    except Exception as e:
        items.append(_item("disk_free", "unknown", str(e)))

    # free -m
    try:
        r = _run(["free", "-m"])
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("Mem:"):
                    parts = line.split()
                    # columns: total used free shared buff/cache available
                    avail_mb = int(parts[6]) if len(parts) >= 7 else int(parts[3])
                    s = "green" if avail_mb >= 2048 else ("amber" if avail_mb >= 512 else "red")
                    items.append(_item("mem_available", s,
                                       f"{avail_mb}MB available",
                                       metric=f"{avail_mb}MB"))
                    break
            else:
                items.append(_item("mem_available", "unknown", "Mem: line not found in free output"))
        else:
            items.append(_item("mem_available", "unknown", "free command failed"))
    except Exception as e:
        items.append(_item("mem_available", "unknown", str(e)))

    # Load average + WSL uptime
    try:
        r = _run(["uptime"])
        if r.returncode == 0:
            text = r.stdout.strip()
            # Extract load averages from "load average: X, Y, Z"
            m = re.search(r"load average[s]?:\s*([\d.]+),?\s*([\d.]+),?\s*([\d.]+)", text)
            if m:
                load1, load5, load15 = float(m.group(1)), float(m.group(2)), float(m.group(3))
                s = "green" if load1 < 4.0 else ("amber" if load1 < 8.0 else "red")
                items.append(_item("load_avg", s,
                                   f"1m={load1} 5m={load5} 15m={load15}",
                                   metric=str(load1)))
            else:
                items.append(_item("load_avg", "unknown", f"could not parse: {text}"))
        else:
            items.append(_item("load_avg", "unknown", "uptime failed"))
    except Exception as e:
        items.append(_item("load_avg", "unknown", str(e)))

    # WSL uptime from /proc/uptime
    try:
        uptime_s = float(Path("/proc/uptime").read_text().split()[0])
        h = int(uptime_s // 3600)
        m = int((uptime_s % 3600) // 60)
        items.append(_item("wsl_uptime", "green", f"up {h}h {m}m", metric=f"{h}h{m}m"))
    except Exception as e:
        items.append(_item("wsl_uptime", "unknown", str(e)))

    return _section("host", "Host", items)


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def _build_diagnostics(sections: list[dict]) -> list[dict]:
    """Flatten every non-green item into a diagnostics list, red→amber sorted."""
    diags: list[dict] = []
    severity_order = {"red": 0, "amber": 1}
    for sec in sections:
        for item in sec.get("items", []):
            sev = item.get("status")
            if sev in ("red", "amber"):
                diags.append({
                    "severity": sev,
                    "source": sec["id"],
                    "message": f"{item['name']}: {item['detail']}",
                })
            elif sev == "unknown":
                diags.append({
                    "severity": "amber",
                    "source": sec["id"],
                    "message": f"{item['name']}: {item['detail']} (unknown)",
                    "hint": "probe degraded — check logs",
                })
    diags.sort(key=lambda d: severity_order.get(d["severity"], 2))
    return diags


def _build_os_snapshot() -> dict:
    """Build full OSSnapshot. All section builders are try/except-guarded internally."""
    import concurrent.futures

    section_builders = [
        _section_gateway,
        _section_providers,
        _section_containers,
        _section_systemd,
        _section_backups,
        _section_memory_stores,
        _section_cron,
        _section_host,
    ]

    sections: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fn) for fn in section_builders]
        for fut, fn in zip(futures, section_builders):
            try:
                sections.append(fut.result(timeout=10))
            except Exception as e:
                sections.append(_section(fn.__name__.replace("_section_", ""),
                                         fn.__name__.replace("_section_", "").capitalize(),
                                         [_item("probe", "unknown", str(e))]))

    overall = _worst_status([s["status"] for s in sections])
    diagnostics = _build_diagnostics(sections)

    # Build Nexus graph (Appendix B) inside the same cached snapshot build
    try:
        graph = _build_os_graph(sections)
    except Exception as e:
        graph = {"nodes": [], "edges": [], "error": str(e)}

    return {
        "generated_at": _now(),
        "overall": overall,
        "sections": sections,
        "diagnostics": diagnostics,
        "graph": graph,
    }


def get_os_snapshot() -> dict:
    """20s-cached OSSnapshot. Thread-safe single-flight."""
    global _OS_CACHE
    now = time.monotonic()
    if _OS_CACHE and now < _OS_CACHE[1]:
        return _OS_CACHE[0]
    with _OS_LOCK:
        now = time.monotonic()
        if _OS_CACHE and now < _OS_CACHE[1]:
            return _OS_CACHE[0]
        data = _build_os_snapshot()
        _OS_CACHE = (data, now + _OS_TTL)
    return data


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

_OS_ENDPOINT_TIMEOUT = 15.0  # hard timeout for the full cold-build path


@router.get("/os", summary="OS infrastructure health snapshot (8 sections)")
async def get_os() -> dict:
    """Read-only OSSnapshot: 8 sections + diagnostics, 20 s TTL cache.

    Never 500 — every probe degrades to status=unknown on failure.
    """
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, get_os_snapshot),
            timeout=_OS_ENDPOINT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return {
            "generated_at": _now(),
            "overall": "unknown",
            "sections": [],
            "diagnostics": [{
                "severity": "red",
                "source": "endpoint",
                "message": "snapshot build timed out",
                "hint": "retry in a moment; individual probes may be stuck",
            }],
        }
