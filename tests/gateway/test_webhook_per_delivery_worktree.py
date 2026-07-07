"""Regression tests for F4 per-delivery webhook worktree broker wiring."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent.codex_session_context import get_active_worktree
from agent.worktree_broker import DiskPressureError, LeaseCapacityError, WorktreeBroker
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


class _FakeSessionStore:
    def __init__(self, entries):
        self._entries = entries
        self.loaded = False

    def _ensure_loaded(self):
        self.loaded = True


def _worktree_list_porcelain(entries):
    chunks = []
    for path, branch, head in entries:
        chunks.extend([
            f"worktree {path}",
            f"HEAD {head}",
            f"branch refs/heads/{branch}",
            "",
        ])
    return "\n".join(chunks)


def _completed_process(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_hydration_ignores_stale_no_live_entries_and_frees_capacity_for_fresh_allocation(tmp_path, monkeypatch):
    """RED/GREEN: stale wh-* worktrees with live_entries=[] must not wedge the cap."""
    adapter = _make_adapter(cap=2)
    adapter._session_store = _FakeSessionStore({})
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    root = hermes_home / "relay-wt" / "deliveries"
    root.mkdir(parents=True)
    repo_root.mkdir()
    stale_paths = []
    heads = ["a" * 40, "b" * 40]
    for i, head in enumerate(heads):
        child = root / f"wh-loki1-stale{i}"
        child.mkdir()
        stale_paths.append(child)
    worktree_stdout = _worktree_list_porcelain(
        [(stale_paths[0], "loki/loki1/stale0", heads[0]), (stale_paths[1], "loki/loki1/stale1", heads[1])]
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "list"]:
            return _completed_process(stdout=worktree_stdout)
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "remove"]:
            Path(cmd[5]).rmdir()
            return _completed_process()
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[2] in {str(stale_paths[0]), str(stale_paths[1])}:
            if cmd[3:5] == ["status", "--porcelain"]:
                return _completed_process(stdout="")
            if cmd[3:5] == ["rev-list", "--count"]:
                return _completed_process(stdout="0\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)
    assert existing == {}
    assert not stale_paths[0].exists()
    assert not stale_paths[1].exists()
    ledger = hermes_home / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["removed", "removed"]
    assert {row["reason"] for row in rows} == {"hydrate_no_live_session"}

    broker = WorktreeBroker(
        repo_root=repo_root,
        hermes_home=hermes_home,
        existing_sessions=existing,
        wt_dir_name="relay-wt/deliveries",
        branch_prefix="loki",
        ports_enabled=False,
        max_active_leases=2,
    )
    with (
        patch.object(broker, "_disk_free_bytes", return_value=10 * 1024**3),
        patch.object(broker, "_git", return_value=_completed_process()),
    ):
        fresh = broker.allocate("wh-loki1-fresh", isa_slug="fresh")
    assert fresh.session_id == "wh-loki1-fresh"


def test_hydration_scan_failure_stays_fail_closed_and_refused(tmp_path, monkeypatch):
    adapter = _make_adapter(cap=1)
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    child = hermes_home / "relay-wt" / "deliveries" / "wh-loki1-live"
    child.mkdir(parents=True)
    repo_root.mkdir()
    worktree_stdout = _worktree_list_porcelain([(child, "loki/loki1/live", "a" * 40)])

    monkeypatch.setattr(adapter, "_live_session_entries", lambda: webhook_module._LIVE_SESSION_SCAN_FAILED)
    monkeypatch.setattr(
        webhook_module.subprocess,
        "run",
        lambda cmd, *args, **kwargs: _completed_process(stdout=worktree_stdout),
    )

    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)
    assert existing == {}
    assert child.exists()
    ledger = hermes_home / "state" / "loki" / "worktree-leases.jsonl"
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["event"] == "refused"
    assert row["reason"] == "hydrate_scan_failure"


def test_hydration_retains_dirty_stale_worktree_on_disk_but_not_active_capacity(tmp_path, monkeypatch):
    adapter = _make_adapter(cap=1)
    adapter._session_store = _FakeSessionStore({})
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    child = hermes_home / "relay-wt" / "deliveries" / "wh-loki1-dirty"
    child.mkdir(parents=True)
    repo_root.mkdir()
    worktree_stdout = _worktree_list_porcelain([(child, "loki/loki1/dirty", "a" * 40)])

    def fake_run(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "list"]:
            return _completed_process(stdout=worktree_stdout)
        if len(cmd) >= 5 and cmd[0] == "git" and cmd[2] == str(child) and cmd[3:5] == ["status", "--porcelain"]:
            return _completed_process(stdout=" M touched.py\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)
    assert existing == {}
    assert child.exists()
    assert child.name not in existing
    ledger = hermes_home / "state" / "loki" / "worktree-leases.jsonl"
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["event"] == "awaiting-harvest"
    assert row["reason"] == "hydrate_no_live_session"


def test_hydration_refuses_live_binding_mismatch_without_touching_disk(tmp_path, monkeypatch):
    adapter = _make_adapter(cap=1)
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    child = hermes_home / "relay-wt" / "deliveries" / "wh-loki1-mismatch"
    wrong_child = hermes_home / "relay-wt" / "deliveries" / "wh-loki1-other"
    child.mkdir(parents=True)
    wrong_child.mkdir()
    repo_root.mkdir()
    adapter._session_store = _FakeSessionStore({
        "agent:main:webhook:loki1:delivery": SimpleNamespace(
            updated_at=datetime.now() + timedelta(seconds=1),
            worktree_path=str(wrong_child),
        ),
    })
    worktree_stdout = _worktree_list_porcelain([(child, "loki/loki1/mismatch", "a" * 40)])
    disk_touch_commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "list"]:
            return _completed_process(stdout=worktree_stdout)
        disk_touch_commands.append(cmd)
        raise AssertionError(f"mismatch path must not inspect/remove disk: {cmd}")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)

    assert existing == {}
    assert child.exists()
    assert disk_touch_commands == []
    ledger = hermes_home / "state" / "loki" / "worktree-leases.jsonl"
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["event"] == "refused"
    assert row["reason"] == "hydrate_live_binding_mismatch"


@pytest.mark.parametrize("timeout_stage", ["status", "rev-list", "remove"])
def test_hydration_subprocess_timeout_retains_worktree_without_crash_or_lock_leak(
    tmp_path,
    monkeypatch,
    timeout_stage,
):
    adapter = _make_adapter(cap=1)
    adapter._session_store = _FakeSessionStore({})
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    child = hermes_home / "relay-wt" / "deliveries" / f"wh-loki1-timeout-{timeout_stage}"
    child.mkdir(parents=True)
    repo_root.mkdir()
    worktree_stdout = _worktree_list_porcelain([(child, "loki/loki1/timeout", "a" * 40)])
    remove_attempts = 0

    def fake_run(cmd, *args, **kwargs):
        nonlocal remove_attempts
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "list"]:
            return _completed_process(stdout=worktree_stdout)
        if len(cmd) >= 5 and cmd[0] == "git" and cmd[2] == str(child) and cmd[3:5] == ["status", "--porcelain"]:
            assert kwargs.get("timeout") == 25
            if timeout_stage == "status":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=25)
            return _completed_process(stdout="")
        if len(cmd) >= 5 and cmd[0] == "git" and cmd[2] == str(child) and cmd[3:5] == ["rev-list", "--count"]:
            assert kwargs.get("timeout") == 25
            if timeout_stage == "rev-list":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=25)
            return _completed_process(stdout="0\n")
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "remove"]:
            remove_attempts += 1
            assert kwargs.get("timeout") == 25
            if timeout_stage == "remove":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=25)
            child.rmdir()
            return _completed_process()
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)

    assert existing == {}
    assert child.exists()
    assert remove_attempts == (1 if timeout_stage == "remove" else 0)
    ledger = hermes_home / "state" / "loki" / "worktree-leases.jsonl"
    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["event"] == "awaiting-harvest"
    assert row["reason"] == "hydrate_no_live_session"
    assert adapter._wt_broker_lock.acquire(blocking=False)
    adapter._wt_broker_lock.release()


def test_hydration_dead_pre_start_bound_session_is_stale_completed_not_adopted(tmp_path, monkeypatch):
    process_start = datetime(2026, 7, 6, 12, 0, 0)
    monkeypatch.setattr(webhook_module, "_PROCESS_START", process_start)
    adapter = _make_adapter(cap=1)
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    child = hermes_home / "relay-wt" / "deliveries" / "wh-loki1-dead"
    child.mkdir(parents=True)
    repo_root.mkdir()
    adapter._session_store = _FakeSessionStore({
        "agent:main:webhook:loki1:delivery": SimpleNamespace(
            updated_at=process_start - timedelta(seconds=1),
            worktree_path=str(child),
        ),
    })
    worktree_stdout = _worktree_list_porcelain([(child, "loki/loki1/dead", "a" * 40)])

    def fake_run(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "list"]:
            return _completed_process(stdout=worktree_stdout)
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "remove"]:
            Path(cmd[5]).rmdir()
            return _completed_process()
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[2] == str(child):
            if cmd[3:5] == ["status", "--porcelain"]:
                return _completed_process(stdout="")
            if cmd[3:5] == ["rev-list", "--count"]:
                return _completed_process(stdout="0\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)

    assert existing == {}
    assert not child.exists()
    broker = WorktreeBroker(
        repo_root=repo_root,
        hermes_home=hermes_home,
        existing_sessions=existing,
        wt_dir_name="relay-wt/deliveries",
        branch_prefix="loki",
        ports_enabled=False,
        max_active_leases=1,
    )
    assert broker._registry == {}
    ledger = hermes_home / "state" / "loki" / "worktree-leases.jsonl"
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert row["event"] == "removed"
    assert row["reason"] == "hydrate_no_live_session"


def test_hydration_adopts_matching_post_start_live_session_worktree(tmp_path, monkeypatch):
    process_start = datetime(2026, 7, 6, 12, 0, 0)
    monkeypatch.setattr(webhook_module, "_PROCESS_START", process_start)
    adapter = _make_adapter(cap=1)
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    child = hermes_home / "relay-wt" / "deliveries" / "wh-loki1-live-post-start"
    child.mkdir(parents=True)
    repo_root.mkdir()
    adapter._session_store = _FakeSessionStore({
        "agent:main:webhook:loki1:delivery": SimpleNamespace(
            updated_at=process_start + timedelta(seconds=1),
            worktree_path=str(child),
        ),
    })
    worktree_stdout = _worktree_list_porcelain([(child, "loki/loki1/live-post-start", "a" * 40)])
    monkeypatch.setattr(
        webhook_module.subprocess,
        "run",
        lambda cmd, *args, **kwargs: _completed_process(stdout=worktree_stdout),
    )

    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)

    assert existing == {
        "wh-loki1-live-post-start": {
            "path": str(child),
            "branch": "loki/loki1/live-post-start",
            "base_sha": "a" * 40,
        }
    }
    assert child.exists()


def test_hydration_adopts_only_matching_live_session_worktree(tmp_path, monkeypatch):
    adapter = _make_adapter(cap=1)
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    child = hermes_home / "relay-wt" / "deliveries" / "wh-loki1-live"
    child.mkdir(parents=True)
    repo_root.mkdir()
    adapter._session_store = _FakeSessionStore({
        "agent:main:webhook:loki1:delivery": SimpleNamespace(
            updated_at=datetime.now() + timedelta(seconds=1),
            worktree_path=str(child),
        ),
    })
    worktree_stdout = _worktree_list_porcelain([(child, "loki/loki1/live", "a" * 40)])
    monkeypatch.setattr(
        webhook_module.subprocess,
        "run",
        lambda cmd, *args, **kwargs: _completed_process(stdout=worktree_stdout),
    )

    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)
    assert existing == {
        "wh-loki1-live": {
            "path": str(child),
            "branch": "loki/loki1/live",
            "base_sha": "a" * 40,
        }
    }


def test_hydration_harvests_stale_worktree_when_unrelated_unbound_session_is_live(tmp_path, monkeypatch):
    """Regression (rev-3): a stale wh-* worktree must be stale-completed even when
    the SessionStore is NON-empty, as long as no live session is bound to THIS
    worktree. Rev-2 gated stale-completion on `not live_entries` (store totally
    empty), so any live-but-unbound session (worktree_path=None — the normal
    steady state for interactive/CLI/Telegram sessions) let the stale candidate
    fall through to unconditional adoption, silently re-arming it as a trusted
    lease across restart. This constructs exactly that: one unrelated live entry
    with worktree_path=None + one clean stale worktree bound to nothing live.
    """
    adapter = _make_adapter(cap=2)
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    root = hermes_home / "relay-wt" / "deliveries"
    root.mkdir(parents=True)
    repo_root.mkdir()
    stale = root / "wh-loki1-stale-attacker"
    stale.mkdir()
    # Non-empty store, but the only live session is UNBOUND (no worktree) and
    # unrelated to the stale candidate — so `matching` and `mismatched` are both
    # empty, and rev-2 would have fallen through to adopt.
    adapter._session_store = _FakeSessionStore({
        "agent:main:cli:interactive": SimpleNamespace(
            updated_at=datetime.now() + timedelta(seconds=1),
            worktree_path=None,
        ),
    })
    worktree_stdout = _worktree_list_porcelain([(stale, "loki/loki1/stale-attacker", "a" * 40)])

    def fake_run(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "list"]:
            return _completed_process(stdout=worktree_stdout)
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "remove"]:
            Path(cmd[5]).rmdir()
            return _completed_process()
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[2] == str(stale):
            if cmd[3:5] == ["status", "--porcelain"]:
                return _completed_process(stdout="")
            if cmd[3:5] == ["rev-list", "--count"]:
                return _completed_process(stdout="0\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)

    # NOT adopted — the stale worktree must not become an active broker lease.
    assert existing == {}
    # Clean stale worktree removed from disk.
    assert not stale.exists()
    ledger = hermes_home / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["removed"]
    assert rows[0]["reason"] == "hydrate_no_live_session"


def test_hydration_clock_jump_future_dated_dead_entry_is_not_adopted(tmp_path, monkeypatch):
    """B-hydration-clamp (t_f91b9cc5): a WSL2 clock jump can leave a DEAD
    SessionStore entry future-dated far beyond ``now + skew_tolerance``. The
    original liveness filter (a589fc146) only enforced a lower bound
    (``updated_at >= _PROCESS_START``), so a future-dated entry passed the
    check and was adopted as a trusted lease even though it is not actually
    live. This reproduces that clock-jump scenario: updated_at is hours in
    the future relative to both _PROCESS_START and wall-clock now, which must
    now be treated as NOT live and routed down the same fail-closed
    stale-complete path as a dead/stale entry — disk evidence preserved, not
    adopted.
    """
    process_start = datetime(2026, 7, 6, 12, 0, 0)
    monkeypatch.setattr(webhook_module, "_PROCESS_START", process_start)
    adapter = _make_adapter(cap=1)
    hermes_home = tmp_path / "hermes_home"
    repo_root = tmp_path / "repo"
    child = hermes_home / "relay-wt" / "deliveries" / "wh-loki1-clockjump"
    child.mkdir(parents=True)
    repo_root.mkdir()
    # Clock jumped forward hours beyond both _PROCESS_START and real now —
    # passes the old ">= _PROCESS_START" check but is not actually live.
    future_updated_at = datetime.now() + timedelta(hours=6)
    adapter._session_store = _FakeSessionStore({
        "agent:main:webhook:loki1:delivery": SimpleNamespace(
            updated_at=future_updated_at,
            worktree_path=str(child),
        ),
    })
    worktree_stdout = _worktree_list_porcelain([(child, "loki/loki1/clockjump", "a" * 40)])

    def fake_run(cmd, *args, **kwargs):
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "list"]:
            return _completed_process(stdout=worktree_stdout)
        if cmd[:5] == ["git", "-C", str(repo_root), "worktree", "remove"]:
            Path(cmd[5]).rmdir()
            return _completed_process()
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[2] == str(child):
            if cmd[3:5] == ["status", "--porcelain"]:
                return _completed_process(stdout="")
            if cmd[3:5] == ["rev-list", "--count"]:
                return _completed_process(stdout="0\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(webhook_module.subprocess, "run", fake_run)
    existing = adapter._hydrate_per_delivery_sessions(hermes_home=hermes_home, repo_root=repo_root)

    # NOT adopted — a future-dated (clock-jump) entry must not be treated as live.
    assert existing == {}
    # Fail-closed evidence-preserving path: same as a dead/stale entry, not a
    # parallel one — the worktree is stale-completed (removed from disk) via
    # the existing hydrate_no_live_session path, not left dangling or adopted.
    assert not child.exists()
    ledger = hermes_home / "state" / "loki" / "worktree-leases.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["removed"]
    assert rows[0]["reason"] == "hydrate_no_live_session"
