import asyncio
import logging

from gateway.run import GatewayRunner


def test_dispatcher_missing_dispatch_in_gateway_key_defaults_disabled(monkeypatch, caplog):
    import gateway.run as run
    import hermes_cli.config as config_module

    runner = object.__new__(GatewayRunner)
    runner._running = True
    sleep_calls = []

    async def fail_if_dispatch_loop_starts(_seconds):
        sleep_calls.append(_seconds)
        runner._running = False

    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    monkeypatch.setattr(config_module, "load_config", lambda: {"kanban": {}})
    monkeypatch.setattr(run.asyncio, "sleep", fail_if_dispatch_loop_starts)
    caplog.set_level(logging.INFO)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert sleep_calls == []
    assert "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false" in caplog.text
