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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_cli.observability_slo import DEFAULT_LATEST, SLO_DEFINITIONS, utc_iso
from hermes_constants import get_hermes_home

NOTIFY = Path.home() / ".hermes" / "scripts" / "discord-notify.sh"
DEFAULT_REMINDER_SECONDS = 6 * 60 * 60
DEFAULT_DELIVERY_RETRY_SECONDS = 60 * 60
DEFAULT_NOTIFY_ATTEMPTS = 3
DEFAULT_NOTIFY_RETRY_DELAY_SECONDS = 1.0
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 15
STATE_SCHEMA_VERSION = 3


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
        "recall": snapshot.get("recall") or {},
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


def _effective_delivery_kind(kind: str, previous: dict[str, Any]) -> str:
    if kind == "pending_breach":
        pending = pending_notification(previous)
        if pending is not None:
            return str(pending.get("kind") or "breach")
    return kind


def _pending_matches_breaches(
    pending: dict[str, Any], breach_rows: list[dict[str, Any]]
) -> bool:
    """Return whether a queued transition still describes the live metric set."""
    return incident_metrics(pending["breaches"]) == incident_metrics(breach_rows)


def _delivery_retry_blocked(
    previous: dict[str, Any],
    *,
    kind: str,
    now: datetime,
    breach_rows: list[dict[str, Any]] | None = None,
) -> bool:
    """Suppress only a repeat of the same failed delivery until its retry time."""
    effective_kind = _effective_delivery_kind(kind, previous)
    if previous.get("last_delivery_failure_kind") != effective_kind:
        return False
    pending = pending_notification(previous)
    if (
        kind in {"breach", "changed"}
        and breach_rows
        and pending is not None
        and not _pending_matches_breaches(pending, breach_rows)
    ):
        # A materially different live incident supersedes the stale queued
        # payload. Its notification is not a duplicate of the failed attempt.
        return False
    retry_at = _parse_time(previous.get("next_delivery_retry_at"))
    return retry_at is not None and now.astimezone(timezone.utc) < retry_at


def _clear_delivery_failure(state: dict[str, Any]) -> None:
    for key in (
        "last_delivery_failed_at",
        "last_delivery_failure_kind",
        "next_delivery_retry_at",
        "delivery_failure_count",
    ):
        state.pop(key, None)


def _mark_delivery_failure(
    state: dict[str, Any],
    previous: dict[str, Any],
    *,
    kind: str,
    now: datetime,
    delivery_retry_seconds: int,
) -> None:
    effective_kind = _effective_delivery_kind(kind, previous)
    same_failure = previous.get("last_delivery_failure_kind") == effective_kind
    prior_count = int(previous.get("delivery_failure_count") or 0) if same_failure else 0
    state["last_delivery_failed_at"] = now.astimezone(timezone.utc).isoformat()
    state["last_delivery_failure_kind"] = effective_kind
    state["next_delivery_retry_at"] = (
        now.astimezone(timezone.utc) + timedelta(seconds=delivery_retry_seconds)
    ).isoformat()
    state["delivery_failure_count"] = prior_count + 1


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
        if breach_rows and not _pending_matches_breaches(pending, breach_rows):
            # Supersede stale queued content with the live identity. Retain the
            # original transition label: an initial page that never landed is
            # still a breach, while an undelivered change remains a change.
            kind: str | None = str(pending["kind"])
        else:
            kind = "pending_breach"
    elif not breach_rows:
        kind = "recovery" if previous.get("status") == "breached" else None
    elif previous.get("status") != "breached":
        kind = "breach"
    elif not same_incident(previous, breach_rows):
        kind = "changed"
    else:
        last_notified = _parse_time(previous.get("last_notified_at"))
        if last_notified is None:
            kind = "breach"
        elif (now.astimezone(timezone.utc) - last_notified).total_seconds() >= reminder_seconds:
            kind = "reminder"
        else:
            kind = None
    if kind is not None and _delivery_retry_blocked(
        previous, kind=kind, now=now, breach_rows=breach_rows
    ):
        return None
    return kind


