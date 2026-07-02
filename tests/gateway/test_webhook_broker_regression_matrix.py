"""F4 broker regression matrix pins for webhook per-delivery worktrees."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent.codex_session_context import get_active_worktree
from agent.worktree_broker import RepoStateError, Worktree, WorktreeBroker
from gateway.config import PlatformConfig
from gateway.platforms import webhook as webhook_module
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from tools import approval as approval_module


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
    approval_module._session_deny_patterns.clear()
    approval_module._session_credential_taint.clear()
    yield
    webhook_module._AGENT_RUN_SEMAPHORE = None
    webhook_module._AGENT_RUN_SEMAPHORE_CAP = None
    approval_module._session_deny_patterns.clear()
    approval_module._session_credential_taint.clear()


def _make_adapter(*, cap: int = 2) -> WebhookAdapter:
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
    adapter._resolve_worktree_base_sha = lambda: "f" * 40
    return adapter


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _post(cli: TestClient, delivery_id: str):
    return await cli.post(
        "/webhooks/loki1",
        json={"message": "OBJECTIVE: exercise broker regression matrix"},
        headers={"X-Request-ID": delivery_id},
    )


def _make_broker(tmp_path: Path, *, cap: int = 2) -> WorktreeBroker:
    repo = tmp_path / "repo"
    home = Path(os.environ["HERMES_HOME"])
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
        if args[:2] == ("worktree", "remove"):
            Path(args[-1]).rmdir()
        return DummyResult()

    broker._git = fake_git  # type: ignore[method-assign]
    return broker


@pytest.mark.asyncio
async def test_two_delivery_happy_path_binds_distinct_worktrees_and_branches(tmp_path):
    adapter = _make_adapter(cap=2)
    adapter._wt_broker = _make_broker(tmp_path, cap=2)
    observed: list[str | None] = []

    async def handler(_event) -> None:
        observed.append(get_active_worktree())

    adapter.handle_message = handler  # type: ignore[method-assign]

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        first = await _post(cli, "alpha-delivery")
        second = await _post(cli, "bravo-delivery")
        assert first.status == 202, await first.json()
        assert second.status == 202, await second.json()
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert len(observed) == 2
    assert observed[0] != observed[1]
    assert all(path and "/relay-wt/deliveries/wh-loki1-" in path for path in observed)
    ledger = Path(os.environ["HERMES_HOME"]) / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    leased = [row for row in rows if row["event"] == "leased"]
    assert len(leased) == 2
    assert leased[0]["branch"] != leased[1]["branch"]
    assert leased[0]["path"] != leased[1]["path"]
    assert all(row["base"] == "f" * 40 for row in leased)


@pytest.mark.asyncio
async def test_same_full_delivery_id_concurrent_posts_share_one_lease_without_double_allocate(tmp_path):
    adapter = _make_adapter(cap=2)
    real_broker = _make_broker(tmp_path, cap=2)
    adapter._wt_broker = real_broker
    original_allocate = adapter._allocate_per_delivery_worktree
    entered = 0
    release_allocate = threading.Event()
    allocation_sids: list[str] = []

    def blocking_allocate(route_name: str, delivery_id: str) -> dict:
        nonlocal entered
        entered += 1
        if entered == 1:
            release_allocate.wait(1.0)
        lease = original_allocate(route_name, delivery_id)
        allocation_sids.append(lease["sid"])
        return lease

    adapter._allocate_per_delivery_worktree = blocking_allocate  # type: ignore[method-assign]
    handled: list[str] = []

    async def handler(event) -> None:
        handled.append(event.message_id)

    adapter.handle_message = handler  # type: ignore[method-assign]

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        first_task = asyncio.create_task(_post(cli, "identical-full-delivery"))
        await asyncio.sleep(0.05)
        second_task = asyncio.create_task(_post(cli, "identical-full-delivery"))
        await asyncio.sleep(0.05)
        release_allocate.set()
        first, second = await asyncio.gather(first_task, second_task)
        first_body = await first.json()
        second_body = await second.json()
        statuses = sorted([first.status, second.status])
        assert statuses == [200, 202], (first_body, second_body)
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert handled == ["identical-full-delivery"]
    assert len(set(allocation_sids)) <= 1
    ledger = Path(os.environ["HERMES_HOME"]) / "state" / "loki" / "worktree-leases.jsonl"
    leased = [json.loads(line) for line in ledger.read_text().splitlines() if '"event": "leased"' in line]
    assert len(leased) == 1
    assert list(real_broker._registry) == []
    assert adapter._agent_run_semaphore._value == 2


@pytest.mark.asyncio
async def test_repo_state_error_at_allocate_returns_503_and_rolls_back_slot_seen_and_finalizer():
    adapter = _make_adapter(cap=1)
    adapter._allocate_per_delivery_worktree = lambda _route, _delivery: (_ for _ in ()).throw(
        RepoStateError("dirty parent repo")
    )
    handled: list[str] = []
    adapter.handle_message = lambda event: handled.append(event.message_id)  # type: ignore[method-assign]

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        response = await _post(cli, "dirty-repo")
        body = await response.json()

    assert response.status == 503, body
    assert body["error"] == "worktree_unavailable"
    assert adapter._agent_run_semaphore._value == 1
    assert "dirty-repo" not in adapter._seen_deliveries
    assert adapter._run_finalizers == {}
    assert adapter._lease_by_finalizer == {}
    assert handled == []


def test_resume_hydrates_existing_per_delivery_worktree_and_fail_closes_when_gone(tmp_path, monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    repo = tmp_path / "repo"
    existing = home / "relay-wt" / "deliveries" / "wh-loki1-existing"
    existing.mkdir(parents=True)
    repo.mkdir()

    def fake_run(args, **_kwargs):
        assert args[:4] == ["git", "-C", str(repo), "worktree"]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=f"worktree {existing}\nHEAD {'a' * 40}\nbranch refs/heads/loki/loki1/existing\n\n",
            stderr="",
        )

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    adapter = _make_adapter(cap=1)
    adopted = adapter._hydrate_per_delivery_sessions(hermes_home=home, repo_root=repo)

    assert adopted == {
        "wh-loki1-existing": {
            "path": str(existing),
            "branch": "loki/loki1/existing",
            "base_sha": "a" * 40,
        }
    }

    existing.rmdir()
    assert adapter._hydrate_per_delivery_sessions(hermes_home=home, repo_root=repo) == {}


def test_finalize_once_only_across_success_exception_refusal_duplicate_and_retry(tmp_path):
    adapter = _make_adapter(cap=3)
    completed: list[tuple[str, str | None]] = []

    class FakeBroker:
        def complete_lease(self, sid: str, *, base_sha: str | None = None) -> str:
            completed.append((sid, base_sha))
            if sid == "raises-on-complete":
                raise RuntimeError("complete boom")
            return "removed"

    adapter._wt_broker = FakeBroker()  # type: ignore[assignment]
    adapter._agent_run_semaphore._value = 1
    success = {"sid": "success", "delivery_id": "success", "route": "loki1", "path": "p", "branch": "b", "base": "1" * 40}
    boom = {"sid": "raises-on-complete", "delivery_id": "boom", "route": "loki1", "path": "p", "branch": "b", "base": "2" * 40}

    def register(key: str, lease: dict) -> str:
        source = adapter.build_source(
            chat_id=f"webhook:loki1:{key}",
            chat_name="webhook/loki1",
            chat_type="webhook",
            user_id="webhook:loki1",
            user_name="loki1",
        )
        event = MessageEvent(
            text="",
            message_type=MessageType.TEXT,
            source=source,
            raw_message={},
            message_id=key,
        )
        return adapter._register_run_finalizer(event, None, lease)

    success_key = register("success-key", success)
    boom_key = register("boom-key", boom)

    adapter._finalize_run(success_key)
    adapter._finalize_run(success_key)
    with pytest.raises(RuntimeError):
        adapter._finalize_run(boom_key)
    adapter._finalize_run(boom_key)
    adapter._finalize_run("refusal-after-acquire")
    adapter._finalize_run("duplicate-delivery")
    adapter._finalize_run("processing-complete-retry")

    assert completed == [("success", "1" * 40), ("raises-on-complete", "2" * 40)]
    assert adapter._agent_run_semaphore._value == 3
    assert adapter._agent_run_semaphore._value <= adapter._max_concurrent_agent_runs
    assert adapter._run_finalizers == {}
    assert adapter._lease_by_finalizer == {}


def test_lease_ledger_uses_immutable_40_char_base_sha_and_clean_compare_receives_sha(tmp_path):
    adapter = _make_adapter(cap=1)
    base_sha = "0123456789abcdef0123456789abcdef01234567"
    adapter._resolve_worktree_base_sha = lambda: base_sha
    broker = _make_broker(tmp_path, cap=1)
    adapter._wt_broker = broker

    lease = adapter._allocate_per_delivery_worktree("loki1", "base-pin")
    assert lease["base"] == base_sha
    assert lease["base_ref"] == "fork/main"
    assert len(lease["base"]) == 40
    int(lease["base"], 16)

    seen_compare: list[tuple[Path, str | None]] = []
    broker._worktree_is_clean_for_removal = lambda path, base: seen_compare.append((path, base)) or True  # type: ignore[method-assign]
    adapter._complete_worktree_lease(lease)

    assert seen_compare == [(Path(lease["path"]), base_sha)]
    ledger = Path(os.environ["HERMES_HOME"]) / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert {row["event"] for row in rows} == {"leased", "completed", "removed"}
    assert all(row["base"] == base_sha for row in rows)



def test_switch_off_zero_side_effect_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBHOOK_WORKTREE", "1")
    monkeypatch.delenv("HERMES_WEBHOOK_PER_DELIVERY_WT", raising=False)
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "max_concurrent_agent_runs": 1,
                "routes": {"loki1": {"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "log"}},
            },
        )
    )
    singleton = tmp_path / "relay-wt" / "relay"
    singleton.mkdir(parents=True)
    adapter._ensure_relay_worktree = lambda: str(singleton)  # type: ignore[method-assign]
    adapter._allocate_per_delivery_worktree = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("per-delivery broker must stay off")
    )
    observed: list[str | None] = []

    async def handler(_event) -> None:
        observed.append(get_active_worktree())

    adapter.handle_message = handler  # type: ignore[method-assign]

    async def run() -> None:
        async with TestClient(TestServer(_create_app(adapter))) as cli:
            response = await _post(cli, "switch-off-pin")
            assert response.status == 202, await response.json()
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    asyncio.run(run())

    home = Path(os.environ["HERMES_HOME"])
    assert observed == [str(singleton)]
    assert adapter._wt_broker is None
    assert not (home / "state" / "loki" / "worktree-leases.jsonl").exists()
    assert not (home / "relay-wt" / "deliveries").exists()
    assert not (home / "codex-ports.json").exists()
