"""Cron reliability dashboard backend.

Exposes ``GET /api/cron/reliability`` as a read-only health contract for two
scheduler planes:

* Hermes cron jobs in ``$HERMES_HOME/cron/jobs.json``.
* systemd ``--user`` timers and their activated services, sourced from
  ``systemctl --user show`` and read-only ``journalctl --user -u`` output.

The route intentionally does not mutate cron state, systemd units, timers, or
journals. It is designed for dashboard surfacing of missed/overdue runs,
non-zero exits, restart counts, failure excerpts, and recent success-rate
trends.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/cron", tags=["cron-reliability"])

DEFAULT_HISTORY_LIMIT = 80
MAX_HISTORY_LIMIT = 300
_STANDALONE_SERVICE_ALLOWLIST = {"mvms-supabase.service"}
_OK_RESULTS = {"", "success", "exit-code"}  # exit-code is interpreted with status/code.
_FAILURE_WORDS = ("failed", "failure", "traceback", "error", "exception")


def _utc_iso(ts: float | int | None) -> str | None:
    if ts is None:
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _parse_iso_ts(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _epoch_from_systemd_json(value: Any) -> float | None:
    """Return epoch seconds from systemctl JSON timestamp fields.

    ``systemctl list-timers --output=json`` emits absolute realtime timestamps
    as integer microseconds. Missing values are usually ``0`` or absent.
    """
    if value in (None, "", 0, "0"):
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return None
    if ivalue <= 0:
        return None
    # Absolute systemd realtime timestamps are microseconds since Unix epoch.
    if ivalue > 10_000_000_000:
        return ivalue / 1_000_000.0
    return float(ivalue)


def _safe_run(cmd: list[str], *, timeout: float = 6.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _parse_key_value(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def _systemctl_show(unit: str, properties: list[str] | None = None) -> dict[str, str]:
    cmd = ["systemctl", "--user", "show", unit, "--no-pager"]
    if properties:
        for prop in properties:
            cmd.append(f"--property={prop}")
    proc = _safe_run(cmd)
    if proc.returncode != 0:
        return {"_error": (proc.stderr or proc.stdout or "systemctl show failed").strip()}
    return _parse_key_value(proc.stdout)


def _journal_lines(unit: str, limit: int) -> list[str]:
    safe_limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    proc = _safe_run(
        [
            "journalctl",
            "--user",
            "-u",
            unit,
            "-n",
            str(safe_limit),
            "--no-pager",
            "-o",
            "short-iso",
        ],
        timeout=8.0,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _parse_journal_events(lines: list[str], *, limit: int = 20) -> list[dict[str, Any]]:
    """Extract run outcome events from journalctl lines.

    The parser is intentionally conservative: it records success on systemd's
    ``Finished <unit>`` / ``Deactivated successfully`` messages, and records
    failures on ``Main process exited`` / ``Failed with result`` /
    ``Failed to start`` messages. Failure timestamps are deduplicated within a
    small window because one failed run often emits several failure lines.
    """
    events: list[dict[str, Any]] = []
    last_failure_ts: str | None = None
    for line in lines:
        lower = line.lower()
        timestamp = line.split(" ", 1)[0] if " " in line else None
        exit_code: int | None = None
        status = None

        status_match = re.search(r"status=(\d+)(?:/|\b)", line)
        if status_match:
            try:
                exit_code = int(status_match.group(1))
            except ValueError:
                exit_code = None

        if "main process exited" in lower or "failed with result" in lower or "failed to start" in lower:
            # Avoid triple-counting the same failed invocation.
            if timestamp and timestamp == last_failure_ts and events and events[-1]["status"] == "failure":
                previous_excerpt = events[-1].get("excerpt") or ""
                events[-1]["excerpt"] = _compact_excerpt(f"{previous_excerpt} | {line}")
                if exit_code is not None:
                    events[-1]["exit_code"] = exit_code
                continue
            status = "failure"
            last_failure_ts = timestamp
        elif "finished " in lower or "deactivated successfully" in lower:
            status = "success"
            exit_code = 0

        if status:
            events.append(
                {
                    "timestamp": timestamp,
                    "status": status,
                    "exit_code": exit_code,
                    "excerpt": _compact_excerpt(line),
                }
            )

    return events[-limit:]


def _compact_excerpt(text: Any, *, max_len: int = 500) -> str | None:
    if text in (None, ""):
        return None
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1] + "…"


def _success_rate(events: list[dict[str, Any]], *, window: int = 20) -> dict[str, Any]:
    outcomes = [e for e in events if e.get("status") in {"success", "failure"}]
    recent = outcomes[-window:]
    successes = sum(1 for e in recent if e.get("status") == "success")
    failures = sum(1 for e in recent if e.get("status") == "failure")
    total = successes + failures
    return {
        "window": window,
        "observed": total,
        "successes": successes,
        "failures": failures,
        "rate": round(successes / total, 4) if total else None,
    }


def _grace_seconds(interval_seconds: float | None) -> int:
    if interval_seconds and interval_seconds > 0:
        return int(max(120, min(interval_seconds / 2.0, 7200)))
    return 300


def _freshness(
    *,
    now_ts: float,
    last_run_ts: float | None,
    next_run_ts: float | None,
    interval_seconds: float | None,
    enabled: bool = True,
) -> dict[str, Any]:
    if not enabled:
        return {
            "state": "paused",
            "missed": False,
            "overdue": False,
            "lateness_seconds": 0,
            "interval_seconds": interval_seconds,
            "grace_seconds": _grace_seconds(interval_seconds),
        }

    grace = _grace_seconds(interval_seconds)
    lateness = 0.0
    overdue = False
    missed = False

    if next_run_ts is not None:
        lateness = max(0.0, now_ts - next_run_ts)
        overdue = lateness > 0
        missed = lateness > grace
    elif last_run_ts is not None and interval_seconds:
        expected_next = last_run_ts + interval_seconds
        lateness = max(0.0, now_ts - expected_next)
        overdue = lateness > 0
        missed = lateness > grace

    state = "ok"
    if missed:
        state = "missed"
    elif overdue:
        state = "overdue"
    elif next_run_ts is None and last_run_ts is None:
        state = "unknown"

    return {
        "state": state,
        "missed": bool(missed),
        "overdue": bool(overdue),
        "lateness_seconds": int(lateness),
        "interval_seconds": int(interval_seconds) if interval_seconds else None,
        "grace_seconds": grace,
    }


def _interval_from_cron_schedule(job: dict[str, Any]) -> float | None:
    raw_schedule = job.get("schedule")
    schedule: dict[str, Any] = raw_schedule if isinstance(raw_schedule, dict) else {}
    kind = schedule.get("kind")
    if kind == "interval":
        try:
            return float(schedule.get("minutes") or 0) * 60.0
        except (TypeError, ValueError):
            return None
    # For cron expressions, derive a cheap observed interval when the persisted
    # last/next timestamps both exist. This avoids importing croniter here and
    # keeps the dashboard endpoint read-only/low-risk.
    last_ts = _parse_iso_ts(job.get("last_run_at"))
    next_ts = _parse_iso_ts(job.get("next_run_at"))
    if last_ts and next_ts and next_ts > last_ts:
        return next_ts - last_ts
    return None


def _cron_job_health(job: dict[str, Any], *, now_ts: float) -> dict[str, Any]:
    last_status = job.get("last_status")
    last_error = job.get("last_error") or job.get("last_delivery_error")
    enabled = bool(job.get("enabled", True)) and job.get("state") != "paused"
    last_run_ts = _parse_iso_ts(job.get("last_run_at"))
    next_run_ts = _parse_iso_ts(job.get("next_run_at"))
    interval_seconds = _interval_from_cron_schedule(job)
    freshness = _freshness(
        now_ts=now_ts,
        last_run_ts=last_run_ts,
        next_run_ts=next_run_ts,
        interval_seconds=interval_seconds,
        enabled=enabled,
    )

    exit_code = None
    exit_label = None
    if last_status == "ok":
        exit_code = 0
        exit_label = "ok"
    elif last_status:
        exit_code = 1
        exit_label = str(last_status)

    failure_excerpt = _compact_excerpt(last_error)
    events: list[dict[str, Any]] = []
    if last_status:
        events.append(
            {
                "timestamp": job.get("last_run_at"),
                "status": "success" if last_status == "ok" else "failure",
                "exit_code": exit_code,
                "excerpt": failure_excerpt,
            }
        )

    health = "green"
    reasons: list[str] = []
    if last_status and last_status != "ok":
        health = "red"
        reasons.append("last_status_non_ok")
    if freshness["missed"]:
        health = "red"
        reasons.append("missed_run")
    elif freshness["overdue"] and health != "red":
        health = "amber"
        reasons.append("overdue")
    if not enabled and health == "green":
        health = "gray"
        reasons.append("disabled_or_paused")

    return {
        "id": str(job.get("id") or ""),
        "name": job.get("name") or job.get("id") or "cron job",
        "kind": "hermes-cron-job",
        "source": "jobs.json",
        "profile": job.get("profile") or "default",
        "enabled": enabled,
        "schedule": job.get("schedule"),
        "schedule_display": job.get("schedule_display") or (job.get("schedule") or {}).get("display"),
        "last_run": job.get("last_run_at"),
        "next_run": job.get("next_run_at"),
        "last_exit_status": {
            "code": exit_code,
            "label": exit_label,
            "source": "jobs.json:last_status",
        },
        "n_restarts": 0,
        "last_failure_excerpt": failure_excerpt,
        "freshness_vs_interval": freshness,
        "success_rate": _success_rate(events),
        "history": events,
        "health": health,
        "reasons": reasons,
    }


def _load_cron_jobs() -> tuple[list[dict[str, Any]], Path, str | None]:
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"
    path = home / "cron" / "jobs.json"
    if not path.exists():
        return [], path, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"), strict=False)
    except Exception as exc:
        return [], path, str(exc)
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return [], path, "jobs.json did not contain a list"
    return [j for j in jobs if isinstance(j, dict)], path, None


def _list_user_timers() -> tuple[list[dict[str, Any]], str | None]:
    proc = _safe_run(["systemctl", "--user", "list-timers", "--all", "--no-pager", "--output=json"])
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout or "systemctl list-timers failed").strip()
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"could not parse systemctl timer JSON: {exc}"
    if not isinstance(payload, list):
        return [], "systemctl timer JSON was not a list"
    return [row for row in payload if isinstance(row, dict)], None


def _exit_from_show(props: dict[str, str], events: list[dict[str, Any]]) -> dict[str, Any]:
    result = (props.get("Result") or "").strip()
    code_raw = props.get("ExecMainStatus") or props.get("StatusErrno") or ""
    try:
        code = int(code_raw) if str(code_raw).strip() != "" else None
    except ValueError:
        code = None

    # If systemd has since recorded a successful service state but the recent
    # journal window contains a newer failure event, keep that history visible
    # without pretending it is the current ExecMainStatus.
    return {
        "code": code,
        "label": result or None,
        "exec_main_code": props.get("ExecMainCode") or None,
        "source": "systemctl --user show",
    }


def _last_failure_excerpt(events: list[dict[str, Any]], fallback_lines: list[str]) -> str | None:
    for event in reversed(events):
        if event.get("status") == "failure":
            return _compact_excerpt(event.get("excerpt"))
    for line in reversed(fallback_lines):
        if any(word in line.lower() for word in _FAILURE_WORDS):
            return _compact_excerpt(line)
    return None


def _unit_health(
    *,
    exit_status: dict[str, Any],
    freshness: dict[str, Any],
    events: list[dict[str, Any]],
    show_error: str | None = None,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    health = "green"
    code = exit_status.get("code")
    label = str(exit_status.get("label") or "").lower()
    if show_error:
        health = "red"
        reasons.append("systemctl_show_error")
    if code not in (None, 0):
        health = "red"
        reasons.append("last_exit_non_zero")
    elif label and label not in _OK_RESULTS:
        health = "red"
        reasons.append("systemd_result_non_success")
    if events and events[-1].get("status") == "failure":
        health = "red"
        reasons.append("latest_history_failure")
    if freshness.get("missed"):
        health = "red"
        reasons.append("missed_run")
    elif freshness.get("overdue") and health != "red":
        health = "amber"
        reasons.append("overdue")
    if freshness.get("state") == "unknown" and health == "green":
        health = "gray"
        reasons.append("freshness_unknown")
    return health, reasons


def _timer_health(row: dict[str, Any], *, now_ts: float, history_limit: int) -> dict[str, Any]:
    timer_unit = str(row.get("unit") or row.get("unit_name") or "").strip()
    service_unit = str(row.get("activates") or row.get("Unit") or "").strip()
    next_ts = _epoch_from_systemd_json(row.get("next"))
    last_ts = _epoch_from_systemd_json(row.get("last"))
    interval = (next_ts - last_ts) if next_ts and last_ts and next_ts > last_ts else None

    timer_props = _systemctl_show(
        timer_unit,
        ["ActiveState", "SubState", "Result", "NRestarts", "Unit", "LastTriggerUSec", "NextElapseUSecRealtime"],
    ) if timer_unit else {"_error": "missing timer unit"}
    if not service_unit:
        service_unit = timer_props.get("Unit", "")
    service_props = _systemctl_show(
        service_unit,
        [
            "ActiveState",
            "SubState",
            "Result",
            "ExecMainCode",
            "ExecMainStatus",
            "NRestarts",
            "Restart",
            "InvocationID",
        ],
    ) if service_unit else {"_error": "missing activated service"}

    journal_unit = service_unit or timer_unit
    lines = _journal_lines(journal_unit, history_limit) if journal_unit else []
    events = _parse_journal_events(lines)
    exit_status = _exit_from_show(service_props, events)
    try:
        restarts = int(service_props.get("NRestarts") or timer_props.get("NRestarts") or 0)
    except ValueError:
        restarts = 0

    freshness = _freshness(
        now_ts=now_ts,
        last_run_ts=last_ts,
        next_run_ts=next_ts,
        interval_seconds=interval,
        enabled=timer_props.get("ActiveState") != "inactive",
    )
    show_error = timer_props.get("_error") or service_props.get("_error")
    health, reasons = _unit_health(
        exit_status=exit_status,
        freshness=freshness,
        events=events,
        show_error=show_error,
    )
    return {
        "id": timer_unit,
        "name": timer_unit,
        "kind": "systemd-user-timer",
        "source": "systemctl --user list-timers/show + journalctl --user",
        "unit": timer_unit,
        "service_unit": service_unit or None,
        "active_state": timer_props.get("ActiveState") or None,
        "sub_state": timer_props.get("SubState") or None,
        "last_run": _utc_iso(last_ts),
        "next_run": _utc_iso(next_ts),
        "last_exit_status": exit_status,
        "n_restarts": restarts,
        "last_failure_excerpt": _last_failure_excerpt(events, lines),
        "freshness_vs_interval": freshness,
        "success_rate": _success_rate(events),
        "history": events,
        "health": health,
        "reasons": reasons,
        "errors": [show_error] if show_error else [],
    }


def _standalone_service_health(unit: str, *, now_ts: float, history_limit: int) -> dict[str, Any]:
    props = _systemctl_show(
        unit,
        [
            "ActiveState",
            "SubState",
            "Result",
            "ExecMainCode",
            "ExecMainStatus",
            "NRestarts",
            "Restart",
            "InvocationID",
            "InactiveEnterTimestamp",
            "ActiveEnterTimestamp",
        ],
    )
    lines = _journal_lines(unit, history_limit)
    events = _parse_journal_events(lines)
    exit_status = _exit_from_show(props, events)
    try:
        restarts = int(props.get("NRestarts") or 0)
    except ValueError:
        restarts = 0
    freshness = _freshness(
        now_ts=now_ts,
        last_run_ts=None,
        next_run_ts=None,
        interval_seconds=None,
        enabled=props.get("ActiveState") not in {"inactive", "failed"},
    )
    show_error = props.get("_error")
    health, reasons = _unit_health(
        exit_status=exit_status,
        freshness=freshness,
        events=events,
        show_error=show_error,
    )
    return {
        "id": unit,
        "name": unit,
        "kind": "systemd-user-service",
        "source": "systemctl --user show + journalctl --user",
        "unit": unit,
        "service_unit": unit,
        "active_state": props.get("ActiveState") or None,
        "sub_state": props.get("SubState") or None,
        "last_run": None,
        "next_run": None,
        "last_exit_status": exit_status,
        "n_restarts": restarts,
        "last_failure_excerpt": _last_failure_excerpt(events, lines),
        "freshness_vs_interval": freshness,
        "success_rate": _success_rate(events),
        "history": events,
        "health": health,
        "reasons": reasons,
        "errors": [show_error] if show_error else [],
    }


def build_reliability_snapshot(*, history_limit: int = DEFAULT_HISTORY_LIMIT) -> dict[str, Any]:
    """Build the documented cron reliability response shape."""
    now_ts = time.time()
    safe_history_limit = max(1, min(int(history_limit), MAX_HISTORY_LIMIT))
    warnings: list[str] = []

    cron_jobs, jobs_path, jobs_error = _load_cron_jobs()
    if jobs_error:
        warnings.append(f"jobs.json read error: {jobs_error}")
    cron_health = [_cron_job_health(job, now_ts=now_ts) for job in cron_jobs]

    timer_rows, timer_error = _list_user_timers()
    if timer_error:
        warnings.append(timer_error)
    timer_health = [
        _timer_health(row, now_ts=now_ts, history_limit=safe_history_limit)
        for row in timer_rows
        if row.get("unit") or row.get("activates")
    ]

    timer_services = {item.get("service_unit") for item in timer_health}
    standalone_services = sorted(_STANDALONE_SERVICE_ALLOWLIST - {s for s in timer_services if s})
    service_health = [
        _standalone_service_health(unit, now_ts=now_ts, history_limit=safe_history_limit)
        for unit in standalone_services
    ]

    units = [*cron_health, *timer_health, *service_health]
    counts_by_health = {"green": 0, "amber": 0, "red": 0, "gray": 0}
    for item in units:
        counts_by_health[item.get("health", "gray")] = counts_by_health.get(item.get("health", "gray"), 0) + 1

    return {
        "generated_at": _utc_iso(now_ts),
        "history_limit": safe_history_limit,
        "sources": {
            "jobs_json": str(jobs_path),
            "systemd_timers": "systemctl --user list-timers --all --output=json",
            "systemd_show": "systemctl --user show <unit>",
            "journal": "journalctl --user -u <unit> -n N -o short-iso",
        },
        "summary": {
            "cron_jobs": len(cron_health),
            "systemd_timers": len(timer_health),
            "systemd_services": len(service_health),
            "total_units": len(units),
            "by_health": counts_by_health,
        },
        "cron_jobs": cron_health,
        "systemd_timers": timer_health,
        "systemd_services": service_health,
        "units": units,
        "warnings": warnings,
    }


@router.get("/reliability")
def get_cron_reliability(limit: int = DEFAULT_HISTORY_LIMIT) -> dict[str, Any]:
    """Read-only cron/timer reliability snapshot for the dashboard."""
    try:
        return build_reliability_snapshot(history_limit=limit)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive API boundary
        raise HTTPException(status_code=500, detail=f"cron reliability snapshot failed: {exc}") from exc
