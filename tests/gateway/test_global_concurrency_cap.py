import asyncio
from types import SimpleNamespace

from gateway.run import GatewayRunner


def _runner_without_init() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running = True
    return runner


def test_dispatcher_enforces_global_running_cap_across_boards(monkeypatch):
    import gateway.run as run
    import hermes_cli.config as config_module
    import hermes_cli.kanban_db as kanban_module

    runner = _runner_without_init()
    running_by_board = {"alpha": 2, "bravo": 1}
    dispatch_calls = []
    connections = []
    sleep_calls = 0

    class Conn:
        def __init__(self, board):
            self.board = board
            self.closed = False
            connections.append(self)

        def execute(self, query, params=()):
            assert "COUNT(*)" in query
            assert "status = 'running'" in query
            return SimpleNamespace(fetchone=lambda: (running_by_board[self.board],))

        def close(self):
            self.closed = True

    async def no_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            runner._running = False

    def dispatch_once(conn, **kwargs):
        dispatch_calls.append((conn.board, kwargs["max_spawn"]))
        running_by_board[conn.board] += 1
        return SimpleNamespace(spawned=[(f"task-{conn.board}", "default", "/tmp")])

    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    monkeypatch.setattr(config_module, "load_config", lambda: {"kanban": {"dispatch_in_gateway": True, "max_spawn": 3, "global_max_running": 4}})
    monkeypatch.setattr(run.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(kanban_module, "DEFAULT_BOARD", "alpha")
    monkeypatch.setattr(kanban_module, "DEFAULT_FAILURE_LIMIT", 5)
    monkeypatch.setattr(kanban_module, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kanban_module, "list_boards", lambda include_archived=False: [{"slug": "alpha"}, {"slug": "bravo"}])
    monkeypatch.setattr(kanban_module, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kanban_module, "kanban_db_path", lambda slug: SimpleNamespace(expanduser=lambda: SimpleNamespace(resolve=lambda: f"/{slug}.db"), stat=lambda: SimpleNamespace(st_mtime_ns=1, st_size=1)))
    monkeypatch.setattr(kanban_module, "connect", lambda board=None: Conn(board))
    monkeypatch.setattr(kanban_module, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kanban_module, "has_spawnable_review", lambda conn: False)
    monkeypatch.setattr(kanban_module, "dispatch_once", dispatch_once)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert dispatch_calls == [("alpha", 1)]
    assert sum(running_by_board.values()) == 4
    assert all(conn.closed for conn in connections)


def test_dispatcher_global_cap_defaults_to_max_spawn_when_unset(monkeypatch):
    import gateway.run as run
    import hermes_cli.config as config_module
    import hermes_cli.kanban_db as kanban_module

    runner = _runner_without_init()
    max_spawn_values = []
    sleep_calls = 0

    class Conn:
        def execute(self, query, params=()):
            assert "COUNT(*)" in query
            assert "status = 'running'" in query
            return SimpleNamespace(fetchone=lambda: (0,))

        def close(self):
            pass

    async def no_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            runner._running = False

    def dispatch_once(conn, **kwargs):
        max_spawn_values.append(kwargs["max_spawn"])
        return SimpleNamespace(spawned=[])

    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    monkeypatch.setattr(config_module, "load_config", lambda: {"kanban": {"dispatch_in_gateway": True, "max_spawn": 3}})
    monkeypatch.setattr(run.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(kanban_module, "DEFAULT_BOARD", "solo")
    monkeypatch.setattr(kanban_module, "DEFAULT_FAILURE_LIMIT", 5)
    monkeypatch.setattr(kanban_module, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kanban_module, "list_boards", lambda include_archived=False: [{"slug": "solo"}])
    monkeypatch.setattr(kanban_module, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kanban_module, "kanban_db_path", lambda slug: SimpleNamespace(expanduser=lambda: SimpleNamespace(resolve=lambda: f"/{slug}.db"), stat=lambda: SimpleNamespace(st_mtime_ns=1, st_size=1)))
    monkeypatch.setattr(kanban_module, "connect", lambda board=None: Conn())
    monkeypatch.setattr(kanban_module, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kanban_module, "has_spawnable_review", lambda conn: False)
    monkeypatch.setattr(kanban_module, "dispatch_once", dispatch_once)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert max_spawn_values == [3]
