"""Model B codex sandbox (2026-05-28): deny writes into OTHER code trees (the
live source checkout + sibling worktrees); allow config / state / dotfiles
outside any repo.

Supersedes the original P1.5 "deny everything outside the active worktree"
contract — a tracked codex Discord thread may now edit shared config (e.g.
``~/.hermes/config.yaml``) but still cannot clobber the live tree or a sibling
thread's worktree.
"""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.codex_session_context import set_active_worktree, reset_active_worktree
import tools.file_tools as ft
from tools.file_tools import _enforce_codex_sandbox


@pytest.fixture(autouse=True)
def _reset_protected_cache():
    """The protected-roots list is process-cached; reset around every test."""
    ft._CODEX_PROTECTED_CACHE = None
    yield
    ft._CODEX_PROTECTED_CACHE = None


@pytest.fixture
def codex_env(tmp_path, monkeypatch):
    """Hermetic Model-B environment.

    - ``wt``            active worktree (under codex_wt_root)
    - ``main_repo``     protected 'live source checkout'
    - ``codex_wt_root`` protected parent of all worktrees (sibling protection)
    - ``outside``       a config-ish dir outside every code tree (writable)
    """
    codex_wt_root = tmp_path / "codex-wt"
    wt = codex_wt_root / "test-sid"
    wt.mkdir(parents=True)
    main_repo = tmp_path / "live-checkout"
    main_repo.mkdir()
    outside = tmp_path / "dot-hermes"
    outside.mkdir()
    monkeypatch.setattr(
        ft,
        "_codex_protected_code_roots",
        lambda wt_real: (
            os.path.realpath(str(main_repo)),
            os.path.realpath(str(codex_wt_root)),
        ),
    )
    token = set_active_worktree(str(wt))
    try:
        yield SimpleNamespace(
            wt=wt, main_repo=main_repo, codex_wt_root=codex_wt_root,
            outside=outside, tmp=tmp_path,
        )
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
        assert _enforce_codex_sandbox(str(tmp_path / "anywhere.txt"), "write_file") is None

    def test_inside_worktree_allowed(self, codex_env):
        target = codex_env.wt / "subdir" / "file.txt"
        assert _enforce_codex_sandbox(str(target), "write_file") is None

    def test_worktree_root_allowed(self, codex_env):
        assert _enforce_codex_sandbox(str(codex_env.wt / "file.txt"), "write_file") is None

    def test_live_checkout_denied(self, codex_env):
        """The corruption mode we caught in production: a write into the live
        source checkout must still be a loud refusal."""
        err = _enforce_codex_sandbox(str(codex_env.main_repo / "agent" / "x.py"), "write_file")
        assert err is not None
        assert "CODEX_SANDBOX" in err
        assert "another code tree" in err

    def test_sibling_worktree_denied(self, codex_env):
        """A thread must not write into a *sibling* thread's worktree."""
        sibling = codex_env.codex_wt_root / "other-sid" / "file.txt"
        err = _enforce_codex_sandbox(str(sibling), "write_file")
        assert err is not None
        assert "CODEX_SANDBOX" in err

    def test_config_outside_repos_allowed(self, codex_env):
        """Model B's reason for existing: shared config/state outside any code
        tree is now writable from a tracked codex thread."""
        cfg = codex_env.outside / "config.yaml"
        assert _enforce_codex_sandbox(str(cfg), "write_file") is None

    def test_op_name_appears_in_error(self, codex_env):
        err = _enforce_codex_sandbox(str(codex_env.main_repo / "f"), "patch")
        assert err is not None
        assert "patch denied" in err

    def test_error_points_at_allowlist(self, codex_env):
        err = _enforce_codex_sandbox(str(codex_env.main_repo / "f"), "write_file")
        assert "codex-sandbox-allow.yaml" in err

    def test_stale_contextvar_at_deleted_dir_is_inert(self, tmp_path):
        bogus = tmp_path / "does" / "not" / "exist"
        token = set_active_worktree(str(bogus))
        try:
            assert _enforce_codex_sandbox(str(tmp_path / "any.txt"), "write_file") is None
        finally:
            reset_active_worktree(token)

    def test_symlinked_worktree_inside_is_allowed(self, codex_env, tmp_path):
        link = tmp_path / "wt-symlink"
        link.symlink_to(codex_env.wt)
        token = set_active_worktree(str(link))
        try:
            assert _enforce_codex_sandbox(str(codex_env.wt / "file.txt"), "write_file") is None
            assert _enforce_codex_sandbox(str(link / "file.txt"), "write_file") is None
        finally:
            reset_active_worktree(token)


