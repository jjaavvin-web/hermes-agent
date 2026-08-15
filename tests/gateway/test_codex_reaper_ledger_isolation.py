"""Guard: the reaper/watcher/registry-GC state paths must never escape to the real home.

Why this file exists
--------------------
``CodexSessionReaper``, ``CodexGcWatcher`` and ``CodexRegistryGc`` all resolve
their Hermes home by probing the dispatcher (and, for the reaper, the broker)
for a ``hermes_home`` / ``_hermes_home`` attribute.  That probe *correctly*
rejects a ``MagicMock``'s auto-created attribute via an ``isinstance`` guard —
but the fallback it then took was ``Path.home() / ".hermes"``, which ignores
``HERMES_HOME`` entirely.

The consequence was not theoretical.  ``tests/gateway/test_codex_gc_watcher.py``
uses a hand-rolled ``_FakeDispatcher`` that never sets ``hermes_home`` and a
``MagicMock()`` broker, so every ``await w._tick()`` in that file appended real
JSONL decisions to the *operator's* live ``~/.hermes/state/codex-reaper/
reap-ledger.jsonl`` — 1.8k records, on a file the C7 work also found to be
unbounded at ~12.9 MB / 26k lines.

The fix is at the callsite (``get_hermes_home()``), which makes containment
automatic for the whole suite, because ``tests/conftest.py::_hermetic_environment``
already pins ``HERMES_HOME`` at a per-test tmpdir for every test.  These tests
pin that behaviour down *directly*, so reverting the callsite fix goes red here
rather than silently resuming the writes.

Note the assertions are behavioural, not source greps: a one-directional
"is ``Path.home()`` still absent from the module text" check is exactly the
kind of coverage the C7 audit flagged as a mutation survivor.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.codex_gc_watcher import CodexGcWatcher
from gateway.codex_registry_gc import CodexRegistryGc
from gateway.codex_session_reaper import CodexSessionReaper


class _AttributelessDispatcher:
    """A dispatcher double with NO ``hermes_home`` — the shape that leaked.

    Deliberately mirrors ``test_codex_gc_watcher._FakeDispatcher``: it can load
    state, and it exposes nothing the home-resolution probe can latch onto.
    """

    def __init__(self, rows: dict | None = None) -> None:
        self._state = {"version": 1, "sessions": rows or {}}

    def _load_state(self) -> dict:
        return dict(self._state)

    def _write_state(self, state: dict) -> None:
        self._state = state


def _hermes_home_from_env() -> Path:
    """The per-test tmpdir HERMES_HOME that the root conftest pins."""
    value = os.environ.get("HERMES_HOME", "")
    assert value, "root conftest must pin HERMES_HOME for every test"
    return Path(value)


def _assert_contained(path: Path, label: str) -> None:
    """``path`` is under the per-test HERMES_HOME and not under the real home."""
    pinned = _hermes_home_from_env()
    assert pinned in path.parents, (
        f"{label} resolved to {path}, which is NOT under the per-test "
        f"HERMES_HOME ({pinned}). The Path.home() fallback is reachable again."
    )
    real_home_hermes = Path.home() / ".hermes"
    assert real_home_hermes not in path.parents, (
        f"{label} resolved into the operator's REAL Hermes home ({path}). "
        "This writes live operator state from the test suite."
    )


# --------------------------------------------------------------------------- #
# the three resolvers
# --------------------------------------------------------------------------- #
def test_reaper_ledger_path_honours_hermes_home_when_owners_lack_the_attribute():
    """Neither a bare double nor a MagicMock can push the ledger to the real home."""
    reaper = CodexSessionReaper(
        dispatcher_state=_AttributelessDispatcher(),
        broker=MagicMock(),
        gh_open_branches_fn=lambda: set(),
    )
    _assert_contained(reaper._ledger_path, "CodexSessionReaper._ledger_path")


def test_gc_watcher_reap_ledger_path_honours_hermes_home():
    watcher = CodexGcWatcher(
        dispatcher=_AttributelessDispatcher(),
        worktree_broker=MagicMock(),
        gh_list_open_branches=lambda: set(),
    )
    _assert_contained(
        watcher._reap_ledger_path(), "CodexGcWatcher._reap_ledger_path()"
    )


def test_registry_gc_archive_path_honours_hermes_home():
    """The tombstone archive has the same fallback; no current test reaches it."""
    gc = CodexRegistryGc(_AttributelessDispatcher())
    _assert_contained(gc._archive_path, "CodexRegistryGc._archive_path")


def test_watcher_and_reaper_agree_on_the_same_ledger_file():
    """The watcher's preview ledger and the reaper's decision ledger are one file.

    They resolve independently, so a fix applied to only one of them would
    silently split operator state across two paths.
    """
    disp = _AttributelessDispatcher()
    watcher = CodexGcWatcher(
        dispatcher=disp, worktree_broker=MagicMock(),
        gh_list_open_branches=lambda: set(),
    )
    reaper = CodexSessionReaper(
        dispatcher_state=disp, broker=MagicMock(), gh_open_branches_fn=lambda: set(),
    )
    assert watcher._reap_ledger_path() == reaper._ledger_path


# --------------------------------------------------------------------------- #
# end-to-end: the exact shape that was polluting the live ledger
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gc_watcher_tick_writes_its_ledger_inside_hermes_home():
    """A full ``_tick()`` with the leaky fixture shape stays inside the tmpdir.

    This is the regression proper: on the unfixed callsite these very rows land
    in the operator's real ledger.
    """
    rows = {
        "t1": {"session_id": "sid-aaa", "state": "EXECUTING", "thread_id": "t-1"},
        "t2": {"session_id": "sid-bbb", "state": "EXECUTING", "thread_id": "t-2"},
    }
    broker = MagicMock()
    broker.gc.return_value = []
    broker.reap_deleted.return_value = 0

    watcher = CodexGcWatcher(
        dispatcher=_AttributelessDispatcher(rows),
        worktree_broker=broker,
        gh_list_open_branches=lambda: set(),
    )
    await watcher._tick()

    contained = (
        _hermes_home_from_env() / "state" / "codex-reaper" / "reap-ledger.jsonl"
    )
    assert contained.exists(), (
        "the tick produced no ledger inside the per-test HERMES_HOME — it went "
        "somewhere else, which is the bug this file guards"
    )
    assert contained.read_text(encoding="utf-8").strip(), "ledger written but empty"


# --------------------------------------------------------------------------- #
# the tripwire itself must be immune to the suite's own monkeypatching
# --------------------------------------------------------------------------- #
def test_the_tripwire_fingerprint_ignores_a_patched_os_listdir(tmp_path, monkeypatch):
    """``_codex_reaper_state_fingerprint`` must go through the REAL os hooks.

    Several tests in this package replace ``os.listdir`` globally (a ``/proc``
    walk patched to raise ``OSError``, or one returning a fixed pid list).  The
    fingerprint runs at fixture teardown, i.e. potentially while such a patch is
    still installed, and it fails in two directions if it goes through the patch:
    it errors the victim test (a false red, attributed to the wrong test), and a
    patch returning a fixed listing makes it see no change at all (a false green
    — the worse half).  Both are killed by binding the real callables at import.
    """
    from tests.gateway.conftest import _codex_reaper_state_fingerprint

    probe = tmp_path / "probe-state"
    probe.mkdir()
    (probe / "reap-ledger.jsonl").write_text("x\n", encoding="utf-8")

    truth = _codex_reaper_state_fingerprint(probe)
    assert set(truth) == {"reap-ledger.jsonl"}

    monkeypatch.setattr(os, "listdir", MagicMock(side_effect=OSError("no /proc here")))
    monkeypatch.setattr(os, "stat", MagicMock(side_effect=OSError("no /proc here")))

    assert _codex_reaper_state_fingerprint(probe) == truth, (
        "the guard read the filesystem through the test's monkeypatch: it would "
        "error the patching test at teardown, and could be blinded outright"
    )
