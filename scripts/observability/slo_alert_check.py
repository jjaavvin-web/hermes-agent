#!/usr/bin/env python3
"""Check latest Hermes SLO snapshot and notify Discord on threshold breaches."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from hermes_cli.observability_slo import DEFAULT_LATEST, SLO_DEFINITIONS, utc_iso

NOTIFY = Path.home() / ".hermes" / "scripts" / "discord-notify.sh"


def synthetic_snapshot() -> dict[str, Any]:
    return {
        "generated_at": utc_iso(),
        "turn_count": 42,
        "metrics": {
            "gateway_turn_p95_latency_ms": 240000,
            "turn_error_rate": 0.25,
            "fallback_trigger_rate": 0.50,
            "recall_hit_rate": 0.20,
            "watchdog_restart_count": 3,
            "cost_burn_rate_usd_24h": 99.0,
        },
        "sources": {"synthetic": True},
    }


def load_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def breaches(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = snapshot.get("metrics") or {}
    found = []
    for key, spec in SLO_DEFINITIONS.items():
        if not spec.get("page", True):
            continue  # informational metric (e.g. p95 over mixed lane/interactive turns) — tracked, not paged
        value = metrics.get(key)
        if value is None:
            if key == "recall_hit_rate":
                found.append({"metric": key, "value": "no_data", "target": spec["target"], "severity": "warn"})
            continue
        critical = spec.get("critical")
        if key == "recall_hit_rate":
            bad = float(value) < float(critical)
        else:
            bad = float(value) > float(critical)
        if bad:
            found.append({"metric": key, "value": value, "target": spec["target"], "severity": "critical"})
    return found


def render_alert(snapshot: dict[str, Any], breach_rows: list[dict[str, Any]]) -> str:
    lines = [
        "🚨 Hermes SLO breach",
        f"generated_at={snapshot.get('generated_at')}",
        f"turn_count={snapshot.get('turn_count')}",
        "breaches:",
    ]
    for row in breach_rows:
        lines.append(f"- {row['severity']}: {row['metric']}={row['value']} target {row['target']}")
    sources = snapshot.get("sources") or {}
    if sources:
        lines.append("sources: " + ", ".join(f"{k}={v}" for k, v in sorted(sources.items()) if k in {"state_db", "latest", "synthetic"}))
    lines.append("action: inspect `~/.hermes/observability/slo-latest.json` and recent gateway journal/Loki streams.")
    return "\n".join(lines)


def notify(message: str, *, dry_run: bool) -> int:
    env = os.environ.copy()
    if dry_run:
        env["DISCORD_NOTIFY_DRYRUN"] = "1"
    proc = subprocess.run([str(NOTIFY), message], text=True, capture_output=True, env=env, check=False, timeout=30)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Hermes SLO latest.json and Discord-alert on breach")
    parser.add_argument("--latest", default=str(DEFAULT_LATEST))
    parser.add_argument("--dry-run", action="store_true", help="force DISCORD_NOTIFY_DRYRUN=1")
    parser.add_argument("--synthetic-breach", action="store_true", help="render a guaranteed breach without reading latest.json")
    parser.add_argument("--print-only", action="store_true", help="render alert text but do not invoke discord-notify.sh")
    args = parser.parse_args(argv)

    snapshot = synthetic_snapshot() if args.synthetic_breach else load_snapshot(Path(args.latest))
    breach_rows = breaches(snapshot)
    if not breach_rows:
        print("slo-alert ok: no breaches")
        return 0
    message = render_alert(snapshot, breach_rows)
    print(message)
    if args.print_only:
        return 2
    rc = notify(message, dry_run=args.dry_run)
    return 2 if rc == 0 else rc


if __name__ == "__main__":
    raise SystemExit(main())
