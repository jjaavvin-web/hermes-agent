"""Fork-rail regression tests for ``hermes_cli.config_migrations``.

Covers two v0.21-candidate migration steps that regress fork-specific
guarantees (see MIGRATION-SIM.md, 2026-09-04, which rehearsed the real
v33 -> v39 ladder against copies of the live config and caught both):

* ``_migrate_to_37`` must never raise ``delegation.max_concurrent_children``
  — this fork pins the concurrency cap at 3 (Max-OAuth/Codex quota
  protection, DIVERGENCES V4; ``DEFAULT_CONFIG`` and
  ``tools/delegate_tool.py._DEFAULT_MAX_CONCURRENT_CHILDREN``), unlike
  upstream's raised default of 10.
* ``_migrate_to_34`` must never discard an explicit ``display.personality``
  value — the live config lost ``kawaii`` (an explicit, current user choice)
  to this step's blanket wipe once already, in production.

Runs the real migration ladder (``hermes_cli.config.migrate_config``)
against temp config files under a scratch ``HERMES_HOME``, matching the
existing migration test pattern in ``tests/hermes_cli/test_config.py``.
"""

import os
from unittest.mock import patch

import yaml

from hermes_cli.config import DEFAULT_CONFIG, load_config, migrate_config
from hermes_cli.personality import BUILTIN_PERSONALITIES, render_personality_prompt


def _write(tmp_path, body):
    (tmp_path / "config.yaml").write_text(body, encoding="utf-8")


def _raw(tmp_path):
    return yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))


class TestDelegationCapNeverRaisedByFork:
    """v33 -> v39 must never move delegation.max_concurrent_children 3 -> 10."""

    def test_fork_default_is_3_not_10(self):
        # Guard the assumption the rest of this class rests on: if the
        # fork's own default ever gets bumped to 10, these cases would
        # silently stop meaning anything.
        assert DEFAULT_CONFIG["delegation"]["max_concurrent_children"] == 3

    def test_explicit_cap_3_stays_3(self, tmp_path):
        _write(
            tmp_path,
            "_config_version: 33\n"
            "model:\n  provider: openrouter\n"
            "delegation:\n  max_concurrent_children: 3\n",
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            migrate_config(interactive=False, quiet=True)
            raw = _raw(tmp_path)
            merged = load_config()
        assert raw["delegation"]["max_concurrent_children"] == 3
        assert merged["delegation"]["max_concurrent_children"] == 3

    def test_explicit_cap_5_stays_5(self, tmp_path):
        _write(
            tmp_path,
            "_config_version: 33\n"
            "model:\n  provider: openrouter\n"
            "delegation:\n  max_concurrent_children: 5\n",
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            migrate_config(interactive=False, quiet=True)
            raw = _raw(tmp_path)
            merged = load_config()
        assert raw["delegation"]["max_concurrent_children"] == 5
        assert merged["delegation"]["max_concurrent_children"] == 5

    def test_missing_key_stays_missing_and_inherits_fork_default(self, tmp_path):
        _write(
            tmp_path,
            "_config_version: 33\n"
            "model:\n  provider: openrouter\n",
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            migrate_config(interactive=False, quiet=True)
            raw = _raw(tmp_path)
            merged = load_config()
        assert "delegation" not in raw
        assert merged["delegation"]["max_concurrent_children"] == 3


class TestPersonalityNeverDiscardedByFork:
    """v33 -> v39 must never wipe an explicit display.personality value."""

    def test_explicit_kawaii_survives(self, tmp_path):
        _write(
            tmp_path,
            "_config_version: 33\n"
            "model:\n  provider: openrouter\n"
            "display:\n  personality: kawaii\n",
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            migrate_config(interactive=False, quiet=True)
            raw = _raw(tmp_path)
            merged = load_config()
        assert raw["display"]["personality"] == "kawaii"
        assert merged["display"]["personality"] == "kawaii"

    def test_legacy_rendered_system_prompt_still_scrubbed(self, tmp_path):
        """The one case ``_migrate_to_34`` was actually designed for keeps
        firing: ``agent.system_prompt`` verbatim-equal to a known
        personality's rendered text (a shape only ever auto-written by the
        retired CLI/gateway ``/personality`` path, never hand-typed) is
        still cleared. Proves the fork rail disabled scrub 1 (the name
        wipe) without disabling the whole step."""
        rendered = render_personality_prompt(BUILTIN_PERSONALITIES["kawaii"])
        body = yaml.safe_dump(
            {
                "_config_version": 33,
                "model": {"provider": "openrouter"},
                "agent": {"system_prompt": rendered},
            },
            allow_unicode=True,
        )
        _write(tmp_path, body)
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            migrate_config(interactive=False, quiet=True)
            raw = _raw(tmp_path)
        assert raw.get("agent", {}).get("system_prompt", "") == ""


class TestAlreadyCurrentConfigIsANoOp:
    """A v39 config crossing the ladder must be byte-for-byte untouched —
    mirrors the profile no-op proof in MIGRATION-SIM.md (7/7 profiles
    already at v39 came out hash-identical)."""

    def test_v39_config_unchanged(self, tmp_path):
        body = (
            "_config_version: 39\n"
            "model:\n"
            "  provider: openrouter\n"
            "delegation:\n"
            "  max_concurrent_children: 3\n"
            "display:\n"
            "  personality: kawaii\n"
        )
        _write(tmp_path, body)
        before = (tmp_path / "config.yaml").read_bytes()
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            migrate_config(interactive=False, quiet=True)
        after = (tmp_path / "config.yaml").read_bytes()
        assert after == before