class TestEnforceCodexSandboxAllowlist:
    def test_allowlist_permits_path_in_protected_root(self, codex_env, monkeypatch):
        """An explicit allowlist entry overrides the protected-tree denial —
        opt-in operator config wins."""
        allowed = codex_env.main_repo / "generated"
        monkeypatch.setenv("HERMES_CODEX_SANDBOX_ALLOW", str(allowed))
        import agent.codex_sandbox_allowlist as allow
        allow.reset_cache_for_tests()
        assert _enforce_codex_sandbox(str(allowed / "out.txt"), "write_file") is None

    def test_allowlist_does_not_open_other_protected_paths(self, codex_env, monkeypatch):
        allowed = codex_env.main_repo / "generated"
        monkeypatch.setenv("HERMES_CODEX_SANDBOX_ALLOW", str(allowed))
        import agent.codex_sandbox_allowlist as allow
        allow.reset_cache_for_tests()
        err = _enforce_codex_sandbox(str(codex_env.main_repo / "elsewhere.py"), "write_file")
        assert err is not None
        assert "CODEX_SANDBOX" in err


# ─── End-to-end tool integration tests ───────────────────────────────────


class TestWriteFileSandbox:
    def _patch_common(self, monkeypatch, fops):
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
        monkeypatch.setattr(ft, "_check_file_staleness", lambda p, t: None)
        monkeypatch.setattr(ft.file_state, "lock_path", lambda p: contextlib.nullcontext())
        monkeypatch.setattr(ft.file_state, "check_stale", lambda t, r: None)
        monkeypatch.setattr(ft.file_state, "note_write", lambda t, r: None)

    def test_write_inside_worktree_succeeds(self, codex_env, monkeypatch):
        fops = _mock_file_ops_ok()
        self._patch_common(monkeypatch, fops)
        out = ft.write_file_tool(str(codex_env.wt / "smoke.txt"), "ok", task_id="t-inside")
        assert "CODEX_SANDBOX" not in out
        fops.write_file.assert_called_once()

    def test_write_into_live_checkout_denied(self, codex_env, monkeypatch):
        fops = _mock_file_ops_ok()
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
        target = codex_env.main_repo / "tools" / "leak.py"
        out = ft.write_file_tool(str(target), "leak", task_id="t-live")
        assert "CODEX_SANDBOX" in out
        fops.write_file.assert_not_called()
        assert not target.exists()

    def test_write_config_outside_repos_succeeds(self, codex_env, monkeypatch):
        """Model B: writing shared config from a codex thread now works."""
        fops = _mock_file_ops_ok()
        self._patch_common(monkeypatch, fops)
        out = ft.write_file_tool(str(codex_env.outside / "config.yaml"), "k: v", task_id="t-cfg")
        assert "CODEX_SANDBOX" not in out
        fops.write_file.assert_called_once()

    def test_write_no_contextvar_unrestricted(self, tmp_path, monkeypatch):
        fops = _mock_file_ops_ok()
        self._patch_common(monkeypatch, fops)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        out = ft.write_file_tool("anywhere.txt", "fine", task_id="t-no-codex")
        assert "CODEX_SANDBOX" not in out
        fops.write_file.assert_called_once()


class TestPatchSandbox:
    def test_patch_inside_worktree_succeeds(self, codex_env, monkeypatch):
        target = codex_env.wt / "f.py"
        target.write_text("old line\n")
        fops = _mock_file_ops_ok()
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
        monkeypatch.setattr(ft, "_check_file_staleness", lambda p, t: None)
        monkeypatch.setattr(ft.file_state, "lock_path", lambda p: contextlib.nullcontext())
        monkeypatch.setattr(ft.file_state, "check_stale", lambda t, r: None)
        out = ft.patch_tool(mode="replace", path=str(target), old_string="old",
                            new_string="new", task_id="t-patch-inside")
        assert "CODEX_SANDBOX" not in out

    def test_patch_into_live_checkout_denied(self, codex_env, monkeypatch):
        fops = _mock_file_ops_ok()
        monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
        monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
        target = codex_env.main_repo / "elsewhere.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old\n")
        out = ft.patch_tool(mode="replace", path=str(target), old_string="old",
                            new_string="new", task_id="t-patch-live")
        assert "CODEX_SANDBOX" in out
        fops.patch_replace.assert_not_called()
