"""Tests for the doc/config-coverage lint (WF-08).

The lint (``scripts/lint_config_docs.py``) ensures every load-bearing
``kanban.*`` config key — including the C-03 global dispatch-concurrency cap
``kanban.max_spawn`` — is documented in ``AGENTS.md``.

These tests prove the lint is **failable, not vacuous**:
  1. It passes against the real, fully-documented ``AGENTS.md``.
  2. Removing a documented key's line turns it RED (the core failability check).
  3. ``kanban.max_spawn`` specifically is in the required set (the key that
     blocked the original WF-08 packet).
"""

from __future__ import annotations

import pathlib

import pytest

from scripts.lint_config_docs import (
    AGENTS_MD,
    RUNTIME_ONLY_KANBAN_KEYS,
    find_undocumented_keys,
    required_kanban_keys,
)


def test_lint_passes_against_real_docs():
    """The committed AGENTS.md documents every required kanban.* key."""
    missing = find_undocumented_keys()
    assert missing == [], (
        "doc/config lint is RED against the committed AGENTS.md — these "
        f"kanban.* keys are undocumented: {missing}. Add a `kanban.<key>` "
        "line for each in the Kanban section of AGENTS.md."
    )


def test_max_spawn_is_required():
    """kanban.max_spawn (C-03 global dispatch-concurrency cap) must be in the
    required key set — it is the key that blocked the original WF-08 packet."""
    assert "max_spawn" in required_kanban_keys()
    assert "max_spawn" in RUNTIME_ONLY_KANBAN_KEYS


def test_required_keys_include_default_config_keys():
    """The required set is derived from DEFAULT_CONFIG (not hand-copied from
    the docs), so it tracks real config and can't silently rot."""
    from hermes_cli.config import DEFAULT_CONFIG

    default_keys = set(DEFAULT_CONFIG["kanban"].keys())
    assert default_keys, "DEFAULT_CONFIG['kanban'] unexpectedly empty"
    assert default_keys.issubset(required_kanban_keys())


def test_lint_fails_when_a_documented_key_is_removed(tmp_path: pathlib.Path):
    """Failability: strip the kanban.max_spawn doc line from a copy of
    AGENTS.md and the lint must report it as undocumented (goes RED)."""
    original = AGENTS_MD.read_text(encoding="utf-8")
    assert "kanban.max_spawn" in original, (
        "precondition failed: AGENTS.md must document kanban.max_spawn"
    )

    # Remove every reference to the key so the lint can't find it.
    mutated = original.replace("kanban.max_spawn", "kanban.REMOVED_FOR_TEST")
    doc = tmp_path / "AGENTS.md"
    doc.write_text(mutated, encoding="utf-8")

    missing = find_undocumented_keys(agents_md_path=doc)
    assert "max_spawn" in missing, (
        "lint is VACUOUS: removing kanban.max_spawn from the docs did not "
        "turn the lint RED. The lint must actually verify doc coverage."
    )


def test_lint_fails_when_an_arbitrary_default_key_is_removed(
    tmp_path: pathlib.Path,
):
    """Failability across the whole key set, not just max_spawn: pick a key
    from DEFAULT_CONFIG and prove its removal reds the lint too."""
    from hermes_cli.config import DEFAULT_CONFIG

    # failure_limit is a stable, always-present default key.
    key = "failure_limit"
    assert key in DEFAULT_CONFIG["kanban"]

    original = AGENTS_MD.read_text(encoding="utf-8")
    mutated = original.replace(f"kanban.{key}", f"kanban.REMOVED_{key}")
    doc = tmp_path / "AGENTS.md"
    doc.write_text(mutated, encoding="utf-8")

    missing = find_undocumented_keys(agents_md_path=doc)
    assert key in missing


def test_lint_cli_passes(capsys):
    """The lint's main() entrypoint exits 0 against the real docs."""
    from scripts.lint_config_docs import main

    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out
