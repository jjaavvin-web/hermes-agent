"""Tests for agent.worktree_broker — assertions #1-11 and #15 (P1 scope).

Assertions #12, #13, #14 are P5-only and out of scope.
All git and tmux subprocess calls are mocked; no real git operations run.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from agent.worktree_broker import (
    BranchCollisionError,
    DiskPressureError,
    RepoStateError,
    Worktree,
    WorktreeBroker,
    WorktreeStatus,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_broker(
    tmp_path: Path,
    *,
    port_range=(50000, 50008),
    existing_sessions=None,
    fake_repo_root=None,
) -> WorktreeBroker:
    """Create a WorktreeBroker backed by a tmp_path hermes_home."""
    repo_root = fake_repo_root or (tmp_path / "repo")
    repo_root.mkdir(exist_ok=True)
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(exist_ok=True)
    return WorktreeBroker(
        repo_root=repo_root,
        hermes_home=hermes_home,
        port_range=port_range,
        existing_sessions=existing_sessions,
    )


def _ok_git_result() -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


def _fail_git_result(stderr: str) -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = 1
    r.stdout = ""
    r.stderr = stderr
    return r


def _ok_tmux_result() -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


def _fail_tmux_result() -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = 1
    r.stdout = ""
    r.stderr = "can't find session"
    return r


def _df_output(free_kb: int) -> str:
    return (
        "Filesystem     1024-blocks  Used Available Capacity Mounted on\n"
        f"/dev/sda1       999999  1000 {free_kb}   1% /\n"
    )


# ── Assertion #1 — allocate creates worktree path ─────────────────────────────

class TestAllocateCreatesWorktree:
    def test_worktree_path_returned(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000001"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(broker, "_git", return_value=_ok_git_result()) as mock_git,
        ):
            wt = broker.allocate(sid, isa_slug="my-isa")

        expected_path = broker.hermes_home / "codex-wt" / sid
        assert wt.path == expected_path
        assert wt.session_id == sid
        # git was called with worktree add
        mock_git.assert_called_once()
        args = mock_git.call_args[0]
        assert "worktree" in args
        assert "add" in args


# ── Assertion #2 — allocate creates correct branch ───────────────────────────

class TestAllocateBranchName:
    def test_branch_name(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000002"
        slug = "test-isa"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(broker, "_git", return_value=_ok_git_result()) as mock_git,
        ):
            wt = broker.allocate(sid, isa_slug=slug, base_branch="origin/main")

        expected_branch = f"codex/{sid}/{slug}"
        assert wt.branch == expected_branch
        # Verify git was called with -b <branch> <base_branch>
        git_args = mock_git.call_args[0]
        assert "-b" in git_args
        b_idx = list(git_args).index("-b")
        assert git_args[b_idx + 1] == expected_branch
        assert "origin/main" in git_args


# ── Assertion #3 — allocate claims port in codex-ports.json ──────────────────

class TestAllocateClaimsPort:
    def test_port_reserved_in_json(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000003"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(broker, "_git", return_value=_ok_git_result()),
        ):
            wt = broker.allocate(sid, isa_slug="slug")

        assert wt.port is not None
        assert 50000 <= wt.port <= 50007

        ports = json.loads(broker._ports_path().read_text())
        assert ports[str(wt.port)] == sid


# ── Assertion #4 — allocate twice same sid returns same Worktree ──────────────

class TestAllocateIdempotent:
    def test_second_allocate_no_git_call(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000004"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(broker, "_git", return_value=_ok_git_result()) as mock_git,
        ):
            wt1 = broker.allocate(sid, isa_slug="slug")
            wt2 = broker.allocate(sid, isa_slug="slug")

        assert wt1 is wt2
        assert mock_git.call_count == 1  # git called only once


# ── Assertion #5 — release removes worktree, nulls port, clears registry ─────

class TestRelease:
    def test_release_full_teardown(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000005"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(broker, "_git", return_value=_ok_git_result()),
        ):
            wt = broker.allocate(sid, isa_slug="slug")

        port = wt.port
        assert port is not None

        with (
            patch.object(broker, "_git", return_value=_ok_git_result()),
            patch(
                "agent.worktree_broker.subprocess.run",
                return_value=_ok_tmux_result(),
            ),
        ):
            broker.release(sid)

        assert sid not in broker._registry
        ports = json.loads(broker._ports_path().read_text())
        assert ports[str(port)] is None


# ── Assertion #6 — release on unknown sid is a no-op ─────────────────────────

class TestReleaseUnknownSid:
    def test_no_exception_on_unknown(self, tmp_path):
        broker = _make_broker(tmp_path)
        # Should not raise
        broker.release("nonexistent-sid-00000000000000000000")


# ── Assertion #7 — DiskPressureError when df < 4 GB ──────────────────────────

class TestDiskPressure:
    def test_raises_below_4gb(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000007"

        with patch.object(
            broker, "_disk_free_bytes", return_value=3 * 1024**3
        ):
            with pytest.raises(DiskPressureError):
                broker.allocate(sid, isa_slug="slug")

    def test_no_error_above_4gb(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000007b"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=5 * 1024**3),
            patch.object(broker, "_git", return_value=_ok_git_result()),
        ):
            wt = broker.allocate(sid, isa_slug="slug")
        assert wt.session_id == sid


# ── Assertion #8 — RepoStateError on dirty repo ───────────────────────────────

class TestRepoStateError:
    def test_modified_files_in_stderr(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000008"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(
                broker, "_git",
                return_value=_fail_git_result(
                    "error: modified files in the working tree"
                ),
            ),
        ):
            with pytest.raises(RepoStateError):
                broker.allocate(sid, isa_slug="slug")

    def test_untracked_files_in_stderr(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000008b"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(
                broker, "_git",
                return_value=_fail_git_result(
                    "error: untracked files in the working tree"
                ),
            ),
        ):
            with pytest.raises(RepoStateError):
                broker.allocate(sid, isa_slug="slug")


# ── Assertion #9 — BranchCollisionError on "already exists" ──────────────────

class TestBranchCollisionError:
    def test_branch_already_exists(self, tmp_path):
        broker = _make_broker(tmp_path)
        sid = "aaaaaaaa-0000-4000-8000-000000000009"

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(
                broker, "_git",
                return_value=_fail_git_result(
                    "fatal: A branch named 'codex/sid/slug' already exists."
                ),
            ),
        ):
            with pytest.raises(BranchCollisionError):
                broker.allocate(sid, isa_slug="slug")


# ── Assertion #10 — port exhaustion → Worktree.port is None ──────────────────

class TestPortExhaustion:
    def test_all_ports_occupied_port_is_none(self, tmp_path):
        # Only 2 ports in this range for simplicity
        broker = _make_broker(tmp_path, port_range=(50000, 50002))
        sids = [f"aaaaaaaa-0000-4000-8000-{str(i).zfill(12)}" for i in range(3)]

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(broker, "_git", return_value=_ok_git_result()),
        ):
            wt0 = broker.allocate(sids[0], isa_slug="a")
            wt1 = broker.allocate(sids[1], isa_slug="b")
            wt2 = broker.allocate(sids[2], isa_slug="c")

        assert wt0.port == 50000
        assert wt1.port == 50001
        assert wt2.port is None  # no port available, non-fatal


# ── Assertion #11 — port recovery nulls stale sids at __init__ ───────────────

class TestPortRecovery:
    def test_stale_port_nulled_on_init(self, tmp_path):
        """Ports whose sids are absent from codex_sessions.json are nulled."""
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()

        # Write codex-ports.json with a stale sid
        stale_sid = "stale-sid-00000000000000000000000000"
        live_sid = "live-sid-000000000000000000000000000"
        ports_data = {
            "50000": stale_sid,
            "50001": live_sid,
            "50002": None,
        }
        ports_path = hermes_home / "codex-ports.json"
        ports_path.write_text(json.dumps(ports_data))

        # Write codex_sessions.json with only the live sid
        sessions_data = {live_sid: {"path": "/some/path"}}
        (hermes_home / "codex_sessions.json").write_text(
            json.dumps(sessions_data)
        )

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # Instantiate broker — recovery runs in __init__
        WorktreeBroker(
            repo_root=repo_root,
            hermes_home=hermes_home,
            port_range=(50000, 50003),
        )

        recovered = json.loads(ports_path.read_text())
        assert recovered["50000"] is None   # stale sid nulled
        assert recovered["50001"] == live_sid  # live sid preserved
        assert recovered["50002"] is None   # was already null

    def test_missing_sessions_json_nulls_all(self, tmp_path):
        """If codex_sessions.json is absent, all non-null ports are stale."""
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir()

        some_sid = "some-sid-0000000000000000000000000000"
        ports_data = {"50000": some_sid, "50001": None}
        ports_path = hermes_home / "codex-ports.json"
        ports_path.write_text(json.dumps(ports_data))
        # codex_sessions.json intentionally absent

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        WorktreeBroker(
            repo_root=repo_root,
            hermes_home=hermes_home,
            port_range=(50000, 50002),
        )

        recovered = json.loads(ports_path.read_text())
        assert recovered["50000"] is None
        assert recovered["50001"] is None


# ── Assertion #15 — _git uses correct subprocess pattern ─────────────────────

class TestGitSubprocessPattern:
    def test_uses_check_false_capture_output(self, tmp_path):
        """_git() must mirror git_janitor.py:50-55 exactly."""
        broker = _make_broker(tmp_path)

        with patch("agent.worktree_broker.subprocess.run") as mock_run:
            mock_run.return_value = _ok_git_result()
            broker._git("status")

        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("check") is False

    def test_positional_args_include_git_dash_c_and_repo(self, tmp_path):
        broker = _make_broker(tmp_path)
        repo_root_str = str(broker.repo_root)

        with patch("agent.worktree_broker.subprocess.run") as mock_run:
            mock_run.return_value = _ok_git_result()
            broker._git("log", "--oneline")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git"
        assert cmd[1] == "-C"
        assert cmd[2] == repo_root_str
        assert cmd[3] == "log"
        assert cmd[4] == "--oneline"

    def test_no_check_true_ever(self, tmp_path):
        """Confirm check=True is not passed — always check=False."""
        broker = _make_broker(tmp_path)

        with patch("agent.worktree_broker.subprocess.run") as mock_run:
            mock_run.return_value = _ok_git_result()
            broker._git("rev-parse", "HEAD")

        _, kwargs = mock_run.call_args
        assert kwargs.get("check") is False
        assert kwargs.get("check") is not True


# ── Additional edge-case: existing_sessions pre-populates registry ────────────

class TestExistingSessionsHydration:
    def test_registry_prepopulated(self, tmp_path):
        sid = "aaaaaaaa-0000-4000-8000-bbbbbbbbbbbb"
        wt_path = str(tmp_path / "hermes_home" / "codex-wt" / sid)
        broker = _make_broker(
            tmp_path,
            existing_sessions={sid: wt_path},
        )
        assert sid in broker._registry
        assert str(broker._registry[sid].path) == wt_path

    def test_second_allocate_returns_existing_without_git(self, tmp_path):
        sid = "aaaaaaaa-0000-4000-8000-cccccccccccc"
        hermes_home = tmp_path / "hermes_home"
        hermes_home.mkdir(exist_ok=True)
        wt_path = str(hermes_home / "codex-wt" / sid)
        broker = _make_broker(
            tmp_path,
            existing_sessions={sid: wt_path},
        )

        with (
            patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
            patch.object(broker, "_git", return_value=_ok_git_result()) as mock_git,
        ):
            wt = broker.allocate(sid, isa_slug="slug")

        assert wt.session_id == sid
        mock_git.assert_not_called()  # idempotency — no git call
