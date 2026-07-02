"""Regression tests for webhook per-delivery sid identity isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent.worktree_broker import BranchCollisionError, WorktreeBroker
from gateway.config import PlatformConfig
from gateway.platforms import webhook as webhook_module
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


class DummyResult:
    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


@pytest.fixture(autouse=True)
def _clean_globals(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setenv("HERMES_WEBHOOK_WORKTREE", "1")
    monkeypatch.setenv("HERMES_WEBHOOK_PER_DELIVERY_WT", "1")
    monkeypatch.delenv("HERMES_WEBHOOK_BASE_BRANCH", raising=False)
    webhook_module._AGENT_RUN_SEMAPHORE = None
    webhook_module._AGENT_RUN_SEMAPHORE_CAP = None
    yield
    webhook_module._AGENT_RUN_SEMAPHORE = None
    webhook_module._AGENT_RUN_SEMAPHORE_CAP = None


def _make_adapter(tmp_path: Path, *, cap: int = 2) -> WebhookAdapter:
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
    adapter._wt_enabled = True
    adapter._per_delivery_wt_enabled = True
    adapter._resolve_worktree_base_sha = lambda: "a" * 40

    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    broker = WorktreeBroker(
        repo_root=repo,
        hermes_home=home,
        wt_dir_name="relay-wt/deliveries",
        branch_prefix="loki",
        ports_enabled=False,
        max_active_leases=cap,
    )
    broker._disk_free_bytes = lambda: 10 * 1024**3

    def fake_git(*args: str) -> DummyResult:
        if args[:2] == ("worktree", "add"):
            Path(args[2]).mkdir(parents=True, exist_ok=True)
        return DummyResult()

    broker._git = fake_git  # type: ignore[method-assign]
    adapter._wt_broker = broker
    return adapter


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _post(cli: TestClient, delivery_id: str):
    return await cli.post(
        "/webhooks/loki1",
        json={"message": "OBJECTIVE: exercise identity-safe per-delivery worktree"},
        headers={"X-Request-ID": delivery_id},
    )


@pytest.mark.asyncio
async def test_same_prefix_distinct_full_ids_get_distinct_worktrees(tmp_path):
    adapter = _make_adapter(tmp_path, cap=2)
    observed: list[str | None] = []

    async def handler(event) -> None:
        from agent.codex_session_context import get_active_worktree

        observed.append(get_active_worktree())

    adapter.handle_message = handler  # type: ignore[method-assign]

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        first = await _post(cli, "deadbeef-FIRST")
        second = await _post(cli, "deadbeef-SECOND")
        first_body = await first.json()
        second_body = await second.json()
        assert first.status == 202, first_body
        assert second.status == 202, second_body
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert len(observed) == 2
    assert observed[0] != observed[1]
    assert adapter._wt_broker is not None
    assert len(adapter._wt_broker._registry) == 0

    ledger = Path(__import__("os").environ["HERMES_HOME"]) / "state/loki/worktree-leases.jsonl"
    leased = [row for row in ledger.read_text().splitlines() if '"event": "leased"' in row]
    assert len(leased) == 2


@pytest.mark.asyncio
async def test_identical_full_delivery_id_reattaches_same_lease_without_new_worktree(tmp_path):
    adapter = _make_adapter(tmp_path, cap=2)
    delivery_id = "retry-identical-full-id"

    first_lease = adapter._allocate_per_delivery_worktree("loki1", delivery_id)
    second_lease = adapter._allocate_per_delivery_worktree("loki1", delivery_id)

    assert second_lease["sid"] == first_lease["sid"]
    assert second_lease["path"] == first_lease["path"]
    assert second_lease["branch"] == first_lease["branch"]
    assert adapter._wt_broker is not None
    assert list(adapter._wt_broker._registry) == [first_lease["sid"]]


@pytest.mark.asyncio
async def test_forced_same_sid_different_identity_refuses_and_rolls_back(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path, cap=1)
    fixed_hash = type(
        "FixedHash",
        (),
        {"hexdigest": lambda self: "cafebabecafe0123456789abcdef"},
    )
    monkeypatch.setattr(webhook_module.hashlib, "sha256", lambda data: fixed_hash())
    seed_lease = adapter._allocate_per_delivery_worktree("loki1", "identity-one")
    assert seed_lease["sid"] == "wh-loki1-cafebabecafe"
    handled: list[str] = []

    async def handler(event) -> None:
        handled.append(event.message_id)

    adapter.handle_message = handler  # type: ignore[method-assign]

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        second = await _post(cli, "identity-two")
        second_body = await second.json()

    assert second.status == 503, second_body
    assert second_body["error"] == "worktree_unavailable"
    assert adapter._agent_run_semaphore._value == 1
    assert "identity-two" not in adapter._seen_deliveries
    assert adapter._run_finalizers == {}
    assert handled == []


def test_broker_default_no_identity_preserves_codex_idempotent_path(tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    broker = WorktreeBroker(repo_root=repo, hermes_home=home)
    broker._disk_free_bytes = lambda: 10 * 1024**3
    git_calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> DummyResult:
        git_calls.append(args)
        if args[:2] == ("worktree", "add"):
            Path(args[2]).mkdir(parents=True, exist_ok=True)
        return DummyResult()

    broker._git = fake_git  # type: ignore[method-assign]

    first = broker.allocate("codex-default", isa_slug="slug")
    second = broker.allocate("codex-default", isa_slug="other-slug")

    assert first is second
    assert first.identity is None
    assert not (home / "state" / "worktree-broker-identities" / "codex-default.json").exists()
    assert [call for call in git_calls if call[:2] == ("worktree", "add")] == [
        ("worktree", "add", str(home / "codex-wt" / "codex-default"), "-b", "codex/codex-default/slug", "origin/main")
    ]


def test_broker_identity_mismatch_raises_existing_collision_error(tmp_path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    broker = WorktreeBroker(repo_root=repo, hermes_home=home, ports_enabled=False)
    broker._disk_free_bytes = lambda: 10 * 1024**3

    def fake_git(*args: str) -> DummyResult:
        if args[:2] == ("worktree", "add"):
            Path(args[2]).mkdir(parents=True, exist_ok=True)
        return DummyResult()

    broker._git = fake_git  # type: ignore[method-assign]

    broker.allocate("same-sid", isa_slug="slug", identity="first-full-delivery")
    assert broker._read_identity(session_id="same-sid") == "first-full-delivery"
    with pytest.raises(BranchCollisionError):
        broker.allocate("same-sid", isa_slug="slug", identity="second-full-delivery")
