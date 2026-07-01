"""Tests for codex-native thread cwd resolution from active worktrees."""

from __future__ import annotations

import inspect
import logging
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import agent.codex_runtime as codex_runtime
from agent.codex_runtime import _resolve_codex_thread_cwd
from agent.codex_session_context import (
    reset_active_worktree,
    set_active_worktree,
)


@contextmanager
def active_worktree(path: str):
    token = set_active_worktree(path)
    try:
        yield
    finally:
        reset_active_worktree(token)


def test_session_cwd_wins_when_unbound(tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    agent = SimpleNamespace(session_cwd=str(session_dir))

    assert _resolve_codex_thread_cwd(agent) == (str(session_dir), "session_cwd")


def test_bound_existing_worktree_used_when_session_cwd_missing(tmp_path):
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    agent = SimpleNamespace(session_cwd=None)

    with active_worktree(str(worktree_dir)):
        assert _resolve_codex_thread_cwd(agent) == (
            os.path.realpath(str(worktree_dir)),
            "bound_worktree",
        )


def test_bound_nonexistent_worktree_raises_confinement_error(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    missing_worktree = tmp_path / "missing-worktree"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    agent = SimpleNamespace(session_cwd=None)

    with active_worktree(str(missing_worktree)):
        with pytest.raises(codex_runtime.CodexCwdConfinementError) as excinfo:
            _resolve_codex_thread_cwd(agent)

    message = str(excinfo.value)
    assert "missing or invalid" in message
    assert os.path.realpath(str(missing_worktree)) in message
    assert str(cwd) not in message


def test_no_session_cwd_or_worktree_falls_back_to_process_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = SimpleNamespace(session_cwd=None)

    assert _resolve_codex_thread_cwd(agent) == (os.getcwd(), "legacy_process_cwd")


def test_resolver_reports_source_for_each_precedence_tier(tmp_path, monkeypatch):
    bound_worktree = tmp_path / "bound-worktree"
    bound_session = bound_worktree / "nested" / "session"
    unbound_session = tmp_path / "unbound-session"
    legacy_cwd = tmp_path / "legacy-cwd"
    bound_session.mkdir(parents=True)
    unbound_session.mkdir()
    legacy_cwd.mkdir()

    with active_worktree(str(bound_worktree)):
        assert _resolve_codex_thread_cwd(
            SimpleNamespace(session_cwd=str(bound_session))
        ) == (os.path.realpath(str(bound_session)), "session_cwd")
        assert _resolve_codex_thread_cwd(SimpleNamespace(session_cwd=None)) == (
            os.path.realpath(str(bound_worktree)),
            "bound_worktree",
        )

    assert _resolve_codex_thread_cwd(
        SimpleNamespace(session_cwd=str(unbound_session))
    ) == (str(unbound_session), "session_cwd")

    monkeypatch.chdir(legacy_cwd)
    assert _resolve_codex_thread_cwd(SimpleNamespace(session_cwd=None)) == (
        os.getcwd(),
        "legacy_process_cwd",
    )


def test_run_codex_app_server_turn_uses_thread_cwd_helper():
    source = inspect.getsource(codex_runtime.run_codex_app_server_turn)

    assert "cwd, cwd_source = _resolve_codex_thread_cwd(agent)" in source
    assert "codex thread cwd resolved: %s (source=%s)" in source
    assert "getattr(agent, \"session_cwd\", None) or os.getcwd()" not in source


def test_bound_worktree_allows_session_cwd_inside_worktree(tmp_path):
    worktree_dir = tmp_path / "worktree"
    session_dir = worktree_dir / "nested" / "session"
    session_dir.mkdir(parents=True)
    agent = SimpleNamespace(session_cwd=str(session_dir))

    with active_worktree(str(worktree_dir)):
        assert _resolve_codex_thread_cwd(agent) == (
            os.path.realpath(str(session_dir)),
            "session_cwd",
        )


def test_bound_worktree_rejects_session_cwd_outside_worktree(tmp_path):
    worktree_dir = tmp_path / "worktree"
    sibling_dir = tmp_path / "sibling"
    worktree_dir.mkdir()
    sibling_dir.mkdir()
    agent = SimpleNamespace(session_cwd=str(sibling_dir))

    with active_worktree(str(worktree_dir)):
        with pytest.raises(codex_runtime.CodexCwdConfinementError) as excinfo:
            _resolve_codex_thread_cwd(agent)

    message = str(excinfo.value)
    assert "outside bound worktree" in message
    assert os.path.realpath(str(sibling_dir)) in message
    assert os.path.realpath(str(worktree_dir)) in message


def test_bound_worktree_rejects_simulated_protected_main_checkout(tmp_path):
    worktree_dir = tmp_path / "worktree"
    main_checkout_dir = tmp_path / "main-checkout"
    worktree_dir.mkdir()
    main_checkout_dir.mkdir()
    agent = SimpleNamespace(session_cwd=str(main_checkout_dir))

    with active_worktree(str(worktree_dir)):
        with pytest.raises(codex_runtime.CodexCwdConfinementError) as excinfo:
            _resolve_codex_thread_cwd(agent)

    message = str(excinfo.value)
    assert "outside bound worktree" in message
    assert "main-checkout" in message
    assert os.path.realpath(str(worktree_dir)) in message


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_bound_worktree_rejects_symlink_escape(tmp_path):
    worktree_dir = tmp_path / "worktree"
    outside_dir = tmp_path / "outside"
    symlink_path = worktree_dir / "link"
    worktree_dir.mkdir()
    outside_dir.mkdir()
    os.symlink(outside_dir, symlink_path)
    agent = SimpleNamespace(session_cwd=str(symlink_path))

    with active_worktree(str(worktree_dir)):
        with pytest.raises(codex_runtime.CodexCwdConfinementError) as excinfo:
            _resolve_codex_thread_cwd(agent)

    message = str(excinfo.value)
    assert "outside bound worktree" in message
    assert os.path.realpath(str(symlink_path)) in message
    assert os.path.realpath(str(worktree_dir)) in message


def _assert_run_turn_rejects_before_construction(monkeypatch, *, worktree: str, session_cwd: str | None):
    class ConstructorMustNotBeCalled:
        def __init__(self, **kwargs):
            raise AssertionError("constructor must not be called")

    monkeypatch.setattr(
        codex_runtime,
        "CodexAppServerSession",
        ConstructorMustNotBeCalled,
    )
    agent = SimpleNamespace(session_cwd=session_cwd, _codex_session=None)

    with active_worktree(worktree):
        with pytest.raises(codex_runtime.CodexCwdConfinementError):
            codex_runtime.run_codex_app_server_turn(
                agent,
                user_message="hello",
                original_user_message="hello",
                messages=[],
                effective_task_id="task-1",
            )


def test_run_turn_rejects_outside_session_cwd_before_construction(tmp_path, monkeypatch):
    worktree_dir = tmp_path / "worktree"
    outside_dir = tmp_path / "outside"
    worktree_dir.mkdir()
    outside_dir.mkdir()

    _assert_run_turn_rejects_before_construction(
        monkeypatch,
        worktree=str(worktree_dir),
        session_cwd=str(outside_dir),
    )


def test_run_turn_rejects_simulated_main_checkout_before_construction(tmp_path, monkeypatch):
    worktree_dir = tmp_path / "worktree"
    main_checkout_dir = tmp_path / "main-checkout"
    worktree_dir.mkdir()
    main_checkout_dir.mkdir()

    _assert_run_turn_rejects_before_construction(
        monkeypatch,
        worktree=str(worktree_dir),
        session_cwd=str(main_checkout_dir),
    )


def test_run_turn_rejects_missing_bound_worktree_before_construction(tmp_path, monkeypatch):
    missing_worktree = tmp_path / "missing-worktree"

    _assert_run_turn_rejects_before_construction(
        monkeypatch,
        worktree=str(missing_worktree),
        session_cwd=None,
    )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_run_turn_rejects_symlink_escape_before_construction(tmp_path, monkeypatch):
    worktree_dir = tmp_path / "worktree"
    outside_dir = tmp_path / "outside"
    symlink_path = worktree_dir / "link"
    worktree_dir.mkdir()
    outside_dir.mkdir()
    os.symlink(outside_dir, symlink_path)

    _assert_run_turn_rejects_before_construction(
        monkeypatch,
        worktree=str(worktree_dir),
        session_cwd=str(symlink_path),
    )


def test_run_codex_app_server_turn_constructs_session_with_bound_worktree_cwd(
    tmp_path, monkeypatch, caplog
):
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    captured_kwargs: dict[str, object] = {}

    class SentinelConstructor(Exception):
        pass

    class CapturingCodexAppServerSession:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            raise SentinelConstructor("constructor captured")

    monkeypatch.setattr(
        codex_runtime,
        "CodexAppServerSession",
        CapturingCodexAppServerSession,
    )
    agent = SimpleNamespace(session_cwd=None, _codex_session=None)
    caplog.set_level(logging.INFO, logger=codex_runtime.__name__)

    with active_worktree(str(worktree_dir)):
        resolver_cwd, resolver_source = _resolve_codex_thread_cwd(
            SimpleNamespace(session_cwd=None)
        )
        with pytest.raises(SentinelConstructor):
            codex_runtime.run_codex_app_server_turn(
                agent,
                user_message="hello",
                original_user_message="hello",
                messages=[],
                effective_task_id="task-1",
            )

    assert captured_kwargs["cwd"] == os.path.realpath(str(worktree_dir))
    assert (resolver_cwd, resolver_source) == (
        os.path.realpath(str(worktree_dir)),
        "bound_worktree",
    )
    assert "codex thread cwd resolved:" in caplog.text
    assert "source=bound_worktree" in caplog.text
