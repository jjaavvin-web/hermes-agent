import asyncio
import fcntl
import logging
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner


def _runner_without_init() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running = True
    return runner


async def _run_single_dispatch_tick(monkeypatch, tmp_path, *, tick=None):
    import gateway.run as run
    import hermes_cli.config as config_module
    import hermes_cli.kanban_db as kanban_module
    import hermes_constants

    runner = _runner_without_init()

    sleep_calls = 0

    async def no_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            runner._running = False

    class FakeKanban:
        DEFAULT_BOARD = "default"
        DEFAULT_FAILURE_LIMIT = 5

        @staticmethod
        def reap_worker_zombies():
            return []

        @staticmethod
        def list_boards(include_archived=False):
            return [{"slug": "default"}]

        @staticmethod
        def read_board_metadata(_slug):
            return {"slug": "default"}

        @staticmethod
        def kanban_db_path(slug):
            return tmp_path / f"{slug}.db"

        @staticmethod
        def has_spawnable_ready(_conn):
            return False

        @staticmethod
        def has_spawnable_review(_conn):
            return False

        @staticmethod
        def connect(board=None):
            class Conn:
                def close(self):
                    pass

            return Conn()

        @staticmethod
        def dispatch_once(*args, **kwargs):
            if tick is not None:
                return tick()
            return object()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(run, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(run.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(config_module, "load_config", lambda: {"kanban": {"dispatch_in_gateway": True}})
    for name in (
        "reap_worker_zombies",
        "list_boards",
        "read_board_metadata",
        "kanban_db_path",
        "has_spawnable_ready",
        "has_spawnable_review",
        "connect",
        "dispatch_once",
    ):
        monkeypatch.setattr(kanban_module, name, getattr(FakeKanban, name))
    monkeypatch.setattr(kanban_module, "DEFAULT_BOARD", FakeKanban.DEFAULT_BOARD)
    monkeypatch.setattr(kanban_module, "DEFAULT_FAILURE_LIMIT", FakeKanban.DEFAULT_FAILURE_LIMIT)

    await runner._kanban_dispatcher_watcher()
    return tmp_path / "kanban" / "dispatch.lock"


def test_dispatch_tick_uses_profile_dispatch_lock(monkeypatch, tmp_path):
    lock_seen_by_tick = []

    def tick():
        lock_path = tmp_path / "kanban" / "dispatch.lock"
        with lock_path.open("a+", encoding="utf-8") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_seen_by_tick.append(True)
            else:
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                lock_seen_by_tick.append(False)
        return SimpleNamespace(spawned=[])

    lock_path = asyncio.run(_run_single_dispatch_tick(monkeypatch, tmp_path, tick=tick))

    assert lock_path == tmp_path / "kanban" / "dispatch.lock"
    assert lock_path.exists()
    assert lock_path.stat().st_mode & 0o777 == 0o600
    assert lock_seen_by_tick == [True]


def test_dispatch_tick_skips_when_lock_is_already_held(monkeypatch, tmp_path, caplog):
    lock_path = tmp_path / "kanban" / "dispatch.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.touch(mode=0o600)
    tick_calls = []

    with lock_path.open("a+", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        caplog.set_level(logging.INFO)
        asyncio.run(
            _run_single_dispatch_tick(
                monkeypatch,
                tmp_path,
                tick=lambda: tick_calls.append("called"),
            )
        )
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    assert tick_calls == []
    assert "kanban dispatcher: dispatch lock held; skipping tick" in caplog.text


def test_dispatch_lock_released_when_tick_raises(monkeypatch, tmp_path):
    def tick():
        raise RuntimeError("boom")

    lock_path = asyncio.run(_run_single_dispatch_tick(monkeypatch, tmp_path, tick=tick))

    with lock_path.open("a+", encoding="utf-8") as probe:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
