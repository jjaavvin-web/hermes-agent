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


def test_v2_state_without_delivery_fields_loads_and_transitions(
    tmp_path: Path,
) -> None:
    now = _now()
    rows = _rows("fallback_trigger_rate")
    state_path = tmp_path / "slo-alert-state.json"
    legacy = {
        "schema_version": 2,
        "status": "breached",
        "fingerprint": checker.incident_fingerprint(rows),
        "active_breaches": rows,
        "first_detected_at": (now - timedelta(hours=8)).isoformat(),
        "last_notified_at": (now - timedelta(hours=7)).isoformat(),
        "last_notification_kind": "breach",
    }
    checker.save_state(state_path, legacy)

    loaded = checker.load_state(state_path)

    assert loaded == legacy
    assert (
        checker.notification_kind(
            rows, loaded, now=now, reminder_seconds=6 * 3600
        )
        == "reminder"
    )
    assert (
        checker.notification_kind(
            [], loaded, now=now, reminder_seconds=6 * 3600
        )
        == "recovery"
    )
    rebound = checker.updated_state(
        _snapshot(),
        rows,
        loaded,
        now=now,
        notification_kind=None,
        notification_sent=False,
    )
    assert rebound["schema_version"] == checker.STATE_SCHEMA_VERSION
    assert rebound["first_detected_at"] == legacy["first_detected_at"]
    assert rebound["last_notified_at"] == legacy["last_notified_at"]


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