def updated_state(
    snapshot: dict[str, Any],
    breach_rows: list[dict[str, Any]],
    previous: dict[str, Any],
    *,
    now: datetime,
    notification_kind: str | None,
    notification_sent: bool,
    delivery_retry_seconds: int = DEFAULT_DELIVERY_RETRY_SECONDS,
) -> dict[str, Any]:
    """Build the next state while preserving ordered delivery retries."""
    now_iso = now.astimezone(timezone.utc).isoformat()
    snapshot_at = snapshot.get("generated_at")

    if notification_kind == "pending_breach":
        state = dict(previous)
        state["schema_version"] = STATE_SCHEMA_VERSION
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
            _clear_delivery_failure(state)
        elif notification_kind:
            _mark_delivery_failure(
                state,
                previous,
                kind=notification_kind,
                now=now,
                delivery_retry_seconds=delivery_retry_seconds,
            )
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
        if same_identity:
            for key in (
                "last_delivery_failed_at",
                "last_delivery_failure_kind",
                "next_delivery_retry_at",
                "delivery_failure_count",
            ):
                if key in previous:
                    state[key] = previous[key]
        if previous.get("last_delivery_failure_kind") == "recovery":
            # A live breach means the undelivered recovery intent is obsolete.
            # Keep the incident open without letting stale recovery backoff
            # poison the next real transition.
            _clear_delivery_failure(state)
        if notification_kind and notification_sent:
            state["last_notified_at"] = now_iso
            state["last_notification_kind"] = notification_kind
            state["pending_notification"] = None
            _clear_delivery_failure(state)
        elif notification_kind in {"breach", "changed"}:
            state["pending_notification"] = {
                "kind": notification_kind,
                "fingerprint": fingerprint,
                "breaches": breach_rows,
                "snapshot": _notification_snapshot(snapshot),
                "detected_at": now_iso,
            }
            _mark_delivery_failure(
                state,
                previous,
                kind=notification_kind,
                now=now,
                delivery_retry_seconds=delivery_retry_seconds,
            )
        elif notification_kind:
            _mark_delivery_failure(
                state,
                previous,
                kind=notification_kind,
                now=now,
                delivery_retry_seconds=delivery_retry_seconds,
            )
        return state

    if previous.get("status") == "breached" and not (
        notification_kind == "recovery" and notification_sent
    ):
        # Keep the incident open so a failed recovery notification is retried.
        state = dict(previous)
        state["schema_version"] = STATE_SCHEMA_VERSION
        state["last_observed_at"] = now_iso
        state["snapshot_generated_at"] = snapshot_at
        state["pending_recovery_at"] = now_iso
        if notification_kind == "recovery":
            _mark_delivery_failure(
                state,
                previous,
                kind=notification_kind,
                now=now,
                delivery_retry_seconds=delivery_retry_seconds,
            )
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
                if key in {"state_db", "recall_canary", "latest", "synthetic"}
            )
        )
    recall = snapshot.get("recall") or {}
    if any(row.get("metric") == "recall_hit_rate" for row in breach_rows) and recall:
        lines.append(
            "recall_evidence: "
            f"target_misses={recall.get('target_misses')} "
            f"discrimination_warnings={recall.get('cosine_collapses')} "
            f"samples={recall.get('total')}"
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


def notify(
    message: str,
    *,
    dry_run: bool,
    attempts: int = DEFAULT_NOTIFY_ATTEMPTS,
    retry_delay_seconds: float = DEFAULT_NOTIFY_RETRY_DELAY_SECONDS,
) -> int:
    env = os.environ.copy()
    if dry_run:
        env["DISCORD_NOTIFY_DRYRUN"] = "1"
    last_rc = 1
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                [str(NOTIFY), message],
                text=True,
                capture_output=True,
                env=env,
                check=False,
                timeout=DEFAULT_NOTIFY_TIMEOUT_SECONDS,
            )
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            last_rc = proc.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            print(
                f"slo-alert delivery attempt {attempt}/{attempts} failed: {exc}",
                file=sys.stderr,
            )
            last_rc = 1
        if last_rc == 0:
            return 0
        if attempt < attempts:
            print(
                f"slo-alert delivery attempt {attempt}/{attempts} returned rc={last_rc}; retrying",
                file=sys.stderr,
            )
            time.sleep(retry_delay_seconds * attempt)
    return last_rc


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
    parser.add_argument(
        "--delivery-retry-seconds",
        type=int,
        default=int(
            os.environ.get(
                "HERMES_SLO_DELIVERY_RETRY_SECONDS", DEFAULT_DELIVERY_RETRY_SECONDS
            )
        ),
        help="minimum seconds before retrying the same failed notification (default: 3600)",
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
    if args.delivery_retry_seconds <= 0:
        parser.error("--delivery-retry-seconds must be positive")

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
            delivery_retry_seconds=args.delivery_retry_seconds,
        )
        if persist_state:
            save_state(state_path, state)
        if breach_rows:
            metrics = ",".join(sorted(str(row["metric"]) for row in breach_rows))
            failure_kind = previous.get("last_delivery_failure_kind")
            retry_at = _parse_time(previous.get("next_delivery_retry_at"))
            if (
                failure_kind
                and failure_kind != "recovery"
                and retry_at is not None
                and now < retry_at
            ):
                print(
                    "slo-alert delivery suppressed: "
                    f"{failure_kind} retry backoff until="
                    f"{previous.get('next_delivery_retry_at')}"
                )
            else:
                print(
                    f"slo-alert suppressed: unchanged incident ({metrics}); "
                    f"reminder interval={args.reminder_seconds}s"
                )
        elif previous.get("status") == "breached" and previous.get(
            "last_delivery_failure_kind"
        ) == "recovery":
            print(
                "slo-alert delivery suppressed: recovery retry backoff until="
                f"{previous.get('next_delivery_retry_at')}"
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
        delivery_retry_seconds=args.delivery_retry_seconds,
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
