"""Regression tests for item A-always-approval (card t_ec1d82e1).

Root cause (forensics 07-06): choosing "always" for a destructive-class
dangerous command (recursive delete, root delete, config/env overwrite,
remote pipe-to-shell, shell -c/-lc wrapper, service restart/stop,
execute_code) called ``save_permanent_allowlist()`` unconditionally and
persisted the pattern into the durable ``command_allowlist``. That is how a
"recursive delete" entry regrew into the live allowlist between 06-17 and
07-06.

These tests pin the fix: by default, "always" on a destructive-class
command is downgraded to a session-only grant (command still runs this
session; nothing is written to the permanent allowlist). Non-destructive
dangerous commands keep the exact previous "always" persist behavior. The
``security.allow_permanent_destructive_approvals`` config escape hatch
restores the legacy persist behavior when explicitly set True.
"""

from unittest.mock import MagicMock, patch as mock_patch

import pytest

import tools.approval as approval_module
from tools.approval import (
    check_all_command_guards,
    check_dangerous_command,
    detect_dangerous_command,
)


@pytest.fixture(autouse=True)
def _clear_approval_state():
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    approval_module.clear_session("test-session")
    yield
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    approval_module.clear_session("test-session")


# A recursive-delete command with a relative (non-absolute, non-root) path,
# so it matches ONLY the "recursive delete" pattern -- not "delete in root
# path" (which requires a leading "/") and nowhere near the unconditional
# root-filesystem hardline block.
DESTRUCTIVE_CMD = "rm -rf some-project-dir"
DESTRUCTIVE_KEY = "recursive delete"

# A dangerous-but-not-destructive-class command: git force push is flagged
# by DANGEROUS_PATTERNS but is not one of the named destructive classes
# (recursive delete / root delete / config-env overwrite / remote
# pipe-to-shell / shell -c-lc wrapper / service restart-stop / execute_code).
BENIGN_CMD = "git push --force origin main"
BENIGN_KEY = "git force push (rewrites remote history)"


def _sanity_check_patterns():
    is_dangerous, pattern_key, _ = detect_dangerous_command(DESTRUCTIVE_CMD)
    assert is_dangerous and pattern_key == DESTRUCTIVE_KEY
    is_dangerous, pattern_key, _ = detect_dangerous_command(BENIGN_CMD)
    assert is_dangerous and pattern_key == BENIGN_KEY


class TestAlwaysDestructiveDowngradeDefaultConfig:
    """1) always on a destructive-class command, default config -> no
    permanent allowlist write; session approval still granted."""

    def test_check_all_command_guards_cli_path(self, monkeypatch):
        _sanity_check_patterns()
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

        mock_save_config = MagicMock()
        with mock_patch("hermes_cli.config.load_config", return_value={}), \
             mock_patch("hermes_cli.config.save_config", mock_save_config):
            cb = MagicMock(return_value="always")
            result = check_all_command_guards(
                DESTRUCTIVE_CMD, "local", approval_callback=cb,
            )

        # Command still runs this session -- "always" is not a denial.
        assert result["approved"] is True

        # But nothing was persisted to the permanent allowlist.
        mock_save_config.assert_not_called()
        assert DESTRUCTIVE_KEY not in approval_module._permanent_approved

        # Session-scoped grant IS still in effect (re-running would not
        # re-prompt within this session).
        session_key = approval_module.get_current_session_key()
        assert approval_module.is_approved(session_key, DESTRUCTIVE_KEY) is True

        # A clear one-line notice says permanent approval was unavailable.
        assert result.get("message"), "expected a downgrade notice message"
        assert "permanent" in result["message"].lower()

    def test_check_dangerous_command_legacy_entrypoint(self, monkeypatch):
        """Same downgrade behavior on the standalone check_dangerous_command
        entry point (shares the identical always -> persist code path)."""
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

        mock_save_config = MagicMock()
        with mock_patch("hermes_cli.config.load_config", return_value={}), \
             mock_patch("hermes_cli.config.save_config", mock_save_config):
            result = check_dangerous_command(
                DESTRUCTIVE_CMD, "local", approval_callback=lambda *a, **k: "always",
            )

        assert result["approved"] is True
        mock_save_config.assert_not_called()
        assert DESTRUCTIVE_KEY not in approval_module._permanent_approved


class TestAlwaysBenignStillPersists:
    """2) always on a benign (non-destructive-class) dangerous command ->
    still persists, exactly matching current/legacy behavior."""

    def test_check_all_command_guards_cli_path(self, monkeypatch):
        _sanity_check_patterns()
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

        mock_save_config = MagicMock()
        with mock_patch("hermes_cli.config.load_config", return_value={}), \
             mock_patch("hermes_cli.config.save_config", mock_save_config):
            cb = MagicMock(return_value="always")
            result = check_all_command_guards(
                BENIGN_CMD, "local", approval_callback=cb,
            )

        assert result["approved"] is True
        mock_save_config.assert_called_once()
        assert BENIGN_KEY in approval_module._permanent_approved
        # No downgrade notice for a non-destructive-class pattern.
        assert not result.get("message")


class TestEscapeHatchRestoresLegacyPersist:
    """3) security.allow_permanent_destructive_approvals: true -> old
    persist behavior restored for the destructive class."""

    def test_check_all_command_guards_cli_path(self, monkeypatch):
        _sanity_check_patterns()
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

        mock_save_config = MagicMock()
        fake_config = {"security": {"allow_permanent_destructive_approvals": True}}
        with mock_patch("hermes_cli.config.load_config", return_value=fake_config), \
             mock_patch("hermes_cli.config.save_config", mock_save_config):
            cb = MagicMock(return_value="always")
            result = check_all_command_guards(
                DESTRUCTIVE_CMD, "local", approval_callback=cb,
            )

        assert result["approved"] is True
        mock_save_config.assert_called_once()
        assert DESTRUCTIVE_KEY in approval_module._permanent_approved
        # Escape hatch active -> no downgrade notice.
        assert not result.get("message")

    def test_check_dangerous_command_legacy_entrypoint(self, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

        mock_save_config = MagicMock()
        fake_config = {"security": {"allow_permanent_destructive_approvals": True}}
        with mock_patch("hermes_cli.config.load_config", return_value=fake_config), \
             mock_patch("hermes_cli.config.save_config", mock_save_config):
            result = check_dangerous_command(
                DESTRUCTIVE_CMD, "local", approval_callback=lambda *a, **k: "always",
            )

        assert result["approved"] is True
        mock_save_config.assert_called_once()
        assert DESTRUCTIVE_KEY in approval_module._permanent_approved


class TestHardlineFloorUnaffected:
    """Root-filesystem recursive delete must stay hardline-blocked regardless
    of the always-approval downgrade change -- this proves we didn't loosen
    any existing block/deny path."""

    def test_rm_rf_root_still_hardline_blocked(self, monkeypatch):
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        result = check_all_command_guards(
            "rm -rf /", "local", approval_callback=lambda *a, **k: "always",
        )
        assert result["approved"] is False
        assert "hardline" in result.get("message", "").lower() or "BLOCKED" in result.get("message", "")
