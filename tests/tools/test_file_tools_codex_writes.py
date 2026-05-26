"""Tests for P1.3: file_tools tool calls pass the contextvar-resolved
absolute path to file_ops so writes land in the codex worktree (not the
tracked-cwd fallback)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.codex_session_context import (
    set_active_worktree,
    reset_active_worktree,
)
import tools.file_tools as ft


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


def _mock_file_ops_write_success() -> MagicMock:
    fops = MagicMock()
    res = MagicMock()
    res.to_dict.return_value = {"bytes": 21, "lint": None}
    fops.write_file.return_value = res
    fops.read_file.return_value = MagicMock(
        to_dict=lambda: {"content": "stub", "file_size": 4, "total_lines": 1},
        content="stub",
    )
    fops.patch_replace.return_value = MagicMock(
        to_dict=lambda: {"replacements": 1}
    )
    return fops


def test_write_file_passes_resolved_path_to_file_ops(codex_worktree, monkeypatch):
    """write_file_tool must forward the worktree-resolved absolute path
    to file_ops.write_file — NOT the original relative path."""
    fops = _mock_file_ops_write_success()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
    monkeypatch.setattr(ft, "_check_file_staleness", lambda p, t: None)
    monkeypatch.setattr(ft.file_state, "lock_path", lambda p: __import__("contextlib").nullcontext())
    monkeypatch.setattr(ft.file_state, "check_stale", lambda t, r: None)
    monkeypatch.setattr(ft.file_state, "note_write", lambda t, r: None)

    ft.write_file_tool("smoke.txt", "P1.3 sentinel", task_id="t-1")

    fops.write_file.assert_called_once()
    written_path = fops.write_file.call_args.args[0]
    # Must be an absolute path inside the codex worktree.
    assert os.path.isabs(str(written_path))
    assert str(written_path).startswith(str(codex_worktree.resolve()))
    assert str(written_path).endswith("smoke.txt")


def test_read_file_passes_resolved_path_to_file_ops(codex_worktree, monkeypatch, tmp_path):
    """read_file_tool must forward the worktree-resolved path."""
    target = codex_worktree / "smoke.txt"
    target.write_text("P1.3 sentinel")
    fops = _mock_file_ops_write_success()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)

    # Skip device + binary + hub guards by passing a regular extension.
    ft.read_file_tool("smoke.txt", task_id="t-2")

    fops.read_file.assert_called_once()
    read_path = fops.read_file.call_args.args[0]
    assert os.path.isabs(str(read_path))
    assert str(read_path).startswith(str(codex_worktree.resolve()))


def test_patch_replace_passes_resolved_path_to_file_ops(codex_worktree, monkeypatch):
    """patch_tool (replace mode) must forward the worktree-resolved path."""
    target = codex_worktree / "smoke.txt"
    target.write_text("old line\n")
    fops = _mock_file_ops_write_success()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
    monkeypatch.setattr(ft, "_check_file_staleness", lambda p, t: None)
    monkeypatch.setattr(ft.file_state, "lock_path", lambda p: __import__("contextlib").nullcontext())
    monkeypatch.setattr(ft.file_state, "check_stale", lambda t, r: None)

    ft.patch_tool(mode="replace", path="smoke.txt", old_string="old",
                  new_string="new", task_id="t-3")

    fops.patch_replace.assert_called_once()
    patch_path = fops.patch_replace.call_args.args[0]
    assert os.path.isabs(str(patch_path))
    assert str(patch_path).startswith(str(codex_worktree.resolve()))


def test_no_contextvar_keeps_original_path(monkeypatch, tmp_path):
    """When the contextvar is unset (regular Discord chat), the path that
    reaches file_ops is still the resolver's output — which falls back to
    TERMINAL_CWD-based resolution.  P1.3 preserves the path that ends up
    passed: it's absolute, NOT bare relative."""
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    fops = _mock_file_ops_write_success()
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": fops)
    monkeypatch.setattr(ft, "_check_sensitive_path", lambda p, t: None)
    monkeypatch.setattr(ft, "_check_file_staleness", lambda p, t: None)
    monkeypatch.setattr(ft.file_state, "lock_path", lambda p: __import__("contextlib").nullcontext())
    monkeypatch.setattr(ft.file_state, "check_stale", lambda t, r: None)
    monkeypatch.setattr(ft.file_state, "note_write", lambda t, r: None)

    ft.write_file_tool("smoke.txt", "no-codex", task_id="t-no-codex")
    written = fops.write_file.call_args.args[0]
    # Still an absolute path (good — resolver always absolutizes).
    assert os.path.isabs(str(written))
    # And it landed under TERMINAL_CWD (the resolver's fallback).
    assert str(written).startswith(str(tmp_path.resolve()))
