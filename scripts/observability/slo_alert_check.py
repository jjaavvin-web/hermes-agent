#!/usr/bin/env python3
"""Check the latest Hermes SLO snapshot and notify Discord on state changes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli.observability_slo import DEFAULT_LATEST, SLO_DEFINITIONS, utc_iso
from hermes_constants import get_hermes_home

NOTIFY = Path.home() / ".hermes" / "scripts" / "discord-notify.sh"
DEFAULT_REMINDER_SECONDS = 6 * 60 * 60
STATE_SCHEMA_VERSION = 2


def default_state_path() -> Path:
    """Resolve the active profile's alert state at runtime."""
    return get_hermes_home() / "observability" / "slo-alert-state.json"


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


def load_state(path: Path) -> dict[str, Any]:
    """Load incident state; a missing or malformed file starts fail-open."""
    path = path.expanduser()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"slo-alert warning: ignoring unreadable state {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(value, dict):
        print(f"slo-alert warning: ignoring non-object state {path}", file=sys.stderr)
        return {}
    return value


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically persist state beside the SLO snapshot."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise


def breaches(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = snapshot.get("metrics") or {}
    found = []
    for key, spec in SLO_DEFINITIONS.items():
        if not spec.get("page", True):
            continue  # informational metric (e.g. mixed-lane p95) — tracked, not paged
        value = metrics.get(key)
        if value is None:
            if key == "recall_hit_rate":
                found.append(
                    {
                        "metric": key,
                        "value": "no_data",
                        "target": spec["target"],
                        "severity": "warn",
                    }
                )
            continue
        critical = spec.get("critical")
        if key == "recall_hit_rate":
            bad = float(value) < float(critical)
        else:
            bad = float(value) > float(critical)
        if bad:
            found.append(
                {
                    "metric": key,
                    "value": value,
                    "target": spec["target"],
                    "severity": "critical",
                }
            )
    return found


def incident_metrics(breach_rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({str(row.get("metric")) for row in breach_rows}))


def incident_fingerprint(breach_rows: list[dict[str, Any]]) -> str:
    """Identify an incident by its breached metric set, not presentation drift."""
    encoded = json.dumps(incident_metrics(breach_rows), separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def same_incident(previous: dict[str, Any], breach_rows: list[dict[str, Any]]) -> bool:
    """Match current incidents across fingerprint-algorithm migrations."""
    if previous.get("status") != "breached":
        return False
    fingerprint = incident_fingerprint(breach_rows)
    if previous.get("fingerprint") == fingerprint:
        return True
    active = previous.get("active_breaches")
    return isinstance(active, list) and incident_metrics(active) == incident_metrics(breach_rows)


def pending_notification(previous: dict[str, Any]) -> dict[str, Any] | None:
    """Return a validated undelivered breach transition, if one exists."""
    value = previous.get("pending_notification")
    if not isinstance(value, dict):
        return None
    if value.get("kind") not in {"breach", "changed"}:
        return None
    if not isinstance(value.get("breaches"), list) or not value["breaches"]:
        return None
    if not isinstance(value.get("snapshot"), dict):
        return None
    return value


def _notification_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": snapshot.get("generated_at"),
        "turn_count": snapshot.get("turn_count"),
        "sources": snapshot.get("sources") or {},
    }


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def notification_kind(
    breach_rows: list[dict[str, Any]],
    previous: dict[str, Any],
    *,
    now: datetime,
    reminder_seconds: int,
) -> str | None:
    """Return breach/changed/reminder/recovery, or None for a quiet poll."""
    pending = pending_notification(previous)
    if pending is not None:
        return "pending_breach"
    if not breach_rows:
        return "recovery" if previous.get("status") == "breached" else None

    if previous.get("status") != "breached":
        return "breach"
    if not same_incident(previous, breach_rows):
        return "changed"

    last_notified = _parse_time(previous.get("last_notified_at"))
    if last_notified is None:
        return "breach"
    if (now.astimezone(timezone.utc) - last_notified).total_seconds() >= reminder_seconds:
        return "reminder"
    return None


def updated_state(
    snapshot: dict[str, Any],
    breach_rows: list[dict[str, Any]],
    previous: dict[str, Any],
    *,
    now: datetime,
    notification_kind: str | None,
    notification_sent: bool,
) -> dict[str, Any]:
    """Build the next state while preserving ordered delivery retries."""
    now_iso = now.astimezone(timezone.utc).isoformat()
    snapshot_at = snapshot.get("generated_at")

    if notification_kind == "pending_breach":
        state = dict(previous)
        state["last_observed_at"] = now_iso
        state["snapshot_generated_at"] = snapshot_at
        if not breach_rows:
            state["pending_recovery_at"] = now_iso
        pending = pending_notification(previous)
        if notification_sent:
            state["last_notified_at"] = now_iso
            state["last_notification_kind"] = (
                pending.get("kind") if pending is not None else "breach"
            )
            state["pending_notification"] = None
        return state

    if breach_rows:
        fingerprint = incident_fingerprint(breach_rows)
        same_identity = same_incident(previous, breach_rows)
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "status": "breached",
            "fingerprint": fingerprint,
            "active_breaches": breach_rows,
            "first_detected_at": (
                previous.get("first_detected_at") if same_identity else now_iso
            ),
            "last_observed_at": now_iso,
            "snapshot_generated_at": snapshot_at,
            "last_notified_at": (
                previous.get("last_notified_at") if same_identity else None
            ),
            "last_notification_kind": (
                previous.get("last_notification_kind") if same_identity else None
            ),
            "pending_notification": (
                previous.get("pending_notification") if same_identity else None
            ),
        }
        if notification_kind and notification_sent:
            state["last_notified_at"] = now_iso
            state["last_notification_kind"] = notification_kind
            state["pending_notification"] = None
        elif notification_kind in {"breach", "changed"}:
            state["pending_notification"] = {
                "kind": notification_kind,
                "fingerprint": fingerprint,
                "breaches": breach_rows,
                "snapshot": _notification_snapshot(snapshot),
                "detected_at": now_iso,
            }
        return state

    if previous.get("status") == "breached" and not (
        notification_kind == "recovery" and notification_sent
    ):
        # Keep the incident open so a failed recovery notification is retried.
        state = dict(previous)
        state["last_observed_at"] = now_iso
        state["snapshot_generated_at"] = snapshot_at
        state["pending_recovery_at"] = now_iso
        return state

    resolved = previous.get("active_breaches", []) if previous else []
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": "healthy",
        "fingerprint": None,
        "active_breaches": [],
        "resolved_breaches": resolved,
        "last_observed_at": now_iso,
        "snapshot_generated_at": snapshot_at,
        "last_resolved_at": (
            now_iso if previous.get("status") == "breached" else previous.get("last_resolved_at")
        ),
        "pending_notification": None,
    }
    if notification_kind == "recovery" and notification_sent:
        state["last_notified_at"] = now_iso
        state["last_notification_kind"] = "recovery"
    return state


