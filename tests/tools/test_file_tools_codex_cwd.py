"""Tests for P1.2: _resolve_path_for_task honors codex worktree contextvar."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.codex_session_context import (
    set_active_worktree,
    reset_active_worktree,
)
from tools.file_tools import _resolve_path_for_task


@pytest.fixture
def codex_worktree(tmp_path):
    """Create a temp worktree dir and bind it via the contextvar."""
    wt = tmp_path / "codex-wt" / "test-sid"
    wt.mkdir(parents=True)
    token = set_active_worktree(str(wt))
    try:
        yield wt
    finally:
        reset_active_worktree(token)


def test_relative_path_resolves_into_worktree(codex_worktree):
    """A relative path inside a codex thread MUST land in the worktree,
    even before any bash command has updated live tracking."""
    resolved = _resolve_path_for_task("foo.py", task_id="t-codex")
    assert str(resolved).startswith(str(codex_worktree.resolve()))
    assert resolved.name == "foo.py"


def test_relative_path_with_subdir_resolves_into_worktree(codex_worktree):
    resolved = _resolve_path_for_task("agent/bar.py", task_id="t-codex")
    assert str(resolved) == str((codex_worktree / "agent" / "bar.py").resolve())


def test_absolute_path_unchanged_inside_codex_thread(codex_worktree, tmp_path):
    """Absolute paths skip resolution — they're not codex-scoped."""
    abs_target = tmp_path / "outside" / "thing.py"
    abs_target.parent.mkdir(parents=True, exist_ok=True)
    abs_target.write_text("")
    resolved = _resolve_path_for_task(str(abs_target), task_id="t-codex")
    assert resolved == abs_target.resolve()
    # Critically: NOT inside the codex worktree.
    assert not str(resolved).startswith(str(codex_worktree.resolve()))


def test_no_contextvar_falls_through_to_live_tracking_or_cwd(tmp_path, monkeypatch):
    """When no codex worktree is set, behavior matches the pre-P1.2 path."""
    # Make sure no token leaked from another test.
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    resolved = _resolve_path_for_task("foo.py", task_id="t-no-codex")
    # Either resolves to TERMINAL_CWD/foo.py or to live-tracking-cwd/foo.py
    # depending on prior tests' state — but it must NOT be a bare 'foo.py'.
    assert resolved.is_absolute()
    assert resolved.name == "foo.py"


def test_missing_worktree_dir_falls_through(tmp_path, monkeypatch):
    """If the contextvar points at a non-existent dir, fall through to
    live tracking + TERMINAL_CWD instead of building a phantom path."""
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    bogus = tmp_path / "does" / "not" / "exist"
    token = set_active_worktree(str(bogus))
    try:
        resolved = _resolve_path_for_task("foo.py", task_id="t-missing")
        # Should land in TERMINAL_CWD/foo.py, NOT under the bogus path.
        assert not str(resolved).startswith(str(bogus))
        assert str(resolved).startswith(str(tmp_path.resolve()))
    finally:
        reset_active_worktree(token)


def test_codex_wt_overrides_terminal_cwd(codex_worktree, tmp_path, monkeypatch):
    """When both are set, the codex worktree wins (it's more specific)."""
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    resolved = _resolve_path_for_task("foo.py", task_id="t-priority")
    assert str(resolved).startswith(str(codex_worktree.resolve()))
