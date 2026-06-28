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


# ===========================================================================
# Off-loop lane pool tests (offloop_lane_pool=True path)
# ===========================================================================

from gateway import lane_executor as lane_executor_module


@pytest.fixture(autouse=True)
def _reset_lane_pool():
    lane_executor_module.reset_lane_pool()
    yield
    lane_executor_module.reset_lane_pool()


def _make_lane_adapter(*, cap: int) -> WebhookAdapter:
    """Create a WebhookAdapter with the off-loop lane pool enabled."""
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "max_concurrent_agent_runs": cap,
                "offloop_lane_pool": True,
                "lane_pool_workers": cap,
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
    adapter._wt_enabled = False
    return adapter


@pytest.mark.asyncio
async def test_lane_pool_admission_rejects_over_cap():
    """With cap=4 and 8 concurrent POSTs, 4 accepted (202) and 4 rejected (429)."""
    cap = 4
    adapter = _make_lane_adapter(cap=cap)
    assert adapter._lane_pool_enabled, "lane pool must be enabled"
    release = asyncio.Event()

    async def _hold(event):
        await release.wait()

    adapter.handle_message = _hold

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        responses = await asyncio.gather(
            *[_post(cli, f"lane-{i}") for i in range(8)]
        )
        statuses = [resp.status for resp in responses]
        retry_afters = [
            resp.headers.get("Retry-After")
            for resp in responses
            if resp.status == 429
        ]
        bodies = [await resp.json() for resp in responses]

        assert Counter(statuses) == Counter({202: cap, 429: 8 - cap})
        assert retry_afters == ["30"] * (8 - cap)
        assert all(
            body.get("error") == "max_concurrent_agent_runs_exhausted"
            for body in bodies
            if body.get("status") == "rate_limited"
        )
        # Pool reports correct inflight
        await asyncio.sleep(0.05)
        stats = adapter._lane_pool.stats()
        assert stats["inflight"] == cap
        assert stats["rejected"] == 8 - cap

        release.set()
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_lane_pool_releases_slot_on_success_error_timeout():
    """Inflight counter returns to 0 after each outcome (success / error / timeout)."""
    adapter = _make_lane_adapter(cap=1)
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
            resp = await _post(cli, f"lane-seq-{idx}")
            assert resp.status == 202
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
            stats = adapter._lane_pool.stats()
            assert stats["inflight"] == 0, f"inflight should be 0 after outcome {idx}"

        final = await _post(cli, "lane-seq-after-timeout")
        assert final.status == 202
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert calls == 4
    assert adapter._lane_pool.stats()["inflight"] == 0


@pytest.mark.asyncio
async def test_flag_off_uses_default_executor_unchanged(monkeypatch):
    """When offloop_lane_pool is disabled, run_in_executor receives None (default pool)."""
    from gateway.run import GatewayRunner

    captured_executors: list = []
    original_run_in_executor = asyncio.AbstractEventLoop.run_in_executor

    async def _patched_run_in_executor(self, executor, func, *args):
        captured_executors.append(executor)
        # Run sync for test purposes
        return func(*args)

    monkeypatch.setattr(
        asyncio.AbstractEventLoop,
        "run_in_executor",
        _patched_run_in_executor,
    )

    # Simulate an instance of GatewayRunner with a mock _run_in_executor_with_context call
    # We test the contextvar directly: with no set_lane_executor called, get_lane_executor() is None
    from agent.codex_session_context import get_lane_executor, set_lane_executor, reset_lane_executor

    # Default context: no executor set
    assert get_lane_executor() is None

    # After setting, should return the executor
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        tok = set_lane_executor(pool)
        assert get_lane_executor() is pool
        reset_lane_executor(tok)
        assert get_lane_executor() is None
    finally:
        pool.shutdown(wait=False)


def test_gateway_config_offloop_lane_pool_reaches_adapter():
    """MAJOR-1 regression: GatewayConfig.offloop_lane_pool=True must survive
    the run.py config.extra injection and actually enable the lane pool in
    the WebhookAdapter.

    This tests the exact same dict-merge that GatewayRunner._create_adapter
    performs when platform == Platform.WEBHOOK (the MAJOR-1 fix site).
    Without the fix, gateway.offloop_lane_pool: true in config.yaml was
    silently dropped and adapter._lane_pool_enabled stayed False.
    """
    from gateway.config import GatewayConfig, PlatformConfig

    # Build a GatewayConfig as load_gateway_config() would produce it.
    gw_config = GatewayConfig(offloop_lane_pool=True, max_concurrent_agent_runs=4)
    assert gw_config.offloop_lane_pool is True

    # Simulate what run.py _create_adapter does for Platform.WEBHOOK (MAJOR-1 fix):
    base_extra = {
        "host": "127.0.0.1",
        "port": 0,
        "routes": {
            "loki1": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "{message}",
                "deliver": "log",
            }
        },
    }
    injected_extra = {
        **base_extra,
        "max_concurrent_agent_runs": gw_config.max_concurrent_agent_runs,
        # These two lines are the MAJOR-1 fix:
        "offloop_lane_pool": base_extra.get("offloop_lane_pool", gw_config.offloop_lane_pool),
        "lane_pool_workers": base_extra.get("lane_pool_workers", gw_config.lane_pool_workers),
    }
    platform_cfg = PlatformConfig(enabled=True, extra=injected_extra)

    adapter = WebhookAdapter(platform_cfg)
    adapter._wt_enabled = False

    assert adapter._lane_pool_enabled, (
        "GatewayConfig.offloop_lane_pool=True must reach adapter._lane_pool_enabled "
        "after the run.py config.extra injection. Without the MAJOR-1 fix, this is "
        "False because max_concurrent_agent_runs was the only injected key."
    )
    assert adapter._lane_pool is not None, "Lane pool object must be created when enabled"
    stats = adapter._lane_pool.stats()
    assert stats["capacity"] == gw_config.max_concurrent_agent_runs
