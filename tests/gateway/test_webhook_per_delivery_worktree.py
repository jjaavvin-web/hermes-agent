"""Regression tests for F4 per-delivery webhook worktree broker wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent.codex_session_context import get_active_worktree
from agent.worktree_broker import DiskPressureError, LeaseCapacityError
from gateway.config import PlatformConfig
from gateway.platforms import webhook as webhook_module
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from tools import approval as approval_module


async def _post(cli: TestClient, delivery_id: str, route: str = "loki1"):
    return await cli.post(
        f"/webhooks/{route}",
        json={"message": "OBJECTIVE: exercise per-delivery worktree"},
        headers={"X-Request-ID": delivery_id},
    )


def _reset_global_state() -> None:
    webhook_module._AGENT_RUN_SEMAPHORE = None
    webhook_module._AGENT_RUN_SEMAPHORE_CAP = None
    approval_module._session_deny_patterns.clear()
    approval_module._session_credential_taint.clear()


@pytest.fixture(autouse=True)
def _clean_globals(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.delenv("HERMES_WEBHOOK_WORKTREE", raising=False)
    monkeypatch.delenv("HERMES_WEBHOOK_PER_DELIVERY_WT", raising=False)
    monkeypatch.delenv("HERMES_WEBHOOK_BASE_BRANCH", raising=False)
    _reset_global_state()
    yield
    _reset_global_state()


def _make_adapter(*, cap: int = 2) -> WebhookAdapter:
    return WebhookAdapter(
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


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


@pytest.mark.asyncio
async def test_switch_off_preserves_singleton_path_and_has_zero_broker_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBHOOK_WORKTREE", "1")
    monkeypatch.delenv("HERMES_WEBHOOK_PER_DELIVERY_WT", raising=False)
    adapter = _make_adapter(cap=1)
    adapter._ensure_relay_worktree = lambda: str(tmp_path / "relay-wt" / "relay")
    Path(adapter._ensure_relay_worktree()).mkdir(parents=True)
    allocated = []

    def _unexpected_allocate(*args, **kwargs):
        allocated.append((args, kwargs))
        raise AssertionError("per-delivery broker must stay off")

    adapter._allocate_per_delivery_worktree = _unexpected_allocate
    observed: list[str | None] = []

    async def _handler(event):
        observed.append(get_active_worktree())

    adapter.handle_message = _handler

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        response = await _post(cli, "off-delivery")
        body = await response.json()
        assert response.status == 202, body
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    hermes_home = Path(__import__("os").environ["HERMES_HOME"])
    assert observed == [str(tmp_path / "relay-wt" / "relay")]
    assert allocated == []
    assert adapter._wt_broker is None
    assert not (hermes_home / "state" / "loki" / "worktree-leases.jsonl").exists()
    assert not (hermes_home / "relay-wt" / "deliveries").exists()
    assert not (hermes_home / "codex-ports.json").exists()


@pytest.mark.asyncio
async def test_switch_on_two_deliveries_get_distinct_bound_worktrees_and_branches(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBHOOK_WORKTREE", "1")
    monkeypatch.setenv("HERMES_WEBHOOK_PER_DELIVERY_WT", "1")
    adapter = _make_adapter(cap=2)
    leases: list[dict] = []

    def _fake_allocate(route_name: str, delivery_id: str) -> dict:
        short = delivery_id[:8]
        path = tmp_path / "hermes_home" / "relay-wt" / "deliveries" / f"wh-{route_name}-{short}"
        path.mkdir(parents=True)
        lease = {
            "sid": f"wh-{route_name}-{short}",
            "delivery_id": delivery_id,
            "route": route_name,
            "path": str(path),
            "branch": f"loki/{route_name}/{short}",
            "base": "a" * 40,
        }
        leases.append(lease)
        return lease

    adapter._allocate_per_delivery_worktree = _fake_allocate
    observed: list[str | None] = []

    async def _handler(event):
        observed.append(get_active_worktree())

    adapter.handle_message = _handler

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        first = await _post(cli, "aaaaaaaa1111")
        second = await _post(cli, "bbbbbbbb2222")
        assert first.status == 202, await first.json()
        assert second.status == 202, await second.json()
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert len(leases) == 2
    assert leases[0]["path"] != leases[1]["path"]
    assert leases[0]["branch"] == "loki/loki1/aaaaaaaa"
    assert leases[1]["branch"] == "loki/loki1/bbbbbbbb"
    assert observed == [leases[0]["path"], leases[1]["path"]]


@pytest.mark.asyncio
async def test_per_delivery_refusals_roll_back_seen_slot_and_finalizer(monkeypatch):
    monkeypatch.setenv("HERMES_WEBHOOK_WORKTREE", "1")
    monkeypatch.setenv("HERMES_WEBHOOK_PER_DELIVERY_WT", "1")
    adapter = _make_adapter(cap=1)
    adapter._allocate_per_delivery_worktree = lambda route, delivery: (_ for _ in ()).throw(
        DiskPressureError("disk floor")
    )

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        response = await _post(cli, "disk-refusal")
        body = await response.json()

    assert response.status == 503, body
    assert body["error"] == "worktree_unavailable"
    assert adapter._agent_run_semaphore._value == 1
    assert adapter._run_finalizers == {}
    assert "disk-refusal" not in adapter._seen_deliveries

    adapter._allocate_per_delivery_worktree = lambda route, delivery: (_ for _ in ()).throw(
        LeaseCapacityError("full")
    )
    async with TestClient(TestServer(_create_app(adapter))) as cli:
        response = await _post(cli, "capacity-refusal")
        body = await response.json()

    assert response.status == 429, body
    assert response.headers["Retry-After"] == "30"
    assert body["error"] == "worktree_lease_capacity_exhausted"
    assert adapter._agent_run_semaphore._value == 1
    assert adapter._run_finalizers == {}
    assert "capacity-refusal" not in adapter._seen_deliveries


def test_finalize_completes_lease_once_and_writes_required_ledger_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    adapter = _make_adapter(cap=1)
    completed: list[tuple[str, str | None]] = []

    class FakeBroker:
        def complete_lease(self, sid: str, *, base_sha: str | None = None) -> str:
            completed.append((sid, base_sha))
            return "awaiting-harvest"

    adapter._wt_broker = FakeBroker()
    key = "webhook:loki1:lease-finalize"
    lease = {
        "sid": "wh-loki1-lease-fi",
        "delivery_id": "lease-finalize",
        "route": "loki1",
        "path": str(tmp_path / "hermes_home" / "relay-wt" / "deliveries" / "wh-loki1-lease-fi"),
        "branch": "loki/loki1/lease-fi",
        "base": "b" * 40,
    }
    adapter._run_finalizers[key] = lambda: None
    adapter._lease_by_finalizer[key] = lease

    adapter._finalize_run(key)
    adapter._finalize_run(key)

    assert completed == [("wh-loki1-lease-fi", "b" * 40)]
    ledger = tmp_path / "hermes_home" / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["completed", "awaiting-harvest"]
    for row in rows:
        assert row["sid"] == lease["sid"]
        assert row["delivery_id"] == lease["delivery_id"]
        assert row["route"] == lease["route"]
        assert row["path"] == lease["path"]
        assert row["branch"] == lease["branch"]
        assert row["base"] == lease["base"]