def render_alert(
    snapshot: dict[str, Any],
    breach_rows: list[dict[str, Any]],
    notification_kind: str = "breach",
) -> str:
    title = {
        "changed": "🚨 Hermes SLO breach changed",
        "reminder": "⏰ Hermes SLO breach persists",
    }.get(notification_kind, "🚨 Hermes SLO breach")
    lines = [
        title,
        f"generated_at={snapshot.get('generated_at')}",
        f"turn_count={snapshot.get('turn_count')}",
        "breaches:",
    ]
    for row in breach_rows:
        lines.append(
            f"- {row['severity']}: {row['metric']}={row['value']} target {row['target']}"
        )
    sources = snapshot.get("sources") or {}
    if sources:
        lines.append(
            "sources: "
            + ", ".join(
                f"{key}={value}"
                for key, value in sorted(sources.items())
                if key in {"state_db", "latest", "synthetic"}
            )
        )
    if notification_kind == "reminder":
        lines.append("action: incident remains open; duplicate pages are suppressed between reminders.")
    else:
        lines.append(
            "action: inspect `~/.hermes/observability/slo-latest.json` and recent gateway journal/Loki streams."
        )
    return "\n".join(lines)


def render_recovery(snapshot: dict[str, Any], previous: dict[str, Any]) -> str:
    lines = [
        "✅ Hermes SLO recovered",
        f"generated_at={snapshot.get('generated_at')}",
        "resolved:",
    ]
    for row in previous.get("active_breaches", []):
        lines.append(f"- {row.get('metric')}")
    lines.append(f"incident_started_at={previous.get('first_detected_at')}")
    lines.append("action: no action required; monitoring continues.")
    return "\n".join(lines)


