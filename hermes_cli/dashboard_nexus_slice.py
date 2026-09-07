"""Thin Nexus live slice for backup -> off-box replication truth.

GET /api/dashboard/nexus/slice/backup-offbox returns one normalized truth
object.  It reuses the OS dashboard's backup/off-box probes and never mutates
backup state.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from hermes_cli import dashboard_os

router = APIRouter(prefix="/api/dashboard/nexus", tags=["dashboard-nexus-slice"])

TRUTH_ID = "edge:nightly-backup->off-box"
TRUTH_KEYS = (
    "id",
    "probe_state",
    "freshness_age_s",
    "confidence",
    "evidence_refs",
    "last_checked",
    "safe_next_action",
    "locked_actions",
    "what_would_prove_green",
    "what_breaks_if_false",
)
PROBE_STATES = {"broken", "stale", "manual", "live", "unknown", "gated"}
_TTL_S = 20.0
_CACHE: tuple[dict[str, Any], float] | None = None
_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(path: Path) -> float:
    return max(0.0, time.time() - path.stat().st_mtime)


def _find_newest_backup(mvms_dir: Path, pattern: str) -> Path | None:
    matches = sorted(mvms_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _offbox_marker_candidates(mvms_dir: Path) -> list[Path]:
    return [
        mvms_dir / "OFFBOX-REPLICATION-OK",
        mvms_dir / "offbox-replication.ok",
        dashboard_os.HERMES_HOME / "audits" / "veracrypt-backup" / "OFFBOX-REPLICATION-OK",
    ]


def _item_by_name(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("name") == name), None)


def _status_to_probe_state(status: str, marker_present: bool) -> str:
    if not marker_present:
        return "broken"
    if status == "green":
        return "live"
    if status in {"amber", "red"}:
        return "stale"
    if status == "unknown":
        return "unknown"
    return "manual"


def _backup_refs(mvms_dir: Path) -> tuple[list[str], float]:
    refs: list[str] = []
    ages: list[float] = []
    for pattern in (
        "mvms-canonical-*.sql.gz",
        "honcho-live-store-*.sql.gz",
        "hermes-app-state-*.tar.gz",
    ):
        newest = _find_newest_backup(mvms_dir, pattern)
        if newest is None:
            refs.append(f"backup:{pattern}:missing")
            continue
        ages.append(_age_seconds(newest))
        refs.append(str(newest))
    return refs, max(ages) if ages else 0.0


def _build_truth_object() -> dict[str, Any]:
    last_checked = _iso_now()
    mvms_dir = dashboard_os.HERMES_HOME / "backups" / "mvms"

    try:
        # Reuse the OS dashboard internals so this slice cannot drift from the
        # existing backup-age/off-box-marker/VeraCrypt logic.
        backup_section = dashboard_os._section_backups()
        dr_status = dashboard_os._dr_status()

        items = backup_section.get("items") if isinstance(backup_section, dict) else []
        if not isinstance(items, list):
            items = []
        offbox_item = _item_by_name(items, "mvms-backup-gap-offbox") or {}
        vera_item = _item_by_name(items, "veracrypt_weekly") or {}

        marker_candidates = _offbox_marker_candidates(mvms_dir)
        marker = next((candidate for candidate in marker_candidates if candidate.exists()), None)
        marker_present = marker is not None
        marker_refs = [str(candidate) for candidate in marker_candidates]
        backup_refs, backup_age_s = _backup_refs(mvms_dir)
        freshness_age_s = _age_seconds(marker) if marker_present else backup_age_s
        state = _status_to_probe_state(str(offbox_item.get("status") or "unknown"), marker_present)

        evidence_refs = [*backup_refs, f"marker-scan:{'|'.join(marker_refs)}"]
        if offbox_item:
            evidence_refs.append(f"offbox:{offbox_item.get('status')}:{offbox_item.get('detail')}")
        if vera_item:
            evidence_refs.append(f"veracrypt:{vera_item.get('status')}:{vera_item.get('detail')}")
        evidence_refs.append(
            f"dr-status:{dr_status.get('returncode', 'n/a')}:{dr_status.get('status', 'unknown')}:{dr_status.get('detail', '')}"
        )

        confidence = "corroborated" if dr_status.get("status") in {"red", "amber", "green"} else "single-probe"
        return {
            "id": TRUTH_ID,
            "probe_state": state,
            "freshness_age_s": round(float(freshness_age_s), 3),
            "confidence": confidence,
            "evidence_refs": evidence_refs,
            "last_checked": last_checked,
            "safe_next_action": "re-probe after tonight's 02:30 dump window",
            "locked_actions": [
                "force replication run (G2: destination+credentials — josep)",
                "any remediation",
            ],
            "what_would_prove_green": "replication success marker present and fresher than the newest local dump",
            "what_breaks_if_false": "box loss loses all shared memory newer than the last off-box copy",
        }
    except Exception as exc:  # pragma: no cover - exercised via monkeypatch raising tests
        return {
            "id": TRUTH_ID,
            "probe_state": "unknown",
            "freshness_age_s": 0.0,
            "confidence": "single-probe",
            "evidence_refs": [f"exception:{type(exc).__name__}:{exc}"],
            "last_checked": last_checked,
            "safe_next_action": "re-probe after tonight's 02:30 dump window",
            "locked_actions": [
                "force replication run (G2: destination+credentials — josep)",
                "any remediation",
            ],
            "what_would_prove_green": "replication success marker present and fresher than the newest local dump",
            "what_breaks_if_false": "box loss loses all shared memory newer than the last off-box copy",
        }


def _validate_truth_object(payload: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in TRUTH_KEYS if key not in payload]
    if missing:
        raise ValueError(f"missing truth keys: {missing}")
    if payload["id"] != TRUTH_ID:
        raise ValueError(f"unexpected truth id: {payload['id']}")
    if payload["probe_state"] not in PROBE_STATES:
        raise ValueError(f"invalid probe_state: {payload['probe_state']}")
    return {key: payload[key] for key in TRUTH_KEYS}


@router.get("/slice/backup-offbox")
def backup_offbox_slice() -> dict[str, Any]:
    """Return the cached, never-500 backup -> off-box truth object."""
    global _CACHE
    now = time.monotonic()
    with _LOCK:
        if _CACHE is not None and now < _CACHE[1]:
            return _CACHE[0]
        payload = _validate_truth_object(_build_truth_object())
        _CACHE = (payload, now + _TTL_S)
        return payload


def clear_cache_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None
