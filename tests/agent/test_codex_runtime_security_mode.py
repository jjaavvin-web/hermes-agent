"""Tests for profile-level tools.terminal.security_mode wiring in codex_runtime.

Covers the A2b governance rail: CodexAppServerSession must be constructed with
the mapped permission_profile when the active profile config explicitly sets
tools.terminal.security_mode, and with permission_profile=None when it does not
(so the session's own env-var fallback is unchanged for all existing profiles).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.codex_runtime import _resolve_codex_permission_profile


# ---------------------------------------------------------------------------
# _resolve_codex_permission_profile unit tests
# ---------------------------------------------------------------------------


class TestResolveCodexPermissionProfile:
    """Unit-test the resolver in isolation (no agent, no subprocess)."""

    def test_known_mode_maps_correctly(self):
        """tools.terminal.security_mode='approval-required' → 'read-only-with-approval'."""
        cfg = {"tools": {"terminal": {"security_mode": "approval-required"}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            result = _resolve_codex_permission_profile()
        assert result == "read-only-with-approval"

    def test_auto_maps_to_workspace_write(self):
        cfg = {"tools": {"terminal": {"security_mode": "auto"}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            result = _resolve_codex_permission_profile()
        assert result == "workspace-write"

    def test_unrestricted_maps_to_full_access(self):
        cfg = {"tools": {"terminal": {"security_mode": "unrestricted"}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            result = _resolve_codex_permission_profile()
        assert result == "full-access"

    def test_yolo_maps_to_full_access(self):
        cfg = {"tools": {"terminal": {"security_mode": "yolo"}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            result = _resolve_codex_permission_profile()
        assert result == "full-access"

    def test_no_security_mode_key_returns_none(self):
        """Profile with no tools.terminal.security_mode → None (env-var fallback runs)."""
        cfg = {"tools": {"terminal": {}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            result = _resolve_codex_permission_profile()
        assert result is None

    def test_no_terminal_section_returns_none(self):
        cfg = {"tools": {}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            result = _resolve_codex_permission_profile()
        assert result is None

    def test_no_tools_section_returns_none(self):
        cfg = {}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            result = _resolve_codex_permission_profile()
        assert result is None

    def test_empty_config_returns_none(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            result = _resolve_codex_permission_profile()
        assert result is None

    def test_unknown_mode_returns_none_never_full_access(self, caplog):
        """Garbage/unknown mode must NOT map to full-access; returns None + warning."""
        cfg = {"tools": {"terminal": {"security_mode": "danger-zone"}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            with caplog.at_level(logging.WARNING, logger="agent.codex_runtime"):
                result = _resolve_codex_permission_profile()
        assert result is None
        assert result != "full-access"
        # A warning must have been emitted.
        assert any("unknown" in r.message.lower() for r in caplog.records)

    def test_unknown_mode_warning_mentions_valid_values(self, caplog):
        cfg = {"tools": {"terminal": {"security_mode": "totally-bogus"}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            with caplog.at_level(logging.WARNING, logger="agent.codex_runtime"):
                _resolve_codex_permission_profile()
        combined = " ".join(r.message for r in caplog.records)
        # At least one of the valid keys should appear in the warning text.
        from agent.transports.codex_app_server_session import (
            _HERMES_TO_CODEX_PERMISSION_PROFILE,
        )
        assert any(k in combined for k in _HERMES_TO_CODEX_PERMISSION_PROFILE)

    def test_load_config_exception_returns_none(self):
        """If load_config raises, we must fail open (return None, not crash)."""
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("cfg broken")):
            result = _resolve_codex_permission_profile()
        assert result is None

    def test_whitespace_only_mode_returns_none(self):
        cfg = {"tools": {"terminal": {"security_mode": "   "}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            result = _resolve_codex_permission_profile()
        assert result is None


# ---------------------------------------------------------------------------
# Integration: CodexAppServerSession constructor call-site in codex_runtime
# ---------------------------------------------------------------------------


def _make_stub_agent(**attrs):
    """Minimal agent stub suitable for driving run_codex_app_server_turn."""
    defaults = {
        "session_cwd": None,
    }
    defaults.update(attrs)
    return SimpleNamespace(**defaults)


class TestRunCodexAppServerTurnPermissionProfileWiring:
    """Verify that run_codex_app_server_turn passes the resolved
    permission_profile to CodexAppServerSession.__init__.

    CodexAppServerSession is imported lazily inside run_codex_app_server_turn
    via `from agent.transports.codex_app_server_session import
    CodexAppServerSession`.  Patching the name at the source module intercepts
    the lazy import correctly.
    """

    _SESSION_CLS_TARGET = (
        "agent.transports.codex_app_server_session.CodexAppServerSession"
    )
    _LOAD_CFG_TARGET = "hermes_cli.config.load_config"
    _APPROVAL_TARGET = "tools.terminal_tool._get_approval_callback"

    def _capture_session_kwargs(self, agent, cfg):
        """Run one turn (aborted at session creation) and return the kwargs
        that were passed to CodexAppServerSession.__init__."""
        captured = {}

        class _FakeSession:
            def __init__(self_inner, **kw):
                captured.update(kw)
                raise _SessionCreated()

        class _SessionCreated(Exception):
            pass

        with (
            patch(self._LOAD_CFG_TARGET, return_value=cfg),
            patch(self._SESSION_CLS_TARGET, _FakeSession),
            patch(self._APPROVAL_TARGET, return_value=None, create=True),
        ):
            from agent.codex_runtime import run_codex_app_server_turn
            try:
                run_codex_app_server_turn(
                    agent,
                    user_message="hi",
                    original_user_message="hi",
                    messages=[],
                    effective_task_id="t1",
                )
            except _SessionCreated:
                pass

        return captured

    def test_profile_with_approval_required_passes_correct_profile(self):
        """(a) Profile sets approval-required → read-only-with-approval is passed."""
        cfg = {"tools": {"terminal": {"security_mode": "approval-required"}}}
        agent = _make_stub_agent(_codex_session=None)
        kw = self._capture_session_kwargs(agent, cfg)
        assert kw.get("permission_profile") == "read-only-with-approval"

    def test_profile_with_no_security_mode_passes_none(self):
        """(b) Profile with NO security_mode → permission_profile=None passed,
        so the session's own env-var fallback runs unchanged."""
        cfg = {}  # no tools.terminal.security_mode
        agent = _make_stub_agent(_codex_session=None)
        kw = self._capture_session_kwargs(agent, cfg)
        assert kw.get("permission_profile") is None

    def test_unknown_garbage_mode_passes_none_not_full_access(self, caplog):
        """(c) Unknown/garbage mode → None passed, NEVER full-access."""
        cfg = {"tools": {"terminal": {"security_mode": "pwn-everything"}}}
        agent = _make_stub_agent(_codex_session=None)
        with caplog.at_level(logging.WARNING, logger="agent.codex_runtime"):
            kw = self._capture_session_kwargs(agent, cfg)
        assert kw.get("permission_profile") is None
        assert kw.get("permission_profile") != "full-access"
        assert any("unknown" in r.message.lower() for r in caplog.records)

    def test_session_reused_on_second_turn_skips_constructor(self):
        """Once _codex_session is already set, the constructor is never called."""
        fake_turn = SimpleNamespace(
            final_text="ok",
            projected_messages=[],
            tool_iterations=0,
            interrupted=False,
            error=None,
            thread_id="th1",
            turn_id="tu1",
            token_usage_last=None,
            token_usage_total=None,
            model_context_window=None,
            should_retire=False,
        )
        existing_session = MagicMock()
        existing_session.run_turn.return_value = fake_turn

        # Provide all attrs touched by run_codex_app_server_turn after session reuse.
        agent = _make_stub_agent(
            _codex_session=existing_session,  # already set — constructor must not fire
            _session_db=None,
            session_id="s1",
            _session_db_created=True,
            model="gpt-4o",
            provider="openai",
            base_url="",
            api_key="",
            session_api_calls=0,
            session_prompt_tokens=0,
            session_completion_tokens=0,
            session_total_tokens=0,
            session_input_tokens=0,
            session_output_tokens=0,
            session_cache_read_tokens=0,
            session_cache_write_tokens=0,
            session_reasoning_tokens=0,
            session_estimated_cost_usd=0.0,
            session_cost_status="unknown",
            session_cost_source="unknown",
            _iters_since_skill=0,
            _skill_nudge_interval=0,
            valid_tool_names=set(),
            context_compressor=None,
            _interrupt_requested=False,
        )
        agent._sync_external_memory_for_turn = lambda **kw: None
        agent._spawn_background_review = lambda **kw: None

        constructor_calls = []

        class _SpySession:
            def __init__(self_inner, **kw):
                constructor_calls.append(kw)

        with (
            patch(self._LOAD_CFG_TARGET, return_value={}),
            patch(self._SESSION_CLS_TARGET, _SpySession),
            patch(self._APPROVAL_TARGET, return_value=None, create=True),
        ):
            from agent.codex_runtime import run_codex_app_server_turn
            run_codex_app_server_turn(
                agent,
                user_message="turn2",
                original_user_message="turn2",
                messages=[],
                effective_task_id="t2",
            )

        # The session was already set; the constructor must NOT have been called.
        assert constructor_calls == []
