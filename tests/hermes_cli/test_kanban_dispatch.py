"""Tests for the Phase 2 dispatch guardrail in ``kanban_db.dispatch_once``.

The guardrail refuses to auto-dispatch a ready card whose branch (or card
id) collides with a live run recorded in ``~/.hermes/run-registry``. It
blocks the card, records it on ``DispatchResult.blocked_guardrail``, and
is skipped entirely under ``dry_run``.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def spawnable(monkeypatch):
    """Make every assignee a valid Hermes profile so dispatch reaches
    the guardrail (the profile-exists check sits just before it)."""
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def _write_lock(home, slug, *, branch=None, tracking_card=None,
                tmux_session="p2gh"):
    """Drop a run-registry lock file mimicking the Phase 1 launcher."""
    registry = home / "run-registry"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{slug}.lock").write_text(json.dumps({
        "slug": slug,
        "branch": branch,
        "tracking_card": tracking_card,
        "tmux_session": tmux_session,
        "started_at": "2026-05-22T15:37:06Z",
    }))


# ---------------------------------------------------------------------------
# dispatch_once — the guardrail in the ready_rows loop
# ---------------------------------------------------------------------------

def test_guardrail_blocks_conflicting_card(kanban_home, spawnable):
    """A ready card whose run-registry lock is live gets blocked."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="conflicted", assignee="alice")
    _write_lock(kanban_home, "live-run", tracking_card=tid,
                branch="worker/conflicted")

    with kb.connect() as conn:
        result = kb.dispatch_once(conn)

    assert tid in result.blocked_guardrail
    assert tid not in [s[0] for s in result.spawned]
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "blocked"


def test_non_conflicting_card_dispatches_normally(kanban_home, spawnable):
    """A card with no matching live run dispatches as usual."""
    spawned = []

    def fake_spawn(task, workspace):
        spawned.append(task.id)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="clean", assignee="alice")
    # A lock exists, but for an unrelated card/branch — must not match.
    _write_lock(kanban_home, "other-run", tracking_card="t_unrelated",
                branch="worker/something-else")

    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert result.blocked_guardrail == []
    assert tid in [s[0] for s in result.spawned]
    assert spawned == [tid]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"


def test_dry_run_skips_the_guardrail_block(kanban_home, spawnable):
    """Under dry_run the guardrail never blocks — observation only."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="conflicted", assignee="alice")
    _write_lock(kanban_home, "live-run", tracking_card=tid,
                branch="worker/conflicted")

    with kb.connect() as conn:
        result = kb.dispatch_once(conn, dry_run=True)

    assert result.blocked_guardrail == []
    assert tid in [s[0] for s in result.spawned]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def test_dispatch_result_has_blocked_guardrail_field():
    """The DispatchResult dataclass exposes the new field as a list."""
    result = kb.DispatchResult()
    assert result.blocked_guardrail == []


# ---------------------------------------------------------------------------
# _hive_registry_conflict — the helper
# ---------------------------------------------------------------------------

def test_hive_registry_conflict_matches_branch_name(kanban_home):
    _write_lock(kanban_home, "run-a", tracking_card="t_owner",
                branch="worker/feature-y")
    task = types.SimpleNamespace(id="t_new", branch_name="worker/feature-y")
    assert kb._hive_registry_conflict(task) == "t_owner"


def test_hive_registry_conflict_matches_card_id(kanban_home):
    _write_lock(kanban_home, "run-b", tracking_card="t_dup",
                branch="worker/anything")
    task = types.SimpleNamespace(id="t_dup", branch_name=None)
    assert kb._hive_registry_conflict(task) == "t_dup"


def test_hive_registry_conflict_returns_none_when_no_match(kanban_home):
    _write_lock(kanban_home, "run-c", tracking_card="t_owner",
                branch="worker/feature-y")
    task = types.SimpleNamespace(id="t_other", branch_name="worker/unrelated")
    assert kb._hive_registry_conflict(task) is None


def test_hive_registry_conflict_no_registry_dir(kanban_home):
    # No locks written at all — registry dir may not even exist.
    task = types.SimpleNamespace(id="t_x", branch_name="worker/x")
    assert kb._hive_registry_conflict(task) is None
