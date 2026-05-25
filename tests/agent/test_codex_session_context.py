"""Tests for agent.codex_session_context (P1.5)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from agent.codex_session_context import (
    get_active_worktree,
    reset_active_worktree,
    set_active_worktree,
)


class TestBasicGetSet:
    def test_default_is_none(self):
        assert get_active_worktree() is None

    def test_set_then_get(self):
        token = set_active_worktree("/some/worktree")
        try:
            assert get_active_worktree() == "/some/worktree"
        finally:
            reset_active_worktree(token)
        assert get_active_worktree() is None

    def test_set_none_explicitly(self):
        outer_token = set_active_worktree("/outer")
        try:
            inner_token = set_active_worktree(None)
            try:
                assert get_active_worktree() is None
            finally:
                reset_active_worktree(inner_token)
            assert get_active_worktree() == "/outer"
        finally:
            reset_active_worktree(outer_token)


class TestAsyncTaskIsolation:
    """ContextVar must give each async task its own copy — concurrent
    Discord threads must not see each other's worktrees."""

    def test_two_tasks_dont_see_each_others_value(self):
        async def task_a():
            set_active_worktree("/wt-a")
            await asyncio.sleep(0)
            return get_active_worktree()

        async def task_b():
            set_active_worktree("/wt-b")
            await asyncio.sleep(0)
            return get_active_worktree()

        async def runner():
            return await asyncio.gather(task_a(), task_b())

        a, b = asyncio.run(runner())
        assert a == "/wt-a"
        assert b == "/wt-b"

    def test_parent_value_inherited_by_child_task(self):
        async def child():
            return get_active_worktree()

        async def parent():
            set_active_worktree("/parent")
            return await asyncio.create_task(child())

        result = asyncio.run(parent())
        assert result == "/parent"

    def test_child_set_does_not_leak_to_parent(self):
        async def child():
            set_active_worktree("/child-only")
            await asyncio.sleep(0)

        async def parent():
            set_active_worktree("/parent")
            await asyncio.create_task(child())
            return get_active_worktree()

        result = asyncio.run(parent())
        assert result == "/parent"


class TestLocalEnvironmentExecuteIntegration:
    """``BaseEnvironment.execute`` (the actual entry point used by the
    terminal tool) must honor the contextvar override when one is set,
    and fall back to ``self.cwd`` otherwise.  Asserting via ``execute``
    (not ``_run_bash`` alone) catches the case where the bash wrapper's
    ``builtin cd`` would otherwise shadow Popen's cwd argument."""

    def test_execute_uses_contextvar_when_set(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        configured = tmp_path / "configured"
        configured.mkdir()
        override = tmp_path / "override"
        override.mkdir()

        env = LocalEnvironment(cwd=str(configured), timeout=10)
        token = set_active_worktree(str(override))
        try:
            result = env.execute("pwd", timeout=5)
            assert os.path.realpath(result["output"].strip()) == os.path.realpath(str(override))
        finally:
            reset_active_worktree(token)

    def test_execute_uses_self_cwd_when_contextvar_unset(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        configured = tmp_path / "configured"
        configured.mkdir()
        env = LocalEnvironment(cwd=str(configured), timeout=10)
        assert get_active_worktree() is None
        result = env.execute("pwd", timeout=5)
        assert os.path.realpath(result["output"].strip()) == os.path.realpath(str(configured))

    def test_execute_explicit_cwd_param_beats_contextvar(self, tmp_path):
        """LLM-supplied cwd should win over the implicit codex context —
        the contextvar is a default, not an override of explicit intent."""
        from tools.environments.local import LocalEnvironment

        configured = tmp_path / "configured"
        configured.mkdir()
        codex_wt = tmp_path / "codex-wt"
        codex_wt.mkdir()
        explicit = tmp_path / "explicit"
        explicit.mkdir()

        env = LocalEnvironment(cwd=str(configured), timeout=10)
        token = set_active_worktree(str(codex_wt))
        try:
            result = env.execute("pwd", cwd=str(explicit), timeout=5)
            assert os.path.realpath(result["output"].strip()) == os.path.realpath(str(explicit))
        finally:
            reset_active_worktree(token)

    def test_execute_ignores_contextvar_when_dir_missing(self, tmp_path):
        """Defense: if the contextvar points at a deleted worktree,
        fall back to self.cwd instead of failing the cd."""
        from tools.environments.local import LocalEnvironment

        configured = tmp_path / "configured"
        configured.mkdir()
        env = LocalEnvironment(cwd=str(configured), timeout=10)
        token = set_active_worktree(str(tmp_path / "never-existed"))
        try:
            result = env.execute("pwd", timeout=5)
            assert os.path.realpath(result["output"].strip()) == os.path.realpath(str(configured))
        finally:
            reset_active_worktree(token)
