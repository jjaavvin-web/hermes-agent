"""Tests for P1.5 sandbox: refuse writes outside the active codex worktree."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.codex_session_context import (
    set_active_worktree,
    reset_active_worktree,
)
import tools.file_tools as ft
from tools.file_tools import _enforce_codex_sandbox


@pytest.fixture
def codex_worktree(tmp_path):
    """Bind a temp dir as the active codex worktree."""
    wt = tmp_path / "codex-wt" / "test-sid"
    wt.mkdir(parents=True)
    token = set_active_worktree(str(wt))
    try:
        yield wt
    finally:
        reset_active_worktree(token)


def _mock_file_ops_ok() -> MagicMock:
    fops = MagicMock()
    res = MagicMock()
    res.to_dict.return_value = {"bytes": 10}
    fops.write_file.return_value = res
    fops.patch_replace.return_value = MagicMock(to_dict=lambda: {"replacements": 1})
    return fops


# ─── _enforce_codex_sandbox unit tests ──────────────────────────────────


class TestEnforceCodexSandbox:
    def test_no_contextvar_allows_anything(self, tmp_path):
        # No contextvar set — sandbox is inert.
        assert _enforce_codex_sandbox(str(tmp_path / "anywhere.txt"), "write_file") is None

    def test_inside_worktree_allowed(self, codex_worktree):
        target = codex_worktree / "subdir" / "file.txt"
        assert _enforce_codex_sandbox(str(target), "write_file") is None

    def test_worktree_root_allowed(self, codex_worktree):
        target = codex_worktree / "file.txt"
        assert _enforce_codex_sandbox(str(target), "write_file") is None

    def test_outside_worktree_denied(self, codex_worktree, tmp_path):
        outside = tmp_path / "elsewhere" / "file.txt"
        err = _enforce_codex_sandbox(str(outside), "write_file")
        assert err is not None
        assert "CODEX_SANDBOX" in err
        assert "write_file" in err
        assert str(outside) in err

    def test_op_name_appears_in_error(self, codex_worktree, tmp_path):
        outside = tmp_path / "elsewhere" / "file.txt"
        err = _enforce_codex_sandbox(str(outside), "patch")
        assert "patch denied" in err

    def test_live_tree_corruption_path_denied(self, codex_worktree, tmp_path):
        """The exact corruption mode we caught in production:
        agent uses an absolute path from /home/josep/.local/share/hermes-agent/.
        Must be denied."""
        live_tree_target = tmp_path / "live-tree" / "codex-smoke.txt"
        live_tree_target.parent.mkdir(parents=True, exist_ok=True)
        err = _enforce_codex_sandbox(str(live_tree_target), "write_file")
        assert err is not None
        assert "outside the active codex session worktree" in err

    def test_stale_contextvar_at_deleted_dir_is_inert(self, tmp_path):
        """If contextvar points at a deleted directory, sandbox falls
        through to None (defer to OS) rather than building a phantom comparison."""
        bogus = tmp_path / "does" / "not" / "exist"
        token = set_active_worktree(str(bogus))
        try:
            assert _enforce_codex_sandbox(str(tmp_path / "any.txt"), "write_file") is None
        finally:
            reset_active_worktree(token)

    def test_symlinked_worktree_inside_is_allowed(self, codex_worktree, tmp_path):
        """Worktree referenced by symlink: realpath canonicalization makes
        the inside-check work even when paths use different routes."""
        link = tmp_path / "wt-symlink"
        link.symlink_to(codex_worktree)
        # Re-bind via the symlinked path.
        reset_active_worktree(set_active_worktree(str(link)))
        token = set_active_worktree(str(link))
        try:
            target_via_real = codex_worktree / "file.txt"
            target_via_link = link / "file.txt"
            # Both should resolve to the same canonical place — both allowed.
            assert _enforce_codex_sandbox(str(target_via_real), "write_file") is None
            assert _enforce_codex_sandbox(str(target_via_link), "write_file") is None
        finally:
            reset_active_worktree(token)


# ─── End-to-end tool integration tests ───────────────────────────────────


class TestEnforceCodexSandboxAllowlist:
    """The opt-in extra-roots allowlist lets _enforce_codex_sandbox permit
    specific paths outside the worktree without bypassing the guard wholesale."""

    def test_allowlist_permits_outside_path(self, codex_worktree, tmp_path, monkeypatch):
        outside = tmp_path / "vault"
        outside.mkdir()
        monkeypatch.setenv("HERMES_CODEX_SANDBOX_ALLOW", str(outside))
        import agent.codex_sandbox_allowlist as allow
        allow.reset_cache_for_tests()
        target = outside / "note.md"
        assert _enforce_codex_sandbox(str(target), "write_file") is None

    def test_allowlist_does_not_open_unrelated_paths(
        self, codex_worktree, tmp_path, monkeypatch
    ):
        allowed = tmp_path / "vault"
        allowed.mkdir()
        unrelated = tmp_path / "elsewhere"
        unrelated.mkdir()
        monkeypatch.setenv("HERMES_CODEX_SANDBOX_ALLOW", str(allowed))
        import agent.codex_sandbox_allowlist as allow
        allow.reset_cache_for_tests()
        err = _enforce_codex_sandbox(str(unrelated / "leak.txt"), "write_file")
        assert err is not None
        assert "CODEX_SANDBOX" in err

    def test_default_no_allowlist_preserves_original_behavior(
        self, codex_worktree, tmp_path, monkeypatch
    ):
        """Regression guard: with no allowlist configured, every outside
        path is still denied — original P1.5 semantics unchanged."""
        monkeypatch.delenv("HERMES_CODEX_SANDBOX_ALLOW", raising=False)
        # Point hermes_home at an empty dir so no real ~/.hermes/codex-sandbox-allow.yaml
        # leaks into the test.
        import hermes_constants
        empty = tmp_path / "empty-hermes-home"
        empty.mkdir()
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: empty)
        import agent.codex_sandbox_allowlist as allow
        allow.reset_cache_for_tests()
        err = _enforce_codex_sandbox(str(tmp_path / "anywhere.txt"), "write_file")
        assert err is not None
        assert "CODEX_SANDBOX" in err

    def test_allowlist_error_message_points_at_config_file(
        self, codex_worktree, tmp_path, monkeypatch
    ):
        """When a write is refused, the error mentions the allowlist config
        so an agent or operator can self-serve the fix."""
        monkeypatch.delenv("HERMES_CODEX_SANDBOX_ALLOW", raising=False)
        import hermes_constants
        empty = tmp_path / "empty-hermes-home"
        empty.mkdir()
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: empty)
        import agent.codex_sandbox_allowlist as allow
        allow.reset_cache_for_tests()
        err = _enforce_codex_sandbox(str(tmp_path / "nope.txt"), "write_file")
        assert err is not None
        assert "codex-sandbox-allow.yaml" in err


class TestWriteFileSandbox:
    def test_write_inside_worktree_succeeds(self, codex_worktree, monkeypatch):
        fops = _mock_file_ops_ok()
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
        monkeypatch.setattr(ft, "_check_file_staleness", lambda p, t: None)
        monkeypatch.setattr(ft.file_state, "lock_path", lambda p: __import__("contextlib").nullcontext())
        monkeypatch.setattr(ft.file_state, "check_stale", lambda t, r: None)
        monkeypatch.setattr(ft.file_state, "note_write", lambda t, r: None)

        out = ft.write_file_tool("smoke.txt", "ok", task_id="t-inside")
        # Successful write — no sandbox denial.
        assert "CODEX_SANDBOX" not in out
        fops.write_file.assert_called_once()

    def test_write_outside_worktree_denied(self, codex_worktree, tmp_path, monkeypatch):
        """Agent uses an absolute path outside the worktree — write must be denied."""
        fops = _mock_file_ops_ok()
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)

        # Target an absolute path outside the worktree (simulates the
        # production bug: agent runs os.path.abspath in a terminal cwd-leaked
        # subprocess, gets the live-tree path, then re-writes there).
        outside = tmp_path / "live-tree-copy.txt"
        out = ft.write_file_tool(str(outside), "leak", task_id="t-outside")

        assert "CODEX_SANDBOX" in out
        # The actual file_ops.write_file should NOT have been called.
        fops.write_file.assert_not_called()
        # The file should NOT exist on disk.
        assert not outside.exists()

    def test_write_no_contextvar_unrestricted(self, tmp_path, monkeypatch):
        """Regular Discord chat (no codex session) — writes anywhere allowed."""
        fops = _mock_file_ops_ok()
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
        monkeypatch.setattr(ft, "_check_file_staleness", lambda p, t: None)
        monkeypatch.setattr(ft.file_state, "lock_path", lambda p: __import__("contextlib").nullcontext())
        monkeypatch.setattr(ft.file_state, "check_stale", lambda t, r: None)
        monkeypatch.setattr(ft.file_state, "note_write", lambda t, r: None)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

        out = ft.write_file_tool("anywhere.txt", "fine", task_id="t-no-codex")
        assert "CODEX_SANDBOX" not in out
        fops.write_file.assert_called_once()


class TestPatchSandbox:
    def test_patch_inside_worktree_succeeds(self, codex_worktree, monkeypatch):
        target = codex_worktree / "f.py"
        target.write_text("old line\n")
        fops = _mock_file_ops_ok()
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
        monkeypatch.setattr(ft, "_check_file_staleness", lambda p, t: None)
        monkeypatch.setattr(ft.file_state, "lock_path", lambda p: __import__("contextlib").nullcontext())
        monkeypatch.setattr(ft.file_state, "check_stale", lambda t, r: None)

        out = ft.patch_tool(mode="replace", path="f.py", old_string="old",
                            new_string="new", task_id="t-patch-inside")
        assert "CODEX_SANDBOX" not in out

    def test_patch_outside_worktree_denied(self, codex_worktree, tmp_path, monkeypatch):
        fops = _mock_file_ops_ok()
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)

        # Absolute path outside the worktree.
        outside = tmp_path / "elsewhere.py"
        outside.write_text("old\n")
        out = ft.patch_tool(mode="replace", path=str(outside), old_string="old",
                            new_string="new", task_id="t-patch-outside")
        assert "CODEX_SANDBOX" in out
        fops.patch_replace.assert_not_called()
