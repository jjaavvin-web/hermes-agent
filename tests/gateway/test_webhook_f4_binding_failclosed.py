"""F4 RED pins: per-delivery worktree adoption must fail closed."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import webhook as webhook_module
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from tools import approval as approval_module


@pytest.fixture(autouse=True)
def _clean_globals(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setenv("HERMES_WEBHOOK_WORKTREE", "1")
    monkeypatch.setenv("HERMES_WEBHOOK_PER_DELIVERY_WT", "1")
    webhook_module._AGENT_RUN_SEMAPHORE = None
    webhook_module._AGENT_RUN_SEMAPHORE_CAP = None
    approval_module._session_deny_patterns.clear()
    approval_module._session_credential_taint.clear()
    yield
    webhook_module._AGENT_RUN_SEMAPHORE = None
    webhook_module._AGENT_RUN_SEMAPHORE_CAP = None
    approval_module._session_deny_patterns.clear()
    approval_module._session_credential_taint.clear()


def _make_adapter(*, cap: int = 1) -> WebhookAdapter:
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "max_concurrent_agent_runs": cap,
                "routes": {
                    "loki1": {"secret": _INSECURE_NO_AUTH, "prompt": "{message}", "deliver": "log"}
                },
            },
        )
    )
    adapter._wt_enabled = True
    adapter._per_delivery_wt_enabled = True
    return adapter


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _post(cli: TestClient, delivery_id: str):
    return await cli.post(
        "/webhooks/loki1",
        json={"message": "OBJECTIVE: exercise adoption guard"},
        headers={"X-Request-ID": delivery_id},
    )


@pytest.mark.asyncio
async def test_recreated_existing_session_mismatched_worktree_refuses_503_and_never_spawns(tmp_path):
    adapter = _make_adapter(cap=1)
    leased_path = tmp_path / "hermes_home" / "relay-wt" / "deliveries" / "wh-loki1-new"
    leased_path.mkdir(parents=True)
    lease = {
        "sid": "wh-loki1-new",
        "delivery_id": "restart-delivery",
        "route": "loki1",
        "path": str(leased_path),
        "branch": "loki/loki1/new",
        "base": "c" * 40,
    }
    adapter._allocate_per_delivery_worktree = lambda _route, _delivery: lease  # type: ignore[method-assign]

    released: list[str] = []
    completed: list[str] = []

    class FakeBroker:
        def release(self, sid: str) -> None:
            released.append(sid)

        def complete_lease(self, sid: str, *, base_sha: str | None = None) -> str:
            completed.append(sid)
            return "removed"

    adapter._wt_broker = FakeBroker()  # type: ignore[assignment]
    session_key = adapter._build_session_key(
        adapter.build_source(
            chat_id="webhook:loki1:restart-delivery",
            chat_name="webhook/loki1",
            chat_type="webhook",
            user_id="webhook:loki1",
            user_name="loki1",
        )
    )
    adapter.set_session_store(
        SimpleNamespace(
            _entries={session_key: SimpleNamespace(worktree_path=str(tmp_path / "old-or-bare-worktree"))}
        )
    )
    handled: list[str] = []

    async def handler(event) -> None:
        handled.append(event.message_id)

    adapter.handle_message = handler  # type: ignore[method-assign]

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        response = await _post(cli, "restart-delivery")
        body = await response.json()
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    assert response.status == 503, body
    assert body["error"] == "worktree_unavailable"
    assert handled == []
    assert completed == []
    assert released == ["wh-loki1-new"]
    assert "restart-delivery" not in adapter._seen_deliveries
    assert adapter._run_finalizers == {}
    assert adapter._lease_by_finalizer == {}
    assert adapter._agent_run_semaphore._value == 1
    ledger = Path(os.environ["HERMES_HOME"]) / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["refused"]


def test_never_adopted_lease_records_refused_not_clean_completed(tmp_path):
    adapter = _make_adapter(cap=1)
    completed: list[str] = []
    released: list[str] = []

    class FakeBroker:
        def complete_lease(self, sid: str, *, base_sha: str | None = None) -> str:
            completed.append(sid)
            return "removed"

        def release(self, sid: str) -> None:
            released.append(sid)

    adapter._wt_broker = FakeBroker()  # type: ignore[assignment]
    lease = {
        "sid": "wh-loki1-never-adopted",
        "delivery_id": "never-adopted",
        "route": "loki1",
        "path": str(tmp_path / "hermes_home" / "relay-wt" / "deliveries" / "wh-loki1-never-adopted"),
        "branch": "loki/loki1/never-adopted",
        "base": "d" * 40,
    }

    adapter._refuse_worktree_lease(lease, reason="adoption_mismatch")

    assert completed == []
    assert released == ["wh-loki1-never-adopted"]
    ledger = Path(os.environ["HERMES_HOME"]) / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["refused"]
    assert "completed" not in {row["event"] for row in rows}
    assert "removed" not in {row["event"] for row in rows}


def test_hydration_live_session_scan_failure_refuses_every_candidate_without_adoption(tmp_path, monkeypatch):
    home = Path(os.environ["HERMES_HOME"])
    repo = tmp_path / "repo"
    repo.mkdir()
    candidates = [
        home / "relay-wt" / "deliveries" / "wh-loki1-scan-a",
        home / "relay-wt" / "deliveries" / "wh-loki1-scan-b",
    ]
    for candidate in candidates:
        candidate.mkdir(parents=True)

    def fake_run(args, **_kwargs):
        assert args[:4] == ["git", "-C", str(repo), "worktree"]
        stdout = "".join(
            f"worktree {candidate}\nHEAD {'e' * 40}\nbranch refs/heads/loki/loki1/{candidate.name}\n\n"
            for candidate in candidates
        )
        return webhook_module.subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    adapter = _make_adapter(cap=1)

    class RaisingStore:
        def _ensure_loaded(self) -> None:
            raise RuntimeError("session store scan exploded")

    adapter.set_session_store(RaisingStore())

    assert adapter._hydrate_per_delivery_sessions(hermes_home=home, repo_root=repo) == {}
    ledger = home / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["refused", "refused"]
    assert {row["reason"] for row in rows} == {"hydrate_scan_failure"}
    assert {row["sid"] for row in rows} == {candidate.name for candidate in candidates}


def test_hydration_live_session_non_dict_entries_refuses_every_candidate_without_adoption(
    tmp_path, monkeypatch
):
    home = Path(os.environ["HERMES_HOME"])
    repo = tmp_path / "repo"
    repo.mkdir()
    candidates = [
        home / "relay-wt" / "deliveries" / "wh-loki1-corrupt-a",
        home / "relay-wt" / "deliveries" / "wh-loki1-corrupt-b",
    ]
    for candidate in candidates:
        candidate.mkdir(parents=True)

    def fake_run(args, **_kwargs):
        assert args[:4] == ["git", "-C", str(repo), "worktree"]
        stdout = "".join(
            f"worktree {candidate}\nHEAD {'f' * 40}\nbranch refs/heads/loki/loki1/{candidate.name}\n\n"
            for candidate in candidates
        )
        return webhook_module.subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    adapter = _make_adapter(cap=1)

    class CorruptedEntriesStore:
        _entries = "not-a-dict-corrupted"

        def _ensure_loaded(self) -> None:
            return None

    adapter.set_session_store(CorruptedEntriesStore())

    assert adapter._hydrate_per_delivery_sessions(hermes_home=home, repo_root=repo) == {}
    ledger = home / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["refused", "refused"]
    assert {row["reason"] for row in rows} == {"hydrate_scan_failure"}
    assert {row["sid"] for row in rows} == {candidate.name for candidate in candidates}


def test_adoption_lookup_finds_profile_namespaced_live_entry_under_alternate_key(tmp_path):
    adapter = _make_adapter(cap=1)
    source = adapter.build_source(
        chat_id="webhook:loki1:profile-delivery",
        chat_name="webhook/loki1",
        chat_type="webhook",
        user_id="webhook:loki1",
        user_name="loki1",
    )
    dispatch_key = adapter._build_session_key(source)
    namespaced_key = dispatch_key.replace("agent:main:", "agent:brain:", 1)
    assert namespaced_key != dispatch_key
    leased_path = tmp_path / "lease"
    stale_path = tmp_path / "other-profile-bound-tree"
    adapter.set_session_store(
        SimpleNamespace(_entries={namespaced_key: SimpleNamespace(worktree_path=str(stale_path))})
    )

    assert adapter._lookup_live_session_entry(dispatch_key) is not None
    assert adapter._verify_per_delivery_adoption(
        session_key=dispatch_key,
        worktree_path=str(leased_path),
    ) is False
