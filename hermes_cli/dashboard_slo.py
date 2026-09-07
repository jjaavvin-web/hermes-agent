"""Dashboard SLO panel and JSON contract.

Additive route: no scheduler, service, or gateway mutation. The dashboard serves
the latest file written by hermes_cli.observability_slo / scripts.
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from hermes_cli.observability_slo import DEFAULT_LATEST, SLO_DEFINITIONS

router = APIRouter(prefix="/api/dashboard/slo", tags=["slo-observability"])


def latest_path() -> Path:
    return Path(os.environ.get("HERMES_SLO_LATEST", str(DEFAULT_LATEST))).expanduser()


def load_latest() -> dict[str, Any]:
    path = latest_path()
    if not path.exists():
        return {
            "status": "no_data",
            "detail": f"SLO latest file not found: {path}",
            "slo_definitions": SLO_DEFINITIONS,
            "metrics": {},
            "series": [],
            "sources": {"latest": str(path)},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "detail": f"SLO latest file is invalid JSON: {exc}",
            "slo_definitions": SLO_DEFINITIONS,
            "metrics": {},
            "series": [],
            "sources": {"latest": str(path)},
        }
    payload.setdefault("status", "ok")
    payload.setdefault("slo_definitions", SLO_DEFINITIONS)
    payload.setdefault("sources", {})["latest"] = str(path)
    return payload


@router.get("")
def get_slo_snapshot() -> dict[str, Any]:
    return load_latest()


@router.get("/panel", response_class=HTMLResponse)
def get_slo_panel() -> HTMLResponse:
    payload = load_latest()
    metrics = payload.get("metrics") or {}
    series = payload.get("series") or []
    generated = html.escape(str(payload.get("generated_at") or payload.get("detail") or "no_data"))
    rows = []
    for key, spec in (payload.get("slo_definitions") or SLO_DEFINITIONS).items():
        value = metrics.get(key)
        rows.append(
            "<tr>"
            f"<td>{html.escape(key)}</td>"
            f"<td>{html.escape(str(value))}</td>"
            f"<td>{html.escape(str(spec.get('target')))}</td>"
            f"<td>{html.escape(str(spec.get('source')))}</td>"
            "</tr>"
        )
    points = [p for p in series[-36:] if isinstance(p, dict)]
    p95s = [p.get("gateway_turn_p95_latency_ms") for p in points if p.get("gateway_turn_p95_latency_ms") is not None]
    max_p95 = max([float(v) for v in p95s], default=1.0)
    poly_points = []
    for idx, point in enumerate(points):
        value = point.get("gateway_turn_p95_latency_ms")
        if value is None:
            continue
        x = 20 + idx * (560 / max(1, len(points) - 1))
        y = 170 - (float(value) / max_p95) * 140
        poly_points.append(f"{x:.1f},{y:.1f}")
    sparkline = " ".join(poly_points)
    embedded = html.escape(json.dumps({"metrics": metrics, "points": len(series)}, sort_keys=True))
    body = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Hermes SLO Panel</title>
<style>
body{{font-family:Inter,system-ui,sans-serif;background:#080b12;color:#e9eefb;margin:24px}}
.card{{border:1px solid #263248;border-radius:16px;background:#111827;padding:18px;margin-bottom:18px;box-shadow:0 10px 30px #0006}}
h1{{margin:0 0 6px;font-size:24px}} .muted{{color:#9fb0cc}} table{{width:100%;border-collapse:collapse}}
td,th{{border-bottom:1px solid #263248;padding:9px;text-align:left;vertical-align:top}} code{{color:#93c5fd}}
.badge{{display:inline-block;padding:4px 8px;border-radius:999px;background:#1d4ed8;color:white;font-size:12px}}
</style></head><body>
<div class=\"card\"><span class=\"badge\">SLO observability</span><h1>Hermes SLO panel</h1>
<p class=\"muted\">Generated: {generated} · turns: {html.escape(str(payload.get('turn_count')))} · series points: {len(series)}</p>
<svg width=\"620\" height=\"190\" role=\"img\" aria-label=\"gateway p95 latency sparkline\"><rect x=\"0\" y=\"0\" width=\"620\" height=\"190\" rx=\"12\" fill=\"#0b1020\"/><polyline points=\"{sparkline}\" fill=\"none\" stroke=\"#22d3ee\" stroke-width=\"3\"/><text x=\"20\" y=\"22\" fill=\"#9fb0cc\">gateway turn p95 latency</text></svg>
</div><div class=\"card\"><table><thead><tr><th>SLO</th><th>Current</th><th>Target</th><th>Source</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<script type=\"application/json\" id=\"slo-data\">{embedded}</script>
</body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})