def test_failed_breach_notification_is_backed_off_then_retried() -> None:
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
    assert failed["last_delivery_failure_kind"] == "breach"
    assert failed["next_delivery_retry_at"] == (now + timedelta(hours=1)).isoformat()
    assert (
        checker.notification_kind(
            rows,
            failed,
            now=now + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        is None
    )
    assert (
        checker.notification_kind(
            rows,
            failed,
            now=now + timedelta(hours=1),
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
    recovery_at = now + timedelta(hours=1)

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
    assert failed["schema_version"] == checker.STATE_SCHEMA_VERSION
    assert failed["pending_recovery_at"] == recovery_at.isoformat()
    assert (
        checker.notification_kind(
            [],
            failed,
            now=recovery_at + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        is None
    )
    assert (
        checker.notification_kind(
            [],
            failed,
            now=recovery_at + timedelta(hours=1),
            reminder_seconds=6 * 3600,
        )
        == "recovery"
    )


def test_failed_reminder_is_backed_off_without_changing_incident() -> None:
    now = _now()
    rows = _rows("recall_hit_rate")
    breached = checker.updated_state(
        _snapshot(),
        rows,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=True,
    )
    reminder_at = now + timedelta(hours=6)
    failed = checker.updated_state(
        _snapshot(generated_at="2026-07-24T06:00:00+00:00"),
        rows,
        breached,
        now=reminder_at,
        notification_kind="reminder",
        notification_sent=False,
    )

    assert failed["status"] == "breached"
    assert failed["last_notified_at"] == now.isoformat()
    assert failed["last_delivery_failure_kind"] == "reminder"
    assert (
        checker.notification_kind(
            rows,
            failed,
            now=reminder_at + timedelta(minutes=15),
            reminder_seconds=6 * 3600,
        )
        is None
    )
    assert (
        checker.notification_kind(
            rows,
            failed,
            now=reminder_at + timedelta(hours=1),
            reminder_seconds=6 * 3600,
        )
        == "reminder"
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


def test_main_logs_recovery_delivery_backoff_without_notifying(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    snapshot_path = tmp_path / "latest.json"
    snapshot = _snapshot()
    snapshot["metrics"] = {
        "turn_error_rate": 0.0,
        "fallback_trigger_rate": 0.0,
        "recall_hit_rate": 1.0,
        "watchdog_restart_count": 0,
        "cost_burn_rate_usd_24h": 0.0,
    }
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    state_path = tmp_path / "state.json"
    synthetic = checker.synthetic_snapshot()
    state = checker.updated_state(
        synthetic,
        checker.breaches(synthetic),
        {},
        now=_now(),
        notification_kind="breach",
        notification_sent=True,
    )
    state.update(
        {
            "last_delivery_failure_kind": "recovery",
            "last_delivery_failed_at": _now().isoformat(),
            "next_delivery_retry_at": "2999-01-01T00:00:00+00:00",
            "delivery_failure_count": 1,
        }
    )
    checker.save_state(state_path, state)
    monkeypatch.setattr(
        checker,
        "notify",
        lambda message, dry_run: (_ for _ in ()).throw(
            AssertionError("recovery notifier must remain backed off")
        ),
    )

    assert checker.main(["--latest", str(snapshot_path), "--state", str(state_path)]) == 0
    assert (
        "slo-alert delivery suppressed: recovery retry backoff until=2999-01-01"
        in capsys.readouterr().out
    )


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

    # Advance only the isolated fixture's durable retry gate; production time is
    # never patched and the live state is never read by this test.
    failed["next_delivery_retry_at"] = "2026-01-01T00:00:00+00:00"
    checker.save_state(state_path, failed)

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
    assert delivered["first_detected_at"] == failed["first_detected_at"]
    assert delivered["active_breaches"] == failed["active_breaches"]
    for key in (
        "last_delivery_failed_at",
        "last_delivery_failure_kind",
        "next_delivery_retry_at",
        "delivery_failure_count",
    ):
        assert key not in delivered

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
        raise subprocess.TimeoutExpired(
            cmd="discord-notify.sh", timeout=checker.DEFAULT_NOTIFY_TIMEOUT_SECONDS
        )

    monkeypatch.setattr(checker, "notify", timeout)

    rc = checker.main(
        ["--latest", str(snapshot_path), "--state", str(state_path)]
    )

    assert rc == 1
    state = checker.load_state(state_path)
    pending = checker.pending_notification(state)
    assert pending is not None
    assert pending["kind"] == "breach"


def test_main_exhausts_bounded_notify_attempts_and_stays_pending(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "latest.json"
    snapshot = checker.synthetic_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    state_path = tmp_path / "state.json"
    attempts: list[list[str]] = []
    sleeps: list[float] = []

    def fail_sink(args: list[str], **kwargs) -> subprocess.CompletedProcess:
        attempts.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=5, stdout="", stderr="sink unavailable\n"
        )

    monkeypatch.setattr(checker.subprocess, "run", fail_sink)
    monkeypatch.setattr(checker.time, "sleep", sleeps.append)

    rc = checker.main(
        ["--latest", str(snapshot_path), "--state", str(state_path)]
    )

    assert rc == 5
    assert len(attempts) == checker.DEFAULT_NOTIFY_ATTEMPTS
    assert sleeps == [1.0, 2.0]
    state = checker.load_state(state_path)
    assert checker.pending_notification(state) is not None
    assert state["last_notified_at"] is None
    assert state["last_delivery_failure_kind"] == "breach"
    assert state["delivery_failure_count"] == 1


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


def test_notify_retries_transient_failure_then_succeeds(monkeypatch) -> None:
    results = iter(
        [
            subprocess.CompletedProcess(args=[], returncode=5, stdout="", stderr="first\n"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="sent\n", stderr=""),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(checker.subprocess, "run", lambda *args, **kwargs: next(results))
    monkeypatch.setattr(checker.time, "sleep", sleeps.append)

    assert checker.notify("test", dry_run=True, attempts=3, retry_delay_seconds=0.5) == 0
    assert sleeps == [0.5]


def test_recall_alert_names_real_source_and_separates_gap_warning() -> None:
    snapshot = _snapshot()
    snapshot["sources"]["recall_canary"] = "/tmp/recall-canary.jsonl"
    snapshot["recall"] = {
        "target_misses": 1,
        "cosine_collapses": 4,
        "total": 5,
    }

    text = checker.render_alert(snapshot, _rows("recall_hit_rate"))

    assert "recall_canary=/tmp/recall-canary.jsonl" in text
    assert "target_misses=1 discrimination_warnings=4 samples=5" in text


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


def test_failed_pending_breach_supersedes_to_expanded_set_during_backoff() -> None:
    """Pending breach A must supersede to live A+B during delivery backoff.

    Stale A-only pending/backoff must not suppress the material identity
    transition. Because no notification ever succeeded, the desired kind stays
    the original pending kind ("breach"), not a free "changed" page that
    pretends a prior delivery landed. After a failed superseding attempt, state
    and pending must describe A+B.
    """
    now = _now()
    rows_a = _rows("fallback_trigger_rate")
    failed_a = checker.updated_state(
        _snapshot(),
        rows_a,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=False,
    )
    assert failed_a["last_notified_at"] is None
    assert checker.pending_notification(failed_a) is not None
    assert checker.pending_notification(failed_a)["kind"] == "breach"

    rows_ab = _rows("fallback_trigger_rate", "watchdog_restart_count")
    supersede_at = now + timedelta(minutes=15)
    fingerprint_ab = checker.incident_fingerprint(rows_ab)

    # Material transition must not be suppressed by stale A-only delivery backoff.
    kind = checker.notification_kind(
        rows_ab,
        failed_a,
        now=supersede_at,
        reminder_seconds=6 * 3600,
    )
    assert kind == "breach"

    superseded = checker.updated_state(
        _snapshot(generated_at="2026-07-24T00:15:00+00:00"),
        rows_ab,
        failed_a,
        now=supersede_at,
        notification_kind=kind,
        notification_sent=False,
    )

    assert superseded["status"] == "breached"
    assert superseded["fingerprint"] == fingerprint_ab
    assert superseded["last_notified_at"] is None
    assert superseded["last_delivery_failure_kind"] == "breach"
    assert superseded.get("next_delivery_retry_at") is not None

    pending = checker.pending_notification(superseded)
    assert pending is not None
    assert pending["kind"] == "breach"
    assert pending["fingerprint"] == fingerprint_ab
    assert checker.incident_metrics(pending["breaches"]) == checker.incident_metrics(rows_ab)
    assert checker.incident_metrics(superseded["active_breaches"]) == checker.incident_metrics(
        rows_ab
    )


def test_reappearance_after_failed_recovery_clears_backoff_and_allows_changed() -> None:
    """Same-set reappearance stays quiet; recovery failure metadata must clear.

    Operator never received recovery, so same-incident A may remain dedup-quiet
    (no duplicate breach Discord page). updated_state for that quiet poll must
    cancel recovery-failure pending/backoff metadata. A changed A+B set must
    still return changed immediately.
    """
    now = _now()
    rows_a = _rows("fallback_trigger_rate")
    breached = checker.updated_state(
        _snapshot(),
        rows_a,
        {},
        now=now,
        notification_kind="breach",
        notification_sent=True,
    )
    recovery_at = now + timedelta(hours=1)
    failed_recovery = checker.updated_state(
        _snapshot(generated_at="2026-07-24T01:00:00+00:00"),
        [],
        breached,
        now=recovery_at,
        notification_kind="recovery",
        notification_sent=False,
    )
    assert failed_recovery["last_delivery_failure_kind"] == "recovery"
    assert failed_recovery.get("pending_recovery_at") is not None

    reappear_at = recovery_at + timedelta(minutes=15)
    assert (
        checker.notification_kind(
            rows_a,
            failed_recovery,
            now=reappear_at,
            reminder_seconds=6 * 3600,
        )
        is None
    )

    same_state = checker.updated_state(
        _snapshot(generated_at="2026-07-24T01:15:00+00:00"),
        rows_a,
        failed_recovery,
        now=reappear_at,
        notification_kind=None,
        notification_sent=False,
    )
    assert same_state["status"] == "breached"
    assert same_state["last_notified_at"] == now.isoformat()
    assert same_state.get("last_delivery_failure_kind") != "recovery"
    assert same_state.get("pending_recovery_at") is None
    assert same_state.get("last_delivery_failed_at") is None
    assert same_state.get("next_delivery_retry_at") is None
    assert same_state.get("delivery_failure_count") in (None, 0)

    rows_ab = _rows("fallback_trigger_rate", "watchdog_restart_count")
    assert (
        checker.notification_kind(
            rows_ab,
            failed_recovery,
            now=reappear_at,
            reminder_seconds=6 * 3600,
        )
        == "changed"
    )


def test_main_logs_breach_delivery_backoff_without_claiming_reminder_suppression(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Same-breach delivery backoff must log delivery/backoff, not reminder interval."""
    snapshot_path = tmp_path / "latest.json"
    snapshot = checker.synthetic_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    state_path = tmp_path / "state.json"
    rows = checker.breaches(snapshot)
    failed = checker.updated_state(
        snapshot,
        rows,
        {},
        now=_now(),
        notification_kind="breach",
        notification_sent=False,
    )
    failed["next_delivery_retry_at"] = "2999-01-01T00:00:00+00:00"
    checker.save_state(state_path, failed)
    monkeypatch.setattr(
        checker,
        "notify",
        lambda message, dry_run: (_ for _ in ()).throw(
            AssertionError("breach delivery backoff must not notify")
        ),
    )

    assert checker.main(["--latest", str(snapshot_path), "--state", str(state_path)]) == 0
    out = capsys.readouterr().out
    assert "reminder interval=" not in out
    assert "slo-alert suppressed: unchanged incident" not in out
    assert "delivery" in out.lower() or "backoff" in out.lower() or "retry" in out.lower()

    after = checker.load_state(state_path)
    assert checker.pending_notification(after) is not None
    assert after["last_notified_at"] is None
    assert after["last_delivery_failure_kind"] == "breach"