def notify(message: str, *, dry_run: bool) -> int:
    env = os.environ.copy()
    if dry_run:
        env["DISCORD_NOTIFY_DRYRUN"] = "1"
    proc = subprocess.run(
        [str(NOTIFY), message],
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check Hermes SLO latest.json and Discord-alert on incident transitions"
    )
    parser.add_argument("--latest", default=str(DEFAULT_LATEST))
    parser.add_argument(
        "--state",
        default=None,
        help="incident state path (default: active HERMES_HOME observability directory)",
    )
    parser.add_argument(
        "--reminder-seconds",
        type=int,
        default=int(os.environ.get("HERMES_SLO_REMINDER_SECONDS", DEFAULT_REMINDER_SECONDS)),
        help="minimum seconds between unchanged-incident reminders (default: 21600)",
    )
    parser.add_argument("--dry-run", action="store_true", help="force DISCORD_NOTIFY_DRYRUN=1")
    parser.add_argument(
        "--synthetic-breach",
        action="store_true",
        help="render a guaranteed breach without reading latest.json",
    )
    parser.add_argument(
        "--print-only", action="store_true", help="render notification but do not notify or persist state"
    )
    args = parser.parse_args(argv)
    if args.reminder_seconds <= 0:
        parser.error("--reminder-seconds must be positive")

    explicit_state = args.state is not None
    state_path = Path(args.state).expanduser() if explicit_state else default_state_path()
    snapshot = synthetic_snapshot() if args.synthetic_breach else load_snapshot(Path(args.latest))
    isolated_probe = not explicit_state and (
        args.synthetic_breach or args.dry_run or args.print_only
    )
    # Verification probes must not even read profile-live incident state. An
    # explicit --state opts into an isolated state machine for transition tests.
    previous = {} if isolated_probe else load_state(state_path)
    breach_rows = breaches(snapshot)
    now = datetime.now(timezone.utc)
    kind = notification_kind(
        breach_rows,
        previous,
        now=now,
        reminder_seconds=args.reminder_seconds,
    )

    # Synthetic/dry-run probes cannot alter live incident state unless the
    # operator explicitly supplies a separate --state path.
    persist_state = not args.print_only and (
        not (args.synthetic_breach or args.dry_run) or explicit_state
    )

    if kind is None:
        state = updated_state(
            snapshot,
            breach_rows,
            previous,
            now=now,
            notification_kind=None,
            notification_sent=False,
        )
        if persist_state:
            save_state(state_path, state)
        if breach_rows:
            metrics = ",".join(sorted(str(row["metric"]) for row in breach_rows))
            print(
                f"slo-alert suppressed: unchanged incident ({metrics}); "
                f"reminder interval={args.reminder_seconds}s"
            )
        else:
            print("slo-alert ok: no breaches")
        return 0

    pending = pending_notification(previous) if kind == "pending_breach" else None
    if kind == "pending_breach" and pending is not None:
        message = render_alert(
            pending["snapshot"], pending["breaches"], pending["kind"]
        )
    elif kind == "recovery":
        message = render_recovery(snapshot, previous)
    else:
        message = render_alert(snapshot, breach_rows, kind)
    print(message)
    if args.print_only:
        return 2

    try:
        rc = notify(message, dry_run=args.dry_run)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"slo-alert delivery failed: {exc}", file=sys.stderr)
        rc = 1
    state = updated_state(
        snapshot,
        breach_rows,
        previous,
        now=now,
        notification_kind=kind,
        notification_sent=rc == 0,
    )
    if persist_state:
        save_state(state_path, state)
    if rc == 0:
        return 2
    # systemd declares exit 2 successful for "breach found and delivered";
    # never let a notifier failure with the same rc masquerade as delivery.
    return 1 if rc == 2 else rc


if __name__ == "__main__":
    raise SystemExit(main())
