"""Tests for dashboard session-token persistence in hermes_cli.web_server.

Covers ISA 20260522-1412_dashboard-token-persist (ISC-1 .. ISC-6).
"""

import os
import stat
from pathlib import Path
from unittest.mock import patch


class TestSessionTokenPersistence:
    """Tests for _load_or_create_session_token() — token survives restarts."""

    def test_creates_file_when_absent(self, tmp_path):
        """ISC-1: a missing token file is created on first init."""
        from hermes_cli.web_server import _load_or_create_session_token

        with patch.dict(
            _load_or_create_session_token.__globals__,
            {"get_hermes_home": lambda: tmp_path},
        ):
            token = _load_or_create_session_token()

        assert token
        assert (tmp_path / ".dashboard-token").is_file()

    def test_file_mode_is_0600(self, tmp_path):
        """ISC-2: the created token file has filesystem mode 0600."""
        from hermes_cli.web_server import _load_or_create_session_token

        with patch.dict(
            _load_or_create_session_token.__globals__,
            {"get_hermes_home": lambda: tmp_path},
        ):
            _load_or_create_session_token()

        mode = (tmp_path / ".dashboard-token").stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_second_init_reuses_token(self, tmp_path):
        """ISC-3: a second init returns the identical token value."""
        from hermes_cli.web_server import _load_or_create_session_token

        with patch.dict(
            _load_or_create_session_token.__globals__,
            {"get_hermes_home": lambda: tmp_path},
        ):
            first = _load_or_create_session_token()
            second = _load_or_create_session_token()

        assert first == second

    def test_token_matches_file_content(self, tmp_path):
        """ISC-4: the returned token equals the file content, stripped."""
        from hermes_cli.web_server import _load_or_create_session_token

        with patch.dict(
            _load_or_create_session_token.__globals__,
            {"get_hermes_home": lambda: tmp_path},
        ):
            token = _load_or_create_session_token()

        on_disk = (tmp_path / ".dashboard-token").read_text(encoding="utf-8")
        assert token == on_disk.strip()

    def test_blank_file_regenerates(self, tmp_path):
        """ISC-5 (Anti): a whitespace-only token file is replaced, never used."""
        from hermes_cli.web_server import _load_or_create_session_token

        token_file = tmp_path / ".dashboard-token"
        token_file.write_text("  \n", encoding="utf-8")

        with patch.dict(
            _load_or_create_session_token.__globals__,
            {"get_hermes_home": lambda: tmp_path},
        ):
            token = _load_or_create_session_token()

        assert token.strip()
        assert token_file.read_text(encoding="utf-8").strip() == token

    def test_unreadable_file_falls_back(self, tmp_path):
        """ISC-6 (Anti): an unreadable token file yields a token, not an error."""
        from hermes_cli.web_server import _load_or_create_session_token

        token_file = tmp_path / ".dashboard-token"
        token_file.write_text("preexisting-token", encoding="utf-8")

        def _raise_oserror(*args, **kwargs):
            raise OSError("permission denied")

        with patch.dict(
            _load_or_create_session_token.__globals__,
            {"get_hermes_home": lambda: tmp_path},
        ):
            with patch.object(Path, "read_text", _raise_oserror):
                token = _load_or_create_session_token()

        assert token

    def test_unwritable_path_falls_back(self, tmp_path):
        """ISC-6 (Anti): an unwritable token path yields a token, not an error."""
        from hermes_cli.web_server import _load_or_create_session_token

        def _raise_oserror(*args, **kwargs):
            raise OSError("read-only file system")

        with patch.dict(
            _load_or_create_session_token.__globals__,
            {"get_hermes_home": lambda: tmp_path},
        ):
            with patch.object(os, "open", _raise_oserror):
                token = _load_or_create_session_token()

        assert token
