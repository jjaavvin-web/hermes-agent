"""Fail-closed worktree-confinement tests for bound Codex/file-tool lanes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agent.codex_session_context as ctx
import tools.file_tools as ft


def _json(raw: str) -> dict:
    return json.loads(raw)


@pytest.fixture(autouse=True)
def _clean_worktree_context(monkeypatch, request):
    """Keep direct ContextVar manipulation confined to each test."""
    import tools.terminal_tool as _tt

    def _is_last_test_in_this_module() -> bool:
        try:
            items = request.session.items
            index = items.index(request.node)
            this_file = Path(__file__).resolve()
            return not any(Path(str(item.fspath)).resolve() == this_file for item in items[index + 1 :])
        except Exception:
            return False

    file_ops_snapshot = None
    active_env_snapshot = None
    try:
        if hasattr(ft, "_file_ops_cache") and hasattr(ft, "_file_ops_lock"):
            with ft._file_ops_lock:
                file_ops_snapshot = dict(ft._file_ops_cache)
                ft._file_ops_cache.clear()
    except Exception:
        file_ops_snapshot = None
    try:
        if hasattr(_tt, "_active_environments") and hasattr(_tt, "_env_lock"):
            with _tt._env_lock:
                active_env_snapshot = dict(_tt._active_environments)
                _tt._active_environments.clear()
    except Exception:
        active_env_snapshot = None
    active_token = ctx._active_worktree_var.set(None)
    required_token = ctx._confinement_required_var.set(False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    try:
        yield
    finally:
        ctx._confinement_required_var.reset(required_token)
        ctx._active_worktree_var.reset(active_token)
        try:
            if file_ops_snapshot is not None and hasattr(ft, "_file_ops_cache") and hasattr(ft, "_file_ops_lock"):
                with ft._file_ops_lock:
                    ft._file_ops_cache.clear()
                    ft._file_ops_cache.update(file_ops_snapshot)
        except Exception:
            pass
        try:
            if active_env_snapshot is not None and hasattr(_tt, "_active_environments") and hasattr(_tt, "_env_lock"):
                with _tt._env_lock:
                    _tt._active_environments.clear()
                    _tt._active_environments.update(active_env_snapshot)
        except Exception:
            pass
        if _is_last_test_in_this_module():
            try:
                if hasattr(ft, "_file_ops_cache") and hasattr(ft, "_file_ops_lock"):
                    with ft._file_ops_lock:
                        ft._file_ops_cache.clear()
            except Exception:
                pass
            try:
                if hasattr(_tt, "_active_environments") and hasattr(_tt, "_env_lock"):
                    with _tt._env_lock:
                        _tt._active_environments.clear()
            except Exception:
                pass


def test_marker_lifecycle_bind_reset(tmp_path):
    token = ctx.set_active_worktree(str(tmp_path))
    try:
        assert ctx.get_active_worktree() == str(tmp_path)
        assert ctx.is_worktree_confinement_required() is True
    finally:
        ctx.reset_active_worktree(token)

    assert ctx.get_active_worktree() is None
    assert ctx.is_worktree_confinement_required() is False


def test_deleted_bound_worktree_relative_write_denied_no_side_effect(tmp_path, monkeypatch):
    worktree = tmp_path / "bound-wt"
    worktree.mkdir()
    decoy_cwd = tmp_path / "decoy-cwd"
    decoy_cwd.mkdir()
    monkeypatch.chdir(decoy_cwd)
    monkeypatch.setattr(ft, "_get_live_tracking_cwd", lambda task_id="default": None)
    token = ctx.set_active_worktree(str(worktree))
    worktree.rmdir()
    try:
        out = _json(ft.write_file_tool("probe.txt", "should-not-write", task_id="deleted-wt"))
    finally:
        ctx.reset_active_worktree(token)

    assert "WORKTREE_CONFINEMENT" in out["error"]
    assert not (worktree / "probe.txt").exists()
    assert not (decoy_cwd / "probe.txt").exists()
    assert not (Path(os.getcwd()) / "probe.txt").exists()


def test_marker_true_missing_bind_denies_without_tier2_or_tier3_fallback(tmp_path, monkeypatch):
    live_poison = tmp_path / "live-poison"
    env_poison = tmp_path / "env-poison"
    cwd_poison = tmp_path / "cwd-poison"
    for path in (live_poison, env_poison, cwd_poison):
        path.mkdir()
    monkeypatch.chdir(cwd_poison)
    monkeypatch.setenv("TERMINAL_CWD", str(env_poison))

    def _poison_live_tracking(task_id="default"):
        raise AssertionError("tier-2 live cwd fallback was used")

    monkeypatch.setattr(ft, "_get_live_tracking_cwd", _poison_live_tracking)
    active_token = ctx._active_worktree_var.set(None)
    required_token = ctx._confinement_required_var.set(True)
    try:
        out = _json(ft.write_file_tool("probe.txt", "should-not-write", task_id="missing-bind"))
    finally:
        ctx._confinement_required_var.reset(required_token)
        ctx._active_worktree_var.reset(active_token)

    assert "WORKTREE_CONFINEMENT" in out["error"]
    assert not (live_poison / "probe.txt").exists()
    assert not (env_poison / "probe.txt").exists()
    assert not (cwd_poison / "probe.txt").exists()


def test_legacy_unbound_relative_write_still_uses_existing_fallback_chain(tmp_path, monkeypatch):
    legacy_base = tmp_path / "legacy-base"
    legacy_base.mkdir()
    monkeypatch.setattr(ft, "_get_live_tracking_cwd", lambda task_id="default": str(legacy_base))

    out = _json(ft.write_file_tool("legacy.txt", "legacy-ok", task_id="legacy"))

    assert out.get("error") is None
    assert out["resolved_path"] == str((legacy_base / "legacy.txt").resolve())
    assert (legacy_base / "legacy.txt").read_text(encoding="utf-8") == "legacy-ok"


def test_valid_bound_worktree_relative_and_absolute_writes_allowed_and_sensitive_denied(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "valid-wt"
    worktree.mkdir()
    decoy_cwd = tmp_path / "decoy-cwd"
    decoy_cwd.mkdir()
    monkeypatch.chdir(decoy_cwd)
    monkeypatch.setattr(ft, "_get_live_tracking_cwd", lambda task_id="default": str(decoy_cwd))
    token = ctx.set_active_worktree(str(worktree))
    try:
        rel_out = _json(ft.write_file_tool("rel.txt", "rel-ok", task_id="bound-positive"))
        abs_inside = worktree / "abs.txt"
        abs_out = _json(ft.write_file_tool(str(abs_inside), "abs-ok", task_id="bound-positive"))
        sensitive_out = _json(ft.write_file_tool("/etc/hermes-f3-denied", "nope", task_id="bound-positive"))
    finally:
        ctx.reset_active_worktree(token)

    assert rel_out.get("error") is None
    assert rel_out["resolved_path"] == str((worktree / "rel.txt").resolve())
    assert (worktree / "rel.txt").read_text(encoding="utf-8") == "rel-ok"
    assert not (decoy_cwd / "rel.txt").exists()

    assert abs_out.get("error") is None
    assert abs_out["resolved_path"] == str(abs_inside.resolve())
    assert abs_inside.read_text(encoding="utf-8") == "abs-ok"

    assert "sensitive system path" in sensitive_out["error"]
    assert not Path("/etc/hermes-f3-denied").exists()


def test_patch_replace_deleted_worktree_denied_no_side_effect(tmp_path, monkeypatch):
    worktree = tmp_path / "bound-wt"
    worktree.mkdir()
    decoy_cwd = tmp_path / "decoy-cwd"
    decoy_cwd.mkdir()
    (decoy_cwd / "patch_target.txt").write_text("old\n", encoding="utf-8")
    monkeypatch.chdir(decoy_cwd)
    monkeypatch.setattr(ft, "_get_live_tracking_cwd", lambda task_id="default": str(decoy_cwd))
    token = ctx.set_active_worktree(str(worktree))
    worktree.rmdir()
    try:
        out = _json(
            ft.patch_tool(
                mode="replace",
                path="patch_target.txt",
                old_string="old",
                new_string="new",
                task_id="patch-deleted-wt",
            )
        )
    finally:
        ctx.reset_active_worktree(token)

    assert "WORKTREE_CONFINEMENT" in out["error"]
    assert (decoy_cwd / "patch_target.txt").read_text(encoding="utf-8") == "old\n"
    assert not (worktree / "patch_target.txt").exists()
