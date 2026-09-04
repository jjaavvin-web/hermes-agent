"""Certification that the retired Kanban tombstones stay inert (P7 item 8).

Kanban was fully retired live on 2026-09-01. The former live paths are now
fail-closed tombstones:

* ``~/.hermes/kanban.db`` is a mode-0555 **directory** containing a single
  read-only file ``RETIRED``.
* ``~/.hermes/kanban/`` is a mode-0555 directory containing only
  ``RETIRED.md``.

The merged code (``hermes_cli/kanban_db.py``, ``gateway/kanban_watchers.py``)
still ships and must stay INERT against that shape: no database recreation,
no writable SQLite connection, no directory creation, no watcher/dispatcher
loop spinning hot, and no unhandled exception out of a single watcher tick.

Every test builds its own isolated ``HERMES_HOME`` mirroring the tombstone
shape via ``tmp_path`` + ``monkeypatch`` — nothing under the real
``~/.hermes`` is ever opened or modified.
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest


def _make_tombstone_home(tmp_path: Path) -> Path:
    """Build a temp HERMES_HOME mirroring the live retired Kanban shape."""
    home = tmp_path / ".hermes"
    home.mkdir()
    # Minimal config.yaml — no kanban section, so every kanban.* flag falls
    # through to its fail-closed code default (DISP-1).
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")

    kanban_db_dir = home / "kanban.db"
    kanban_db_dir.mkdir()
    (kanban_db_dir / "RETIRED").write_text(
        "Kanban retired 2026-09-01. Board history is read-only.\n",
        encoding="utf-8",
    )
    kanban_db_dir.chmod(0o555)

    kanban_dir = home / "kanban"
    kanban_dir.mkdir()
    (kanban_dir / "RETIRED.md").write_text(
        "# Kanban retired\n\nSee project history for the archived board.\n",
        encoding="utf-8",
    )
    kanban_dir.chmod(0o555)

    return home


def _db_sidecar_paths(root: Path) -> set[str]:
    """Every path under root whose name looks like a sqlite DB or sidecar."""
    hits: set[str] = set()
    for p in root.rglob("*"):
        name = p.name
        if name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
            hits.add(str(p.relative_to(root)))
    return hits


@pytest.fixture
def tombstone_home(tmp_path, monkeypatch):
    home = _make_tombstone_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield home
    # Restore write bits so pytest's own tmp_path cleanup can rmtree these
    # directories — the read-only permission is the thing under test, not a
    # constraint on teardown.
    for name in ("kanban.db", "kanban"):
        try:
            (home / name).chmod(0o755)
        except OSError:
            pass


def _assert_tombstone_untouched(home: Path) -> None:
    kanban_db_dir = home / "kanban.db"
    assert kanban_db_dir.is_dir(), "kanban.db tombstone must remain a directory"
    mode = stat.S_IMODE(kanban_db_dir.stat().st_mode)
    assert mode == 0o555, f"kanban.db tombstone permissions changed to {oct(mode)}"
    assert [p.name for p in kanban_db_dir.iterdir()] == ["RETIRED"], (
        "kanban.db tombstone must contain only RETIRED"
    )


class TestConnectDoesNotRecreateTheDatabase:
    """2a — the public opener must not create/open a writable DB."""

    def test_connect_raises_instead_of_recreating_the_db(self, tombstone_home):
        from hermes_cli import kanban_db as kb

        with pytest.raises(Exception):
            kb.connect()

    def test_connect_failure_leaves_the_tombstone_directory_unchanged(
        self, tombstone_home
    ):
        from hermes_cli import kanban_db as kb

        try:
            kb.connect()
        except Exception:
            pass
        _assert_tombstone_untouched(tombstone_home)

    def test_connect_failure_creates_no_db_or_sidecar_files(self, tombstone_home):
        from hermes_cli import kanban_db as kb

        before = _db_sidecar_paths(tombstone_home)
        try:
            kb.connect()
        except Exception:
            pass
        after = _db_sidecar_paths(tombstone_home)
        assert after == before, f"connect() left new DB/sidecar files: {after - before}"


class TestNotifierWatcherSingleTick:
    """2b — one notifier-collection tick must not raise and must not write."""

    @staticmethod
    def _make_runner():
        from gateway.run import GatewayRunner

        class _NoopAdapter:
            async def send(self, chat_id, text, metadata=None):
                return None

            async def handle_message(self, event):
                return None

        from gateway.config import Platform

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        runner.adapters = {Platform.TELEGRAM: _NoopAdapter()}
        runner._kanban_sub_fail_counts = {}
        runner._kanban_dispatcher_lock_handle = object()
        return runner

    @staticmethod
    async def _run_one_tick(monkeypatch, runner):
        real_sleep = asyncio.sleep

        async def fake_sleep(delay):
            if delay == 5:
                return None
            runner._running = False
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await asyncio.wait_for(runner._kanban_notifier_watcher(interval=1), timeout=2.0)

    def test_single_tick_completes_without_raising(self, tombstone_home, monkeypatch):
        runner = self._make_runner()
        asyncio.run(self._run_one_tick(monkeypatch, runner))

    def test_single_tick_creates_no_db_or_sidecar_files(self, tombstone_home, monkeypatch):
        runner = self._make_runner()
        before = _db_sidecar_paths(tombstone_home)
        asyncio.run(self._run_one_tick(monkeypatch, runner))
        after = _db_sidecar_paths(tombstone_home)
        assert after == before, f"notifier tick left new DB/sidecar files: {after - before}"

    def test_single_tick_leaves_the_tombstone_directory_unchanged(
        self, tombstone_home, monkeypatch
    ):
        runner = self._make_runner()
        asyncio.run(self._run_one_tick(monkeypatch, runner))
        _assert_tombstone_untouched(tombstone_home)


class TestDispatcherWatcherFailClosed:
    """2c — the dispatcher watcher must return early under the fail-closed
    default ``kanban.dispatch_in_gateway=False`` and never touch the board.
    """

    @staticmethod
    def _make_runner():
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        return runner

    def test_dispatcher_watcher_returns_without_raising(self, tombstone_home):
        runner = self._make_runner()
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=2.0))

    def test_dispatcher_watcher_creates_no_db_or_sidecar_files(self, tombstone_home):
        runner = self._make_runner()
        before = _db_sidecar_paths(tombstone_home)
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=2.0))
        after = _db_sidecar_paths(tombstone_home)
        assert after == before, f"dispatcher watcher left new DB/sidecar files: {after - before}"

    def test_dispatcher_watcher_leaves_the_tombstone_directory_unchanged(
        self, tombstone_home
    ):
        runner = self._make_runner()
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=2.0))
        _assert_tombstone_untouched(tombstone_home)


class TestBoardListingUnderRetiredKanbanDir:
    """2d — board discovery under kanban/ must not raise and must find
    nothing beyond the always-present 'default' entry when the directory
    holds only RETIRED.md (no boards/ subtree to scan).
    """

    def test_list_boards_does_not_raise(self, tombstone_home):
        from hermes_cli import kanban_db as kb

        kb.list_boards()

    def test_list_boards_discovers_no_boards_beyond_default(self, tombstone_home):
        from hermes_cli import kanban_db as kb

        boards = kb.list_boards()
        discovered = [b.get("slug") for b in boards if b.get("slug") != kb.DEFAULT_BOARD]
        assert discovered == [], f"retired kanban/ dir yielded extra boards: {discovered}"
