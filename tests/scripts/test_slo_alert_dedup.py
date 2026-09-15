from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

CHECKER_PATH = Path(__file__).resolve().parents[2] / "scripts/observability/slo_alert_check.py"
spec = importlib.util.spec_from_file_location("candidate_slo_alert_check", CHECKER_PATH)
assert spec and spec.loader
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

A, B, C = "a" * 64, "b" * 64, "c" * 64  # Synthetic, valid-shape event evidence.


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real notifier/subprocess/SQLite access forbidden")
    monkeypatch.setattr(checker, "notify", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.delenv("DISCORD_NOTIFY_DRYRUN", raising=False)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    class Harness:
        now = 1_789_264_800.0
        messages: list[str]
        rc = 0
        # Own directory: the repo conftest also creates a fake HERMES_HOME under
        # tmp_path, and the "no stray files" assertions list this directory.
        latest = tmp_path / "slo" / "latest.json"
        state = tmp_path / "slo" / "slo-alert-dedup-v1.json"

        def snapshot(self, *, turns=10, errors=(A,), fallbacks=(), evidence=True, **metrics):
            snap = {"generated_at": checker.utc_iso(self.now), "since": checker.utc_iso(self.now - 86400),
                    "window_seconds": 86400, "turn_count": turns,
                    "metrics": {"gateway_turn_p95_latency_ms": 240000,
                                "turn_error_rate": min(1, len(errors) / turns) if turns else None,
                                "fallback_trigger_rate": min(1, len(fallbacks) / turns) if turns else None,
                                "recall_hit_rate": 1.0, "watchdog_restart_count": 0,
                                "cost_burn_rate_usd_24h": 0.0, **metrics},
                    "journal_counts": {"turn_error_events": len(errors), "fallback_events": len(fallbacks)},
                    "sources": {"state_db": "/private/path/state.db", "private_prompt": "DO NOT PRINT"}}
            if evidence:
                snap["event_evidence"] = {"turn_error_rate": list(errors), "fallback_trigger_rate": list(fallbacks)}
            return snap

        def run(self, snapshot=None, *options):
            if snapshot is not None:
                self.latest.write_text(json.dumps(snapshot))
            return checker.main(["--latest", str(self.latest), *options])

        def send(self, message, **kwargs):
            assert kwargs == {"dry_run": False}
            self.messages.append(message)
            return self.rc

        def advance(self, seconds=900):
            self.now += seconds

    h = Harness()
    h.latest.parent.mkdir()
    h.messages = []
    monkeypatch.setattr(checker.time, "time", lambda: h.now)
    monkeypatch.setattr(checker, "notify", h.send)
    return h


def test_same_snapshot_only_sends_once(harness):
    h = harness
    assert h.run(h.snapshot()) == 2
    assert h.run() == 0
    assert len(h.messages) == 1
    assert sorted(p.name for p in h.latest.parent.iterdir()) == ["latest.json", "slo-alert-dedup-v1.json"]


@pytest.mark.parametrize("metric", ["turn_error_rate", "fallback_trigger_rate"])
def test_new_event_same_count_denominator_only_and_subset_aging(harness, metric):
    h = harness
    def snap(events, turns=10):
        return h.snapshot(errors=events if metric == "turn_error_rate" else (),
                          fallbacks=events if metric == "fallback_trigger_rate" else (), turns=turns)
    assert h.run(snap((A, B))) == 2
    h.advance()
    assert h.run(snap((A, B), turns=5)) == 0  # Same numerator, worsening ratio.
    h.advance()
    assert h.run(snap((B,), turns=5)) == 0  # Old event aged out.
    h.advance()
    assert h.run(snap((B, C), turns=5)) == 2
    h.advance()
    assert h.run(snap((A, B), turns=5)) == 0  # Reappearing known event, not new.
    h.advance()
    assert h.run(snap((C,), turns=5)) == 0
    h.advance()
    assert h.run(snap((A,), turns=5)) == 0
    assert len(h.messages) == 2


@pytest.mark.parametrize("metric", ["turn_error_rate", "fallback_trigger_rate"])
def test_one_for_one_event_replacement_repages(harness, metric):
    h = harness
    def snap(event):
        return h.snapshot(turns=5, errors=(event,) if metric == "turn_error_rate" else (),
                          fallbacks=(event,) if metric == "fallback_trigger_rate" else ())
    assert h.run(snap(A)) == 2
    h.advance()
    assert h.run(snap(B)) == 2
    assert "new event evidence" in h.messages[-1]


def test_six_hour_reminder_boundary_and_old_valid_state(harness):
    h = harness
    assert h.run(h.snapshot()) == 2
    for _ in range(23):
        h.advance()
        assert h.run(h.snapshot()) == 0
    h.advance(899)
    assert h.run(h.snapshot()) == 0
    h.advance(1)
    assert h.run(h.snapshot()) == 2
    assert "6-hour reminder" in h.messages[-1]
    assert h.run() == 0
    h.advance(7 * 86400)
    assert h.run(h.snapshot()) == 2  # Never discard an old incident state.


def test_new_metric_and_worsened_non_event_values(harness):
    h = harness
    assert h.run(h.snapshot()) == 2
    h.advance()
    assert h.run(h.snapshot(cost_burn_rate_usd_24h=11)) == 2
    h.advance()
    assert h.run(h.snapshot(cost_burn_rate_usd_24h=12)) == 2
    h.advance()
    assert h.run(h.snapshot(cost_burn_rate_usd_24h=11)) == 0
    h.advance()
    assert h.run(h.snapshot(cost_burn_rate_usd_24h=11, recall_hit_rate=0.60)) == 2
    h.advance()
    assert h.run(h.snapshot(cost_burn_rate_usd_24h=11, recall_hit_rate=0.50)) == 2


def test_no_evidence_uses_count_high_water_not_rate(harness):
    h = harness
    assert h.run(h.snapshot(errors=(A, B), evidence=False)) == 2
    h.advance()
    assert h.run(h.snapshot(errors=(A, B), turns=5, evidence=False)) == 0
    h.advance()
    assert h.run(h.snapshot(errors=(B,), turns=5, evidence=False)) == 0
    h.advance()
    assert h.run(h.snapshot(errors=(B, C), turns=5, evidence=False)) == 0
    h.advance()
    assert h.run(h.snapshot(errors=(A, B, C), turns=5, evidence=False)) == 2
    assert "count increased" in h.messages[-1]


def test_no_counts_or_evidence_reminders_only(harness):
    h = harness
    snap = h.snapshot(evidence=False)
    del snap["journal_counts"]
    assert h.run(snap) == 2
    snap["metrics"]["turn_error_rate"] = 0.9
    assert h.run(snap) == 0
    h.advance(checker.REMINDER_SECONDS)
    snap["generated_at"], snap["since"] = checker.utc_iso(h.now), checker.utc_iso(h.now - 86400)
    assert h.run(snap) == 2


@pytest.mark.parametrize("rc", [1, 2, 7, -9])
def test_failed_notify_does_not_commit_and_retries(harness, rc):
    h = harness
    h.rc = rc
    assert h.run(h.snapshot()) == 1
    assert not h.state.exists()
    h.rc = 0
    assert h.run() == 2
    before = h.state.read_bytes()
    h.advance()
    h.rc = rc
    assert h.run(h.snapshot(errors=(B,))) == 1
    assert h.state.read_bytes() == before
    h.rc = 0
    assert h.run() == 2
    assert "new event evidence" in h.messages[-1]


@pytest.mark.parametrize("error", [OSError("fake"), subprocess.TimeoutExpired("fake", 30)])
def test_notify_exception_keeps_state(harness, monkeypatch, error):
    h = harness
    assert h.run(h.snapshot()) == 2
    before = h.state.read_bytes()
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(checker, "notify", fail)
    h.advance()
    assert h.run(h.snapshot(errors=(B,))) == 1
    assert h.state.read_bytes() == before


def test_one_recovery_then_recurrence(harness):
    h = harness
    assert h.run(h.snapshot()) == 2
    h.advance()
    assert h.run(h.snapshot(errors=())) == 2
    assert "SLO recovery" in h.messages[-1]
    assert h.run() == 0
    h.advance()
    assert h.run(h.snapshot(errors=(B,))) == 2
    assert len(h.messages) == 3


def test_recovery_failure_retries_without_clearing(harness):
    h = harness
    assert h.run(h.snapshot()) == 2
    before = h.state.read_bytes()
    h.advance()
    h.rc = 2
    assert h.run(h.snapshot(errors=())) == 1
    assert h.state.read_bytes() == before
    h.rc = 0
    assert h.run() == 2
    assert h.run() == 0


@pytest.mark.parametrize("option", ["--print-only", "--dry-run", "--synthetic-breach"])
def test_previews_never_send_or_mutate(harness, option):
    h = harness
    other = h.latest.parent / "absent-dir" / "state.json"
    assert h.run(h.snapshot(), option, "--state", str(other)) == 0
    assert not other.parent.exists()
    assert not h.messages
    assert not h.state.exists()
    assert h.run() == 2
    before = h.state.read_bytes()
    assert h.run(h.snapshot(errors=()), option) == 0
    assert h.state.read_bytes() == before
    assert len(h.messages) == 1


def test_environment_dryrun_is_preview(harness, monkeypatch):
    h = harness
    monkeypatch.setenv("DISCORD_NOTIFY_DRYRUN", "1")
    assert h.run(h.snapshot()) == 0
    assert not h.messages and not h.state.exists()


@pytest.mark.parametrize("kind", ["stale", "missing_metrics", "missing_metric", "none_rate", "nan", "negative",
                                  "bad_evidence", "partial_evidence", "duplicate_evidence", "bad_count", "bad_window", "bad_since", "naive_time", "future"])
def test_bad_snapshots_do_not_recover(harness, kind):
    h = harness
    assert h.run(h.snapshot()) == 2
    before = h.state.read_bytes()
    h.advance()
    snap = h.snapshot(errors=())
    if kind == "stale":
        snap["generated_at"] = checker.utc_iso(h.now - 901)
    elif kind == "missing_metrics":
        snap["metrics"] = {}
    elif kind == "missing_metric":
        del snap["metrics"]["turn_error_rate"]
    elif kind == "none_rate":
        snap["metrics"]["turn_error_rate"] = None
    elif kind == "nan":
        snap["metrics"]["cost_burn_rate_usd_24h"] = float("nan")
    elif kind == "negative":
        snap["metrics"]["watchdog_restart_count"] = -1
    elif kind == "bad_evidence":
        snap["event_evidence"]["turn_error_rate"] = ["private-session-id"]
    elif kind == "partial_evidence":
        del snap["event_evidence"]["fallback_trigger_rate"]
    elif kind == "duplicate_evidence":
        snap["event_evidence"]["turn_error_rate"] = [A, A]
    elif kind == "bad_count":
        snap["journal_counts"]["turn_error_events"] = "two"
    elif kind == "bad_window":
        snap["window_seconds"] = 0
    elif kind == "bad_since":
        snap["since"] = checker.utc_iso(h.now)
    elif kind == "naive_time":
        snap["generated_at"] = "2026-09-13T00:00:00"
    elif kind == "future":
        snap["generated_at"] = checker.utc_iso(h.now + 61)
    assert h.run(snap) == 1
    assert h.state.read_bytes() == before
    assert len(h.messages) == 1


@pytest.mark.parametrize("text", ["not-json", "null", "[]", "{}"])
def test_malformed_snapshot(harness, text):
    h = harness
    assert h.run(h.snapshot()) == 2
    before = h.state.read_bytes()
    h.latest.write_text(text)
    assert h.run() == 1
    assert h.state.read_bytes() == before


@pytest.mark.parametrize("kind", ["json", "version", "shape", "time", "evidence", "counts", "active"])
def test_corrupt_state_is_error_not_reset(harness, kind):
    h = harness
    assert h.run(h.snapshot()) == 2
    state = json.loads(h.state.read_text())
    if kind == "version":
        state["version"] = 999
    elif kind == "shape":
        state["active"] = []
    elif kind == "time":
        state["last_sent_at"] = h.now + 60
    elif kind == "evidence":
        state["event_evidence"] = {"turn_error_rate": ["bad"]}
    elif kind == "counts":
        state["event_counts"] = {"turn_error_rate": -1}
    elif kind == "active":
        state["active"] = {"recall_hit_rate": float("nan")}
    h.state.write_text("{" if kind == "json" else json.dumps(state))
    before = h.state.read_bytes()
    assert h.run(h.snapshot(errors=())) == 1
    assert h.state.read_bytes() == before
    assert len(h.messages) == 1


def test_snapshot_rollback_is_error(harness):
    h = harness
    old = h.snapshot(errors=())
    assert h.run(h.snapshot()) == 2
    h.advance(30)
    assert h.run(h.snapshot()) == 0
    before = h.state.read_bytes()
    assert h.run(old) == 1
    assert h.state.read_bytes() == before


def test_recall_no_data_retains_existing_warning_and_no_recovery(harness):
    h = harness
    assert h.run(h.snapshot()) == 2
    h.advance()
    assert h.run(h.snapshot(errors=(), recall_hit_rate=None)) == 2
    assert "no data (warning)" in h.messages[-1]
    assert "SLO recovery" not in h.messages[-1]
    assert h.run() == 0


def test_thresholds_unchanged_and_nonpaging_latency(harness):
    h = harness
    snap = h.snapshot(turns=40, errors=(A, B), fallbacks=(A, B, C, "d" * 64),
                      recall_hit_rate=0.65, watchdog_restart_count=1, cost_burn_rate_usd_24h=10)
    assert checker.breaches(snap) == []
    assert h.run(snap) == 0
    assert not h.messages


def test_readable_sanitized_rendering(harness):
    h = harness
    snap = h.snapshot(turns=10, errors=(A, B), cost_burn_rate_usd_24h=12.5)
    assert h.run(snap) == 2
    message = h.messages[0]
    assert "20.0% (2 events / 10 turns)" in message
    assert "pages above 5%" in message
    assert "$12.50" in message
    assert "Snapshot:" in message and "UTC" in message and "Window: rolling 24h" in message
    assert "not incident onset" in message
    assert all(secret not in message for secret in [A, B, "private/path", "DO NOT PRINT", "state_db"])


def test_custom_state_path_and_atomic_replace(harness, monkeypatch):
    h = harness
    target = h.latest.parent / "custom" / "notice.json"
    replacements = []
    replace = checker.os.replace
    def spy(source, destination):
        assert Path(source).parent == target.parent
        assert json.loads(Path(source).read_text())["active"]
        replacements.append((source, destination))
        replace(source, destination)
    monkeypatch.setattr(checker.os, "replace", spy)
    assert h.run(h.snapshot(), "--state", str(target)) == 2
    assert target.exists() and not h.state.exists()
    assert len(replacements) == 1
    assert list(target.parent.iterdir()) == [target]
    assert target.stat().st_mode & 0o777 == 0o600


def test_write_failure_is_error_and_keeps_old_state(harness, monkeypatch):
    h = harness
    assert h.run(h.snapshot()) == 2
    before = h.state.read_bytes()
    def fail(*args):
        raise OSError("injected replace failure")
    monkeypatch.setattr(checker.os, "replace", fail)
    h.advance()
    assert h.run(h.snapshot(errors=(B,))) == 1
    assert h.state.read_bytes() == before
    assert sorted(p.name for p in h.latest.parent.iterdir()) == ["latest.json", "slo-alert-dedup-v1.json"]


def test_concurrent_checks_only_send_once(harness, monkeypatch):
    h = harness
    h.latest.write_text(json.dumps(h.snapshot()))
    entered, release = threading.Event(), threading.Event()
    results = []
    def delayed(message, **kwargs):
        h.messages.append(message)
        entered.set()
        assert release.wait(5)
        return 0
    monkeypatch.setattr(checker, "notify", delayed)
    worker = threading.Thread(target=lambda: results.append(h.run()))
    worker.start()
    try:
        assert entered.wait(5)
        assert h.run() == 0
        assert len(h.messages) == 1
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results == [2]
    assert h.run() == 0
    assert len(h.messages) == 1


def test_zero_turns_without_incident_is_quiet(harness):
    h = harness
    assert h.run(h.snapshot(turns=0, errors=())) == 0
    assert not h.messages
    h.advance()
    assert h.run(h.snapshot(turns=0, errors=(), cost_burn_rate_usd_24h=11)) == 2
    assert "Cost: $11.00" in h.messages[-1]


def test_zero_turns_retains_incident_until_bounded_unknown_reminder(harness):
    h = harness
    assert h.run(h.snapshot()) == 2
    for _ in range(23):
        h.advance()
        assert h.run(h.snapshot(turns=0, errors=())) == 0
        assert json.loads(h.state.read_text())["active"]["turn_error_rate"] == "no_data"
    h.advance()
    assert h.run(h.snapshot(turns=0, errors=())) == 2
    assert "unknown (no turns; prior breach retained)" in h.messages[-1]
    assert "SLO recovery" not in h.messages[-1]
    assert h.run() == 0
    h.advance()
    assert h.run(h.snapshot(errors=())) == 2
    assert "SLO recovery" in h.messages[-1]
    assert h.run() == 0


def test_denominator_threshold_oscillation_does_not_reopen_same_events(harness):
    h = harness
    assert h.run(h.snapshot(turns=10)) == 2
    h.advance()
    assert h.run(h.snapshot(turns=40)) == 2
    assert "SLO recovery" in h.messages[-1]
    for _ in range(3):
        h.advance()
        assert h.run(h.snapshot(turns=10)) == 0
        h.advance()
        assert h.run(h.snapshot(turns=40)) == 0
    h.advance()
    assert h.run(h.snapshot(turns=10, errors=(B,))) == 2
    assert "new event evidence" in h.messages[-1]


def test_evidence_count_disagreement_must_not_recover(harness):
    h = harness
    assert h.run(h.snapshot()) == 2
    before = h.state.read_bytes()
    snap = h.snapshot(errors=())
    snap["journal_counts"]["turn_error_events"] = 1
    assert h.run(snap) == 1
    assert h.state.read_bytes() == before
    assert len(h.messages) == 1


def test_state_must_not_overwrite_snapshot(harness):
    h = harness
    snap = h.snapshot()
    assert h.run(snap, "--state", str(h.latest)) == 1
    assert json.loads(h.latest.read_text()) == snap
    assert not h.messages


def test_legacy_default_state_is_preserved(harness):
    h = harness
    legacy = h.latest.with_name("slo-alert-state.json")
    old = '{"schema_version":3,"status":"healthy","active_breaches":[],"pending_notification":null}\n'
    legacy.write_text(old)
    assert h.run(h.snapshot(errors=())) == 0
    assert h.state.exists()
    assert json.loads(h.state.read_text())["version"] == 1
    assert legacy.read_text() == old
    assert h.messages == []
