#!/usr/bin/env python3
"""One-shot Hermes journald/logfile -> Loki pusher.

Default mode pushes recent hermes-gateway journald lines plus Hermes lane/log files
to Loki's /loki/api/v1/push endpoint. A timer can run it repeatedly; it does not
write state.db and it does not require Prometheus/Grafana.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
DEFAULT_LOG_FILES = [
    HERMES_HOME / "logs" / "gateway.log",
    HERMES_HOME / "logs" / "agent.log",
    HERMES_HOME / "logs" / "errors.log",
]


def ns_now() -> int:
    return time.time_ns()


def journal_entries(unit: str, limit: int) -> list[tuple[int, str, dict[str, str]]]:
    proc = subprocess.run(
        ["journalctl", "--user", "-u", unit, "-n", str(limit), "--no-pager", "-o", "json"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if proc.returncode != 0:
        return []
    entries: list[tuple[int, str, dict[str, str]]] = []
    for raw in proc.stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = str(event.get("MESSAGE") or "").strip()
        if not msg:
            continue
        try:
            ts_ns = int(event.get("__REALTIME_TIMESTAMP", 0)) * 1000
        except (TypeError, ValueError):
            ts_ns = ns_now()
        entries.append((ts_ns, msg, {"unit": unit, "source": "journald", "job": "hermes-gateway"}))
    return entries


def file_entries(path: Path, limit: int) -> list[tuple[int, str, dict[str, str]]]:
    if not path.exists() or not path.is_file():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()[-limit:]
        base_ns = int(path.stat().st_mtime * 1_000_000_000)
    except OSError:
        return []
    entries = []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        entries.append((base_ns + idx, line[:8000], {"source": "file", "job": "hermes-lane-log", "logfile": path.name}))
    return entries


def build_streams(entries: list[tuple[int, str, dict[str, str]]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[tuple[str, str], ...], list[list[str]]] = {}
    labels_by_key: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    for ts_ns, msg, labels in entries:
        clean_labels = {k: v for k, v in labels.items() if v}
        key = tuple(sorted(clean_labels.items()))
        labels_by_key[key] = clean_labels
        grouped.setdefault(key, []).append([str(ts_ns), msg])
    return [{"stream": labels_by_key[key], "values": values} for key, values in grouped.items()]


def push_loki(loki_url: str, streams: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
    payload = {"streams": streams}
    if dry_run:
        return {"dry_run": True, "streams": len(streams), "entries": sum(len(s["values"]) for s in streams)}
    url = loki_url.rstrip("/") + "/loki/api/v1/push"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return {"status": resp.status, "reason": resp.reason, "streams": len(streams), "entries": sum(len(s["values"]) for s in streams)}
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", "replace")
        return {"status": exc.code, "error": detail, "streams": len(streams), "entries": sum(len(s["values"]) for s in streams)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push Hermes gateway journald + lane logs to Loki")
    parser.add_argument("--loki-url", default=os.environ.get("LOKI_URL", "http://127.0.0.1:3100"))
    parser.add_argument("--journal-unit", action="append", default=["hermes-gateway.service"])
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--log-file", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    entries: list[tuple[int, str, dict[str, str]]] = []
    for unit in args.journal_unit:
        entries.extend(journal_entries(unit, args.limit))
    files = [Path(p).expanduser() for p in args.log_file] if args.log_file else DEFAULT_LOG_FILES
    for path in files:
        entries.extend(file_entries(path, args.limit))
    streams = build_streams(entries)
    result = push_loki(args.loki_url, streams, dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    if result.get("status") not in (None, 200, 204) and not result.get("dry_run"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
