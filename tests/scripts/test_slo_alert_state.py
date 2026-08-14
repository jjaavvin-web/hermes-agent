from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.observability import slo_alert_check as checker


def _now() -> datetime:
    return datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)


def _snapshot(*, generated_at: str = "2026-07-24T00:00:00+00:00") -> dict:
    return {
        "generated_at": generated_at,
        "turn_count": 6,
        "metrics": {},
        "sources": {"state_db": "/tmp/state.db"},
    }


def _rows(*metrics: str) -> list[dict]:
    return [
        {
            "metric": metric,
            "value": 1.0,
            "target": "<=0.10",
            "severity": "critical",
        }
        for metric in metrics
    ]


def test_new_breach_pages_once_then_suppresses_identical_incident() -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate", "watchdog_restart_count")

    kind = checker.notification_kind(rows, {}, now=now, reminder_seconds=6 * 3600)
    assert kind == "breach"

    state = checker.updated_state(
        _snapshot(), rows, {}, now=now, notification_kind=kind, notification_sent=True
    )
    assert state["status"] == "breached"
    assert state["last_notified_at"] == now.isoformat()

    assert (
        checker.notification_kind(
            rows,
            state,
            now=now + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        is None
    )


def test_changed_breach_set_pages_immediately() -> None:
    now = _now()
    first_rows = _rows("fallback_trigger_rate")
    state = checker.updated_state(
        _snapshot(),
        first_rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=True,
    )

    changed_rows = _rows("fallback_trigger_rate", "watchdog_restart_count")
    assert (
        checker.notification_kind(
            changed_rows,
            state,
            now=now + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        == "changed"
    )


def test_fingerprint_migration_rebinds_without_changed_page() -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate", "watchdog_restart_count")
    previous = {
        "schema_version": 1,
        "status": "breached",
        "fingerprint": "legacy-algorithm-hash",
        "active_breaches": [dict(row, value=0.5) for row in rows],
        "first_detected_at": "2026-07-23T23:00:00+00:00",
        "last_notified_at": "2026-07-23T23:15:00+00:00",
        "last_notification_kind": "breach",
        "pending_notification": None,
    }

    assert checker.same_incident(previous, rows)
    assert (
        checker.notification_kind(
            rows,
            previous,
            now=now,
            reminder_seconds=6 * 3600,
        )
        is None
    )
    rebound = checker.updated_state(
        _snapshot(),
        rows,
        previous,
        now=now,
        notification_kind=None,
        notification_sent=False,
    )
    assert rebound["fingerprint"] == checker.incident_fingerprint(rows)
    assert rebound["first_detected_at"] == previous["first_detected_at"]
    assert rebound["last_notified_at"] == previous["last_notified_at"]


def test_persistent_breach_gets_bounded_reminder() -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate")
    state = checker.updated_state(
        _snapshot(),
        rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=True,
    )

    assert (
        checker.notification_kind(
            rows,
            state,
            now=now + timedelta(hours=5, minutes=59),
            reminder_seconds=6 * 3600,
        )
        is None
    )
    assert (
        checker.notification_kind(
            rows,
            state,
            now=now + timedelta(hours=6),
            reminder_seconds=6 * 3600,
        )
        == "reminder"
    )


def test_recovery_pages_once_then_stays_quiet() -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate")
    breached = checker.updated_state(
        _snapshot(),
        rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=True,
    )

    recovery_at = now + timedelta(hours=1)
    assert (
        checker.notification_kind(
            [], breached, now=recovery_at, reminder_seconds=6 * 3600
        )
        == "recovery"
    )
    recovered = checker.updated_state(
        _snapshot(generated_at="2026-07-24T01:00:00+00:00"),
        [],
        breached,
        now=recovery_at,
        notification_kind="recovery",
        notification_sent=True,
    )
    assert recovered["status"] == "healthy"
    assert recovered["last_resolved_at"] == recovery_at.isoformat()
    assert (
        checker.notification_kind(
            [],
            recovered,
            now=recovery_at + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        is None
    )


def test_metric_value_severity_and_target_drift_do_not_change_incident_identity() -> None:
    now = _now()
    rows = _rows("recall_hit_rate")
    state = checker.updated_state(
        _snapshot(),
        rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=True,
    )
    drifted = [dict(rows[0], value="no_data", severity="warn", target=">=0.65")]

    assert checker.incident_metrics(rows + [rows[0]]) == checker.incident_metrics(rows)
    assert checker.incident_fingerprint(drifted) == state["fingerprint"]
    assert (
        checker.notification_kind(
            drifted,
            state,
            now=now + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        is None
    )


def test_default_state_path_honors_runtime_hermes_home(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert checker.default_state_path() == (
        tmp_path / "observability" / "slo-alert-state.json"
    )


def test_state_round_trip_is_atomic_and_profile_local(tmp_path: Path) -> None:
    state_path = tmp_path / "observability" / "slo-alert-state.json"
    payload = {
        "schema_version": 1,
        "status": "breached",
        "fingerprint": "abc",
    }

    checker.save_state(state_path, payload)

    assert checker.load_state(state_path) == payload
    assert list(state_path.parent.glob(f".{state_path.name}.*.tmp")) == []


def test_state_save_fsyncs_parent_directory(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "observability" / "slo-alert-state.json"
    original_fsync = checker.os.fsync
    directory_fsyncs: list[int] = []

    def recording_fsync(fd: int) -> None:
        if checker.os.path.isdir(f"/proc/self/fd/{fd}"):
            directory_fsyncs.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(checker.os, "fsync", recording_fsync)

    checker.save_state(state_path, {"schema_version": 1, "status": "healthy"})

    assert directory_fsyncs


def test_failed_breach_notification_is_retried() -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate")
    failed = checker.updated_state(
        _snapshot(),
        rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=False,
    )

    assert failed["status"] == "breached"
    assert failed["last_notified_at"] is None
    assert (
        checker.notification_kind(
            rows,
            failed,
            now=now + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        == "pending_breach"
    )


def test_unsent_breach_is_delivered_before_recovery_when_metric_clears() -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate")
    failed = checker.updated_state(
        _snapshot(),
        rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=False,
    )
    recovery_at = now + timedelta(minutes=15)

    assert (
        checker.notification_kind(
            [], failed, now=recovery_at, reminder_seconds=6 * 3600
        )
        == "pending_breach"
    )
    pending = checker.pending_notification(failed)
    assert pending is not None
    assert pending["kind"] == "breach"
    assert pending["breaches"] == rows

    delivered = checker.updated_state(
        _snapshot(generated_at="2026-07-24T00:15:00+00:00"),
        [],
        failed,
        now=recovery_at,
        notification_kind="pending_breach",
        notification_sent=True,
    )
    assert delivered["status"] == "breached"
    assert delivered["pending_recovery_at"] == recovery_at.isoformat()
    assert delivered["pending_notification"] is None
    assert (
        checker.notification_kind(
            [],
            delivered,
            now=recovery_at + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        == "recovery"
    )


def test_failed_recovery_notification_keeps_incident_open_for_retry() -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate")
    breached = checker.updated_state(
        _snapshot(),
        rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=True,
    )
    recovery_at = now + timedelta(hours=1)

    failed = checker.updated_state(
        _snapshot(generated_at="2026-07-24T01:00:00+00:00"),
        [],
        breached,
        now=recovery_at,
        notification_kind="recovery",
        notification_sent=False,
    )

    assert failed["status"] == "breached"
    assert failed["pending_recovery_at"] == recovery_at.isoformat()
    assert (
        checker.notification_kind(
            [],
            failed,
            now=recovery_at + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        == "recovery"
    )


def test_probe_modes_never_read_live_state_without_explicit_state(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "latest.json"
    snapshot = checker.synthetic_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(
        checker,
        "load_state",
        lambda path: (_ for _ in ()).throw(AssertionError(f"unexpected state read: {path}")),
    )
    monkeypatch.setattr(checker, "notify", lambda message, dry_run: 0)

    assert checker.main(["--latest", str(snapshot_path), "--dry-run"]) == 2
    assert checker.main(["--latest", str(snapshot_path), "--print-only"]) == 2
    assert checker.main(["--synthetic-breach"]) == 2
    assert checker.main(["--synthetic-breach", "--dry-run"]) == 2


def test_explicit_state_is_read_for_isolated_probe(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    reads: list[Path] = []
    monkeypatch.setattr(
        checker,
        "load_state",
        lambda path: reads.append(path) or {},
    )
    monkeypatch.setattr(checker, "notify", lambda message, dry_run: 0)

    assert (
        checker.main(
            ["--synthetic-breach", "--dry-run", "--state", str(state_path)]
        )
        == 2
    )
    assert reads == [state_path]


def test_dry_run_probe_ignores_live_state_and_never_persists(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "latest.json"
    snapshot = checker.synthetic_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    live_state = tmp_path / "live-state.json"
    rows = checker.breaches(snapshot)
    existing = checker.updated_state(
        snapshot,
        rows,
        {},
        now=_now(),
        notification_kind="breach",
        notification_sent=True,
    )
    checker.save_state(live_state, existing)
    before = live_state.read_bytes()
    monkeypatch.setattr(checker, "default_state_path", lambda: live_state)
    monkeypatch.setattr(checker, "notify", lambda message, dry_run: 0)

    rc = checker.main(["--latest", str(snapshot_path), "--dry-run"])

    assert rc == 2
    assert live_state.read_bytes() == before


def test_print_only_probe_ignores_live_state_and_never_persists(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "latest.json"
    snapshot = checker.synthetic_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    live_state = tmp_path / "live-state.json"
    rows = checker.breaches(snapshot)
    existing = checker.updated_state(
        snapshot,
        rows,
        {},
        now=_now(),
        notification_kind="breach",
        notification_sent=True,
    )
    checker.save_state(live_state, existing)
    before = live_state.read_bytes()
    monkeypatch.setattr(checker, "default_state_path", lambda: live_state)

    rc = checker.main(["--latest", str(snapshot_path), "--print-only"])

    assert rc == 2
    assert live_state.read_bytes() == before


def test_main_orders_failed_breach_before_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "latest.json"
    state_path = tmp_path / "state.json"
    breached_snapshot = checker.synthetic_snapshot()
    snapshot_path.write_text(json.dumps(breached_snapshot), encoding="utf-8")
    messages: list[str] = []
    return_codes = iter([1, 0, 0])

    def fake_notify(message: str, *, dry_run: bool) -> int:
        messages.append(message)
        return next(return_codes)

    monkeypatch.setattr(checker, "notify", fake_notify)

    assert checker.main(["--latest", str(snapshot_path), "--state", str(state_path)]) == 1
    failed = checker.load_state(state_path)
    assert checker.pending_notification(failed) is not None

    healthy = _snapshot(generated_at="2026-07-24T00:15:00+00:00")
    healthy["metrics"] = {
        "turn_error_rate": 0.0,
        "fallback_trigger_rate": 0.0,
        "recall_hit_rate": 1.0,
        "watchdog_restart_count": 0,
        "cost_burn_rate_usd_24h": 0.0,
    }
    snapshot_path.write_text(json.dumps(healthy), encoding="utf-8")

    assert checker.main(["--latest", str(snapshot_path), "--state", str(state_path)]) == 2
    assert "Hermes SLO breach" in messages[-1]
    assert "Hermes SLO recovered" not in messages[-1]
    delivered = checker.load_state(state_path)
    assert delivered["status"] == "breached"
    assert delivered["pending_notification"] is None

    assert checker.main(["--latest", str(snapshot_path), "--state", str(state_path)]) == 2
    assert "Hermes SLO recovered" in messages[-1]
    recovered = checker.load_state(state_path)
    assert recovered["status"] == "healthy"

    before = len(messages)
    assert checker.main(["--latest", str(snapshot_path), "--state", str(state_path)]) == 0
    assert len(messages) == before


def test_notifier_timeout_persists_pending_transition(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "latest.json"
    snapshot = checker.synthetic_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    state_path = tmp_path / "state.json"

    def timeout(message: str, *, dry_run: bool) -> int:
        raise subprocess.TimeoutExpired(cmd="discord-notify.sh", timeout=30)

    monkeypatch.setattr(checker, "notify", timeout)

    rc = checker.main(
        ["--latest", str(snapshot_path), "--state", str(state_path)]
    )

    assert rc == 1
    state = checker.load_state(state_path)
    pending = checker.pending_notification(state)
    assert pending is not None
    assert pending["kind"] == "breach"


def test_notifier_rc_two_is_remapped_to_real_failure(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "latest.json"
    snapshot = checker.synthetic_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(checker, "notify", lambda message, dry_run: 2)

    rc = checker.main(
        ["--latest", str(snapshot_path), "--state", str(state_path)]
    )

    assert rc == 1
    state = checker.load_state(state_path)
    assert state["status"] == "breached"
    assert state["last_notified_at"] is None


def test_recovery_message_names_resolved_metrics() -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate", "watchdog_restart_count")
    state = checker.updated_state(
        _snapshot(),
        rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=True,
    )

    text = checker.render_recovery(_snapshot(), state)

    assert "Hermes SLO recovered" in text
    assert "fallback_trigger_rate" in text
    assert "watchdog_restart_count" in text
