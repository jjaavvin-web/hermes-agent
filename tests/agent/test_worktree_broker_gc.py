"""Tests for WorktreeBroker.gc + reap_deleted (P5)."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.worktree_broker import GcAction, WorktreeBroker


def _make_broker(tmp_path: Path) -> WorktreeBroker:
    """Lightweight broker pointed at a tmp hermes_home — git ops mocked at call sites."""
    broker = WorktreeBroker.__new__(WorktreeBroker)
    broker.repo_root = tmp_path / "repo"
    broker.hermes_home = tmp_path / "home"
    broker.hermes_home.mkdir(parents=True, exist_ok=True)
    broker.port_range = (50000, 50008)
    broker._registry = {}
    # gc + reap don't touch _git in any branch we test; replace anyway.
    broker._git = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    return broker


def _seed_worktree(broker: WorktreeBroker, sid: str) -> Path:
    p = broker.hermes_home / "codex-wt" / sid
    p.mkdir(parents=True, exist_ok=True)
    (p / "some_file.txt").write_text("hi", encoding="utf-8")
    return p


# ── gc ────────────────────────────────────────────────────────────────


class TestGc:
    def test_no_codex_wt_dir_returns_empty(self, tmp_path):
        broker = _make_broker(tmp_path)
        assert broker.gc(tracked_sids=set()) == []

    def test_tracked_sids_are_left_alone(self, tmp_path):
        broker = _make_broker(tmp_path)
        _seed_worktree(broker, "sid-a")
        _seed_worktree(broker, "sid-b")
        actions = broker.gc(tracked_sids={"sid-a", "sid-b"})
        assert actions == []
        assert (broker.hermes_home / "codex-wt" / "sid-a").is_dir()
        assert (broker.hermes_home / "codex-wt" / "sid-b").is_dir()

    def test_untracked_sid_is_renamed_to_deleted_bucket(self, tmp_path):
        broker = _make_broker(tmp_path)
        _seed_worktree(broker, "sid-orphan")
        _seed_worktree(broker, "sid-tracked")
        actions = broker.gc(tracked_sids={"sid-tracked"})
        assert len(actions) == 1
        action = actions[0]
        assert action.sid == "sid-orphan"
        assert ".deleted-" in str(action.new_path)
        # Tracked sid is untouched; orphan is gone from its original location.
        assert (broker.hermes_home / "codex-wt" / "sid-tracked").is_dir()
        assert not (broker.hermes_home / "codex-wt" / "sid-orphan").is_dir()
        # The new path under .deleted-<ts>/ exists.
        assert action.new_path.is_dir()

    def test_open_pr_branch_keeps_worktree(self, tmp_path):
        broker = _make_broker(tmp_path)
        _seed_worktree(broker, "sid-pr-open")
        actions = broker.gc(
            tracked_sids=set(),
            live_branches={"codex/sid-pr-open/some-isa/"},
        )
        # Branch match → no gc.
        assert actions == []
        assert (broker.hermes_home / "codex-wt" / "sid-pr-open").is_dir()

    def test_deleted_bucket_dirs_skipped(self, tmp_path):
        broker = _make_broker(tmp_path)
        (broker.hermes_home / "codex-wt" / ".deleted-20260101T000000Z").mkdir(parents=True)
        actions = broker.gc(tracked_sids=set())
        # Hidden dirs are never gc'd.
        assert actions == []


# ── reap_deleted ──────────────────────────────────────────────────────


class TestReapDeleted:
    def test_purges_dirs_older_than_threshold(self, tmp_path):
        broker = _make_broker(tmp_path)
        wt_root = broker.hermes_home / "codex-wt"
        old = wt_root / ".deleted-20200101T000000Z"
        old.mkdir(parents=True)
        (old / "file").write_text("x", encoding="utf-8")
        purged = broker.reap_deleted(max_age_days=7)
        assert purged == 1
        assert not old.is_dir()

    def test_keeps_recent_dirs(self, tmp_path):
        broker = _make_broker(tmp_path)
        wt_root = broker.hermes_home / "codex-wt"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        recent = wt_root / f".deleted-{ts}"
        recent.mkdir(parents=True)
        (recent / "file").write_text("x", encoding="utf-8")
        purged = broker.reap_deleted(max_age_days=7)
        assert purged == 0
        assert recent.is_dir()

    def test_unparseable_timestamp_is_skipped(self, tmp_path):
        broker = _make_broker(tmp_path)
        wt_root = broker.hermes_home / "codex-wt"
        weird = wt_root / ".deleted-not-a-timestamp"
        weird.mkdir(parents=True)
        purged = broker.reap_deleted(max_age_days=7)
        assert purged == 0
        assert weird.is_dir()


# ── anti probes ───────────────────────────────────────────────────────


class TestAntiProbes:
    def test_no_rm_rf_in_worktree_broker(self):
        import agent.worktree_broker as _m
        src = Path(_m.__file__).read_text(encoding="utf-8")
        import re
        cleaned = re.sub(r'"""[\s\S]*?"""', '', src)
        assert "rm -rf" not in cleaned
        assert "git clean -fxd" not in cleaned
