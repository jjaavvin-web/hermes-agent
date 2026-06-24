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

Status = str  # "green" | "amber" | "red" | "info" | "unknown"


def _item(
    name: str,
    status: Status,
    detail: str,
    metric: Optional[str] = None,
    reason: Optional[str] = None,
    **extra: Any,
) -> dict:
    d: dict = {"name": name, "status": status, "detail": detail}
    if metric is not None:
        d["metric"] = metric
    if reason is not None:
        d["reason"] = reason
    elif status != "green":
        # Safety net: every non-green status carries a why for the Diagnostics drawer.
        d["reason"] = detail
    for key, value in extra.items():
        if value is not None:
            d[key] = value
    return d


def _section(id_: str, label: str, items: list[dict]) -> dict:
    # Info is visible context, never posture. Unknown still degrades as amber via diagnostics.
    posture_statuses = [i.get("status", "unknown") for i in items if i.get("status") != "info"]
    worst = _worst_status(posture_statuses) if posture_statuses else "green"
    return {"id": id_, "label": label, "status": worst, "items": items}


def _worst_status(statuses: list[Status]) -> Status:
    order = {"red": 0, "amber": 1, "info": 2, "unknown": 3, "green": 4}
    if not statuses:
        return "unknown"
    return min(statuses, key=lambda s: order.get(str(s or "unknown"), 3))


