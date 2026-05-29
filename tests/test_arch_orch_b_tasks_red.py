"""P0 stop-bleeding B1-B4 regression tests.

These tests codify the B-slice acceptance criteria from the audit TASK-LIST:
B1 janitor systemd dry-run + no /tmp roots, B2 Codex GC dry-run/terminal-row
safety, B3 scheduled health check + dashboard liveness badge data, and B4
run-registry bootstrap + janitor reader.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.worktree_broker import WorktreeBroker
from gateway.codex_gc_watcher import CodexGcWatcher
from hermes_cli import dashboard_codex_sessions as dcs
from hermes_cli import git_janitor as gj
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated shared Hermes/Kanban home for run-registry bootstrap tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_b1_janitor_service_is_alert_first_dry_run_and_timer_exists():
    root = Path(__file__).resolve().parents[1]
    service = root / "plugins" / "kanban" / "systemd" / "hermes-worktree-janitor.service"
    timer = root / "plugins" / "kanban" / "systemd" / "hermes-worktree-janitor.timer"

    assert service.exists()
    body = service.read_text(encoding="utf-8")
    assert "git-health janitor" in body
    assert "--dry-run" in body
    assert "--confirm" not in body
    assert "systemd-cat" in body or "journal" in body
    assert timer.exists()
    timer_body = timer.read_text(encoding="utf-8")
    assert "OnCalendar=" in timer_body
    assert "Persistent=true" in timer_body


def test_b1_git_janitor_refuses_tmp_repo_roots():
    tmp_repo = Path("/tmp/hermes-unsafe-janitor-root")
    with pytest.raises(ValueError, match="/tmp"):
        gj.validate_janitor_repo_root(tmp_repo)


def test_b1_git_health_janitor_cli_rejects_tmp_repo_root(capsys):
    args = SimpleNamespace(
        git_health_command="janitor",
        repo="/tmp/hermes-unsafe-janitor-root",
        stale_days=7,
        confirm=None,
    )

    rc = gj.git_health_command(args)

    assert rc == 2
    assert "must not be under /tmp" in capsys.readouterr().out


def test_b2_worktree_gc_supports_dry_run_without_renaming(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / ".hermes"
    orphan = home / "codex-wt" / "sid-orphan"
    (orphan / ".git").mkdir(parents=True)
    broker = WorktreeBroker(repo_root=repo, hermes_home=home)
    broker._git = MagicMock()

    actions = broker.gc(tracked_sids=set(), live_branches=set(), dry_run=True)

    assert orphan.exists(), "dry-run GC must not rename or delete the orphan"
    assert actions
    assert actions[0].sid == "sid-orphan"
    assert actions[0].reason.startswith("dry-run:")
    broker._git.assert_not_called()


@pytest.mark.asyncio
async def test_b2_gc_watcher_dry_run_passes_through_to_broker(tmp_path):
    sessions = tmp_path / "codex_sessions.json"
    sessions.write_text(json.dumps({"version": 1, "sessions": {}}), encoding="utf-8")
    disp = SimpleNamespace(_load_state=lambda: json.loads(sessions.read_text()))
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0

    watcher = CodexGcWatcher(
        dispatcher=disp,
        worktree_broker=broker,
        gh_list_open_branches=lambda: set(),
        dry_run=True,
    )
    await watcher._tick()

    assert broker.gc.call_args.kwargs["dry_run"] is True
    broker.reap_deleted.assert_not_called()


@pytest.mark.asyncio
async def test_b2_gc_watcher_excludes_terminal_rows_from_tracked_sids(tmp_path):
    rows = {
        "t-active": {"session_id": "sid-active", "state": "EXECUTING"},
        "t-complete": {"session_id": "sid-complete", "state": "COMPLETE"},
        "t-escalated": {"session_id": "sid-escalated", "state": "ESCALATED"},
    }
    disp = SimpleNamespace(_load_state=lambda: {"version": 1, "sessions": rows})
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0
    watcher = CodexGcWatcher(dispatcher=disp, worktree_broker=broker, gh_list_open_branches=lambda: set())

    await watcher._tick()

    assert broker.gc.call_args.kwargs["tracked_sids"] == {"sid-active"}


def test_b2_worktree_gc_can_dry_run_tracked_merged_inactive_sid(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / ".hermes"
    tracked = home / "codex-wt" / "sid-tracked"
    tracked.mkdir(parents=True)
    broker = WorktreeBroker(repo_root=repo, hermes_home=home)
    monkeypatch.setattr(broker, "_tmux_session_alive", lambda sid: False)
    monkeypatch.setattr(broker, "_worktree_head", lambda path: "abc123")
    monkeypatch.setattr(broker, "_head_is_ancestor", lambda head, base: True)
    broker._git = MagicMock()

    actions = broker.gc(tracked_sids={"sid-tracked"}, live_branches=set(), dry_run=True)

    assert tracked.exists()
    assert actions[0].sid == "sid-tracked"
    assert "merged into fork/main" in actions[0].reason
    broker._git.assert_not_called()


def test_b2_health_check_stuck_threshold_default_is_15_minutes():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "codex-parallel-health-check.py"
    assert script.exists()
    spec = importlib.util.spec_from_file_location("codex_parallel_health_check_test", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.STUCK_THRESHOLD_MINUTES == 15


def test_b2_health_check_parse_dt_normalizes_zulu_to_aware_utc():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "codex-parallel-health-check.py"
    spec = importlib.util.spec_from_file_location("codex_parallel_health_check_tz_test", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parsed = mod.parse_dt("2026-05-29T07:02:01Z")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_b3_health_check_systemd_timer_installed_in_repo():
    root = Path(__file__).resolve().parents[1]
    service = root / "plugins" / "kanban" / "systemd" / "hermes-codex-health.service"
    timer = root / "plugins" / "kanban" / "systemd" / "hermes-codex-health.timer"

    assert service.exists()
    service_body = service.read_text(encoding="utf-8")
    assert "codex-parallel-health-check.py" in service_body
    assert "Type=oneshot" in service_body
    assert timer.exists()
    timer_body = timer.read_text(encoding="utf-8")
    assert "OnCalendar=" in timer_body or "OnUnitActiveSec=" in timer_body
    assert "Persistent=true" in timer_body


def test_b3_dashboard_snapshot_surfaces_red_liveness(monkeypatch, tmp_path):
    now = datetime(2026, 5, 29, 7, 30, tzinfo=timezone.utc)
    sessions = tmp_path / "codex_sessions.json"
    sessions.write_text(json.dumps({
        "version": 1,
        "sessions": {
            "thread-1": {
                "session_id": "sid-stale",
                "state": "EXECUTING",
                "worktree_path": str(tmp_path / "missing"),
                "created_at": "2026-05-29T06:00:00Z",
                "last_message_at": "2026-05-29T06:30:00Z",
            }
        },
    }), encoding="utf-8")

    monkeypatch.setattr(dcs, "_SESSIONS_PATH", sessions)
    monkeypatch.setattr(dcs, "_REVIEW_STATE_PATH", tmp_path / "review.json")
    monkeypatch.setattr(dcs, "_PORTS_PATH", tmp_path / "ports.json")
    monkeypatch.setattr(dcs, "_now_dt", lambda: now)

    row = dcs._build_snapshot()["sessions"][0]
    assert row["liveness"] == "red"
    assert row["last_activity_age_seconds"] == 3600


def test_b4_run_registry_dir_bootstrapped_by_kanban_init(kanban_home):
    kb.init_db()
    registry = kanban_home / "run-registry"
    assert registry.is_dir()
    assert (registry / ".gitignore").read_text(encoding="utf-8").strip() == "*.lock"


def test_b4_janitor_lock_reader_tolerates_missing_run_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert gj._read_run_registry() == []


def test_b4_janitor_lock_reader_consumes_seeded_b4_lease(kanban_home):
    registry = kb.ensure_run_registry()
    lease = {
        "branch": "worker/b4-lease",
        "worktree_path": "/repo/.worktrees/b4-lease",
        "spawner": "pytest",
        "tmux_session": "pytest-b4",
        "kanban_card_id": "card-123",
        "repo_root": "/repo",
        "created_at": "2026-05-29T07:02:01Z",
    }
    (registry / "sample.lock").write_text(json.dumps(lease), encoding="utf-8")

    locks = gj._read_run_registry()
    lock = gj._lock_for_branch(locks, "worker/b4-lease")

    assert lock is not None
    assert lock["worktree_path"] == lease["worktree_path"]
    assert gj._lock_card_id(lock) == "card-123"
