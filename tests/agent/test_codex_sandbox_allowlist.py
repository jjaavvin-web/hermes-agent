"""Tests for agent.codex_sandbox_allowlist."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import agent.codex_sandbox_allowlist as allow


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Each test gets a fresh cache and a hermes_home pointing at tmp_path
    (so the loader doesn't see the real ~/.hermes/codex-sandbox-allow.yaml)."""
    allow.reset_cache_for_tests()
    monkeypatch.delenv(allow._ENV_VAR, raising=False)
    # Point hermes_constants.get_hermes_home() at tmp_path for this test.
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    yield
    allow.reset_cache_for_tests()


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "codex-sandbox-allow.yaml"
    p.write_text(body)
    return p


class TestFileSource:
    def test_missing_file_yields_empty(self, tmp_path):
        assert allow.get_allowed_roots() == []
        assert allow.is_path_allowed(str(tmp_path / "anything")) is False

    def test_basic_allowed_path(self, tmp_path):
        target = tmp_path / "vault"
        target.mkdir()
        _write_yaml(tmp_path, f"allowed_paths:\n  - {target}\n")
        assert str(target) in allow.get_allowed_roots()
        assert allow.is_path_allowed(str(target)) is True
        assert allow.is_path_allowed(str(target / "notes" / "x.md")) is True

    def test_unrelated_path_not_allowed(self, tmp_path):
        target = tmp_path / "vault"
        target.mkdir()
        other = tmp_path / "elsewhere"
        other.mkdir()
        _write_yaml(tmp_path, f"allowed_paths:\n  - {target}\n")
        assert allow.is_path_allowed(str(other / "leak.txt")) is False

    def test_prefix_is_not_a_match(self, tmp_path):
        """`/foo/bar` should not match `/foo/barbaz/...` — startswith would
        false-positive without the os.sep separator."""
        a = tmp_path / "bar"
        a.mkdir()
        b = tmp_path / "barbaz"
        b.mkdir()
        _write_yaml(tmp_path, f"allowed_paths:\n  - {a}\n")
        assert allow.is_path_allowed(str(b / "x.txt")) is False

    def test_non_absolute_entries_dropped(self, tmp_path):
        _write_yaml(tmp_path, "allowed_paths:\n  - relative/path\n  - /abs/ok\n")
        roots = allow.get_allowed_roots()
        assert "/abs/ok" in roots
        assert not any("relative/path" in r for r in roots)

    def test_malformed_yaml_falls_back_to_empty(self, tmp_path):
        _write_yaml(tmp_path, "allowed_paths: [unterminated\n")
        assert allow.get_allowed_roots() == []

    def test_top_level_not_mapping_falls_back(self, tmp_path):
        _write_yaml(tmp_path, "- /just/a/list\n")
        assert allow.get_allowed_roots() == []

    def test_missing_key_falls_back(self, tmp_path):
        _write_yaml(tmp_path, "other_key: 1\n")
        assert allow.get_allowed_roots() == []

    def test_allowed_paths_not_a_list_falls_back(self, tmp_path):
        _write_yaml(tmp_path, "allowed_paths: '/single/string'\n")
        assert allow.get_allowed_roots() == []

    def test_symlinked_entry_canonicalizes(self, tmp_path):
        real = tmp_path / "real-vault"
        real.mkdir()
        link = tmp_path / "link-vault"
        link.symlink_to(real)
        _write_yaml(tmp_path, f"allowed_paths:\n  - {link}\n")
        # Target reached via the realpath should match.
        assert allow.is_path_allowed(str(real / "note.md")) is True

    def test_cache_invalidated_on_file_change(self, tmp_path):
        """Editing the allowlist file should cause the next call to see the
        new content.  We force a size delta to make the (path, mtime, size)
        cache key change regardless of filesystem mtime granularity — fs
        timestamps on /tmp (especially under tmpfs) can collide between
        two writes that happen within the same tick."""
        p = _write_yaml(tmp_path, f"allowed_paths:\n  - {tmp_path / 'a'}\n")
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        assert allow.is_path_allowed(str(tmp_path / "a" / "x")) is True
        # New content with a long inline comment so the size is definitely
        # different from the first version — guarantees cache invalidation.
        p.write_text(
            f"# allowlist updated — size delta forces cache invalidation\n"
            f"allowed_paths:\n  - {tmp_path / 'b'}\n"
        )
        assert allow.is_path_allowed(str(tmp_path / "a" / "x")) is False
        assert allow.is_path_allowed(str(tmp_path / "b" / "x")) is True


class TestEnvVarSource:
    def test_env_var_replaces_file(self, tmp_path, monkeypatch):
        a = tmp_path / "from-file"
        a.mkdir()
        b = tmp_path / "from-env"
        b.mkdir()
        _write_yaml(tmp_path, f"allowed_paths:\n  - {a}\n")
        monkeypatch.setenv(allow._ENV_VAR, str(b))
        # Env wins: only b is allowed, a is not.
        assert allow.is_path_allowed(str(b / "x")) is True
        assert allow.is_path_allowed(str(a / "x")) is False

    def test_env_var_colon_separated(self, tmp_path, monkeypatch):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        monkeypatch.setenv(allow._ENV_VAR, f"{a}{os.pathsep}{b}")
        assert allow.is_path_allowed(str(a / "x")) is True
        assert allow.is_path_allowed(str(b / "x")) is True

    def test_empty_env_var_falls_through_to_file(self, tmp_path, monkeypatch):
        a = tmp_path / "from-file"
        a.mkdir()
        _write_yaml(tmp_path, f"allowed_paths:\n  - {a}\n")
        monkeypatch.setenv(allow._ENV_VAR, "   ")
        # Empty/whitespace env var means "no override" — file is honored.
        assert allow.is_path_allowed(str(a / "x")) is True

    def test_env_var_non_absolute_entries_dropped(self, tmp_path, monkeypatch):
        a = tmp_path / "a"; a.mkdir()
        monkeypatch.setenv(allow._ENV_VAR, f"relative{os.pathsep}{a}")
        assert allow.is_path_allowed(str(a / "x")) is True
