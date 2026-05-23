"""Tests for the read-only Hives endpoints (GET /api/dashboard/hives and
GET /api/dashboard/hives/{id}/log).

All I/O is monkeypatched so the tests are fully offline and never touch the
real ~/.hermes/ruflo-work directory.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hive(
    *,
    hive_id: str = "test-hive-20260522",
    status: str = "completed",
    tmux_alive: bool = False,
    log_path: str | None = None,
    tracking_card: str | None = "t_abc123",
    final_report_status: str | None = "COMPLETE",
    final_report_path: str | None = "/fake/FINAL-REPORT.md",
) -> dict:
    return {
        "id": hive_id,
        "workdir": f"/home/user/.hermes/ruflo-work/{hive_id}",
        "session": None,
        "status": status,
        "tracking_card": tracking_card,
        "started_at": "2026-05-22T12:00:00+00:00",
        "updated_at": "2026-05-22T13:00:00+00:00",
        "elapsed_seconds": 3600,
        "final_report_status": final_report_status,
        "final_report_path": final_report_path,
        "log_path": log_path,
        "log_size_bytes": 1024 if log_path else 0,
        "log_mtime": "2026-05-22T13:00:00+00:00" if log_path else None,
        "tmux_alive": tmux_alive,
        "track_title": "Test Hive Run",
        "objective_summary": "Consolidate nexus health metrics",
    }


def _mock_hives(monkeypatch, hives: list[dict] | None = None):
    """Monkeypatch the hives snapshot cache so tests are offline."""
    from hermes_cli import dashboard_health

    snapshot = {
        "hives": hives or [],
        "scanned_at": "2026-05-22T13:00:00+00:00",
        "active_count": sum(1 for h in (hives or []) if h["status"] == "running"),
        "completed_count": sum(1 for h in (hives or []) if h["status"] in {"completed", "blocked"}),
        "stale_count": sum(1 for h in (hives or []) if h["status"] == "stale"),
    }
    monkeypatch.setattr(dashboard_health, "_get_hives_snapshot", lambda: snapshot)
    monkeypatch.setattr(dashboard_health, "_HIVES_CACHE", None)
    return dashboard_health


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def test_hives_routes_registered():
    """Both hive routes must be registered on the dashboard router."""
    from hermes_cli import dashboard_health

    paths = {route.path for route in dashboard_health.router.routes}
    assert "/api/dashboard/hives" in paths
    assert "/api/dashboard/hives/{hive_id}/log" in paths


# ---------------------------------------------------------------------------
# Snapshot shape
# ---------------------------------------------------------------------------

def test_hives_snapshot_returns_required_keys(monkeypatch, tmp_path):
    """_build_hives_snapshot must return the documented top-level keys."""
    from hermes_cli import dashboard_health

    # Point the scanner at a real temp directory with one fake hive workdir.
    fake_work = tmp_path / "ruflo-work"
    fake_work.mkdir()
    hive_dir = fake_work / "my-hive-20260522"
    hive_dir.mkdir()
    (hive_dir / "objective.md").write_text("Test objective here.")

    monkeypatch.setattr(dashboard_health, "RUFLO_WORK_DIR", fake_work)
    # Suppress tmux so tests don't depend on a live tmux session.
    monkeypatch.setattr(dashboard_health, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(dashboard_health, "_HIVES_CACHE", None)

    result = dashboard_health._build_hives_snapshot()

    assert {"hives", "scanned_at", "active_count", "completed_count", "stale_count"} <= set(result)
    assert isinstance(result["hives"], list)
    assert len(result["hives"]) == 1


def test_hive_entry_has_required_fields(monkeypatch, tmp_path):
    """Each hive entry must carry all documented fields."""
    from hermes_cli import dashboard_health

    fake_work = tmp_path / "ruflo-work"
    fake_work.mkdir()
    hive_dir = fake_work / "alpha-hive-20260522"
    hive_dir.mkdir()
    (hive_dir / "LAUNCH.sh").write_text('#!/bin/bash\nTRACK_TITLE="Alpha Hive"\n')
    (hive_dir / "objective.md").write_text("Do some work.")
    (hive_dir / ".ruflo-status.json").write_text(json.dumps({
        "session": "alphahive",
        "status": "completed",
        "tracking_card": "t_xyz",
        "updated_at": "2026-05-22T14:00:00Z",
    }))
    (hive_dir / "FINAL-REPORT.md").write_text("Status: COMPLETE\nAll done.")
    (hive_dir / "hive-mind.log").write_text("line1\nline2\n")

    monkeypatch.setattr(dashboard_health, "RUFLO_WORK_DIR", fake_work)
    monkeypatch.setattr(dashboard_health, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(dashboard_health, "_HIVES_CACHE", None)

    result = dashboard_health._build_hives_snapshot()
    assert len(result["hives"]) == 1
    h = result["hives"][0]

    required = {
        "id", "workdir", "session", "status", "tracking_card",
        "started_at", "updated_at", "elapsed_seconds", "final_report_status",
        "final_report_path", "log_path", "log_size_bytes", "log_mtime",
        "tmux_alive", "track_title", "objective_summary",
    }
    assert required <= set(h), f"Missing fields: {required - set(h)}"
    assert h["id"] == "alpha-hive-20260522"
    assert h["status"] == "completed"
    assert h["final_report_status"] == "COMPLETE"
    assert h["track_title"] == "Alpha Hive"
    assert h["tracking_card"] == "t_xyz"


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

def test_status_running_when_tmux_alive_no_report(monkeypatch, tmp_path):
    from hermes_cli import dashboard_health

    fake_work = tmp_path / "ruflo-work"
    fake_work.mkdir()
    hive_dir = fake_work / "live-hive"
    hive_dir.mkdir()
    (hive_dir / "LAUNCH.sh").write_text("#!/bin/bash\n")
    (hive_dir / ".ruflo-status.json").write_text(json.dumps({"session": "livehive"}))

    monkeypatch.setattr(dashboard_health, "RUFLO_WORK_DIR", fake_work)
    monkeypatch.setattr(dashboard_health, "_tmux_sessions", lambda: {"livehive"})
    monkeypatch.setattr(dashboard_health, "_HIVES_CACHE", None)

    result = dashboard_health._build_hives_snapshot()
    assert result["hives"][0]["status"] == "running"
    assert result["active_count"] == 1


def test_status_stale_no_tmux_no_report(monkeypatch, tmp_path):
    from hermes_cli import dashboard_health

    fake_work = tmp_path / "ruflo-work"
    fake_work.mkdir()
    hive_dir = fake_work / "dead-hive"
    hive_dir.mkdir()
    (hive_dir / "LAUNCH.sh").write_text("#!/bin/bash\n")

    monkeypatch.setattr(dashboard_health, "RUFLO_WORK_DIR", fake_work)
    monkeypatch.setattr(dashboard_health, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(dashboard_health, "_HIVES_CACHE", None)

    result = dashboard_health._build_hives_snapshot()
    assert result["hives"][0]["status"] == "stale"
    assert result["stale_count"] == 1


def test_status_blocked_when_report_blocked(monkeypatch, tmp_path):
    from hermes_cli import dashboard_health

    fake_work = tmp_path / "ruflo-work"
    fake_work.mkdir()
    hive_dir = fake_work / "blocked-hive"
    hive_dir.mkdir()
    (hive_dir / "LAUNCH.sh").write_text("#!/bin/bash\n")
    (hive_dir / "FINAL-REPORT.md").write_text("Status: BLOCKED\nNeeds operator.")

    monkeypatch.setattr(dashboard_health, "RUFLO_WORK_DIR", fake_work)
    monkeypatch.setattr(dashboard_health, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(dashboard_health, "_HIVES_CACHE", None)

    result = dashboard_health._build_hives_snapshot()
    h = result["hives"][0]
    assert h["status"] == "blocked"
    assert h["final_report_status"] == "BLOCKED"


# ---------------------------------------------------------------------------
# Junk dir filtering
# ---------------------------------------------------------------------------

def test_junk_dirs_ignored(monkeypatch, tmp_path):
    """Dirs without LAUNCH.sh, objective.md or .ruflo-status.json are skipped."""
    from hermes_cli import dashboard_health

    fake_work = tmp_path / "ruflo-work"
    fake_work.mkdir()
    junk = fake_work / "not-a-hive"
    junk.mkdir()
    (junk / "README.txt").write_text("nothing here")

    monkeypatch.setattr(dashboard_health, "RUFLO_WORK_DIR", fake_work)
    monkeypatch.setattr(dashboard_health, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(dashboard_health, "_HIVES_CACHE", None)

    result = dashboard_health._build_hives_snapshot()
    assert len(result["hives"]) == 0


# ---------------------------------------------------------------------------
# Log endpoint
# ---------------------------------------------------------------------------

def test_log_endpoint_returns_lines(monkeypatch, tmp_path):
    """_get_hive_log_tail returns the last N lines of hive-mind.log."""
    from hermes_cli import dashboard_health

    log_file = tmp_path / "hive-mind.log"
    log_file.write_text("\n".join(f"line {i}" for i in range(300)))

    hive = _make_hive(hive_id="log-hive", log_path=str(log_file))
    _mock_hives(monkeypatch, [hive])

    result = dashboard_health._get_hive_log_tail("log-hive", tail=50)
    assert result is not None
    assert len(result["lines"]) == 50
    assert result["truncated_to"] == 50
    # Should be the last 50 lines
    assert result["lines"][0] == "line 250"
    assert result["lines"][-1] == "line 299"


def test_log_endpoint_unknown_hive_returns_none(monkeypatch):
    """Unknown hive_id → None (HTTP layer raises 404)."""
    from hermes_cli import dashboard_health

    _mock_hives(monkeypatch, [_make_hive(hive_id="real-hive")])
    result = dashboard_health._get_hive_log_tail("does-not-exist", tail=100)
    assert result is None


def test_log_tail_cap(monkeypatch, tmp_path):
    """tail is capped at 1000 even if caller passes a larger value."""
    from hermes_cli import dashboard_health

    log_file = tmp_path / "hive-mind.log"
    log_file.write_text("\n".join(f"L{i}" for i in range(2000)))

    hive = _make_hive(hive_id="biglog", log_path=str(log_file))
    _mock_hives(monkeypatch, [hive])

    result = dashboard_health._get_hive_log_tail("biglog", tail=9999)
    assert result is not None
    assert result["truncated_to"] == 1000
    assert len(result["lines"]) == 1000


def test_log_endpoint_no_log_file(monkeypatch):
    """Hive with no log_path returns empty lines dict, not None."""
    from hermes_cli import dashboard_health

    hive = _make_hive(hive_id="nolog-hive", log_path=None)
    _mock_hives(monkeypatch, [hive])

    result = dashboard_health._get_hive_log_tail("nolog-hive", tail=100)
    assert result is not None
    assert result["lines"] == []


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------

def test_hives_cache_prevents_rebuild(monkeypatch, tmp_path):
    """Second call within TTL returns cached value without calling _build."""
    from hermes_cli import dashboard_health

    call_count = {"n": 0}
    orig_build = dashboard_health._build_hives_snapshot

    def counting_build():
        call_count["n"] += 1
        return orig_build()

    monkeypatch.setattr(dashboard_health, "_build_hives_snapshot", counting_build)
    monkeypatch.setattr(dashboard_health, "RUFLO_WORK_DIR", tmp_path)
    monkeypatch.setattr(dashboard_health, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(dashboard_health, "_HIVES_CACHE", None)

    dashboard_health._get_hives_snapshot()
    dashboard_health._get_hives_snapshot()

    assert call_count["n"] == 1, "Cache should prevent second build call"


# ---------------------------------------------------------------------------
# API endpoint integration (path + basic shape)
# ---------------------------------------------------------------------------

def test_api_get_hives_route_exists():
    """The /api/dashboard/hives route must be registered on the router."""
    from hermes_cli import dashboard_health

    paths = {route.path for route in dashboard_health.router.routes}
    assert "/api/dashboard/hives" in paths


def test_api_get_hives_log_route_exists():
    """The /api/dashboard/hives/{hive_id}/log route must be registered."""
    from hermes_cli import dashboard_health

    paths = {route.path for route in dashboard_health.router.routes}
    assert "/api/dashboard/hives/{hive_id}/log" in paths
