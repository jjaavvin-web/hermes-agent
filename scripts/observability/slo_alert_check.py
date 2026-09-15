#!/usr/bin/env python3
"""Check fresh Hermes SLO snapshots; deduplicate notifications in local JSON state."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeGuard

from hermes_cli.observability_slo import DEFAULT_LATEST, SLO_DEFINITIONS, utc_iso

NOTIFY = Path.home() / ".hermes" / "scripts" / "discord-notify.sh"
MAX_SNAPSHOT_AGE = 15 * 60
REMINDER_SECONDS = 6 * 60 * 60
EVENT_METRICS = {"turn_error_rate": "turn_error_events", "fallback_trigger_rate": "fallback_events"}
LABELS = {"turn_error_rate": "Failed-event estimate", "fallback_trigger_rate": "Provider fallback rate",
          "recall_hit_rate": "Recall hit rate", "watchdog_restart_count": "Watchdog restarts",
          "cost_burn_rate_usd_24h": "Cost"}


def synthetic_snapshot() -> dict[str, Any]:
    now = time.time()
    return {"generated_at": utc_iso(now), "since": utc_iso(now - 86400), "window_seconds": 86400,
            "turn_count": 40, "metrics": {"gateway_turn_p95_latency_ms": 240000,
            "turn_error_rate": 0.25, "fallback_trigger_rate": 0.50, "recall_hit_rate": 0.20,
            "watchdog_restart_count": 3, "cost_burn_rate_usd_24h": 99.0}, "sources": {"synthetic": True}}


def load_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def epoch(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("snapshot timestamp must be timezone-aware ISO time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("snapshot timestamp must include timezone")
    return parsed.timestamp()


def valid_evidence(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in value) and len(set(value)) == len(value)


def event_count(snapshot: dict[str, Any], key: str) -> int | None:
    evidence = snapshot.get("event_evidence", {}).get(key)
    if evidence is not None:
        return len(evidence)
    # No trustworthy numerator means reminders only, never a rate-based repage.
    return snapshot.get("journal_counts", {}).get(EVENT_METRICS[key])


def validate_snapshot(snapshot: dict[str, Any], now: float) -> float:
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    generated = epoch(snapshot.get("generated_at"))
    if now - generated > MAX_SNAPSHOT_AGE or generated > now + 60:
        raise ValueError("snapshot is stale (>15 minutes) or future-dated")
    if type(snapshot.get("turn_count")) is not int or snapshot["turn_count"] < 0:
        raise ValueError("snapshot has invalid turn count")
    window = snapshot.get("window_seconds")
    if not number(window) or window <= 0:
        raise ValueError("snapshot requires a positive measurement window")
    if "since" in snapshot and abs(epoch(snapshot["since"]) - (generated - window)) > 1:
        raise ValueError("snapshot measurement window is inconsistent")
    metrics = snapshot.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("snapshot has no metrics")
    for key, spec in SLO_DEFINITIONS.items():
        if not spec.get("page", True):
            continue
        if key in metrics and metrics[key] is None and (key == "recall_hit_rate" or (key in EVENT_METRICS and snapshot["turn_count"] == 0)):
            continue  # Recall warning; zero-turn event rates are unknown, not healthy.
        value = metrics.get(key)
        if not number(value) or value < 0 or (spec["unit"] == "ratio" and value > 1):
            raise ValueError(f"invalid or missing {key}")
    evidence, counts = snapshot.get("event_evidence", {}), snapshot.get("journal_counts", {})
    if not isinstance(evidence, dict) or not isinstance(counts, dict):
        raise ValueError("invalid event evidence or journal counts")
    if "event_evidence" in snapshot and not EVENT_METRICS.keys() <= evidence.keys():
        raise ValueError("partial event evidence map")
    for key, count_key in EVENT_METRICS.items():
        if key in evidence and not valid_evidence(evidence[key]):
            raise ValueError("invalid event evidence (expected unique SHA256 hashes)")
        if count_key in counts and (type(counts[count_key]) is not int or counts[count_key] < 0):
            raise ValueError("invalid event count")
        if key in evidence and count_key in counts and len(evidence[key]) != counts[count_key]:
            raise ValueError("event evidence and journal count disagree")
        count = event_count(snapshot, key)
        if snapshot["turn_count"] == 0:
            if metrics[key] is not None:
                raise ValueError("zero-turn event rate must be null")
            continue
        if count is not None and not math.isclose(metrics[key], min(1.0, count / snapshot["turn_count"]), abs_tol=1e-6):
            raise ValueError("event rate and numerator disagree")
    return generated


def breaches(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = snapshot.get("metrics") or {}
    found = []
    for key, spec in SLO_DEFINITIONS.items():
        if not spec.get("page", True):
            continue
        value = metrics.get(key)
        if value is None:
            if key == "recall_hit_rate":
                found.append({"metric": key, "value": "no_data", "target": spec["target"], "severity": "warn"})
            continue
        critical = spec["critical"]
        bad = float(value) < float(critical) if key == "recall_hit_rate" else float(value) > float(critical)
        if bad:
            found.append({"metric": key, "value": value, "target": spec["target"], "severity": "critical"})
    return found


def load_state(path: Path, now: float) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "snapshot_at": 0, "last_sent_at": None, "active": {}, "event_evidence": {}, "event_counts": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("invalid notification state version")
    stamp, sent = state.get("snapshot_at"), state.get("last_sent_at")
    if not number(stamp) or stamp < 0 or stamp > now + 60 or (sent is not None and (not number(sent) or sent < 0 or sent > now)):
        raise ValueError("invalid or future-dated notification state time")
    active, evidence, counts = state.get("active"), state.get("event_evidence"), state.get("event_counts")
    if not isinstance(active, dict) or not isinstance(evidence, dict) or not isinstance(counts, dict) or (active and sent is None):
        raise ValueError("invalid notification state")
    for key, value in active.items():
        if key not in SLO_DEFINITIONS or not SLO_DEFINITIONS[key].get("page", True):
            raise ValueError("invalid state metric")
        if not ((key == "recall_hit_rate" or key in EVENT_METRICS) and value == "no_data") and (not number(value) or value < 0 or (SLO_DEFINITIONS[key]["unit"] == "ratio" and value > 1)):
            raise ValueError("invalid state metric value")
    if any(k not in EVENT_METRICS or not valid_evidence(v) for k, v in evidence.items()):
        raise ValueError("invalid state event evidence")
    if any(k not in EVENT_METRICS or type(v) is not int or v < 0 for k, v in counts.items()):
        raise ValueError("invalid state event counts")
    # Old valid state is NOT expired: its incident still needs a reminder/recovery.
    return state


def transition(snapshot: dict[str, Any], rows: list[dict[str, Any]], state: dict[str, Any], now: float) -> tuple[str | None, dict[str, Any]]:
    active = {row["metric"]: row["value"] for row in rows}
    reason = None
    if active:
        new_metrics = active.keys() - state["active"].keys()
        # Known event evidence survives recovery: denominator-only oscillation
        # must not reopen an already notified event inside the reminder interval.
        if any(k not in EVENT_METRICS or (k not in state["event_evidence"] and k not in state["event_counts"]) for k in new_metrics):
            reason = "new breached metric" if state["active"] else "first breach"
        for key, value in active.items():
            if key in EVENT_METRICS:
                if snapshot["turn_count"] == 0:
                    continue
                evidence = snapshot.get("event_evidence", {}).get(key)
                previous = state["event_evidence"].get(key)
                count, old_count = event_count(snapshot, key), state["event_counts"].get(key)
                if evidence is not None and previous is not None:
                    if set(evidence) - set(previous):
                        reason = reason or "new event evidence"
                elif count is not None and old_count is not None and count > old_count:
                    reason = reason or "event count increased (no comparable evidence)"
            elif key in state["active"]:
                old = state["active"][key]
                if value == "no_data" and old != "no_data":
                    reason = reason or "recall data unavailable"
                elif number(value) and number(old) and (value < old if key == "recall_hit_rate" else value > old):
                    reason = reason or "worsened metric"
        if state["last_sent_at"] is not None and now - state["last_sent_at"] >= REMINDER_SECONDS:
            reason = reason or "6-hour reminder; breach persists"
    elif state["active"]:
        reason = "recovery"
    next_state = {**state, "active": active if reason or state["active"] else {}, "snapshot_at": epoch(snapshot["generated_at"]),
                  "event_evidence": dict(state["event_evidence"]), "event_counts": dict(state["event_counts"])}
    for key in active.keys() & EVENT_METRICS.keys():
        if snapshot["turn_count"] == 0:
            continue
        evidence = snapshot.get("event_evidence", {}).get(key)
        if evidence is not None:
            next_state["event_evidence"][key] = sorted(set(state["event_evidence"].get(key, [])) | set(evidence))
        count = event_count(snapshot, key)
        if count is not None:
            next_state["event_counts"][key] = max(count, state["event_counts"].get(key, 0))
    return reason, next_state


def atomic_state(path: Path, state: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, sort_keys=True, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_alert(snapshot: dict[str, Any], breach_rows: list[dict[str, Any]], reason: str = "breach") -> str:
    def stamp(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    generated, window = epoch(snapshot["generated_at"]), snapshot["window_seconds"]
    lines = ["✅ Hermes SLO recovery" if not breach_rows else "🚨 Hermes SLO breach",
             f"Reason: {reason}", f"Snapshot: {stamp(generated)} (export time, not incident onset)",
             f"Window: rolling {window / 3600:g}h, {stamp(generated - window)} → {stamp(generated)}; {snapshot['turn_count']:,} turns"]
    for row in breach_rows:
        key, value = row["metric"], row["value"]
        spec = SLO_DEFINITIONS[key]
        if value == "no_data":
            display = "no data (warning)" if key == "recall_hit_rate" else "unknown (no turns; prior breach retained)"
        elif spec["unit"] == "ratio":
            display = f"{value:.1%}"
        elif spec["unit"] == "USD/24h":
            display = f"${value:,.2f}"
        else:
            display = f"{value:,g}"
        if key in EVENT_METRICS:
            count = event_count(snapshot, key)
            display += f" ({count:,} events / {snapshot['turn_count']:,} turns)" if count is not None else " (event count unavailable)"
        threshold = f"{spec['critical']:.0%}" if spec["unit"] == "ratio" else f"{spec['critical']:g}"
        lines.append(f"- {LABELS.get(key, key)}: {display}; pages {'below' if key == 'recall_hit_rate' else 'above'} {threshold}")
    if not breach_rows:
        lines.append("Previously reported breaches cleared in this fresh snapshot.")
    lines.append("Rolling-window totals are not new events since the previous check.")
    return "\n".join(lines)


def notify(message: str, *, dry_run: bool) -> int:
    if dry_run:
        return 0  # Never execute a notifier in preview mode.
    return subprocess.run([str(NOTIFY), message], text=True, encoding="utf-8", errors="replace",
                          capture_output=True, check=False, timeout=30).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Hermes SLO latest.json and deduplicate breach/recovery notifications")
    parser.add_argument("--latest", default=str(DEFAULT_LATEST))
    parser.add_argument("--state", help="notification state JSON (default: slo-alert-dedup-v1.json beside --latest)")
    parser.add_argument("--dry-run", action="store_true", help="preview only; no notifier or state writes")
    parser.add_argument("--synthetic-breach", action="store_true", help="preview a synthetic breach; never sends or writes state")
    parser.add_argument("--print-only", action="store_true", help="preview only; no notifier or state writes")
    args = parser.parse_args(argv)
    latest = Path(args.latest).expanduser()
    state_path = Path(args.state).expanduser() if args.state else latest.with_name("slo-alert-dedup-v1.json")
    preview = args.print_only or args.dry_run or args.synthetic_breach or os.environ.get("DISCORD_NOTIFY_DRYRUN") == "1"
    lock_fd = None
    try:
        if state_path.resolve() == latest.resolve():
            raise ValueError("state and snapshot paths must differ")
        if not preview:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            # Lock the stable directory inode: atomic state replacement cannot defeat
            # this lock, and no second persistent lock file is needed (Linux/WSL).
            lock_fd = os.open(state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("slo-alert suppressed: another checker holds the state-directory lock")
                return 0
        now = time.time()
        snapshot = synthetic_snapshot() if args.synthetic_breach else load_snapshot(latest)
        generated = validate_snapshot(snapshot, now)
        state = load_state(state_path, now)
        if generated < state["snapshot_at"]:
            raise ValueError("snapshot predates notification state; refusing incident rollback")
        rows = breaches(snapshot)
        if snapshot["turn_count"] == 0:
            rows.extend({"metric": key, "value": "no_data", "target": SLO_DEFINITIONS[key]["target"], "severity": "warn"}
                        for key in state["active"] if key in EVENT_METRICS)
        reason, next_state = transition(snapshot, rows, state, now)
        if preview:
            print(render_alert(snapshot, rows, reason or "preview: no new notification") if rows or reason else "slo-alert ok: no breaches")
            return 0
        if reason:
            message = render_alert(snapshot, rows, reason)
            print(message)
            if notify(message, dry_run=False) != 0:
                print("slo-alert error: notification failed; state unchanged", file=sys.stderr)
                return 1  # Notifier status 2 must never masquerade as successful delivery.
            next_state["last_sent_at"] = time.time()
        atomic_state(state_path, next_state)
        assert lock_fd is not None
        os.fsync(lock_fd)
        if not reason:
            print("slo-alert suppressed: no new notification" if rows else "slo-alert ok: no breaches")
        return 2 if reason else 0
    except (OSError, ValueError, TypeError, OverflowError, subprocess.SubprocessError):
        print("slo-alert error: invalid/unavailable snapshot or notification state, or delivery/write failure; incident not cleared", file=sys.stderr)
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
