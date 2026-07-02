"""Regression tests for webhook run-end finalization lifecycle."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import webhook as webhook_module
from gateway.platforms.webhook import (
    DEFAULT_WEBHOOK_DENY_PATTERNS,
    WebhookAdapter,
    _INSECURE_NO_AUTH,
)
from tools import approval as approval_module
from tools.approval import check_session_deny_patterns, get_session_deny_pattern_strings


async def _post(cli: TestClient, delivery_id: str):
    return await cli.post(
        "/webhooks/loki1",
        json={"message": "OBJECTIVE: hold lane open"},
        headers={"X-Request-ID": delivery_id},
    )


def _reset_global_state() -> None:
    webhook_module._AGENT_RUN_SEMAPHORE = None
    webhook_module._AGENT_RUN_SEMAPHORE_CAP = None
    approval_module._session_deny_patterns.clear()
    approval_module._session_credential_taint.clear()


@pytest.fixture(autouse=True)
def _clean_globals():
    _reset_global_state()
    yield
    _reset_global_state()


def _make_adapter(*, cap: int = 1) -> WebhookAdapter:
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
    # Keep these tests focused on lifecycle finalization, not relay-worktree setup.
    adapter._wt_enabled = False
    return adapter


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _start_slow_real_run(adapter: WebhookAdapter, cli: TestClient, delivery_id: str):
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    observed: dict[str, object] = {}

    async def slow_handler(event):
        observed["event"] = event
        observed["session_key"] = adapter._build_session_key(event.source)
        started.set()
        try:
            await release.wait()
        finally:
            finished.set()
        return None

    # Exercise BasePlatformAdapter.handle_message() -> _process_message_background().
    # Stubbing handle_message directly is the blind spot this regression protects.
    adapter.set_message_handler(slow_handler)

    response = await _post(cli, delivery_id)
    body = await response.json()
    assert response.status == 202, body
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    return observed, release, finished


@pytest.mark.asyncio
async def test_semaphore_permit_held_after_202_until_processing_complete():
    adapter = _make_adapter(cap=1)

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        _observed, release, finished = await _start_slow_real_run(adapter, cli, "run-held")

        assert finished.is_set() is False
        assert adapter._agent_run_semaphore._value == 0
        assert adapter._agent_run_semaphore.locked() is True

        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        if adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        await asyncio.sleep(0.05)

        assert adapter._agent_run_semaphore._value == 1
        assert adapter._run_finalizers == {}


@pytest.mark.asyncio
async def test_second_delivery_receives_429_while_first_run_processing():
    adapter = _make_adapter(cap=1)

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        _observed, release, finished = await _start_slow_real_run(adapter, cli, "first")

        second = await _post(cli, "second")
        second_body = await second.json()
        assert second.status == 429, second_body
        assert second_body["error"] == "max_concurrent_agent_runs_exhausted"
        assert second.headers["Retry-After"] == "30"
        assert "second" not in adapter._seen_deliveries

        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        if adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_default_route_deny_patterns_clear_only_after_processing_complete():
    adapter = _make_adapter(cap=1)

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        observed, release, finished = await _start_slow_real_run(adapter, cli, "deny-lifecycle")
        session_key = observed["session_key"]

        registered = get_session_deny_pattern_strings(session_key)
        assert registered[: len(DEFAULT_WEBHOOK_DENY_PATTERNS)] == DEFAULT_WEBHOOK_DENY_PATTERNS
        assert check_session_deny_patterns("git push origin HEAD", session_key=session_key)[0] is True

        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        if adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        await asyncio.sleep(0.05)

        assert get_session_deny_pattern_strings(session_key) == []
        assert check_session_deny_patterns("git push origin HEAD", session_key=session_key)[0] is False


@pytest.mark.asyncio
async def test_finalizer_key_matches_processing_complete_lookup_and_is_idempotent():
    adapter = _make_adapter(cap=1)

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        observed, release, finished = await _start_slow_real_run(adapter, cli, "idempotent")
        event = observed["event"]
        session_key = observed["session_key"]

        assert list(adapter._run_finalizers) == [session_key]
        assert adapter._run_finalizer_key(event) == session_key
        assert adapter._agent_run_semaphore._value == 0

        adapter._finalize_run(session_key)
        assert adapter._agent_run_semaphore._value == 1
        assert adapter._run_finalizers == {}

        adapter._finalize_run(session_key)
        assert adapter._agent_run_semaphore._value == 1

        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        if adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
        await asyncio.sleep(0.05)

        assert adapter._agent_run_semaphore._value == 1
        assert adapter._run_finalizers == {}


@pytest.mark.asyncio
async def test_worktree_refusal_releases_slot_once_and_registers_no_finalizer():
    adapter = _make_adapter(cap=1)
    adapter._wt_enabled = True
    adapter._ensure_relay_worktree = lambda: None

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        response = await _post(cli, "refused")
        body = await response.json()

    assert response.status == 503, body
    assert body["error"] == "worktree_unavailable"
    assert adapter._agent_run_semaphore._value == 1
    assert adapter._run_finalizers == {}
    assert "refused" not in adapter._seen_deliveries

    # A second cleanup would have inflated a plain asyncio.Semaphore; refusal
    # paths must not register a finalizer or over-release the acquired slot.
    assert adapter._agent_run_semaphore._value <= adapter._max_concurrent_agent_runs