def _run(cmd: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _state_db_path() -> Path:
    """Resolve the canonical Hermes state.db path, profile-aware when possible."""
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "state.db"
    except Exception:
        return HERMES_HOME / "state.db"


def _age_hours(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 3600


def _age_status(age_h: float, *, green_h: float, amber_h: float) -> Status:
    if age_h <= green_h:
        return "green"
    if age_h <= amber_h:
        return "amber"
    return "red"


def _latest_file(root: Path) -> Optional[Path]:
    if not root.exists():
        return None
    newest: Optional[Path] = None
    newest_mtime = -1.0
    try:
        candidates = root.rglob("*") if root.is_dir() else [root]
        for path in candidates:
            try:
                if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest = path
                newest_mtime = mtime
    except Exception:
        return None
    return newest


def _cost_summary() -> dict:
    """Read-only today-cost probe from turn_usage in state.db."""
    db_path = _state_db_path()
    payload = {
        "status": "unknown",
        "label": "n/a",
        "detail": "turn_usage unavailable",
        "today_usd": None,
    }
    try:
        if not db_path.exists():
            payload["detail"] = "state.db missing"
            return payload
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_usage'"
            ).fetchone()
            if exists is None:
                payload["detail"] = "turn_usage table missing"
                return payload
            day_ago = time.time() - 86_400.0
            row = conn.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS cost_usd, COUNT(*) AS turns
                FROM turn_usage
                WHERE ts >= ?
                """,
                (day_ago,),
            ).fetchone()
        finally:
            conn.close()
        cost = round(float(row[0] or 0.0), 4) if row else 0.0
        turns = int(row[1] or 0) if row else 0
        if cost <= 0.50:
            status: Status = "green"
        elif cost <= 5.0:
            status = "amber"
        else:
            status = "red"
        return {
            "status": status,
            "label": f"${cost:.2f}",
            "detail": f"today cost=${cost:.4f} across {turns} turn(s)",
            "today_usd": cost,
            "turns": turns,
            "source": str(db_path),
        }
    except Exception as exc:
        payload["detail"] = f"cost probe failed: {exc}"
        return payload


def _parse_ts_age_hours(value: Any) -> Optional[float]:
    """Parse an ISO timestamp into age hours; None on absent/bad values."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except Exception:
        return None


def _score_status(score: float) -> Status:
    if score >= 0.90:
        return "green"
    if score >= 0.70:
        return "amber"
    return "red"


def _dr_status() -> dict:
    """DR readiness from dr-status.py JSON, preserving every store row.

    Untested/undrilled stores are AMBER coverage gaps, not GREEN. The legacy
    text probe returned GREEN when the script exited 0 even if half the stores
    were N/A, and it truncated the table to the header line.
    """
    script = HERMES_HOME / "scripts" / "dr-status.py"
    python = HOME / ".local" / "share" / "hermes-agent" / "venv" / "bin" / "python"
    if script.exists() and python.exists():
        try:
            r = _run([str(python), str(script), "--json"], timeout=10.0)
            output = (r.stdout or "").strip()
            data = json.loads(output) if output else {}
            rows = data.get("rows") if isinstance(data, dict) else []
            failures = data.get("failures") if isinstance(data, dict) else []
            if not isinstance(rows, list) or not rows:
                return {"status": "unknown", "label": "n/a", "detail": "dr-status.py JSON had no rows", "source": str(script), "returncode": r.returncode}

            normalized_rows: list[dict[str, Any]] = []
            red_count = 0
            undrilled_count = 0
            green_count = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                verdict = str(row.get("verdict") or "UNKNOWN").upper()
                if verdict == "GREEN":
                    row_status: Status = "green"
                    green_count += 1
                elif verdict in {"N/A", "NA", "UNDRILLED", "UNTESTED"}:
                    row_status = "amber"
                    undrilled_count += 1
                elif verdict in {"RED", "FAIL", "FAILED"}:
                    row_status = "red"
                    red_count += 1
                else:
                    row_status = "unknown"
                    undrilled_count += 1
                enriched = dict(row)
                enriched["status"] = row_status
                normalized_rows.append(enriched)

            if red_count or r.returncode != 0:
                status: Status = "red"
            elif undrilled_count:
                status = "amber"
            else:
                status = "green"
            label = f"{green_count}/{len(normalized_rows)} drilled"
            if undrilled_count:
                label += f" · {undrilled_count} untested"
            if red_count:
                label += f" · {red_count} red"
            detail = "; ".join(f"{row.get('store', '?')}={row.get('verdict', '?')}" for row in normalized_rows)
            return {
                "status": status,
                "label": label,
                "detail": detail,
                "source": str(script),
                "returncode": r.returncode,
                "rows": normalized_rows,
                "failures": failures if isinstance(failures, list) else [],
            }
        except Exception as exc:
            return {"status": "unknown", "label": "n/a", "detail": f"dr-status.py JSON probe failed: {exc}", "source": str(script)}

    newest = _latest_file(HERMES_HOME / "backups")
    if newest is None:
        return {"status": "unknown", "label": "n/a", "detail": "no DR script or backup artifact found", "source": str(HERMES_HOME / "backups")}
    age_h = _age_hours(newest)
    status = _age_status(age_h, green_h=24.0, amber_h=168.0)
    return {
        "status": status,
        "label": f"{age_h:.1f}h",
        "detail": f"fallback newest backup {newest.name} age={age_h:.1f}h (per-store DR unmeasured)",
        "age_hours": round(age_h, 2),
        "source": str(newest),
        "rows": [],
    }


def _evals_status() -> dict:
    """Worst fresh recall holdout set from score-history.jsonl.

    The old probe only read the latest row, hiding a fresh weaker blind
    holdout whenever the easier default holdout ran later.
    """
    path = HERMES_HOME / "evals" / "recall" / "score-history.jsonl"
    fresh_hours = 72.0
    try:
        if not path.exists():
            return {"status": "unknown", "label": "n/a", "detail": "recall score-history.jsonl missing", "source": str(path)}
        latest_by_set: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            agg = row.get("agg") if isinstance(row.get("agg"), dict) else {}
            recall = agg.get("recall_at_k")
            if recall is None:
                continue
            holdout = str(row.get("holdout_file") or "holdout:default")
            key = Path(holdout).name if holdout != "holdout:default" else holdout
            age_h = _parse_ts_age_hours(row.get("ts"))
            enriched = dict(row)
            enriched["holdout_key"] = key
            enriched["age_hours"] = age_h
            prev_age = latest_by_set.get(key, {}).get("age_hours")
            if key not in latest_by_set or (age_h is not None and (prev_age is None or age_h < prev_age)):
                latest_by_set[key] = enriched
        if not latest_by_set:
            return {"status": "unknown", "label": "n/a", "detail": "recall score history has no valid scored rows", "source": str(path)}

        fresh_rows = [
            row
            for row in latest_by_set.values()
            if row.get("age_hours") is None or float(row.get("age_hours") or 0.0) <= fresh_hours
        ]
        rows_for_gate = fresh_rows or list(latest_by_set.values())
        per_set = []
        for row in latest_by_set.values():
            raw_agg = row.get("agg")
            agg = raw_agg if isinstance(raw_agg, dict) else {}
            score_raw = agg.get("recall_at_k")
            if score_raw is None:
                continue
            score = float(score_raw)
            per_set.append({
                "holdout": row.get("holdout_key"),
                "status": _score_status(score),
                "recall_at_k": score,
                "label": f"{score * 100:.0f}%",
                "k": int(agg.get("k") or 10),
                "n": int(agg.get("n") or 0),
                "ts": row.get("ts"),
                "age_hours": row.get("age_hours"),
                "fresh": row in fresh_rows,
            })
        per_set.sort(key=lambda r: (0 if r["fresh"] else 1, float(r["recall_at_k"])))

        def _row_score(row: dict[str, Any]) -> float:
            raw_agg = row.get("agg")
            agg = raw_agg if isinstance(raw_agg, dict) else {}
            score_raw = agg.get("recall_at_k")
            return float(score_raw) if score_raw is not None else -1.0

        worst = min(rows_for_gate, key=_row_score)
        raw_agg = worst.get("agg")
        agg = raw_agg if isinstance(raw_agg, dict) else {}
        recall_f = _row_score(worst)
        status = _score_status(recall_f)
        k = int(agg.get("k") or 10)
        n = int(agg.get("n") or 0)
        holdout = str(worst.get("holdout_key") or "holdout")
        fresh_suffix = "fresh" if fresh_rows else "stale"
        return {
            "status": status,
            "label": f"worst {recall_f * 100:.0f}%",
            "detail": f"worst {fresh_suffix} holdout {holdout}: RECALL@{k}={recall_f:.4f} over n={n}; {len(latest_by_set)} set(s) tracked",
            "recall_at_k": recall_f,
            "worst_holdout": holdout,
            "k": k,
            "n": n,
            "ts": worst.get("ts"),
            "source": str(path),
            "sets": per_set,
        }
    except Exception as exc:
        return {"status": "unknown", "label": "n/a", "detail": f"evals probe failed: {exc}", "source": str(path)}


def _redteam_script_path() -> Path:
    candidates = [
        HERMES_HOME / "scripts" / "run_redteam.py",
        HERMES_HOME / "measure" / "redteam" / "run_redteam.py",
    ]
    return next((p for p in candidates if p.exists()), candidates[0])


def _security_status() -> dict:
    """Execute the red-team JSON suite and gate on breach_count, not artifact age."""
    script = _redteam_script_path()
    python = HOME / ".local" / "share" / "hermes-agent" / "venv" / "bin" / "python"
    if not python.exists():
        python = Path(shutil.which("python3") or "python3")
    if not script.exists():
        return {"status": "unknown", "label": "n/a", "detail": "run_redteam.py missing", "source": str(script)}
    try:
        r = _run([str(python), str(script), "--json"], timeout=30.0)
        output = (r.stdout or "").strip()
        data = json.loads(output) if output else {}
        breach_count = int(data.get("breach_count") or 0) if isinstance(data, dict) else 0
        passed = int(data.get("passed") or 0) if isinstance(data, dict) else 0
        total = int(data.get("total") or 0) if isinstance(data, dict) else 0
        if r.returncode != 0 or breach_count > 0:
            status: Status = "red"
        elif total and passed == total:
            status = "green"
        else:
            status = "amber"
        age_h = _age_hours(script)
        failing: list[str] = []
        tiers = data.get("tiers") if isinstance(data, dict) else {}
        if isinstance(tiers, dict):
            for tier_name, tier in tiers.items():
                for row in (tier or {}).get("rows", []) if isinstance(tier, dict) else []:
                    if isinstance(row, dict) and str(row.get("verdict", "")).upper() != "PASS":
                        failing.append(str(row.get("id") or tier_name))
        return {
            "status": status,
            "label": f"{breach_count} breach{'es' if breach_count != 1 else ''}",
            "detail": f"run_redteam.py --json passed={passed}/{total} breach_count={breach_count}; script age={age_h:.1f}h",
            "breach_count": breach_count,
            "passed": passed,
            "total": total,
            "failing": failing[:10],
            "age_hours": round(age_h, 2),
            "source": str(script),
            "returncode": r.returncode,
        }
    except Exception as exc:
        return {"status": "unknown", "label": "n/a", "detail": f"red-team probe failed: {exc}", "source": str(script)}


def _infra_snapshot() -> dict:
    return {
        "cost": _cost_summary(),
        "dr": _dr_status(),
        "evals": _evals_status(),
        "security": _security_status(),
    }


# ---------------------------------------------------------------------------
# Graph helpers (Appendix B)
# ---------------------------------------------------------------------------

def _bind_item(sections: list[dict], section_id: str, item_name: str) -> Optional[dict]:
    for sec in sections:
        if sec.get("id") == section_id:
            for item in sec.get("items", []):
                if item.get("name") == item_name:
                    return item
            return None
    return None


def _bind_meta(sections: list[dict], section_id: str, item_name: str) -> tuple[Status, Optional[str], Optional[str]]:
    """Look up (status, detail, reason) for a named item within a section.

    Falls back to ("unknown", None, None) when the section or item is absent.
    """
    item = _bind_item(sections, section_id, item_name)
    if item is None:
        return "unknown", None, None
    return item.get("status", "unknown"), item.get("detail"), item.get("reason")


def _bind(sections: list[dict], section_id: str, item_name: str) -> tuple[Status, Optional[str]]:
    s, d, _ = _bind_meta(sections, section_id, item_name)
    return s, d


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


def _systemd_timer_status(
    timer_name: str,
    *,
    expected_enabled: bool = True,
    disabled_reason: Optional[str] = None,
) -> tuple[Status, str, str]:
    """Read-only timer probe for graph-only infra nodes.

    Uses is-active/is-enabled only.  Disabled-by-design timers are informational
    context, not amber, unless a caller explicitly expects the timer enabled.
    """
    try:
        active_r = _run(["systemctl", "--user", "is-active", timer_name], timeout=3.0)
        active = (active_r.stdout or active_r.stderr or "unknown").strip() or "unknown"
    except Exception as exc:
        active = f"unknown ({exc})"
    try:
        enabled_r = _run(["systemctl", "--user", "is-enabled", timer_name], timeout=3.0)
        enabled = (enabled_r.stdout or enabled_r.stderr or "unknown").strip() or "unknown"
    except Exception as exc:
        enabled = f"unknown ({exc})"

    detail = f"{timer_name}: active={active} enabled={enabled}"
    if disabled_reason is not None or not expected_enabled:
        return "info", detail, disabled_reason or "disabled by design; informational only"

    if active == "active" and enabled == "enabled":
        return "green", detail, f"{timer_name} is active and enabled"
    if active.startswith("unknown") and enabled.startswith("unknown"):
        return "unknown", detail, f"{timer_name} systemctl probe failed"
    return "amber", detail, f"{timer_name} is expected active/enabled but reported active={active} enabled={enabled}"


def _state_files_status(
    label: str,
    paths: list[Path],
    *,
    stale_hours: float = 168.0,
) -> tuple[Status, str, str]:
    """Read-only existence/freshness probe for dashboard state-file nodes."""
    try:
        missing = [p for p in paths if not p.exists()]
        if missing:
            names = ", ".join(str(p) for p in missing)
            return "amber", f"missing state file(s): {names}", f"{label} state file missing"
        newest = max(paths, key=lambda p: p.stat().st_mtime)
        age_h = (time.time() - newest.stat().st_mtime) / 3600
        detail = f"{label}: newest={newest.name} age={age_h:.1f}h"
        if age_h <= stale_hours:
            return "green", detail, f"{label} state file exists and is within {stale_hours:.0f}h freshness window"
        return "amber", detail, f"{label} state file is older than {stale_hours:.0f}h"
    except Exception as exc:
        return "unknown", f"{label}: state-file probe failed: {exc}", f"{label} state-file probe raised"


def _required_paths_status(label: str, paths: list[Path]) -> tuple[Status, str, str]:
    """Read-only path-existence probe for graph-only infra nodes."""
    try:
        missing = [p for p in paths if not p.exists()]
        if missing:
            names = ", ".join(str(p) for p in missing)
            return "amber", f"missing required path(s): {names}", f"{label} required path missing"
        names = ", ".join(p.name for p in paths)
        return "green", f"required path(s) present: {names}", f"{label} required files exist"
    except Exception as exc:
        return "unknown", f"{label}: path probe failed: {exc}", f"{label} path probe raised"


def _x_search_config_status() -> tuple[Status, str, str]:
    """Read-only x_search configuration probe; never prints secret values."""
    try:
        cfg = HERMES_HOME / "config.yaml"
        if not cfg.exists():
            return "amber", "config.yaml missing", "x_search config cannot be verified without config.yaml"
        text = cfg.read_text(encoding="utf-8", errors="replace")
        has_section = bool(re.search(r"(?m)^x_search:\s*$", text))
        has_model = bool(re.search(r"(?m)^x_search:\s*$[\s\S]*?^\S", text) and re.search(r"(?m)^\s*model:\s*\S+", text))
        toolset_enabled = bool(re.search(r"(?m)^\s*-\s*x_search\s*$", text))
        if has_section and toolset_enabled:
            detail = "x_search configured and enabled in toolsets"
            if has_model:
                detail += " (model set)"
            return "green", detail, "x_search config section and toolset entry are present"
        bits = []
        if not has_section:
            bits.append("missing x_search section")
        if not toolset_enabled:
            bits.append("x_search toolset not enabled")
        return "amber", ", ".join(bits) or "x_search partially configured", "x_search is not fully configured/enabled"
    except Exception as exc:
        return "unknown", f"x_search config probe failed: {exc}", "x_search config probe raised"


def _build_os_graph(sections: list[dict]) -> dict:
    """Build the Nexus graph (35 nodes, ~35 edges) from Appendix B + full-coverage redesign.

    Node status is BOUND from already-computed sections when available; graph-only
    infra elements use read-only probes (systemctl is-active/is-enabled and file
    existence/freshness). Edges are static topology with dynamic timer-gated states.
    """
    # ------------------------------------------------------------------
    # Helper: worst status across a list of (section_id, item_name) bindings
    # ------------------------------------------------------------------
    def _bind_worst(bindings: list[tuple[str, str]]) -> tuple[Status, Optional[str], Optional[str]]:
        results = [_bind_meta(sections, sid, iname) for sid, iname in bindings]
        worst_s = _worst_status([s for s, _, _ in results])
        # Use the detail/reason from the worst-status binding (first match wins).
        detail = next((d for s, d, _ in results if s == worst_s), None)
        reason = next((r for s, _, r in results if s == worst_s), None)
        return worst_s, detail, reason

    def _node(
        id_: str,
        label: str,
        kind: str,
        group: str,
        status: Status,
        detail: Optional[str] = None,
        section_ref: Optional[str] = None,
        reason: Optional[str] = None,
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
        if reason is not None:
            n["reason"] = reason
        elif status not in {"green", "info"} and detail is not None:
            n["reason"] = detail
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
    # NODES (35)
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
    s, d = _bind(sections, "providers", "codex_live_sessions")
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
    gw_reason = next(
        (item.get("reason") for item in
         next((sec["items"] for sec in sections if sec["id"] == "gateway"), [])
         if item.get("status") == gw_status),
        None,
    )
    nodes.append(_node("gateway", "Gateway", "service", "control", gw_status, gw_detail, "gateway", gw_reason))

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
    cron_reason = next(
        (item.get("reason") for item in
         next((sec["items"] for sec in sections if sec["id"] == "cron"), [])
         if item.get("status") == cron_status),
        None,
    )
    nodes.append(_node("hermes-cron", "Hermes Cron", "scheduler", "control", cron_status, cron_detail, "cron", cron_reason))

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
    sd_reason = next(
        (item.get("reason") for item in
         next((sec["items"] for sec in sections if sec["id"] == "systemd"), [])
         if item.get("status") == sd_status),
        None,
    )
    nodes.append(_node("timers", "Timers", "scheduler", "control", sd_status, sd_detail, "systemd", sd_reason))

    # mvms-watcher / honcho-watcher: explicit control-plane watcher timers.
    s, d, r = _systemd_timer_status("mvms-watcher.timer")
    nodes.append(_node("mvms-watcher", "MVMS Watcher", "guard", "control", s, d, "systemd", r))

    s, d, r = _systemd_timer_status("honcho-watcher.timer")
    nodes.append(_node("honcho-watcher", "Honcho Watcher", "guard", "control", s, d, "systemd", r))

    # --- providers (3) ---
    # chatgpt-backend: bind providers/codex_pipeline_load
    s, d = _bind(sections, "providers", "codex_pipeline_load")
    nodes.append(_node("chatgpt-backend", "ChatGPT Backend", "llm", "providers", s, d, "providers"))

    # claude-max: bind to the real claude CLI probe instead of a fake-green node.
    s, d = _bind(sections, "providers", "claude_cli")
    nodes.append(_node("claude-max", "Claude Max", "llm", "providers", s, d, "providers"))

    # openrouter: bind providers/openrouter_key
    s, d = _bind(sections, "providers", "openrouter_key")
    nodes.append(_node("openrouter", "OpenRouter", "llm", "providers", s, d, "providers"))

    # --- ingest (3) ---
    s, d, r = _required_paths_status(
        "ICT Brain",
        [HERMES_HOME / "scripts" / "ict-brain", HERMES_HOME / "scripts" / "ict-brain" / "tools" / "ictq.py"],
    )
    nodes.append(_node("ict-brain", "ICT Brain", "pipeline", "ingest", s, d, "memory_stores", r))

    s, d, r = _required_paths_status(
        "Opus Extractor",
        [HERMES_HOME / "scripts" / "ict-brain" / "tools" / "opus_extractor.py"],
    )
    nodes.append(_node("opus_extractor", "Opus Extractor", "worker", "ingest", s, d, "providers", r))

    s, d, r = _x_search_config_status()
    nodes.append(_node("x_search", "x_search", "provider", "ingest", s, d, "providers", r))

    # --- memory (10) ---
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

    # hermes-memories: bind to the real profile memory file probe.
    s, d = _bind(sections, "memory_stores", "memory_md")
    nodes.append(_node("hermes-memories", "Hermes Memories", "store", "memory", s, d, "memory_stores"))

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
    nb_reason = next((_bind_meta(sections, "backups", name)[2] for name in ("mvms-canonical-*.sql.gz", "honcho-live-store-*.sql.gz", "hermes-app-state-*.tar.gz") if _bind_meta(sections, "backups", name)[0] == nb_status), None)
    nodes.append(_node("nightly-backup", "Nightly Backup", "backup", "protection",
                       nb_status, nb_detail, "backups", nb_reason))

    # backups-dir: bind to actual backup freshness; not a decorative always-green node.
    s, d = _bind(sections, "backups", "mvms-canonical-*.sql.gz")
    nodes.append(_node("backups-dir", "Backups Dir", "storage", "protection", s, d, "backups"))

    # veracrypt: bind backups/veracrypt_weekly
    s, d = _bind(sections, "backups", "veracrypt_weekly")
    nodes.append(_node("veracrypt", "VeraCrypt", "backup", "protection", s, d, "backups"))

    # mvms-compactor: intentionally disabled; visible as info, never amber.
    s, d, r = _systemd_timer_status(
        "mvms-compactor.timer",
        expected_enabled=False,
        disabled_reason="disabled-by-design; compaction is manually/gate controlled",
    )
    nodes.append(_node("mvms-compactor", "MVMS Compactor", "worker", "protection", "info", d, "systemd", r))

    # off-box-backup-gap: bind the real backup risk surfaced by the backups section.
    offbox_status, offbox_detail, offbox_reason = _bind_meta(sections, "backups", "mvms-backup-gap-offbox")
    if offbox_status == "unknown":
        offbox_status, offbox_detail, offbox_reason = "amber", "no off-box replication marker found", "local backups exist, but off-box replication has no success marker"
    nodes.append(_node("off-box-backup-gap", "Off-box Backup Gap", "backup", "protection", offbox_status, offbox_detail, "backups", offbox_reason))

    # --- learning (3) ---
    s, d, r = _systemd_timer_status("learning-verify.timer")
    nodes.append(_node("learning-verify", "Learning Verify", "guard", "learning", s, d, "systemd", r))

    s, d, r = _state_files_status(
        "Distiller",
        [HERMES_HOME / "state" / "distiller-queue.jsonl", HERMES_HOME / "state" / "distiller-inbox-latest.md"],
        stale_hours=168.0,
    )
    nodes.append(_node("distiller", "Distiller", "worker", "learning", s, d, "memory_stores", r))

    s, d, r = _state_files_status(
        "Reflect Gate",
        [HERMES_HOME / "state" / "learning-loop" / "critic-latest.md"],
        stale_hours=168.0,
    )
    nodes.append(_node("reflect-gate", "Reflect Gate", "guard", "learning", s, d, "memory_stores", r))

    # --- host (1) ---
    # wsl-host: bind host section worst
    host_items = next((sec["items"] for sec in sections if sec["id"] == "host"), [])
    host_statuses = [item.get("status", "unknown") for item in host_items]
    host_status = _worst_status(host_statuses) if host_statuses else "unknown"
    host_detail = next(
        (item.get("detail") for item in host_items if item.get("status") == host_status),
        None,
    )
    host_reason = next(
        (item.get("reason") for item in host_items if item.get("status") == host_status),
        None,
    )
    nodes.append(_node("wsl-host", "WSL Host", "host", "host", host_status, host_detail, "host", host_reason))

    # ------------------------------------------------------------------
    # EDGES (~35)  state: live | disabled | broken | gated
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

    # providers/tools → ingest
    edges.append(_edge("e-claudemax-opus",     "claude-max",    "opus_extractor", "Opus OAuth",            "gated"))
    edges.append(_edge("e-xsearch-ict",        "x_search",      "ict-brain",      "source recall",         "live"))
    edges.append(_edge("e-opus-ict",           "opus_extractor","ict-brain",      "concept extraction",    "gated"))

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

    # ingest → memory
    edges.append(_edge("e-ict-mvms",           "ict-brain",     "mvms",           "ICT assertions",        "live"))
    edges.append(_edge("e-xsearch-mvms",       "x_search",      "mvms",           "scrape evidence",       "live"))

    # watchers → targets
    edges.append(_edge("e-mvmswatcher-mvms",   "mvms-watcher",  "mvms",           "health watch",          "live"))
    edges.append(_edge("e-honchowatcher-hapi", "honcho-watcher","honcho-api",     "health watch",          "live"))
    edges.append(_edge("e-honchowatcher-hdb",  "honcho-watcher","honcho-db",      "store watch",           "live"))

    # kanban-db → mvms (dynamic: live if timer enabled, else disabled)
    bridge_state = _bridge_edge_state()
    edges.append(_edge("e-kanban-mvms",        "kanban-db",     "mvms",           "bridge (staged)",       bridge_state))

    # claude-code → mvms (gated)
    edges.append(_edge("e-claudecode-mvms-gated","claude-code", "mvms",           "lesson promote (weekly, human-gated)", "gated"))

    # learning loop → MVMS
    edges.append(_edge("e-learnverify-mvms",   "learning-verify","mvms",          "recall canary",         "live"))
    edges.append(_edge("e-distiller-mvms",     "distiller",     "mvms",           "promotion queue",       "gated"))
    edges.append(_edge("e-reflectgate-mvms",   "reflect-gate",  "mvms",           "quality critic",        "gated"))

    # MVMS compactor is visible but intentionally gated/disabled.
    edges.append(_edge("e-compactor-mvms",     "mvms-compactor","mvms",           "manual compact",        "gated"))

    # nightly-backup → stores
    edges.append(_edge("e-backup-mvms",        "nightly-backup","mvms",           "02:30 dump",            "live"))
    edges.append(_edge("e-backup-hdb",         "nightly-backup","honcho-db",      "02:30 dump",            "live"))
    edges.append(_edge("e-backup-cmem",        "nightly-backup","claude-memory",  "app-state tar",         "live"))

    # off-box replication risk is separate from local backup freshness.
    edges.append(_edge("e-backup-offboxgap",   "nightly-backup","off-box-backup-gap", "off-box replication", "broken" if offbox_status == "red" else "gated"))

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
            proc_uptime_s = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
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
    return _item("gateway_state", status, detail, reason="gateway_state.json is not running/alive" if status != "green" else None)


def _probe_gateway_systemctl() -> dict:
    r = _run(["systemctl", "--user", "is-active", "hermes-gateway"])
    active = r.stdout.strip()
    status: Status = "green" if active == "active" else ("amber" if active == "activating" else "red")
    return _item("systemd_unit", status, f"hermes-gateway.service: {active}", reason="hermes-gateway systemd unit is not active" if status != "green" else None)


def _probe_gateway_watchdog() -> dict:
    # Silence file → red if present
    silence = HERMES_HOME / "state" / "gateway-watchdog" / "silence"
    if silence.exists():
        return _item("watchdog_silence", "red", "watchdog silenced — touch ~/.hermes/state/gateway-watchdog/silence present", reason="watchdog silence file exists")

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
                    return _item("watchdog_events", s, f"last event: {ev_status} at {ev.get('ts','?')}", reason="last watchdog event was not ok" if s != "green" else None)
        except Exception as e:
            return _item("watchdog_events", "unknown", f"parse error: {e}", reason="watchdog events JSONL parse failed")
    return _item("watchdog_events", "unknown", "no events.jsonl", reason="watchdog events file missing")


def _section_gateway() -> dict:
    items: list[dict] = []
    for probe in (_probe_gateway, _probe_gateway_systemctl, _probe_gateway_watchdog):
        try:
            items.append(probe())
        except Exception as e:
            items.append(_item(probe.__name__, "unknown", str(e), reason="section probe raised"))
    return _section("gateway", "Gateway", items)


# ---------------------------------------------------------------------------
# Section 2: providers
# ---------------------------------------------------------------------------

def _probe_codex_pipeline_load() -> dict:
    """Hermes uses the ChatGPT HTTP backend; no local codex process required.
    Snapshot-loaded is info context, not posture.
    """
    try:
        from hermes_cli.dashboard_codex_sessions import _cached_snapshot
        snap = _cached_snapshot()
        counts = snap.get("counts", {}) if isinstance(snap, dict) else {}
        total = int(counts.get("total") or 0)
        return _item(
            "codex_pipeline_load",
            "info",
            f"backend lane (HTTP); snapshot loaded; {total} sessions tracked",
            metric=str(total),
            reason="snapshot availability is routine context, not a health failure",
        )
    except Exception as e:
        return _item(
            "codex_pipeline_load",
            "amber",
            f"pipeline snapshot unavailable: {e}",
            reason="codex pipeline snapshot loader raised",
        )


def _parse_iso_age_days(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except Exception:
        return None


def _probe_codex_sessions() -> dict:
    """Import codex-sessions snapshot loader and classify states semantically."""
    try:
        from hermes_cli.dashboard_codex_sessions import _cached_snapshot
        snap = _cached_snapshot()
        counts = snap.get("counts", {}) if isinstance(snap, dict) else {}
        sessions = snap.get("sessions", []) if isinstance(snap, dict) else []
        total = int(counts.get("total") or 0)
        by_state = counts.get("by_state", {}) if isinstance(counts, dict) else {}

        escalated = blocked = orphaned = missing_worktree = 0
        stale_completed = stale_unknown_amber = stale_unknown_red = 0
        stale_reasons: list[str] = []
        for row in sessions if isinstance(sessions, list) else []:
            if not isinstance(row, dict):
                continue
            state_raw = str(row.get("state") or row.get("hive_state") or "UNKNOWN")
            state = state_raw.lower()
            if state_raw == "ESCALATED" or state == "escalated":
                escalated += 1
            if state in {"blocked", "block", "blocked_success"}:
                blocked += 1
            if state in {"orphaned", "orphan"}:
                orphaned += 1
            if row.get("worktree_path") and not row.get("worktree_alive", True):
                missing_worktree += 1

            hive = row.get("hive") if isinstance(row.get("hive"), dict) else {}
            hive_state = str(hive.get("state") or row.get("hive_state") or state_raw).lower()
            if hive_state.startswith("stale") or state.startswith("stale"):
                final_report = bool(row.get("final_report") or row.get("report_written") or hive.get("final_report"))
                wt = str(row.get("worktree_path") or "")
                if wt and (Path(wt) / "FINAL-REPORT.md").exists():
                    final_report = True
                age_days = _parse_iso_age_days(row.get("last_message_at") or row.get("created_at"))
                if final_report or "completed" in hive_state or "complete" in state:
                    stale_completed += 1
                elif age_days is not None and age_days > 30:
                    stale_unknown_red += 1
                    stale_reasons.append(f"stale_unknown {age_days:.0f} days, no report")
                else:
                    stale_unknown_amber += 1
                    stale_reasons.append(f"stale_unknown {age_days:.0f} days, no report" if age_days is not None else "stale_unknown age unavailable, no report")

        terminal_red = orphaned + missing_worktree + stale_unknown_red
        terminal_amber = stale_unknown_amber
        s: Status = "red" if terminal_red else ("amber" if terminal_amber else "green")
        parts = [f"{k}={v}" for k, v in by_state.items()] if by_state else ["none"]
        detail = f"{total} sessions: {', '.join(parts)}; blocked_count={blocked}"
        reason_bits = []
        if escalated:
            reason_bits.append(f"ESCALATED={escalated} handled queue")
        if blocked:
            reason_bits.append(f"blocked={blocked} terminal success")
        if orphaned:
            reason_bits.append(f"ORPHANED={orphaned}")
        if missing_worktree:
            reason_bits.append(f"missing_worktree={missing_worktree}")
        if stale_completed:
            reason_bits.append(f"stale_completed={stale_completed}")
        reason_bits.extend(stale_reasons[:3])
        reason = ", ".join(reason_bits) if reason_bits else "all tracked codex sessions are semantically non-actionable"
        return _item("codex_live_sessions", s, detail, metric=str(total), reason=reason, blocked_count=blocked)
    except Exception as e:
        return _item("codex_live_sessions", "unknown", f"codex-sessions import failed: {e}", reason="codex sessions snapshot import raised")


def _probe_claude_cli() -> dict:
    """claude binary exists + last cli-subprocess marker."""
    try:
        binary = shutil.which("claude")
        if not binary:
            return _item("claude_cli", "red", "claude binary not found in PATH", reason="required claude CLI binary missing")
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
        return _item("claude_cli", "unknown", str(e), reason="claude CLI probe raised")


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
        return _item("openrouter_key", "amber", "honcho deriver key missing", reason="LLM_OPENROUTER_API_KEY not present in honcho env")
    except Exception as e:
        return _item("openrouter_key", "unknown", str(e), reason="openrouter key probe raised")


def _section_providers() -> dict:
    items: list[dict] = []
    for probe in (
        _probe_codex_pipeline_load,
        _probe_codex_sessions,
        _probe_claude_cli,
        _probe_openrouter_key,
    ):
        try:
            items.append(probe())
        except Exception as e:
            items.append(_item(probe.__name__, "unknown", str(e), reason="section probe raised"))
    return _section("providers", "Providers", items)


# ---------------------------------------------------------------------------
# Section 3: containers
# ---------------------------------------------------------------------------

# Core containers that must be running; anything else is "extra"
_CORE_CONTAINERS = frozenset({
    "honcho-database-1",
    "honcho-api-1",
    "honcho-deriver-1",
    "honcho-redis-1",
    "supabase_db_goattrade-system",
    "supabase_kong_goattrade-system",
})


def _section_containers() -> dict:
    items: list[dict] = []
    try:
        r = _run(["docker", "ps", "-a", "--format", "{{json .}}"])
        if r.returncode != 0:
            return _section("containers", "Containers",
                            [_item("docker", "unknown", f"docker ps failed: {r.stderr.strip()}", reason="docker ps command failed")])
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
                items.append(_item(name, "red", "container not found", reason="required core container missing"))
                continue
            state = c.get("State", "unknown").lower()
            status_str = c.get("Status", "")
            if state == "running":
                s: Status = "green"
            elif state in ("created", "restarting"):
                s = "amber"
            else:
                s = "red"
            items.append(_item(name, s, f"{state} — {status_str}", reason="core container is not running" if s != "green" else None))

        # Count extra containers (not in core set)
        for name, c in by_name.items():
            if name not in _CORE_CONTAINERS:
                extra_count += 1

        if extra_count:
            items.append(_item("extras", "green", f"{extra_count} non-core container(s) present",
                               metric=str(extra_count)))
    except Exception as e:
        items.append(_item("containers", "unknown", str(e), reason="container section probe raised"))

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


def _load_desired_timers() -> dict[str, dict]:
    path = HERMES_HOME / "desired-timers.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            return {}
        normalized: dict[str, dict] = {}
        for name, spec in data.items():
            normalized[str(name)] = spec if isinstance(spec, dict) else {"enabled": bool(spec)}
        return normalized
    except Exception:
        return {}


def _section_systemd() -> dict:
    items: list[dict] = []
    desired_timers = _load_desired_timers()
    disabled_by_design = {name: spec for name, spec in desired_timers.items() if spec.get("enabled") is False}
    try:
        r_fail = _run(["systemctl", "--user", "--failed", "--no-legend"])
        if r_fail.returncode not in (0, 1):
            items.append(_item("failed_units", "unknown", f"systemctl --failed error: {r_fail.stderr.strip()}", reason="systemctl --failed returned an unexpected code"))
        else:
            failed_lines = [l.strip() for l in r_fail.stdout.splitlines() if l.strip()]
            failed_names = [l.split()[0] for l in failed_lines if l]
            if failed_names:
                items.append(_item("failed_units", "red", f"{len(failed_names)} failed: {', '.join(failed_names)}", metric=str(len(failed_names)), reason="systemd reports failed user units"))
            else:
                items.append(_item("failed_units", "green", "no failed units"))
    except Exception as e:
        items.append(_item("failed_units", "unknown", str(e), reason="failed_units probe raised"))

    try:
        r_timers = _run(["systemctl", "--user", "list-timers", "--no-legend"])
        active_timers: set[str] = set()
        if r_timers.returncode == 0:
            for line in r_timers.stdout.splitlines():
                for part in line.split():
                    if part.endswith(".timer"):
                        active_timers.add(part)
                        break
        expected_timers = {timer for timer in _KEEP_TIMERS if disabled_by_design.get(timer, {}).get("enabled") is not False}
        missing = expected_timers - active_timers
        if missing:
            items.append(_item("timers_missing", "amber", f"missing/inactive expected timers: {', '.join(sorted(missing))}", reason="timer is expected enabled but absent from systemd list-timers"))
        else:
            items.append(_item("timers_keep", "green", f"all {len(expected_timers)} expected KEEP timers active", metric=str(len(active_timers))))
        if disabled_by_design:
            disabled_parts = []
            for name, spec in sorted(disabled_by_design.items()):
                why = spec.get("reason") or "disabled by design"
                disabled_parts.append(f"{name} ({why})")
            items.append(_item("timers_disabled_by_design", "info", "; ".join(disabled_parts), metric=str(len(disabled_by_design)), reason="desired-timers.json marks these timers intentionally disabled"))
    except Exception as e:
        items.append(_item("timers_keep", "unknown", str(e), reason="timers probe raised"))

    return _section("systemd", "Systemd", items)


# ---------------------------------------------------------------------------
# Section 5: backups
# ---------------------------------------------------------------------------

_AMBER_H = 168
_RED_H = 336
_VERA_AMBER_DAYS = 8
_VERA_RED_DAYS = 15


def _backup_age_status(path_glob_parent: Path, pattern: str) -> dict:
    """Find newest matching file; return item with SLA-based status."""
    try:
        matches = sorted(path_glob_parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return _item(pattern, "red", "no backup found", reason="no matching backup artifact exists")
        newest = matches[0]
        age_h = (time.time() - newest.stat().st_mtime) / 3600
        size_mb = newest.stat().st_size / (1024 * 1024)
        if age_h < _AMBER_H:
            s: Status = "info"
            reason = "backup age is within 7d SLA; routine context only"
        elif age_h < _RED_H:
            s = "amber"
            reason = "backup age exceeds 7d SLA but is under 14d red threshold"
        else:
            s = "red"
            reason = "backup age exceeds 14d red threshold"
        return _item(pattern, s, f"{newest.name} age={age_h:.1f}h size={size_mb:.1f}MB", metric=f"{age_h:.1f}h", reason=reason)
    except Exception as e:
        return _item(pattern, "unknown", str(e), reason="backup age probe raised")


def _section_backups() -> dict:
    items: list[dict] = []
    mvms_dir = HERMES_HOME / "backups" / "mvms"

    # MVMS canonical SQL
    items.append(_backup_age_status(mvms_dir, "mvms-canonical-*.sql.gz"))
    # Honcho live store
    items.append(_backup_age_status(mvms_dir, "honcho-live-store-*.sql.gz"))
    # Hermes app state
    items.append(_backup_age_status(mvms_dir, "hermes-app-state-*.tar.gz"))

    # Off-box replication marker: absence is a real risk, separate from within-SLA backup age.
    try:
        marker_candidates = [
            mvms_dir / "OFFBOX-REPLICATION-OK",
            mvms_dir / "offbox-replication.ok",
            HERMES_HOME / "audits" / "veracrypt-backup" / "OFFBOX-REPLICATION-OK",
        ]
        marker = next((m for m in marker_candidates if m.exists()), None)
        if marker is None:
            items.append(_item("mvms-backup-gap-offbox", "amber", "no off-box replication marker found", reason="local backups exist, but off-box replication has no success marker"))
        else:
            age_h = (time.time() - marker.stat().st_mtime) / 3600
            status: Status = "green" if age_h < _AMBER_H else ("amber" if age_h < _RED_H else "red")
            items.append(_item("mvms-backup-gap-offbox", status, f"{marker.name} age={age_h:.1f}h", metric=f"{age_h:.1f}h", reason="off-box replication marker age exceeds SLA" if status != "green" else None))
    except Exception as e:
        items.append(_item("mvms-backup-gap-offbox", "unknown", str(e), reason="off-box marker probe raised"))

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
                                   metric=f"{age_days:.1f}d",
                                   reason="weekly VeraCrypt backup age exceeds configured threshold" if s != "green" else None))
            else:
                items.append(_item("veracrypt_weekly", "amber", "no weekly backup dirs found", reason="veracrypt backup root exists but has no weekly dirs"))
        else:
            items.append(_item("veracrypt_weekly", "amber", "veracrypt-backup dir missing", reason="weekly off-box backup audit directory missing"))
    except Exception as e:
        items.append(_item("veracrypt_weekly", "unknown", str(e), reason="veracrypt weekly probe raised"))

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
            s = _age_status(age_h, green_h=24.0, amber_h=168.0)
            items.append(_item("state_db", s,
                               f"size={size_kb:.0f}KB mtime={age_h:.1f}h ago",
                               metric=f"{size_kb:.0f}KB",
                               reason=(None if s == "green" else f"state.db last modified {age_h:.1f}h ago (stale)")))
        else:
            items.append(_item("state_db", "amber", "state.db not found", reason="Hermes state DB missing"))
    except Exception as e:
        items.append(_item("state_db", "unknown", str(e), reason="state DB stat probe raised"))

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
            s: Status = "amber" if total == 0 else "green"
            items.append(_item("kanban_db", s, detail, metric=str(open_count),
                               reason="kanban board has zero tasks" if total == 0 else None))
        else:
            items.append(_item("kanban_db", "amber", "kanban.db not found at expected path", reason="Hermes board DB missing at expected path"))
    except Exception as e:
        items.append(_item("kanban_db", "unknown", str(e), reason="kanban DB read probe raised"))

    # MEMORY.md size
    try:
        if MEMORY_MD.exists():
            size = MEMORY_MD.stat().st_size
            s = "green" if size < 100000 else "info"
            items.append(_item("memory_md", s,
                               f"size={size} bytes",
                               metric=f"{size}B",
                               reason="exceeds 100KB threshold but MEMORY.md growth is by design" if s == "info" else None))
        else:
            items.append(_item("memory_md", "amber", "MEMORY.md not found", reason="Claude MEMORY.md missing at expected path"))
    except Exception as e:
        items.append(_item("memory_md", "unknown", str(e), reason="MEMORY.md stat probe raised"))

    # MVMS count (docker exec, 60s sub-cache, degrade to unknown)
    try:
        count = _mvms_count()
        if count is None:
            items.append(_item("mvms_observations", "unknown",
                               "docker exec psql failed or timed out", reason="MVMS observations count probe failed"))
        else:
            items.append(_item("mvms_observations", "green",
                               f"{count:,} observations in memory.observations",
                               metric=str(count)))
    except Exception as e:
        items.append(_item("mvms_observations", "unknown", str(e), reason="MVMS observations probe raised"))

    # Honcho messages count (docker exec, 60s sub-cache)
    try:
        count = _honcho_count()
        if count is None:
            items.append(_item("honcho_messages", "unknown",
                               "docker exec psql failed or timed out", reason="Honcho messages count probe failed"))
        else:
            items.append(_item("honcho_messages", "green",
                               f"{count:,} messages in honcho.messages",
                               metric=str(count)))
    except Exception as e:
        items.append(_item("honcho_messages", "unknown", str(e), reason="Honcho messages probe raised"))

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
            items.append(_item("cron_jobs", "amber", "no cron jobs found", reason="cron loader returned zero jobs"))
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
        items.append(_item("cron_jobs", "unknown", str(e), reason="cron jobs loader raised"))

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
            items.append(_item("disk_free", "unknown", "df parse failed", reason="df output parse failed"))
    except Exception as e:
        items.append(_item("disk_free", "unknown", str(e), reason="disk probe raised"))

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
                items.append(_item("mem_available", "unknown", "Mem: line not found in free output", reason="free output missing Mem line"))
        else:
            items.append(_item("mem_available", "unknown", "free command failed", reason="free command returned non-zero"))
    except Exception as e:
        items.append(_item("mem_available", "unknown", str(e), reason="memory probe raised"))

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
                items.append(_item("load_avg", "unknown", f"could not parse: {text}", reason="uptime load parse failed"))
        else:
            items.append(_item("load_avg", "unknown", "uptime failed", reason="uptime command returned non-zero"))
    except Exception as e:
        items.append(_item("load_avg", "unknown", str(e), reason="load average probe raised"))

    # WSL uptime from /proc/uptime
    try:
        uptime_s = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        h = int(uptime_s // 3600)
        m = int((uptime_s % 3600) // 60)
        items.append(_item("wsl_uptime", "green", f"up {h}h {m}m", metric=f"{h}h{m}m"))
    except Exception as e:
        items.append(_item("wsl_uptime", "unknown", str(e), reason="/proc/uptime read failed"))

    return _section("host", "Host", items)




# ---------------------------------------------------------------------------
# Consolidated donor snapshots: System Health attention, Git Health, Work, Pulse
# ---------------------------------------------------------------------------

def _coerce_status(value: Any) -> Status:
    normalized = str(value or "unknown").lower()
    if normalized in {"green", "ok", "online", "running", "active", "enabled", "ready"}:
        return "green"
    if normalized in {"info", "informational", "notice"}:
        return "info"
    if normalized in {"amber", "warn", "warning", "degraded", "stopped", "auth_gated"}:
        return "amber"
    if normalized in {"red", "bad", "error", "failed", "offline", "critical", "orphaned", "missing_worktree"}:
        return "red"
    return "unknown"


def _safe_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _build_attention_snapshot(sections: list[dict]) -> dict:
    diagnostics, _info = _build_diagnostics(sections)
    diagnostics = [d for d in diagnostics if d.get("severity") in {"amber", "red"}]
    chips = []
    for diag in diagnostics[:4]:
        message = str(diag.get("message") or "needs attention")
        label, _, detail = message.partition(":")
        chips.append({
            "source": diag.get("source", "unknown"),
            "status": diag.get("severity", "amber"),
            "label": label.strip().replace("_", " ") or str(diag.get("source", "unknown")).replace("_", " "),
            "detail": detail.strip() or message,
            "section_id": diag.get("source", "unknown"),
        })
    return {
        "posture": _worst_status([s.get("status", "unknown") for s in sections]),
        "chips": chips,
    }


def _repo_section_from_git_health() -> tuple[dict, dict]:
    try:
        from hermes_cli.dashboard_codex_sessions import git_graph, git_health

        health = git_health()
        graph = git_graph()
        summary = health.get("summary", {}) if isinstance(health, dict) else {}
        total = int(summary.get("total") or 0)
        ready = int(summary.get("ready") or 0)
        readiness_pct = round((ready / total) * 100) if total else 0
        uncommitted = int(summary.get("total_uncommitted") or 0)
        changed = int(summary.get("total_files_changed") or 0)
        best = health.get("best_move", {}) if isinstance(health, dict) else {}
        best_text = str(best.get("text") or "No git move available")
        best_status = _coerce_status(best.get("severity"))
        # Best-move warn/idle context is ranked by explicit repo thresholds; only red degrades posture.
        if best_status != "red":
            best_status = "green"
        counts = graph.get("counts", {}) if isinstance(graph, dict) else {}
        ahead_total = int(counts.get("ahead_total") or 0)
        rows = health.get("rows", []) if isinstance(health, dict) else []
        normalized_rows: list[dict] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            normalized = dict(row)
            sev = str(normalized.get("severity") or "unknown").lower()
            if sev == "idle":
                normalized["severity"] = "info"
                normalized.setdefault("reason", "idle lane/no reviewable work; informational only")
            elif sev == "warn":
                normalized["severity"] = "info"
                normalized.setdefault("reason", "git-health warning is velocity-scored separately by total uncommitted threshold")
            elif _coerce_status(sev) in {"amber", "red"}:
                normalized["severity"] = _coerce_status(sev)
                normalized.setdefault("reason", normalized.get("recommendation") or f"git-health severity={sev}")
            normalized_rows.append(normalized)
        rows = normalized_rows
        lanes = graph.get("lanes", []) if isinstance(graph, dict) else []
        actionable_row_statuses = [
            _coerce_status(row.get("severity"))
            for row in rows
            if isinstance(row, dict) and _coerce_status(row.get("severity")) in {"amber", "red"}
        ]
        status: Status = "green"
        if any(s == "red" for s in actionable_row_statuses):
            status = "red"
        elif any(s == "amber" for s in actionable_row_statuses) or uncommitted > 100:
            status = "amber"
        lane_counts: list[tuple[str, int]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            count = int(row.get("uncommitted") or 0)
            if count:
                lane_counts.append((str(row.get("slug") or row.get("thread_id") or "lane"), count))
        lane_counts.sort(key=lambda item: item[1], reverse=True)
        lane_detail = ", ".join(f"{slug}: {count}" for slug, count in lane_counts[:5])
        uncommitted_reason = f"uncommitted={uncommitted} files" + (f" ({lane_detail})" if lane_detail else "")
        payload = {
            "scanned_at": health.get("scanned_at") or graph.get("scanned_at"),
            "summary": summary,
            "readiness_pct": readiness_pct,
            "best_move": best,
            "counts": counts,
            "rows": rows,
            "lanes": lanes,
        }
        section = _section("repo", "Repo", [
            _item("readiness", "green", f"{ready}/{total} lanes ready", metric=f"{readiness_pct}%"),
            _item("uncommitted", "amber" if uncommitted > 100 else "green", f"{uncommitted} uncommitted files across lanes", metric=str(uncommitted), reason=uncommitted_reason if uncommitted > 100 else None),
            _item("ahead_total", "amber" if ahead_total > 20000 else "green", f"{ahead_total} commits ahead total", metric=str(ahead_total), reason="ahead_total exceeds 20k commits" if ahead_total > 20000 else None),
            _item("best_move", best_status, best_text, reason="git-health best move reports actionable severity" if best_status != "green" else None),
            _item("files_changed", "amber" if changed > 15000 else "green", f"{changed} files changed across lanes", metric=str(changed), reason="files_changed exceeds 15k threshold" if changed > 15000 else None),
        ])
        if status in {"red", "amber"} and section.get("status") == "green":
            section["status"] = status
        return section, payload
    except Exception as exc:
        payload = {"error": str(exc), "summary": {}, "readiness_pct": 0, "best_move": {"text": "Git Health unavailable", "severity": "unknown"}, "rows": [], "lanes": []}
        return _section("repo", "Repo", [_item("git_health", "unknown", str(exc), reason="git_health/git_graph import or probe raised")]), payload


def _work_section_from_command_center() -> tuple[dict, dict]:
    try:
        from hermes_cli.dashboard_command_center import get_command_center

        payload = get_command_center()
        projects = payload.get("projects", []) if isinstance(payload, dict) else []
        live = payload.get("live", {}) if isinstance(payload, dict) else {}
        decisions = payload.get("decisions", []) if isinstance(payload, dict) else []
        stalled = payload.get("stalled", []) if isinstance(payload, dict) else []
        active_projects = [p for p in projects if isinstance(p, dict) and not p.get("archived")]
        if active_projects:
            projects_pct = round(sum(float(p.get("completion_pct") or 0) for p in active_projects) / len(active_projects))
        else:
            projects_pct = 0
        runtimes = live.get("runtimes", []) if isinstance(live, dict) else []
        live_runtimes = sum(1 for r in runtimes if _coerce_status((r or {}).get("status")) == "green")
        section = _section("work", "Work", [
            _item("projects_completion", "green" if projects_pct >= 75 else "amber", f"{len(active_projects)} active project cards averaged", metric=f"{projects_pct}%", reason="project completion average below 75%" if projects_pct < 75 else None),
            _item("pending_decisions", "amber" if decisions else "green", f"{len(decisions)} decision items", metric=str(len(decisions)), reason="open decision queue is non-empty" if decisions else None),
            _item("live_runtimes", "green" if live_runtimes else "amber", f"{live_runtimes}/{_safe_len(runtimes)} runtimes live", metric=str(live_runtimes), reason="no live runtimes reported by command center" if not live_runtimes else None),
            _item("stalled", "amber" if stalled else "green", f"{len(stalled)} stalled workers/tasks", metric=str(len(stalled)), reason="stalled worker/task list is non-empty" if stalled else None),
        ])
        payload = {
            **(payload if isinstance(payload, dict) else {}),
            "projects_completion_pct": projects_pct,
            "live_runtimes": live_runtimes,
            "counts": {
                "projects": len(active_projects),
                "decisions": len(decisions),
                "live_runtimes": live_runtimes,
                "stalled": len(stalled),
            },
        }
        return section, payload
    except Exception as exc:
        payload = {
            "error": str(exc),
            "projects": [],
            "live": {"runtimes": []},
            "decisions": [],
            "stalled": [],
            "projects_completion_pct": None,
            "live_runtimes": None,
            "counts": {"projects": None, "decisions": None, "live_runtimes": None, "stalled": None},
        }
        return _section("work", "Work", [_item("command_center", "unknown", str(exc), reason="command center payload probe raised")]), payload


def _activity_section_from_pulse() -> tuple[dict, dict]:
    try:
        from hermes_cli.dashboard_health import _get_queue_depth
        from hermes_cli.pulse_data import build_pulse_kpis, build_pulse_queue

        queue_7d = _get_queue_depth("7d")
        pulse_queue = build_pulse_queue(limit=8)
        kpis = build_pulse_kpis()
        points = queue_7d.get("points", []) if isinstance(queue_7d, dict) else []
        created_7d = sum(int(point.get("count") or 0) for point in points if isinstance(point, dict))
        open_now = int(queue_7d.get("openNow") or 0) if isinstance(queue_7d, dict) else 0
        pending = int(kpis.get("pending_cards") or 0) if isinstance(kpis, dict) else 0
        active_hives = int(kpis.get("active_hives") or 0) if isinstance(kpis, dict) else 0
        cards = pulse_queue.get("cards", []) if isinstance(pulse_queue, dict) else []
        active_hive_status: Status = "amber" if active_hives > 3 else ("info" if active_hives > 0 else "green")
        active_hive_reason = (
            f"active_hives={active_hives} exceeds 3 active-hive threshold"
            if active_hives > 3
            else (f"active_hives={active_hives} is expected background activity" if active_hives > 0 else None)
        )
        section = _section("activity", "Activity", [
            _item("created_7d", "green" if created_7d < 200 else "amber", "tasks created in the last 7 days", metric=str(created_7d), reason="created_7d exceeds 200 expected-activity threshold" if created_7d >= 200 else None),
            _item("open_now", "green" if open_now < 100 else "amber", "currently open kanban tasks", metric=str(open_now), reason="open_now exceeds 100 expected-activity threshold" if open_now >= 100 else None),
            _item("pending_cards", "green" if pending < 20 else "amber", "ready/triage cards waiting", metric=str(pending), reason="pending ready/triage card count exceeds 20" if pending >= 20 else None),
            _item("active_hives", active_hive_status, "active hive runs", metric=str(active_hives), reason=active_hive_reason),
        ])
        payload = {
            "queue_7d": queue_7d,
            "queue": pulse_queue,
            "kpis": kpis,
            "created_7d": created_7d,
            "open_now": open_now,
            "cards": cards,
        }
        return section, payload
    except Exception as exc:
        payload = {
            "error": str(exc),
            "queue_7d": {"range": "7d", "points": [], "openNow": None},
            "queue": {"cards": []},
            "kpis": {},
            "created_7d": None,
            "open_now": None,
            "cards": [],
        }
        return _section("activity", "Activity", [_item("pulse", "unknown", str(exc), reason="pulse KPI/queue probe raised")]), payload


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def _build_diagnostics(sections: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split actionable red/amber diagnostics from muted info context."""
    diags: list[dict] = []
    info_list: list[dict] = []
    severity_order = {"red": 0, "amber": 1}
    for sec in sections:
        for item in sec.get("items", []):
            sev = item.get("status")
            row = {
                "severity": sev,
                "source": sec["id"],
                "message": f"{item['name']}: {item['detail']}",
            }
            if item.get("reason"):
                row["reason"] = item.get("reason")
            if item.get("metric") is not None:
                row["metric"] = item.get("metric")
            if sev == "info":
                info_list.append(row)
            elif sev in ("red", "amber"):
                diags.append(row)
            elif sev == "unknown":
                row["severity"] = "amber"
                row["message"] = f"{item['name']}: {item['detail']} (unknown)"
                row["hint"] = "probe degraded — check logs"
                row.setdefault("reason", item.get("reason") or "probe returned unknown")
                diags.append(row)
    diags.sort(key=lambda d: severity_order.get(d["severity"], 2))
    info_list.sort(key=lambda d: (str(d.get("source")), str(d.get("message"))))
    return diags, info_list


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
                                         [_item("probe", "unknown", str(e), reason="section builder future failed")]))

    repo_section, repo = _repo_section_from_git_health()
    work_section, work = _work_section_from_command_center()
    activity_section, activity = _activity_section_from_pulse()
    infra = _infra_snapshot()
    infra_items = [
        _item("cost", infra["cost"].get("status", "unknown"), infra["cost"].get("detail", "cost unmeasured"), metric=infra["cost"].get("label"), reason=infra["cost"].get("detail") if infra["cost"].get("status") != "green" else None),
        _item("DR", infra["dr"].get("status", "unknown"), infra["dr"].get("detail", "DR unmeasured"), metric=infra["dr"].get("label"), reason=infra["dr"].get("detail") if infra["dr"].get("status") != "green" else None),
        _item("evals", infra["evals"].get("status", "unknown"), infra["evals"].get("detail", "evals unmeasured"), metric=infra["evals"].get("label"), reason=infra["evals"].get("detail") if infra["evals"].get("status") != "green" else None),
        _item("security", infra["security"].get("status", "unknown"), infra["security"].get("detail", "security unmeasured"), metric=infra["security"].get("label"), reason=infra["security"].get("detail") if infra["security"].get("status") != "green" else None),
    ]
    infra_section = _section("infra", "Infra Gates", infra_items)
    sections.extend([infra_section, repo_section, work_section, activity_section])

    overall_statuses = [s["status"] for s in sections if s.get("status") != "info"]
    overall = _worst_status(overall_statuses) if overall_statuses else "green"
    diagnostics, info = _build_diagnostics(sections)
    attention = _build_attention_snapshot(sections)

    # Build Nexus graph (Appendix B) inside the same cached snapshot build
    try:
        graph = _build_os_graph(sections)
    except Exception as e:
        graph = {"nodes": [], "edges": [], "error": str(e)}
        diagnostics.append({
            "severity": "red",
            "source": "graph",
            "message": "topology unavailable",
            "reason": str(e),
        })
        overall = "red"

    return {
        "generated_at": _now(),
        "overall": overall,
        "sections": sections,
        "diagnostics": diagnostics,
        "info": info,
        "attention": attention,
        "repo": repo,
        "work": work,
        "activity": activity,
        "infra": infra,
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


@router.get("/os", summary="OS infrastructure health snapshot (11 sections)")
async def get_os() -> dict:
    """Read-only OSSnapshot: infra + repo/work/activity sections, diagnostics, 20 s TTL cache.

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
                "reason": "snapshot builder exceeded hard endpoint timeout",
            }],
            "info": [],
        }
