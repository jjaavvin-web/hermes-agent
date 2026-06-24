"""Regression tests for webhook inbound agent-run backpressure."""

import asyncio
from collections import Counter

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import webhook as webhook_module
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


async def _post(cli: TestClient, delivery_id: str):
    return await cli.post(
        "/webhooks/loki1",
        json={"message": "OBJECTIVE: hold lane open"},
        headers={"X-Request-ID": delivery_id},
    )


def _make_adapter(*, cap: int) -> WebhookAdapter:
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "max_concurrent_agent_runs": cap,
                "routes": {
                    "loki1": {
                        "secret": _INSECURE_NO_AUTH,
                        "prompt": "{message}",
                        "deliver": "log",
                    }
                },
            },
        )
    )
    # The developer shell may export HERMES_WEBHOOK_WORKTREE=1. Backpressure
    # tests isolate the semaphore behavior, not relay-worktree setup.
    adapter._wt_enabled = False
    return adapter


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _reset_agent_run_semaphore() -> None:
    webhook_module._AGENT_RUN_SEMAPHORE = None
    webhook_module._AGENT_RUN_SEMAPHORE_CAP = None


@pytest.fixture(autouse=True)
def _reset_global_agent_run_semaphore():
    _reset_agent_run_semaphore()
    yield
    _reset_agent_run_semaphore()


def test_global_agent_run_semaphore_is_shared_across_adapter_instances():
    first = _make_adapter(cap=2)
    second = _make_adapter(cap=2)

    assert first._agent_run_semaphore is second._agent_run_semaphore


@pytest.mark.asyncio
async def test_webhook_agent_run_backpressure_rejects_over_cap_with_retry_after():
    """Eight concurrent POSTs should spawn only cap agent tasks; overflow gets 429."""
    cap = 4
    adapter = _make_adapter(cap=cap)
    started: list[str] = []
    release = asyncio.Event()

    async def _hold(event):
        started.append(event.message_id)
        await release.wait()

    adapter.handle_message = _hold

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        responses = await asyncio.gather(
            *[_post(cli, f"delivery-{i}") for i in range(8)]
        )
        statuses = [resp.status for resp in responses]
        retry_after = [resp.headers.get("Retry-After") for resp in responses if resp.status == 429]
        bodies = [await resp.json() for resp in responses]

        assert Counter(statuses) == Counter({202: cap, 429: 8 - cap})
        assert retry_after == ["30"] * (8 - cap)
        assert all(
            body.get("error") == "max_concurrent_agent_runs_exhausted"
            for body in bodies
            if body.get("status") == "rate_limited"
        )

        await asyncio.sleep(0.05)
        assert len(started) == cap

        release.set()
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_webhook_agent_run_backpressure_releases_capacity_after_success_error_and_timeout():
    """The semaphore slot is released from the task finally block for all outcomes."""
    adapter = _make_adapter(cap=1)
    calls = 0

    async def _outcome(event):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        if calls == 2:
            raise RuntimeError("boom")
        if calls == 3:
            raise asyncio.TimeoutError("timed out")

    adapter.handle_message = _outcome

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        for idx in range(3):
            resp = await _post(cli, f"seq-{idx}")
            assert resp.status == 202
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
            assert adapter._agent_run_semaphore._value == 1

        final = await _post(cli, "seq-after-timeout")
        assert final.status == 202
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert calls == 4
    assert adapter._agent_run_semaphore._value == 1


@pytest.mark.asyncio
async def test_webhook_single_and_under_cap_flows_still_accept():
    adapter = _make_adapter(cap=4)
    seen: list[str] = []

    async def _capture(event):
        seen.append(event.message_id)

    adapter.handle_message = _capture

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        single = await _post(cli, "single")
        assert single.status == 202
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

        responses = await asyncio.gather(*[_post(cli, f"under-{i}") for i in range(4)])
        assert [resp.status for resp in responses] == [202, 202, 202, 202]
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert seen == ["single", "under-0", "under-1", "under-2", "under-3"]
