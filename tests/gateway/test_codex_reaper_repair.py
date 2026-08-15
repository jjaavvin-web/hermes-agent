"""C7 / Gate 7 — reaper repair contract tests.

Covers the test list in the C7 recon brief §4/§5(4):

* live-mode flip + dry-run override (:class:`gateway.codex_gc_watcher.CodexGcWatcher`)
* the 6-hour idle threshold (replacing the pre-C7 10-*day* window)
* the RELEASED tombstone is written **before** the disk release
* a refused disk release downgrades the row to ORPHANED
* no resurrection through ``on_thread_message`` / ``discover_threads`` /
  ``on_bot_restart`` / ``is_tracked``, for **every** state in
  :data:`gateway.codex_session_dispatcher.TERMINAL_STATES`
* the open-PR lookup fails **closed**
* the process-owner gate
* the unique-commit custody gate
* the non-force release refusal path (broker-side and reaper-side)
* restart idempotence
* archive-first registry GC, including the 90-day boundary

Everything is offline.  ``gh`` is a callable injected into the reaper, ``tmux``
and ``/proc`` are stubbed, and ``git`` is stubbed except in the handful of tests
whose whole subject *is* real git behaviour — those build a throwaway repo plus
a local **bare** remote in ``tmp_path`` (no network) and are marked
``linux_only`` only where they read ``/proc``.  No test touches the live
registry at ``~/.hermes/codex_sessions.json``.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.worktree_broker import WorktreeBroker, WorktreeReleaseRefused
from gateway.codex_gc_watcher import (
    MODE_LIVE,
    MODE_PREVIEW,
    MODE_UNARMED,
    CodexGcWatcher,
)
from gateway.codex_registry_gc import (
    DEFAULT_MAX_TERMINAL_AGE_DAYS,
    DEFAULT_QUARANTINE_STATES,
    CodexRegistryGc,
    archive_path,
    load_archived_thread_ids,
)
from gateway.codex_session_dispatcher import (
    GC_ELIGIBLE_TERMINAL_STATES,
    QUARANTINE_STATES,
    TERMINAL_STATES,
    CodexSessionDispatcher,
    ThreadEvent,
)
from gateway.codex_session_reaper import DEFAULT_REAP_IDLE_HOURS, CodexSessionReaper

# Deterministic ordering for parametrize ids.
_TERMINAL = sorted(TERMINAL_STATES)
_GC_ELIGIBLE = sorted(GC_ELIGIBLE_TERMINAL_STATES)

_FAKE_HEAD = "d" * 40


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #
class _FakeDispatcher:
    """Registry double that round-trips through real JSON, like production.

    Exposes the exact private contract the reaper, the registry GC and the gc
    watcher rely on (``_load_state`` / ``_write_state``) plus ``hermes_home``.
    """

    def __init__(self, hermes_home: Path, rows: dict | None = None) -> None:
        self.hermes_home = Path(hermes_home)
        self._sessions_path = self.hermes_home / "codex_sessions.json"
        self.writes = 0
        self.write_hook = None
        self._sessions_path.write_text(
            json.dumps({"version": 1, "sessions": rows or {}}), encoding="utf-8"
        )

    def _load_state(self) -> dict:
        return json.loads(self._sessions_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        self.writes += 1
        if self.write_hook is not None:
            self.write_hook(state)
        self._sessions_path.write_text(json.dumps(state), encoding="utf-8")

    def rows(self) -> dict:
        return self._load_state()["sessions"]


class _RecordingBroker:
    """Broker double that records registry state *at the moment of release*.

    ``release`` (the force path) raises: the reaper must never reach for it.
    """

    def __init__(self, dispatcher: _FakeDispatcher, *, refuse: Exception | None = None):
        self._dispatcher = dispatcher
        self._refuse = refuse
        self.nonforce_calls: list[str] = []
        self.rows_seen_at_release: dict | None = None

    def release_nonforce(self, session_id: str) -> None:
        self.nonforce_calls.append(session_id)
        self.rows_seen_at_release = copy.deepcopy(self._dispatcher.rows())
        if self._refuse is not None:
            raise self._refuse

    def release(self, session_id: str) -> None:  # pragma: no cover — must not run
        raise AssertionError(
            "reaper called the FORCE release path; it must only ever use "
            "release_nonforce()"
        )


class _GitStub:
    """Stub for :meth:`CodexSessionReaper._git`, dispatching on the git verb.

    ``None`` models a failed probe (the real helper returns None on non-zero
    exit or a subprocess error), which every gate must treat as inconclusive.
    """

    def __init__(
        self,
        *,
        porcelain: str | None = "",
        rev_list: str | None = "",
        head: str | None = _FAKE_HEAD,
    ) -> None:
        self.porcelain = porcelain
        self.rev_list = rev_list
        self.head = head
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, worktree: Path, *args: str) -> str | None:
        self.calls.append(args)
        verb = args[0] if args else ""
        if verb == "status":
            return self.porcelain
        if verb == "rev-list":
            return self.rev_list
        if verb == "rev-parse":
            return None if self.head is None else self.head + "\n"
        raise AssertionError(f"unexpected git call: {args}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _iso(*, hours: float = 0.0, days: float = 0.0, seconds: float = 0.0) -> str:
    """ISO-8601 timestamp that many hours/days/seconds in the past."""
    delta = timedelta(hours=hours, days=days, seconds=seconds)
    return (datetime.now(timezone.utc) - delta).isoformat()


def _row(
    sid: str,
    worktree: Path | str | None,
    *,
    state: str = "EXECUTING",
    last_message_at: str | None = None,
    created_at: str | None = None,
    isa_slug: str = "task",
    **extra,
) -> dict:
    row = {
        "session_id": sid,
        "thread_id": f"t-{sid}",
        "channel_id": "c1",
        "worktree_path": str(worktree) if worktree is not None else "",
        "tmux_session": None,
        "state": state,
        "paused": False,
        "queued_messages": [],
        "isa_slug": isa_slug,
        "last_message_id": None,
        "last_message_at": last_message_at,
        "created_at": created_at or _iso(days=30),
    }
    row.update(extra)
    return row


def _reaper(
    dispatcher,
    broker=None,
    *,
    open_branches: set[str] | Exception | None = None,
    git: _GitStub | None = None,
    owners: dict | None = None,
    owner_probe_ok: bool = True,
    tmux: tuple[str | None, bool] = (None, True),
    ledger: Path | None = None,
) -> CodexSessionReaper:
    """Build a reaper with every external probe stubbed out.

    ``open_branches`` may be a set (successful lookup) or an ``Exception``
    instance (the lookup raises — the fail-closed path).
    """

    def _gh() -> set[str]:
        if isinstance(open_branches, BaseException):
            raise open_branches
        return set() if open_branches is None else set(open_branches)

    reaper = CodexSessionReaper(
        dispatcher_state=dispatcher,
        broker=broker if broker is not None else MagicMock(),
        gh_open_branches_fn=_gh,
    )
    reaper._git = git or _GitStub()
    reaper._scan_process_owners = lambda worktrees: (owners or {}, owner_probe_ok)
    reaper._tmux_owner = lambda row, sid: tmux
    if ledger is not None:
        reaper._ledger_path = ledger
    return reaper


def _git_run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c", "user.email=c7@test.invalid",
            "-c", "user.name=C7 Test",
            "-c", "commit.gpgsign=false",
            "-C", str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Real repo + a **local bare** remote (no network).  ``(repo, wt_root)``."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    _git_run(repo, "add", "-A")
    _git_run(repo, "commit", "-qm", "seed")
    _git_run(repo, "remote", "add", "origin", str(origin))
    _git_run(repo, "push", "-q", "origin", "main")
    _git_run(repo, "fetch", "-q", "origin")
    return repo, tmp_path / "wt-root"


def _add_worktree(repo: Path, wt_root: Path, sid: str) -> Path:
    wt_root.mkdir(parents=True, exist_ok=True)
    path = wt_root / sid
    _git_run(repo, "worktree", "add", "-q", "-b", f"codex/{sid}/task", str(path))
    return path


def _make_dispatcher(tmp_path: Path):
    """Real :class:`CodexSessionDispatcher` over a tmp hermes_home."""
    broker = MagicMock()

    def _alloc(sid, *, isa_slug, base_branch, **_kw):
        wt = MagicMock()
        wt.path = tmp_path / "codex-wt" / sid
        wt.path.mkdir(parents=True, exist_ok=True)
        wt.port = 50000
        wt.branch = f"codex/{sid}/{isa_slug}"
        return wt

    broker.allocate.side_effect = _alloc
    broker.release.return_value = None
    discord_send = AsyncMock()
    dispatcher = CodexSessionDispatcher(
        hermes_home=tmp_path,
        worktree_broker=broker,
        peer_review_orchestrator=MagicMock(),
        merge_broker=MagicMock(),
        discord_send=discord_send,
        kanban_complete=None,
    )
    return dispatcher, broker, discord_send


def _seed(dispatcher: CodexSessionDispatcher, thread_id: str, row: dict) -> None:
    state = dispatcher._load_state()
    state["sessions"][thread_id] = row
    dispatcher._write_state(state)


def _write_archive(hermes_home: Path, *entries: dict) -> Path:
    path = archive_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fd:
        for entry in entries:
            fd.write(json.dumps(entry) + "\n")
    return path


# =========================================================================== #
# 1. live-mode flip + dry-run override
# =========================================================================== #
class TestLiveModeFlip:
    """C7 blocker B1 — the shipped configuration must tear nothing down.

    Pre-C7 the watcher was hardwired to ``reap_idle_days=10, dry_run=True``.
    The first C7 build swung all the way to ``6h, dry_run=False`` as the
    *built-in default*, which measured against the real registry made every
    running session a release candidate on the first tick.  The contract now:
    the shipped default is the pre-C7 window and preview mode, and going live
    takes two explicit flags.
    """

    def test_shipped_defaults_are_conservative(self, tmp_path, monkeypatch):
        for key in (
            "HERMES_CODEX_REAP_IDLE_HOURS",
            "HERMES_CODEX_REAP_DRY_RUN",
            "HERMES_CODEX_REAP_ARMED",
            "HERMES_CODEX_REAP_CONFIRMED",
            "HERMES_CODEX_REGISTRY_GC_ENABLED",
            "HERMES_CODEX_REGISTRY_GC_MAX_AGE_DAYS",
        ):
            monkeypatch.delenv(key, raising=False)
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
        )

        assert w._reap_mode == MODE_UNARMED
        assert w._reap_dry_run is True, "the shipped reaper must not tear down"
        assert w._reap_idle_hours == 240.0, "10 days — the pre-C7 window"
        assert w._reap_idle_hours == DEFAULT_REAP_IDLE_HOURS
        assert w._registry_gc_enabled is True
        assert w._registry_gc_max_age_days == 90

    def test_six_hours_is_reachable_only_by_explicit_configuration(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_CODEX_REAP_IDLE_HOURS", "6")
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
        )

        assert w._reap_idle_hours == 6.0
        # ...and even then it is still not armed.
        assert w._reap_dry_run is True

    @pytest.mark.parametrize(
        ("armed", "confirmed", "mode", "dry_run"),
        [
            (None, None, MODE_UNARMED, True),
            (True, None, MODE_PREVIEW, True),
            (None, True, MODE_UNARMED, True),
            (True, False, MODE_PREVIEW, True),
            (True, True, MODE_LIVE, False),
        ],
    )
    def test_arming_requires_both_flags(
        self, tmp_path, monkeypatch, armed, confirmed, mode, dry_run
    ):
        for key in ("HERMES_CODEX_REAP_ARMED", "HERMES_CODEX_REAP_CONFIRMED",
                    "HERMES_CODEX_REAP_DRY_RUN"):
            monkeypatch.delenv(key, raising=False)
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
            reap_armed=armed,
            reap_confirmed=confirmed,
        )

        assert w._reap_mode == mode
        assert w._reap_dry_run is dry_run

    def test_arming_via_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_CODEX_REAP_ARMED", "1")
        monkeypatch.setenv("HERMES_CODEX_REAP_CONFIRMED", "yes")
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
        )

        assert w._reap_mode == MODE_LIVE
        assert w._reap_dry_run is False

    def test_dry_run_override_can_only_make_it_safer(self, tmp_path, monkeypatch):
        """``reap_dry_run`` forces preview; it can never arm anything."""
        for key in ("HERMES_CODEX_REAP_ARMED", "HERMES_CODEX_REAP_CONFIRMED",
                    "HERMES_CODEX_REAP_DRY_RUN"):
            monkeypatch.delenv(key, raising=False)

        forced = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
            reap_armed=True, reap_confirmed=True, reap_dry_run=True,
        )
        assert forced._reap_mode == MODE_PREVIEW
        assert forced._reap_dry_run is True

        # dry_run=False on its own must NOT arm — this is the exact shape of
        # the mistake B1 describes.
        not_armed = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
            reap_dry_run=False,
        )
        assert not_armed._reap_mode == MODE_UNARMED
        assert not_armed._reap_dry_run is True

    def test_dry_run_env_false_does_not_arm(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_CODEX_REAP_DRY_RUN", "0")
        monkeypatch.delenv("HERMES_CODEX_REAP_ARMED", raising=False)
        monkeypatch.delenv("HERMES_CODEX_REAP_CONFIRMED", raising=False)
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
        )

        assert w._reap_mode == MODE_UNARMED
        assert w._reap_dry_run is True

    def test_idle_hours_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_CODEX_REAP_IDLE_HOURS", "48")
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
        )

        assert w._reap_idle_hours == 48.0

    def test_explicit_arg_beats_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_CODEX_REAP_ARMED", "0")
        monkeypatch.setenv("HERMES_CODEX_REAP_CONFIRMED", "0")
        monkeypatch.setenv("HERMES_CODEX_REAP_IDLE_HOURS", "48")
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
            reap_armed=True,
            reap_confirmed=True,
            reap_idle_hours=6.0,
        )

        assert w._reap_mode == MODE_LIVE
        assert w._reap_idle_hours == 6.0

    @pytest.mark.parametrize("raw", ["banana", ""])
    def test_unparseable_env_falls_back_to_default(self, tmp_path, monkeypatch, raw):
        monkeypatch.setenv("HERMES_CODEX_REAP_IDLE_HOURS", raw)
        monkeypatch.setenv("HERMES_CODEX_REAP_ARMED", raw)
        monkeypatch.setenv("HERMES_CODEX_REAP_CONFIRMED", raw)
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=MagicMock(),
            gh_list_open_branches=lambda: set(),
        )

        assert w._reap_idle_hours == 240.0
        assert w._reap_mode == MODE_UNARMED
        assert w._reap_dry_run is True

    @pytest.mark.asyncio
    async def test_tick_calls_reap_with_configured_window_and_mode(
        self, tmp_path, monkeypatch
    ):
        """The tick must forward the watcher's config, not a hardwired literal."""
        captured: dict = {}

        class _RecordingReaper:
            def __init__(self, **kwargs):
                captured["ctor"] = kwargs

            def reap(self, **kwargs):
                captured["reap"] = kwargs
                return []

        monkeypatch.setattr(
            "gateway.codex_session_reaper.CodexSessionReaper", _RecordingReaper
        )
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
            reap_idle_hours=6.0,
            reap_armed=True,
            reap_confirmed=True,
        )

        await w._tick()

        assert captured["reap"] == {"reap_idle_hours": 6.0, "dry_run": False}
        assert "reap_idle_days" not in captured["reap"]

    @pytest.mark.asyncio
    async def test_tick_honours_dry_run_override(self, tmp_path, monkeypatch):
        captured: dict = {}

        class _RecordingReaper:
            def __init__(self, **kwargs):
                pass

            def reap(self, **kwargs):
                captured.update(kwargs)
                return []

        monkeypatch.setattr(
            "gateway.codex_session_reaper.CodexSessionReaper", _RecordingReaper
        )
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
            reap_dry_run=True,
            reap_idle_hours=72.0,
        )

        await w._tick()

        assert captured == {"reap_idle_hours": 72.0, "dry_run": True}

    @pytest.mark.asyncio
    async def test_armed_and_confirmed_tick_mutates_the_registry(
        self, tmp_path, monkeypatch
    ):
        """End-to-end proof of the flip: a fully armed tick changes a row.

        Pre-C7 the reaper was ``dry_run=True`` forever, so no tick could ever
        move a row.  Here the row's worktree is gone, so unique-commit custody
        is unprovable and the row must be quarantined (never released).
        """
        monkeypatch.setattr(
            CodexSessionReaper, "_scan_process_owners", lambda self, wts: ({}, True)
        )
        monkeypatch.setattr(
            CodexSessionReaper, "_tmux_owner", lambda self, row, sid: (None, True)
        )
        rows = {"t-a": _row("a", tmp_path / "gone", last_message_at=_iso(hours=99))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=disp,
            worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
            reap_idle_hours=6.0,
            reap_armed=True,
            reap_confirmed=True,
        )

        await w._tick()

        row = disp.rows()["t-a"]
        assert row["state"] == "ORPHANED"
        assert "custody" in row["orphaned_reason"]
        broker.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_same_tick_unarmed_mutates_nothing(self, tmp_path, monkeypatch):
        """The B1 contract, stated as a diff against the test above."""
        monkeypatch.setattr(
            CodexSessionReaper, "_scan_process_owners", lambda self, wts: ({}, True)
        )
        monkeypatch.setattr(
            CodexSessionReaper, "_tmux_owner", lambda self, row, sid: (None, True)
        )
        rows = {"t-a": _row("a", tmp_path / "gone", last_message_at=_iso(hours=99))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=disp,
            worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
            reap_idle_hours=6.0,
        )

        await w._tick()

        assert disp.rows()["t-a"]["state"] == "EXECUTING"
        assert disp.writes == 0
        broker.release.assert_not_called()
        broker.release_nonforce.assert_not_called()

    @pytest.mark.asyncio
    async def test_armed_but_unconfirmed_previews_to_the_ledger(
        self, tmp_path, monkeypatch
    ):
        """B1(c): the first armed run proposes, it does not act."""
        monkeypatch.setattr(
            CodexSessionReaper, "_scan_process_owners", lambda self, wts: ({}, True)
        )
        monkeypatch.setattr(
            CodexSessionReaper, "_tmux_owner", lambda self, row, sid: (None, True)
        )
        rows = {"t-a": _row("a", tmp_path / "gone", last_message_at=_iso(hours=99))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=disp,
            worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
            reap_idle_hours=6.0,
            reap_armed=True,
        )

        await w._tick()

        assert w._reap_mode == MODE_PREVIEW
        assert disp.rows()["t-a"]["state"] == "EXECUTING", "preview must not mutate"
        assert disp.writes == 0
        broker.release_nonforce.assert_not_called()

        ledger = tmp_path / "state" / "codex-reaper" / "reap-ledger.jsonl"
        entries = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        previews = [e for e in entries if e.get("kind") == "reap_preview"]
        assert len(previews) == 1
        preview = previews[0]
        assert preview["mode"] == MODE_PREVIEW
        assert preview["armed"] is True
        assert preview["confirmed"] is False
        assert [p["session_id"] for p in preview["proposals"]] == ["a"]
        assert preview["proposals"][0]["outcome"] == "orphaned"
        assert "HERMES_CODEX_REAP_CONFIRMED" in preview["note"]

    @pytest.mark.asyncio
    async def test_no_preview_record_when_unarmed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CodexSessionReaper, "_scan_process_owners", lambda self, wts: ({}, True)
        )
        rows = {"t-a": _row("a", tmp_path / "gone", last_message_at=_iso(hours=99))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=disp, worktree_broker=broker,
            gh_list_open_branches=lambda: set(), reap_idle_hours=6.0,
        )

        await w._tick()

        ledger = tmp_path / "state" / "codex-reaper" / "reap-ledger.jsonl"
        entries = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [e for e in entries if e.get("kind") == "reap_preview"] == []

    @pytest.mark.asyncio
    async def test_registry_gc_is_wired_into_the_tick(self, tmp_path, monkeypatch):
        captured: dict = {}

        class _RecordingGc:
            def __init__(self, dispatcher, **kwargs):
                captured["ctor"] = kwargs

            def collect(self, **kwargs):
                captured["collect"] = kwargs
                return []

        monkeypatch.setattr("gateway.codex_registry_gc.CodexRegistryGc", _RecordingGc)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
        )

        await w._tick()

        assert captured["ctor"]["max_terminal_age_days"] == 90
        assert set(captured["collect"]["terminal_states"]) == set(
            GC_ELIGIBLE_TERMINAL_STATES
        )
        assert "ORPHANED" not in captured["collect"]["terminal_states"], (
            "B3: quarantine must never be handed to the collector"
        )
        assert set(captured["collect"]["quarantine_states"]) == set(QUARANTINE_STATES)

    @pytest.mark.asyncio
    async def test_registry_gc_can_be_disabled(self, tmp_path, monkeypatch):
        called = []

        class _Boom:
            def __init__(self, *a, **kw):
                called.append(True)

        monkeypatch.setattr("gateway.codex_registry_gc.CodexRegistryGc", _Boom)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path),
            worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
            registry_gc_enabled=False,
        )

        await w._tick()

        assert called == []

    @pytest.mark.asyncio
    async def test_released_tombstone_is_untracked_but_orphaned_stays_tracked(
        self, tmp_path, monkeypatch
    ):
        """gc sweeps a RELEASED row's leftover dir; quarantine is preserved."""
        monkeypatch.setattr(
            CodexSessionReaper, "_scan_process_owners", lambda self, wts: ({}, True)
        )
        rows = {
            "t-rel": _row("sid-rel", tmp_path / "rel", state="RELEASED"),
            "t-orph": _row("sid-orph", tmp_path / "orph", state="ORPHANED"),
            "t-live": _row("sid-live", tmp_path / "live", last_message_at=_iso(hours=1)),
        }
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=_FakeDispatcher(tmp_path, rows),
            worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
        )

        await w._tick()

        assert broker.gc.call_args.kwargs["tracked_sids"] == {"sid-orph", "sid-live"}


# =========================================================================== #
# 2. the 6h idle threshold
# =========================================================================== #
class TestIdleThreshold:
    def test_fires_exactly_at_six_hours(self, tmp_path):
        reaper = _reaper(_FakeDispatcher(tmp_path))
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = {"last_message_at": (now - timedelta(hours=6)).isoformat()}

        reason, block = reaper._idle_reason(row, now, 6.0)

        assert block is None
        assert reason is not None
        assert "6.0h >= 6.0h" in reason

    def test_does_not_fire_one_second_below_six_hours(self, tmp_path):
        reaper = _reaper(_FakeDispatcher(tmp_path))
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = {"last_message_at": (now - timedelta(hours=6, seconds=-1)).isoformat()}

        assert reaper._idle_reason(row, now, 6.0) == (None, None)

    def test_fires_one_second_above_six_hours(self, tmp_path):
        reaper = _reaper(_FakeDispatcher(tmp_path))
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = {"last_message_at": (now - timedelta(hours=6, seconds=1)).isoformat()}

        assert reaper._idle_reason(row, now, 6.0)[0] is not None

    def test_created_at_covers_a_row_that_never_received_a_message(self, tmp_path):
        reaper = _reaper(_FakeDispatcher(tmp_path))
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = {"last_message_at": None, "created_at": (now - timedelta(hours=7)).isoformat()}

        reason, block = reaper._idle_reason(row, now, 6.0)

        assert block is None
        assert reason is not None
        assert "no message ever received" in reason

    def test_recent_last_message_wins_over_ancient_created_at(self, tmp_path):
        """A chatty row is NOT idle — custody, not the clock, decides safety."""
        reaper = _reaper(_FakeDispatcher(tmp_path))
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        row = {
            "last_message_at": (now - timedelta(minutes=5)).isoformat(),
            "created_at": (now - timedelta(days=400)).isoformat(),
        }

        assert reaper._idle_reason(row, now, 6.0) == (None, None)

    def test_no_timestamp_at_all_is_never_idle(self, tmp_path):
        reaper = _reaper(_FakeDispatcher(tmp_path))
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

        assert reaper._idle_reason({}, now, 6.0) == (None, None)
        assert reaper._idle_reason(
            {"last_message_at": None, "created_at": ""}, now, 6.0
        ) == (None, None)


# =========================================================================== #
# 2b. HIGH-2 — an unparseable timestamp fails CLOSED
# =========================================================================== #
class TestMalformedTimestampFailsClosed:
    """A present-but-unreadable ``last_message_at`` must never authorise a reap.

    The pre-fix ``_idle_reason`` could not tell "the field is missing" from
    "the field is corrupt": both parsed to ``None`` and both fell through to
    ``created_at``.  For a chatty session created months ago that fallback
    reports a huge idle age — so one corrupt character in a timestamp turned a
    live session into a release candidate.
    """

    def test_missing_and_malformed_are_distinguished(self, tmp_path):
        reaper = _reaper(_FakeDispatcher(tmp_path))
        now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        ancient = (now - timedelta(days=400)).isoformat()

        missing = reaper._idle_reason(
            {"last_message_at": None, "created_at": ancient}, now, 6.0
        )
        malformed = reaper._idle_reason(
            {"last_message_at": "2026-08-14T99:99:99", "created_at": ancient}, now, 6.0
        )

        # Missing: legitimately falls back to created_at and IS idle.
        assert missing[0] is not None and missing[1] is None
        # Malformed: blocked, never idle, and it never reached created_at.
        assert malformed[0] is None
        assert "unparseable" in malformed[1]

    @pytest.mark.parametrize(
        "bad", ["not-a-date", "2026-13-45T00:00:00", "yesterday", "0", 1723600000],
    )
    def test_a_chatty_row_with_a_corrupt_timestamp_is_never_released(
        self, tmp_path, bad
    ):
        """The full-reap version: no mutation, no broker call, clear reason."""
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {
            "t-a": _row("a", wt, last_message_at=bad, created_at=_iso(days=400)),
        }
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert [d["outcome"] for d in out] == ["skipped"]
        assert "unparseable" in out[0]["reason"]
        assert broker.nonforce_calls == []
        assert disp.rows()["t-a"]["state"] == "EXECUTING"
        assert disp.writes == 0

    def test_a_malformed_created_at_on_a_silent_row_also_fails_closed(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=None, created_at="garbage")}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "skipped"
        assert "created_at" in out[0]["reason"]
        assert broker.nonforce_calls == []

    def test_the_block_reason_is_recorded_in_the_ledger(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at="nope", created_at=_iso(days=400))}
        disp = _FakeDispatcher(tmp_path, rows)
        ledger = tmp_path / "l.jsonl"

        _reaper(disp, ledger=ledger).reap(reap_idle_hours=6.0, dry_run=True)

        entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert entry["probes"]["idle_block"]
        assert "unparseable" in entry["probes"]["idle_block"]


# =========================================================================== #
# 2c. LOW-1 — a row with no session_id is refused outright
# =========================================================================== #
class TestSidLessRowsAreRefused:
    """Without a sid every downstream guard is silently disarmed.

    ``_branch_for`` returns ``""`` so the open-PR guard can never match; the
    process-owner scan keys on sid so ``owners`` is always empty; and
    ``release_nonforce("")`` resolves to ``<wt_root>/`` — the codex-wt ROOT,
    not one session's directory.
    """

    @pytest.mark.parametrize("sid", ["", None])
    def test_a_sid_less_row_is_skipped_with_an_explicit_reason(self, tmp_path, sid):
        wt = tmp_path / "wt"
        wt.mkdir()
        row = _row("placeholder", wt, last_message_at=_iso(hours=99))
        row["session_id"] = sid
        disp = _FakeDispatcher(tmp_path, {"t-a": row})
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert [d["outcome"] for d in out] == ["skipped"]
        assert "no session_id" in out[0]["reason"]
        assert out[0]["probes"]["session_id_present"] is False
        assert broker.nonforce_calls == [], "release_nonforce('') targets the wt ROOT"
        assert disp.rows()["t-a"]["state"] == "EXECUTING"
        assert disp.writes == 0

    def test_the_refusal_happens_before_any_probe_runs(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        row = _row("", wt, last_message_at=_iso(hours=99))
        row["session_id"] = ""
        disp = _FakeDispatcher(tmp_path, {"t-a": row})
        git = _GitStub()

        _reaper(disp, git=git, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=True
        )

        assert git.calls == [], "no git probe should run for a row we refuse"

    def test_the_broker_refuses_an_empty_sid_too(self, tmp_path):
        """Defence in depth at the other end: ``self._wt_root / ""`` is the ROOT.

        The reaper never gets here now, but any other caller with a blank sid
        would have aimed a "release one session" call at every session at once.
        """
        repo, _wt_root = _make_repo_with_remote(tmp_path)
        home = tmp_path / "hermes"
        home.mkdir()
        broker = WorktreeBroker(repo_root=repo, hermes_home=home)

        with pytest.raises(WorktreeReleaseRefused, match="requires a session_id"):
            broker.release_nonforce("")

    def test_a_sid_less_row_does_not_block_its_neighbours(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        bad = _row("x", wt, last_message_at=_iso(hours=99))
        bad["session_id"] = ""
        rows = {
            "t-bad": bad,
            "t-good": _row("good", wt, last_message_at=_iso(hours=99)),
        }
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        by_thread = {d["thread_id"]: d["outcome"] for d in out}
        assert by_thread == {"t-bad": "skipped", "t-good": "released"}
        assert broker.nonforce_calls == ["good"]

    def test_reap_skips_a_row_idle_for_five_hours(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=5))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert [d["outcome"] for d in out] == ["skipped"]
        assert broker.nonforce_calls == []
        assert disp.rows()["t-a"]["state"] == "EXECUTING"

    def test_reap_releases_a_row_idle_for_seven_hours(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=7))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert [d["outcome"] for d in out] == ["released"]
        assert broker.nonforce_calls == ["a"]

    def test_positional_idle_argument_is_rejected(self, tmp_path):
        """The pre-C7 signature took days positionally; ``10`` must not
        silently become 10 *hours*."""
        reaper = _reaper(_FakeDispatcher(tmp_path))

        with pytest.raises(TypeError):
            reaper.reap(10, dry_run=True)  # type: ignore[misc]

    def test_reap_idle_days_alias_still_converts(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=30))}
        disp = _FakeDispatcher(tmp_path, rows)

        # 30h idle: released under a 1-day window, skipped under a 10-day one.
        out_1d = _reaper(disp, ledger=tmp_path / "a.jsonl").reap(
            reap_idle_days=1, dry_run=True
        )
        out_10d = _reaper(disp, ledger=tmp_path / "b.jsonl").reap(
            reap_idle_days=10, dry_run=True
        )

        assert out_1d[0]["outcome"] == "released"
        assert out_10d[0]["outcome"] == "skipped"


# =========================================================================== #
# 3. tombstone written BEFORE the disk release
# =========================================================================== #
class TestTombstoneOrdering:
    def _release_setup(self, tmp_path, *, refuse: Exception | None = None):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9), isa_slug="feat")}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp, refuse=refuse)
        reaper = _reaper(disp, broker, ledger=tmp_path / "ledger.jsonl")
        return disp, broker, reaper

    def test_row_is_already_released_when_the_broker_is_called(self, tmp_path):
        disp, broker, reaper = self._release_setup(tmp_path)

        out = reaper.reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "released"
        assert broker.nonforce_calls == ["a"]
        seen = broker.rows_seen_at_release["t-a"]
        assert seen["state"] == "RELEASED", (
            "the tombstone must be durable BEFORE the disk is touched"
        )
        assert seen["released_at"]
        assert seen["release_receipt"]["head"] == _FAKE_HEAD
        assert seen["release_receipt"]["branch"] == "codex/a/feat"
        assert seen["release_receipt"]["branch_only_commits"] == []
        assert seen["release_receipt"]["worktree_path"] == str(tmp_path / "wt")
        assert "probes" in seen["release_receipt"]

    def test_row_is_retained_not_popped(self, tmp_path):
        """Pre-C7 the released path did ``sessions.pop`` — that is the bug."""
        disp, _broker, reaper = self._release_setup(tmp_path)

        reaper.reap(reap_idle_hours=6.0, dry_run=False)

        assert "t-a" in disp.rows(), "the tombstone must survive as a row"
        assert disp.rows()["t-a"]["state"] == "RELEASED"

    def test_dry_run_writes_nothing_but_still_ledgers(self, tmp_path):
        disp, broker, reaper = self._release_setup(tmp_path)

        out = reaper.reap(reap_idle_hours=6.0, dry_run=True)

        assert out[0]["outcome"] == "released"
        assert out[0]["dry_run"] is True
        assert broker.nonforce_calls == []
        assert disp.rows()["t-a"]["state"] == "EXECUTING"
        assert disp.writes == 0
        ledgered = json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip())
        assert ledgered["outcome"] == "released"
        assert ledgered["dry_run"] is True

    def test_omitting_dry_run_is_a_preview(self, tmp_path):
        """B1: a caller that never mentions ``dry_run`` must get a PREVIEW.

        ``reap()``'s ``dry_run=True`` default was a surviving mutant.  Every one
        of the ~90 ``reap(...)`` call sites in the suite passes ``dry_run=``
        explicitly, and the one test that looks like it covers the default
        (``TestSixLiveSessionsSurviveShippedDefaults``) actually reads the
        *watcher's* attribute and hands it in — certifying
        ``CodexGcWatcher._reap_dry_run``, never this signature.  Flipping the
        default to ``False`` therefore left all 203 tests green.

        Behavioural on purpose: this also kills a guard inversion at the
        ``if not dry_run:`` branch, which a signature check alone cannot see.
        Non-vacuity control is
        ``test_row_is_already_released_when_the_broker_is_called`` above — same
        ``_release_setup``, explicit ``dry_run=False``, and that row IS torn
        down, so this test cannot be passing on an unreapable row.

        ``_release_setup`` pins ``ledger=tmp_path``; keep it.  ``dry_run=True``
        still appends a ledger row, so a variant without it writes the LIVE
        ledger.
        """
        disp, broker, reaper = self._release_setup(tmp_path)

        out = reaper.reap(reap_idle_hours=6.0)  # <- the whole point: no dry_run

        assert out[0]["outcome"] == "released", "the row WOULD have been released"
        assert out[0]["dry_run"] is True, "...but the default must keep it on paper"
        assert broker.nonforce_calls == []
        assert disp.writes == 0
        assert disp.rows()["t-a"]["state"] == "EXECUTING"

    def test_the_declared_dry_run_default_is_pinned(self):
        """Companion localiser: names the mutation when the test above trips.

        Diagnostic only, never the sole assertion — a declaration check is blind
        to a guard inversion that leaves the signature intact.
        """
        assert (
            inspect.signature(CodexSessionReaper.reap).parameters["dry_run"].default
            is True
        )

    def test_vanished_row_downgrades_to_skipped_without_touching_disk(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)
        reaper = _reaper(disp, broker, ledger=tmp_path / "l.jsonl")
        decision = {"outcome": "released", "session_id": "a", "reason": "x"}

        reaper._apply(decision, "t-missing")

        assert decision["outcome"] == "skipped"
        assert broker.nonforce_calls == []


# =========================================================================== #
# 4. release refusal => ORPHANED downgrade
# =========================================================================== #
class TestReleaseRefusalDowngrade:
    def test_refused_release_orphans_the_row_with_the_reason(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(
            disp, refuse=WorktreeReleaseRefused("git worktree remove refused: locked")
        )

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "orphaned"
        assert "locked" in out[0]["release_error"]
        row = disp.rows()["t-a"]
        assert row["state"] == "ORPHANED"
        assert "locked" in row["orphaned_reason"]
        assert row["orphaned_at"]

    def test_broker_without_release_nonforce_orphans_and_never_forces(self, tmp_path):
        """A deployment shipping an older broker must quarantine, not escalate."""

        class _LegacyBroker:
            def __init__(self):
                self.force_calls = []

            def release(self, sid):
                self.force_calls.append(sid)

        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _LegacyBroker()

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "orphaned"
        assert broker.force_calls == []
        assert "release_nonforce" in disp.rows()["t-a"]["orphaned_reason"]

    def test_orphaned_row_is_never_reconsidered_on_the_next_tick(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp, refuse=WorktreeReleaseRefused("nope"))

        _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )
        second = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert second == []
        assert broker.nonforce_calls == ["a"]


# =========================================================================== #
# 5. no resurrection — every TERMINAL_STATE
# =========================================================================== #
class TestNoResurrection:
    def test_terminal_states_vocabulary(self):
        assert TERMINAL_STATES == frozenset(
            {"RELEASED", "COMPLETE", "DONE", "MERGED", "ESCALATED", "ORPHANED"}
        )

    @pytest.mark.parametrize("state", _TERMINAL)
    def test_is_tracked_is_false_for_terminal_rows(self, tmp_path, state):
        d, _broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-x", _row("sid-x", tmp_path / "wt", state=state))

        assert d.is_tracked("t-x") is False

    def test_is_tracked_is_true_for_a_live_row(self, tmp_path):
        d, _broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-x", _row("sid-x", tmp_path / "wt", state="EXECUTING"))

        assert d.is_tracked("t-x") is True

    def test_is_tracked_is_false_for_an_unknown_thread(self, tmp_path):
        d, _broker, _send = _make_dispatcher(tmp_path)

        assert d.is_tracked("nope") is False

    @pytest.mark.parametrize("state", _TERMINAL)
    @pytest.mark.asyncio
    async def test_on_thread_message_refuses_to_resurrect(self, tmp_path, state):
        d, _broker, send = _make_dispatcher(tmp_path)
        _seed(d, "t-x", _row("sid-x", tmp_path / "wt", state=state))

        await d.on_thread_message(
            ThreadEvent(thread_id="t-x", message_id="m1", text="carry on")
        )

        row = d._load_state()["sessions"]["t-x"]
        assert row["state"] == state, "a tombstone must not flip to EXECUTING"
        assert row["last_message_at"] is None, "the idle clock must not be refreshed"
        assert row["queued_messages"] == [], "a tombstone must not accrue work"
        assert row["last_message_id"] == "m1", "the message must still be deduped"
        send.assert_awaited_once()
        assert state in send.await_args.args[1]

    @pytest.mark.asyncio
    async def test_on_thread_message_still_advances_a_live_row(self, tmp_path):
        d, _broker, send = _make_dispatcher(tmp_path)
        _seed(d, "t-x", _row("sid-x", tmp_path / "wt", state="CLAIMED"))

        await d.on_thread_message(ThreadEvent(thread_id="t-x", message_id="m1", text="go"))

        row = d._load_state()["sessions"]["t-x"]
        assert row["state"] == "EXECUTING"
        assert row["last_message_at"] is not None
        send.assert_not_awaited()

    @pytest.mark.parametrize("state", _TERMINAL)
    @pytest.mark.asyncio
    async def test_discover_threads_refuses_a_tombstoned_thread(self, tmp_path, state):
        d, broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-x", _row("sid-x", tmp_path / "wt", state=state))

        results = await d.discover_threads([("t-x", "c1", "Old Thread")])

        assert results == []
        broker.allocate.assert_not_called()
        row = d._load_state()["sessions"]["t-x"]
        assert row["state"] == state
        assert row["session_id"] == "sid-x", "the row must not be replaced"

    @pytest.mark.asyncio
    async def test_discover_threads_refuses_an_archived_thread(self, tmp_path):
        """The archive outlives the row — a retired thread stays retired."""
        d, broker, _send = _make_dispatcher(tmp_path)
        _write_archive(tmp_path, {"thread_id": "t-gone", "session_id": "sid-gone"})

        results = await d.discover_threads([("t-gone", "c1", "Retired")])

        assert results == []
        broker.allocate.assert_not_called()
        assert "t-gone" not in d._load_state()["sessions"]

    @pytest.mark.asyncio
    async def test_discover_threads_still_adopts_a_genuinely_new_thread(self, tmp_path):
        d, broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-dead", _row("sid-dead", tmp_path / "wt", state="RELEASED"))
        _write_archive(tmp_path, {"thread_id": "t-archived"})

        results = await d.discover_threads([
            ("t-dead", "c1", "Released"),
            ("t-archived", "c1", "Archived"),
            ("t-new", "c1", "Fresh Work"),
        ])

        assert [r.thread_id for r in results] == ["t-new"]
        assert broker.allocate.call_count == 1

    @pytest.mark.parametrize("state", _TERMINAL)
    @pytest.mark.asyncio
    async def test_on_bot_restart_leaves_terminal_rows_alone(self, tmp_path, state):
        d, _broker, _send = _make_dispatcher(tmp_path)
        # Worktree deliberately missing: pre-C7 that re-stamped the row ORPHANED.
        _seed(d, "t-x", _row("sid-x", tmp_path / "gone", state=state))

        results = await d.on_bot_restart()

        assert results == []
        assert d._load_state()["sessions"]["t-x"]["state"] == state

    @pytest.mark.asyncio
    async def test_on_bot_restart_leaves_in_flight_merging_alone(self, tmp_path):
        d, _broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-m", _row("sid-m", tmp_path / "gone", state="MERGING"))

        results = await d.on_bot_restart()

        assert results == []
        assert d._load_state()["sessions"]["t-m"]["state"] == "MERGING"

    @pytest.mark.asyncio
    async def test_on_bot_restart_still_orphans_a_live_row_with_no_worktree(self, tmp_path):
        d, _broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-live", _row("sid-live", tmp_path / "gone", state="EXECUTING"))

        results = await d.on_bot_restart()

        assert [(r.sid, r.status) for r in results] == [("sid-live", "orphaned")]
        assert d._load_state()["sessions"]["t-live"]["state"] == "ORPHANED"

    def test_reaper_never_considers_a_terminal_row(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {
            f"t-{state}": _row(state, wt, state=state, last_message_at=_iso(days=400))
            for state in _TERMINAL
        }
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out == []
        assert broker.nonforce_calls == []
        assert disp.writes == 0
        assert {r["state"] for r in disp.rows().values()} == set(_TERMINAL)


# =========================================================================== #
# 6. open-PR lookup fails CLOSED
# =========================================================================== #
class TestOpenPrFailClosed:
    def _rows(self, tmp_path):
        wt_a = tmp_path / "wt-a"
        wt_a.mkdir()
        wt_b = tmp_path / "wt-b"
        wt_b.mkdir()
        return {
            "t-a": _row("a", wt_a, last_message_at=_iso(hours=9)),
            "t-b": _row("b", wt_b, last_message_at=_iso(hours=9)),
        }

    def test_lookup_raising_skips_every_release_this_tick(self, tmp_path):
        disp = _FakeDispatcher(tmp_path, self._rows(tmp_path))
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker,
            open_branches=RuntimeError("gh: connection timed out"),
            ledger=tmp_path / "l.jsonl",
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert [d["outcome"] for d in out] == ["skipped", "skipped"]
        assert all("fail-closed" in d["reason"] for d in out)
        assert all(d["probes"]["pr_lookup_ok"] is False for d in out)
        assert broker.nonforce_calls == []
        assert disp.writes == 0
        assert {r["state"] for r in disp.rows().values()} == {"EXECUTING"}

    def test_lookup_returning_none_fails_closed(self, tmp_path):
        disp = _FakeDispatcher(tmp_path, self._rows(tmp_path))
        broker = _RecordingBroker(disp)
        reaper = CodexSessionReaper(disp, broker, lambda: None)
        reaper._git = _GitStub()
        reaper._scan_process_owners = lambda w: ({}, True)
        reaper._tmux_owner = lambda row, sid: (None, True)
        reaper._ledger_path = tmp_path / "l.jsonl"

        out = reaper.reap(reap_idle_hours=6.0, dry_run=False)

        assert {d["outcome"] for d in out} == {"skipped"}
        assert broker.nonforce_calls == []

    def test_open_branches_reports_ok_on_success_and_coerces_iterables(self, tmp_path):
        disp = _FakeDispatcher(tmp_path)

        as_set = CodexSessionReaper(disp, MagicMock(), lambda: {"codex/a/x"})
        as_list = CodexSessionReaper(disp, MagicMock(), lambda: ["codex/a/x", "codex/b/y"])

        assert as_set._open_branches() == ({"codex/a/x"}, True)
        assert as_list._open_branches() == ({"codex/a/x", "codex/b/y"}, True)

    def test_open_branches_reports_not_ok_on_failure(self, tmp_path):
        disp = _FakeDispatcher(tmp_path)

        def boom():
            raise RuntimeError("gh unavailable")

        assert CodexSessionReaper(disp, MagicMock(), boom)._open_branches() == (set(), False)
        assert CodexSessionReaper(disp, MagicMock(), lambda: None)._open_branches() == (
            set(), False,
        )

    def test_successful_lookup_showing_an_open_pr_quarantines(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9), isa_slug="feat")}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker, open_branches={"codex/a/feat"}, ledger=tmp_path / "l.jsonl"
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "orphaned"
        assert out[0]["in_open_pr"] is True
        assert broker.nonforce_calls == []
        assert disp.rows()["t-a"]["state"] == "ORPHANED"

    def test_successful_lookup_with_no_match_clears_the_gate(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9), isa_slug="feat")}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker,
            open_branches={"codex/other/thing"},
            ledger=tmp_path / "l.jsonl",
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "released"
        assert out[0]["probes"]["pr_lookup_ok"] is True
        assert broker.nonforce_calls == ["a"]


# =========================================================================== #
# 7. process-owner gate
# =========================================================================== #
class TestProcessOwnerGate:
    def _rows(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        return {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}, wt

    def test_a_live_owner_skips_the_release(self, tmp_path):
        rows, _wt = self._rows(tmp_path)
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker,
            owners={"a": [{"pid": "4242", "kind": "cwd", "path": str(tmp_path / "wt")}]},
            ledger=tmp_path / "l.jsonl",
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "skipped"
        assert "live process" in out[0]["reason"]
        assert out[0]["probes"]["process_owners"][0]["pid"] == "4242"
        assert broker.nonforce_calls == []
        assert disp.rows()["t-a"]["state"] == "EXECUTING"

    def test_an_inconclusive_probe_skips_the_release(self, tmp_path):
        rows, _wt = self._rows(tmp_path)
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker, owner_probe_ok=False, ledger=tmp_path / "l.jsonl"
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "skipped"
        assert "inconclusive" in out[0]["reason"]
        assert broker.nonforce_calls == []

    def test_a_live_tmux_session_skips_the_release(self, tmp_path):
        rows, _wt = self._rows(tmp_path)
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker, tmux=("codex-sess-a", True), ledger=tmp_path / "l.jsonl"
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "skipped"
        assert "tmux" in out[0]["reason"]
        assert broker.nonforce_calls == []

    def test_an_inconclusive_tmux_probe_skips_the_release(self, tmp_path):
        rows, _wt = self._rows(tmp_path)
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker, tmux=(None, False), ledger=tmp_path / "l.jsonl"
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "skipped"
        assert "tmux" in out[0]["reason"]
        assert broker.nonforce_calls == []

    def test_missing_tmux_binary_is_conclusive_not_inconclusive(self, tmp_path, monkeypatch):
        """No tmux server on the box means no tmux owner — that is an answer."""
        disp = _FakeDispatcher(tmp_path)
        reaper = CodexSessionReaper(disp, MagicMock(), lambda: set())

        def _no_tmux(*a, **kw):
            raise FileNotFoundError("tmux")

        monkeypatch.setattr(subprocess, "run", _no_tmux)

        assert reaper._tmux_owner({"tmux_session": "codex-sess-a"}, "a") == (None, True)

    def test_tmux_probe_error_is_inconclusive(self, tmp_path, monkeypatch):
        disp = _FakeDispatcher(tmp_path)
        reaper = CodexSessionReaper(disp, MagicMock(), lambda: set())

        def _timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="tmux", timeout=10)

        monkeypatch.setattr(subprocess, "run", _timeout)

        assert reaper._tmux_owner({"tmux_session": "codex-sess-a"}, "a") == (None, False)

    def test_scan_reports_not_ok_when_proc_is_unreadable(self, tmp_path, monkeypatch):
        disp = _FakeDispatcher(tmp_path)
        reaper = CodexSessionReaper(disp, MagicMock(), lambda: set())
        monkeypatch.setattr(
            os, "listdir", MagicMock(side_effect=OSError("no /proc here"))
        )

        owners, ok = reaper._scan_process_owners({"a": tmp_path / "wt"})

        assert ok is False
        assert owners == {"a": []}

    def test_scan_of_no_worktrees_is_trivially_ok(self, tmp_path):
        disp = _FakeDispatcher(tmp_path)
        reaper = CodexSessionReaper(disp, MagicMock(), lambda: set())

        assert reaper._scan_process_owners({}) == ({}, True)

    def test_match_owner_requires_a_path_boundary(self, tmp_path):
        """``/wt-sibling`` must not count as living inside ``/wt``."""
        owners: dict[str, list[dict]] = {"a": []}
        prefixes = {"a": "/home/x/codex-wt/a"}

        CodexSessionReaper._match_owner(
            owners, prefixes, "1", "/home/x/codex-wt/a-sibling/deep", "cwd"
        )
        assert owners["a"] == []

        CodexSessionReaper._match_owner(
            owners, prefixes, "2", "/home/x/codex-wt/a/deep", "cwd"
        )
        assert [o["pid"] for o in owners["a"]] == ["2"]

    @pytest.mark.linux_only
    def test_real_live_process_inside_the_worktree_is_detected(self, tmp_path):
        """The one un-mockable half: a real child with cwd in the worktree."""
        wt = tmp_path / "wt"
        wt.mkdir()
        disp = _FakeDispatcher(tmp_path)
        reaper = CodexSessionReaper(disp, MagicMock(), lambda: set())
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"], cwd=str(wt)
        )
        try:
            owners, _ok = reaper._scan_process_owners({"a": wt})
            assert owners["a"], "a live process rooted in the worktree must be seen"
            assert str(proc.pid) in {o["pid"] for o in owners["a"]}
        finally:
            proc.terminate()
            proc.wait(timeout=10)


# =========================================================================== #
# 8. unique-commit custody gate
# =========================================================================== #
class TestCustodyGate:
    def _rows(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        return {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}

    def test_branch_only_commits_quarantine_the_row(self, tmp_path):
        disp = _FakeDispatcher(tmp_path, self._rows(tmp_path))
        broker = _RecordingBroker(disp)
        git = _GitStub(rev_list="aaa111\nbbb222\n")

        out = _reaper(disp, broker, git=git, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "orphaned"
        assert out[0]["branch_only_commits"] == ["aaa111", "bbb222"]
        assert "only on this branch" in out[0]["reason"]
        assert broker.nonforce_calls == []
        assert disp.rows()["t-a"]["state"] == "ORPHANED"

    def test_a_failed_custody_probe_quarantines(self, tmp_path):
        disp = _FakeDispatcher(tmp_path, self._rows(tmp_path))
        broker = _RecordingBroker(disp)
        git = _GitStub(rev_list=None)

        out = _reaper(disp, broker, git=git, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "orphaned"
        assert out[0]["probes"]["custody_probe_ok"] is False
        assert "unprovable" in out[0]["reason"]
        assert broker.nonforce_calls == []

    def test_a_missing_worktree_cannot_prove_custody(self, tmp_path):
        rows = {"t-a": _row("a", tmp_path / "gone", last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "orphaned"
        assert broker.nonforce_calls == []

    def test_uses_rev_list_head_not_remotes(self, tmp_path):
        disp = _FakeDispatcher(tmp_path, self._rows(tmp_path))
        git = _GitStub()

        _reaper(disp, _RecordingBroker(disp), git=git, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert ("rev-list", "HEAD", "--not", "--remotes") in git.calls
        assert not any(call[0] == "log" for call in git.calls), (
            "the weak commits-since-created_at probe must be gone"
        )

    def test_a_dirty_worktree_quarantines_before_custody_is_consulted(self, tmp_path):
        disp = _FakeDispatcher(tmp_path, self._rows(tmp_path))
        broker = _RecordingBroker(disp)
        git = _GitStub(porcelain="?? uncommitted.txt\n")

        out = _reaper(disp, broker, git=git, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "orphaned"
        assert out[0]["uncommitted_work"] is True
        assert not any(call[0] == "rev-list" for call in git.calls)
        assert broker.nonforce_calls == []

    def test_a_failed_status_probe_is_treated_as_dirty(self, tmp_path):
        disp = _FakeDispatcher(tmp_path, self._rows(tmp_path))
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker, git=_GitStub(porcelain=None), ledger=tmp_path / "l.jsonl"
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "orphaned"
        assert broker.nonforce_calls == []

    def test_stability_drift_skips_the_release(self, tmp_path):
        """A message arriving mid-evaluation invalidates every gate above it."""
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)
        reaper = _reaper(disp, broker, ledger=tmp_path / "l.jsonl")
        original_head = reaper._head_sha

        def _drift(worktree):
            state = disp._load_state()
            state["sessions"]["t-a"]["last_message_id"] = "arrived-mid-flight"
            disp._write_state(state)
            reaper._head_sha = original_head
            return original_head(worktree)

        reaper._head_sha = _drift

        out = reaper.reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "skipped"
        assert "drifted" in out[0]["reason"]
        assert broker.nonforce_calls == []

    # --- real git, local bare remote, no network --------------------------- #
    def test_real_git_custody_clean_releases(self, tmp_path):
        repo, wt_root = _make_repo_with_remote(tmp_path)
        wt = _add_worktree(repo, wt_root, "sid-clean")
        rows = {"t-a": _row("sid-clean", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)
        reaper = _reaper(disp, broker, git=None, ledger=tmp_path / "l.jsonl")
        reaper._git = CodexSessionReaper._git.__get__(reaper)  # real git

        out = reaper.reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "released", out[0]["reason"]
        assert out[0]["branch_only_commits"] == []
        assert broker.nonforce_calls == ["sid-clean"]

    def test_real_git_unpushed_commit_quarantines(self, tmp_path):
        repo, wt_root = _make_repo_with_remote(tmp_path)
        wt = _add_worktree(repo, wt_root, "sid-work")
        (wt / "precious.txt").write_text("unpushed work", encoding="utf-8")
        _git_run(wt, "add", "-A")
        _git_run(wt, "commit", "-qm", "precious")
        rows = {"t-a": _row("sid-work", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)
        reaper = _reaper(disp, broker, git=None, ledger=tmp_path / "l.jsonl")
        reaper._git = CodexSessionReaper._git.__get__(reaper)  # real git

        out = reaper.reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "orphaned"
        assert len(out[0]["branch_only_commits"]) == 1
        assert broker.nonforce_calls == []
        assert (wt / "precious.txt").exists()


# =========================================================================== #
# 9. non-force release refusal (broker side)
# =========================================================================== #
class TestNonForceRelease:
    def _broker(self, tmp_path):
        repo, _wt_root = _make_repo_with_remote(tmp_path)
        home = tmp_path / "hermes"
        home.mkdir()
        broker = WorktreeBroker(repo_root=repo, hermes_home=home)
        return repo, broker, home / "codex-wt"

    def test_clean_worktree_is_removed_without_force(self, tmp_path):
        repo, broker, wt_root = self._broker(tmp_path)
        path = _add_worktree(repo, wt_root, "clean")

        broker.release_nonforce("clean")

        assert not path.exists()

    def test_dirty_worktree_is_refused_and_left_intact(self, tmp_path):
        repo, broker, wt_root = self._broker(tmp_path)
        path = _add_worktree(repo, wt_root, "dirty")
        (path / "uncommitted.txt").write_text("precious work", encoding="utf-8")

        with pytest.raises(WorktreeReleaseRefused) as exc:
            broker.release_nonforce("dirty")

        assert "non-force" in str(exc.value)
        assert path.exists()
        assert (path / "uncommitted.txt").read_text(encoding="utf-8") == "precious work"

    def test_the_force_operator_path_is_unchanged(self, tmp_path):
        repo, broker, wt_root = self._broker(tmp_path)
        path = _add_worktree(repo, wt_root, "dirty")
        (path / "uncommitted.txt").write_text("WIP", encoding="utf-8")

        broker.release("dirty")

        assert not path.exists()

    def test_unknown_session_is_a_noop(self, tmp_path):
        _repo, broker, _wt_root = self._broker(tmp_path)

        broker.release_nonforce("never-existed")  # must not raise

    def test_a_surviving_directory_is_refused_rather_than_rmtree_d(
        self, tmp_path, monkeypatch
    ):
        """git says "removed" but the dir is still there: refuse, never rmtree."""
        _repo, broker, wt_root = self._broker(tmp_path)
        wt_root.mkdir(parents=True, exist_ok=True)
        path = wt_root / "survivor"
        path.mkdir()
        (path / "leftover.txt").write_text("still here", encoding="utf-8")
        monkeypatch.setattr(
            broker,
            "_git",
            lambda *args: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        )

        with pytest.raises(WorktreeReleaseRefused) as exc:
            broker.release_nonforce("survivor")

        assert "refusing rmtree fallback" in str(exc.value)
        assert path.exists(), "the reaper path must never rmtree a surviving worktree"
        assert (path / "leftover.txt").read_text(encoding="utf-8") == "still here"

    def test_release_nonforce_never_shells_out_with_force(self, tmp_path, monkeypatch):
        _repo, broker, wt_root = self._broker(tmp_path)
        wt_root.mkdir(parents=True, exist_ok=True)
        (wt_root / "sid").mkdir()
        calls: list[tuple[str, ...]] = []
        real_git = broker._git

        def _spy(*args: str):
            calls.append(args)
            return real_git(*args)

        monkeypatch.setattr(broker, "_git", _spy)

        with pytest.raises(WorktreeReleaseRefused):
            broker.release_nonforce("sid")

        assert calls, "a removal must have been attempted"
        assert all("--force" not in args and "-f" not in args for args in calls)


# =========================================================================== #
# 10. restart idempotence
# =========================================================================== #
class TestRestartIdempotence:
    def test_a_second_reap_tick_is_a_no_op(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        first = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )
        writes_after_first = disp.writes
        second = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert first[0]["outcome"] == "released"
        assert second == []
        assert broker.nonforce_calls == ["a"]
        assert disp.writes == writes_after_first

    @pytest.mark.asyncio
    async def test_restart_then_discover_then_message_cannot_revive_a_tombstone(
        self, tmp_path
    ):
        """The full resurrection loop, replayed end to end."""
        d, broker, send = _make_dispatcher(tmp_path)
        _seed(d, "t-a", _row("sid-a", tmp_path / "gone", state="RELEASED"))

        for _ in range(2):
            await d.on_bot_restart()
            await d.discover_threads([("t-a", "c1", "Old Thread")])
            await d.on_thread_message(ThreadEvent(thread_id="t-a", message_id="m1"))

        row = d._load_state()["sessions"]["t-a"]
        assert row["state"] == "RELEASED"
        assert row["session_id"] == "sid-a"
        assert row["last_message_at"] is None
        broker.allocate.assert_not_called()
        assert d.is_tracked("t-a") is False

    @pytest.mark.asyncio
    async def test_repeated_restarts_do_not_churn_terminal_rows(self, tmp_path):
        d, _broker, _send = _make_dispatcher(tmp_path)
        for state in _TERMINAL:
            _seed(d, f"t-{state}", _row(f"sid-{state}", tmp_path / "gone", state=state))
        before = copy.deepcopy(d._load_state()["sessions"])

        await d.on_bot_restart()
        await d.on_bot_restart()

        assert d._load_state()["sessions"] == before

    @pytest.mark.asyncio
    async def test_gc_watcher_ticks_are_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CodexSessionReaper, "_scan_process_owners", lambda self, wts: ({}, True)
        )
        monkeypatch.setattr(
            CodexSessionReaper, "_tmux_owner", lambda self, row, sid: (None, True)
        )
        rows = {"t-a": _row("a", tmp_path / "gone", last_message_at=_iso(hours=99))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=disp, worktree_broker=broker, gh_list_open_branches=lambda: set()
        )

        await w._tick()
        snapshot = copy.deepcopy(disp.rows())
        await w._tick()

        assert disp.rows() == snapshot


# =========================================================================== #
# 11. archive-first registry GC
# =========================================================================== #
class TestRegistryGcArchiveFirst:
    def _gc(self, disp, tmp_path, *, days: int = 90) -> CodexRegistryGc:
        return CodexRegistryGc(disp, hermes_home=tmp_path, max_terminal_age_days=days)

    def _archive_lines(self, tmp_path) -> list[dict]:
        path = archive_path(tmp_path)
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_archive_line_lands_before_the_registry_write(self, tmp_path):
        rows = {"t-old": _row("sid-old", None, state="RELEASED",
                              released_at=_iso(days=120))}
        disp = _FakeDispatcher(tmp_path, rows)
        seen_at_write: list[list[dict]] = []
        disp.write_hook = lambda _state: seen_at_write.append(self._archive_lines(tmp_path))

        out = self._gc(disp, tmp_path).collect(terminal_states=TERMINAL_STATES)

        assert [d["outcome"] for d in out] == ["retired"]
        assert len(seen_at_write) == 1, "exactly one registry write"
        archived_at_write = seen_at_write[0]
        assert [e["thread_id"] for e in archived_at_write] == ["t-old"], (
            "the archive append must be durable BEFORE the row is deleted"
        )
        assert "t-old" not in disp.rows()

    def test_the_full_row_is_archived_verbatim(self, tmp_path):
        row = _row("sid-old", "/some/where", state="RELEASED",
                   released_at=_iso(days=120), release_reason="clean custody",
                   release_receipt={"head": "abc", "branch_only_commits": []})
        disp = _FakeDispatcher(tmp_path, {"t-old": row})

        self._gc(disp, tmp_path).collect(terminal_states=TERMINAL_STATES)

        entries = self._archive_lines(tmp_path)
        assert len(entries) == 1
        assert entries[0]["row"] == row
        assert entries[0]["thread_id"] == "t-old"
        assert entries[0]["session_id"] == "sid-old"
        assert entries[0]["state"] == "RELEASED"
        assert entries[0]["archived_at"]

    @pytest.mark.parametrize(
        ("age_days", "expected"),
        [(89.9, "kept"), (90.0, "retired"), (90.1, "retired"), (1.0, "kept")],
    )
    def test_ninety_day_boundary(self, tmp_path, age_days, expected):
        rows = {"t-x": _row("sid-x", None, state="RELEASED",
                            released_at=_iso(days=age_days))}
        disp = _FakeDispatcher(tmp_path, rows)

        out = self._gc(disp, tmp_path).collect(terminal_states=TERMINAL_STATES)

        assert out[0]["outcome"] == expected
        assert ("t-x" in disp.rows()) is (expected == "kept")

    def test_a_row_whose_age_is_unresolvable_is_kept(self, tmp_path):
        rows = {"t-x": {"session_id": "sid-x", "state": "RELEASED",
                        "released_at": "not-a-date", "created_at": None}}
        disp = _FakeDispatcher(tmp_path, rows)

        out = self._gc(disp, tmp_path).collect(terminal_states=TERMINAL_STATES)

        assert out[0]["outcome"] == "kept"
        assert "unresolvable" in out[0]["reason"]
        assert "t-x" in disp.rows()
        assert self._archive_lines(tmp_path) == []

    def test_a_failed_archive_append_keeps_the_row(self, tmp_path, monkeypatch):
        rows = {"t-old": _row("sid-old", None, state="RELEASED",
                              released_at=_iso(days=120))}
        disp = _FakeDispatcher(tmp_path, rows)
        gc = self._gc(disp, tmp_path)
        monkeypatch.setattr(gc, "_archive_row", lambda *a, **kw: False)

        out = gc.collect(terminal_states=TERMINAL_STATES)

        assert out[0]["outcome"] == "kept"
        assert "never delete unarchived" in out[0]["reason"]
        assert "t-old" in disp.rows()
        assert disp.writes == 0, "no registry write at all when nothing was archived"

    def test_archive_row_reports_failure_on_an_oserror(self, tmp_path, monkeypatch):
        disp = _FakeDispatcher(tmp_path)
        gc = self._gc(disp, tmp_path)

        def _boom(*a, **kw):
            raise OSError("read-only fs")

        monkeypatch.setattr("builtins.open", _boom)

        assert gc._archive_row("t-x", {"session_id": "x"}, "because") is False

    def test_dry_run_archives_nothing_and_deletes_nothing(self, tmp_path):
        rows = {"t-old": _row("sid-old", None, state="RELEASED",
                              released_at=_iso(days=120))}
        disp = _FakeDispatcher(tmp_path, rows)

        out = self._gc(disp, tmp_path).collect(
            terminal_states=TERMINAL_STATES, dry_run=True
        )

        assert out[0]["outcome"] == "would_retire"
        assert out[0]["dry_run"] is True
        assert "t-old" in disp.rows()
        assert disp.writes == 0
        assert self._archive_lines(tmp_path) == []

    def test_non_terminal_rows_are_never_examined(self, tmp_path):
        rows = {
            "t-live": _row("sid-live", None, state="EXECUTING",
                           created_at=_iso(days=400)),
            "t-claimed": _row("sid-claimed", None, state="CLAIMED",
                              created_at=_iso(days=400)),
            "t-merging": _row("sid-merging", None, state="MERGING",
                              created_at=_iso(days=400)),
        }
        disp = _FakeDispatcher(tmp_path, rows)

        out = self._gc(disp, tmp_path).collect(terminal_states=TERMINAL_STATES)

        assert out == []
        assert set(disp.rows()) == set(rows)
        assert disp.writes == 0

    @pytest.mark.parametrize("state", _GC_ELIGIBLE)
    def test_every_collectable_terminal_state_is_eligible(self, tmp_path, state):
        rows = {"t-x": _row("sid-x", None, state=state,
                            released_at=_iso(days=200), orphaned_at=_iso(days=200))}
        disp = _FakeDispatcher(tmp_path, rows)

        out = self._gc(disp, tmp_path).collect(
            terminal_states=GC_ELIGIBLE_TERMINAL_STATES
        )

        assert out[0]["outcome"] == "retired"
        assert "t-x" not in disp.rows()

    def test_a_partial_sweep_retires_only_the_aged_rows(self, tmp_path):
        rows = {
            "t-old": _row("sid-old", None, state="RELEASED", released_at=_iso(days=200)),
            "t-young": _row("sid-young", None, state="RELEASED", released_at=_iso(days=3)),
            "t-live": _row("sid-live", None, state="EXECUTING", created_at=_iso(days=400)),
        }
        disp = _FakeDispatcher(tmp_path, rows)

        self._gc(disp, tmp_path).collect(terminal_states=TERMINAL_STATES)

        assert set(disp.rows()) == {"t-young", "t-live"}
        assert [e["thread_id"] for e in self._archive_lines(tmp_path)] == ["t-old"]

    def test_a_second_collect_is_a_no_op(self, tmp_path):
        rows = {"t-old": _row("sid-old", None, state="RELEASED",
                              released_at=_iso(days=200))}
        disp = _FakeDispatcher(tmp_path, rows)
        gc = self._gc(disp, tmp_path)

        gc.collect(terminal_states=TERMINAL_STATES)
        writes = disp.writes
        second = gc.collect(terminal_states=TERMINAL_STATES)

        assert second == []
        assert disp.writes == writes
        assert len(self._archive_lines(tmp_path)) == 1

    def test_archived_thread_ids_round_trip(self, tmp_path):
        rows = {"t-old": _row("sid-old", None, state="RELEASED",
                              released_at=_iso(days=200))}
        disp = _FakeDispatcher(tmp_path, rows)
        gc = self._gc(disp, tmp_path)

        gc.collect(terminal_states=TERMINAL_STATES)

        assert gc.archived_thread_ids() == {"t-old"}
        assert load_archived_thread_ids(tmp_path) == {"t-old"}

    def test_load_archived_thread_ids_tolerates_a_torn_tail(self, tmp_path):
        path = archive_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"thread_id": "t-1"}) + "\n"
            + "\n"
            + json.dumps({"thread_id": "t-2"}) + "\n"
            + '{"thread_id": "t-3", "row": {"parti',
            encoding="utf-8",
        )

        assert load_archived_thread_ids(tmp_path) == {"t-1", "t-2"}

    def test_load_archived_thread_ids_on_a_missing_archive_is_empty(self, tmp_path):
        assert load_archived_thread_ids(tmp_path / "nothing-here") == set()

    def test_archive_path_is_under_the_reaper_state_dir(self, tmp_path):
        assert archive_path(tmp_path) == (
            tmp_path / "state" / "codex-reaper" / "tombstone-archive.jsonl"
        )

    def test_hermes_home_falls_back_to_the_dispatcher(self, tmp_path):
        disp = _FakeDispatcher(tmp_path)

        gc = CodexRegistryGc(disp)

        assert gc._hermes_home() == tmp_path

    def test_the_module_default_retention_window_is_ninety_days(self, tmp_path):
        """Guards the constant itself, not just the watcher's copy of it."""
        assert DEFAULT_MAX_TERMINAL_AGE_DAYS == 90
        rows = {
            "t-old": _row("sid-old", None, state="RELEASED", released_at=_iso(days=91)),
            "t-young": _row("sid-young", None, state="RELEASED", released_at=_iso(days=89)),
        }
        disp = _FakeDispatcher(tmp_path, rows)

        # No max_terminal_age_days -> the module default must apply.
        CodexRegistryGc(disp, hermes_home=tmp_path).collect(
            terminal_states=TERMINAL_STATES
        )

        assert set(disp.rows()) == {"t-young"}


# =========================================================================== #
# cross-cutting: the full released lifecycle
# =========================================================================== #
def test_release_then_gc_then_discover_never_resurrects(tmp_path):
    """Release -> tombstone -> 90d GC -> archive -> discovery still refuses."""
    d, broker_mock, _send = _make_dispatcher(tmp_path)
    wt = tmp_path / "codex-wt" / "sid-a"
    wt.mkdir(parents=True, exist_ok=True)
    _seed(d, "t-a", _row("sid-a", wt, last_message_at=_iso(hours=9)))

    released: list[tuple[str, str]] = []

    class _Broker:
        def release_nonforce(self, sid: str) -> None:
            released.append((sid, d._load_state()["sessions"]["t-a"]["state"]))

    decisions = _reaper(d, _Broker(), ledger=tmp_path / "l.jsonl").reap(
        reap_idle_hours=6.0, dry_run=False
    )

    assert decisions[0]["outcome"] == "released"
    assert released == [("sid-a", "RELEASED")]
    assert d._load_state()["sessions"]["t-a"]["state"] == "RELEASED"

    # Age the tombstone past the retention window and GC it.
    state = d._load_state()
    state["sessions"]["t-a"]["released_at"] = _iso(days=200)
    d._write_state(state)
    gc_out = CodexRegistryGc(d, hermes_home=tmp_path, max_terminal_age_days=90).collect(
        terminal_states=TERMINAL_STATES
    )

    assert gc_out[0]["outcome"] == "retired"
    assert "t-a" not in d._load_state()["sessions"]
    assert load_archived_thread_ids(tmp_path) == {"t-a"}

    # The archive keeps discovery refusing long after the row is gone.
    import asyncio

    results = asyncio.run(d.discover_threads([("t-a", "c1", "Old Thread")]))

    assert results == []
    broker_mock.allocate.assert_not_called()


# =========================================================================== #
# B1 — the measured six-live-session scenario
# =========================================================================== #
_LIVE_FIXTURE = Path(__file__).parent / "fixtures" / "c7_live_sessions_20260814.json"

#: The instant the B1 measurement was taken.  Pinned so the scenario does not
#: quietly change meaning as wall-clock time moves past the fixture.
_MEASURED_AT = datetime(2026, 8, 14, 13, 0, tzinfo=timezone.utc)


def _load_live_scenario(tmp_path: Path) -> tuple[_FakeDispatcher, dict]:
    """Rehydrate the six live rows into a throwaway registry under tmp_path.

    The fixture is a READ-ONLY snapshot of ``~/.hermes/codex_sessions.json``:
    every timestamp, state and identifier is verbatim, and only the two
    absolute paths are re-pointed into ``tmp_path`` so the test cannot touch
    anything real.  Each worktree directory is created, because a session that
    is genuinely running has one.
    """
    doc = json.loads(_LIVE_FIXTURE.read_text(encoding="utf-8"))
    wt_root = tmp_path / "codex-wt"
    isa_root = tmp_path / "work"
    rows: dict = {}
    for thread_id, row in doc["sessions"].items():
        row = dict(row)
        row["worktree_path"] = row["worktree_path"].replace("{WT_ROOT}", str(wt_root))
        row["isa_path"] = row["isa_path"].replace("{ISA_ROOT}", str(isa_root))
        Path(row["worktree_path"]).mkdir(parents=True, exist_ok=True)
        rows[thread_id] = row
    return _FakeDispatcher(tmp_path, rows), doc["_provenance"]


class TestSixLiveSessionsSurviveShippedDefaults:
    """C7 blocker B1, measured: the first post-cutover tick tore down everything.

    All six of these rows were live work — the recon brief names them
    "protected survivors, NOT candidates".  Every one of them has a clean
    worktree, no unique commits and no open PR, so at a 6-hour idle window
    every other guard clears and all six get released.  These tests pin that
    the SHIPPED configuration releases zero of them, and that the dangerous
    configuration is reachable only by asking for it in full.
    """

    def test_the_fixture_really_is_the_six_live_sessions(self, tmp_path):
        disp, provenance = _load_live_scenario(tmp_path)
        rows = disp.rows()

        assert len(rows) == 6
        states = sorted(row["state"] for row in rows.values())
        assert states == ["CLAIMED"] + ["EXECUTING"] * 5
        assert provenance["states"] == {"EXECUTING": 5, "CLAIMED": 1}

    def test_shipped_defaults_release_nothing(self, tmp_path, monkeypatch):
        """The headline assertion: zero rows released, zero rows written."""
        for key in ("HERMES_CODEX_REAP_IDLE_HOURS", "HERMES_CODEX_REAP_DRY_RUN",
                    "HERMES_CODEX_REAP_ARMED", "HERMES_CODEX_REAP_CONFIRMED"):
            monkeypatch.delenv(key, raising=False)
        disp, _ = _load_live_scenario(tmp_path)
        broker = _RecordingBroker(disp)
        reaper = _reaper(disp, broker, ledger=tmp_path / "l.jsonl")

        # Exactly what CodexGcWatcher passes with nothing configured.
        watcher = CodexGcWatcher(
            dispatcher=disp, worktree_broker=broker, gh_list_open_branches=lambda: set(),
        )
        decisions = reaper.reap(
            reap_idle_hours=watcher._reap_idle_hours, dry_run=watcher._reap_dry_run
        )

        assert [d["outcome"] for d in decisions] == ["skipped"] * 6
        assert broker.nonforce_calls == []
        assert disp.writes == 0
        assert sorted(r["state"] for r in disp.rows().values()) == (
            ["CLAIMED"] + ["EXECUTING"] * 5
        )

    def test_zero_released_even_if_fully_armed_at_the_shipped_window(self, tmp_path):
        """Defence in depth: the 10-day window alone already saves all six.

        Arming is not the only thing standing between a deploy and this
        outcome — so is the window.  Both had to be wrong together.
        """
        disp, _ = _load_live_scenario(tmp_path)
        broker = _RecordingBroker(disp)

        decisions = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=DEFAULT_REAP_IDLE_HOURS, dry_run=False
        )

        released = [d for d in decisions if d["outcome"] == "released"]
        assert released == []
        assert broker.nonforce_calls == []
        assert all(d["outcome"] == "skipped" for d in decisions)
        assert all("not idle" in d["reason"] for d in decisions)

    def test_the_scenario_is_not_toothless(self, tmp_path):
        """Prove the fixture WOULD have been destroyed by the old defaults.

        Without this the two tests above could pass against a fixture that was
        never at risk.  Here we reconstruct the pre-fix configuration exactly —
        ``reap_idle_hours=6, dry_run=False`` — and confirm all six are released,
        which is precisely what was measured against the live registry.

        Time-stable by construction: the fixture timestamps only recede as
        wall-clock advances, so a row that was >6h idle at the measurement
        instant stays >6h idle forever.
        """
        disp, _ = _load_live_scenario(tmp_path)
        broker = _RecordingBroker(disp)
        reaper = _reaper(disp, broker, ledger=tmp_path / "l.jsonl")

        decisions = reaper.reap(reap_idle_hours=6.0, dry_run=False)

        assert [d["outcome"] for d in decisions] == ["released"] * 6, (
            "the pre-fix defaults must be shown to destroy all six, or the "
            "tests above prove nothing"
        )
        assert len(broker.nonforce_calls) == 6

    def test_each_row_was_over_the_six_hour_line_at_the_measured_instant(
        self, tmp_path
    ):
        """The measurement itself, pinned to a fixed clock.

        This is the B1 finding restated as an assertion: at 6 hours every one
        of the six passes the idle gate, and at the shipped 10-day window not
        one of them does.
        """
        disp, _ = _load_live_scenario(tmp_path)
        reaper = _reaper(disp, ledger=tmp_path / "l.jsonl")

        for row in disp.rows().values():
            at_six, block_six = reaper._idle_reason(row, _MEASURED_AT, 6.0)
            at_ten_days, block_ten = reaper._idle_reason(
                row, _MEASURED_AT, DEFAULT_REAP_IDLE_HOURS
            )
            assert block_six is None and block_ten is None
            assert at_six is not None, (
                f"{row['session_id']} clears a 6h gate — this is the hazard"
            )
            assert at_ten_days is None, (
                f"{row['session_id']} must NOT clear the shipped 10-day gate"
            )

    def test_arming_at_six_hours_takes_three_deliberate_settings(
        self, tmp_path, monkeypatch
    ):
        """The whole B1 fix, stated as one assertion chain."""
        for key in ("HERMES_CODEX_REAP_IDLE_HOURS", "HERMES_CODEX_REAP_DRY_RUN",
                    "HERMES_CODEX_REAP_ARMED", "HERMES_CODEX_REAP_CONFIRMED"):
            monkeypatch.delenv(key, raising=False)
        disp, _ = _load_live_scenario(tmp_path)
        broker = MagicMock()

        def _mode(**kwargs):
            return CodexGcWatcher(
                dispatcher=disp, worktree_broker=broker,
                gh_list_open_branches=lambda: set(), **kwargs,
            )

        assert _mode()._reap_dry_run is True
        assert _mode(reap_idle_hours=6.0)._reap_dry_run is True
        assert _mode(reap_idle_hours=6.0, reap_armed=True)._reap_mode == MODE_PREVIEW
        live = _mode(reap_idle_hours=6.0, reap_armed=True, reap_confirmed=True)
        assert live._reap_mode == MODE_LIVE
        assert live._reap_dry_run is False


# =========================================================================== #
# B2 — terminal-age fields the dispatcher actually writes
# =========================================================================== #
class TestTerminalAgeFields:
    """The GC must age a row on a clock that measured its terminal life.

    ``closed_at`` was missing from ``_TERMINAL_TS_FIELDS`` while the dispatcher
    wrote exactly that field for every PR closed unmerged, so those rows fell
    through to ``last_message_at`` / ``created_at`` — timestamps that predate
    the transition by an unbounded amount, making a freshly-escalated row
    instantly GC-eligible.
    """

    def _gc(self, disp, tmp_path, *, days: int = 90) -> CodexRegistryGc:
        return CodexRegistryGc(disp, hermes_home=tmp_path, max_terminal_age_days=days)

    @pytest.mark.parametrize(
        ("field", "state"),
        [
            ("released_at", "RELEASED"),
            ("orphaned_at", "ORPHANED"),
            ("escalated_at", "ESCALATED"),
            ("closed_at", "ESCALATED"),
            ("completed_at", "COMPLETE"),
            ("merged_at", "MERGED"),
            ("terminal_at", "DONE"),
        ],
    )
    def test_each_terminal_field_resolves_the_age(self, tmp_path, field, state):
        """A row young by its terminal stamp is KEPT even if the row is ancient."""
        row = _row("sid-x", None, state=state, created_at=_iso(days=900),
                   last_message_at=_iso(days=900))
        row[field] = _iso(days=1)
        disp = _FakeDispatcher(tmp_path, {"t-x": row})

        out = self._gc(disp, tmp_path).collect(
            terminal_states=TERMINAL_STATES, quarantine_states=set(),
        )

        assert out[0]["age_field"] == field
        assert out[0]["outcome"] == "kept"
        assert "t-x" in disp.rows()

    @pytest.mark.parametrize(
        ("field", "state"),
        [
            ("released_at", "RELEASED"),
            ("escalated_at", "ESCALATED"),
            ("closed_at", "ESCALATED"),
            ("completed_at", "COMPLETE"),
            ("merged_at", "MERGED"),
            ("terminal_at", "DONE"),
        ],
    )
    def test_each_terminal_field_also_authorises_an_aged_retire(
        self, tmp_path, field, state
    ):
        row = _row("sid-x", None, state=state)
        row.pop("created_at", None)
        row[field] = _iso(days=200)
        disp = _FakeDispatcher(tmp_path, {"t-x": row})

        out = self._gc(disp, tmp_path).collect(
            terminal_states=TERMINAL_STATES, quarantine_states=set(),
        )

        assert out[0]["age_field"] == field
        assert out[0]["outcome"] == "retired"

    def test_a_pr_closed_unmerged_row_is_not_instantly_collectable(self, tmp_path):
        """The exact B2 shape: dispatcher writes closed_at on an ancient thread."""
        row = _row("sid-x", None, state="ESCALATED",
                   created_at=_iso(days=400), last_message_at=_iso(days=380))
        row["closed_at"] = _iso(days=2)
        row["pr_state"] = "CLOSED"
        disp = _FakeDispatcher(tmp_path, {"t-x": row})

        out = self._gc(disp, tmp_path).collect(
            terminal_states=TERMINAL_STATES, quarantine_states=set(),
        )

        assert out[0]["age_field"] == "closed_at"
        assert out[0]["outcome"] == "kept"
        assert "t-x" in disp.rows(), "a two-day-old escalation must not be deleted"

    @pytest.mark.parametrize("field", ["created_at", "last_message_at"])
    def test_non_terminal_timestamps_are_not_an_age_basis(self, tmp_path, field):
        """An ancient row with no terminal stamp is KEPT, not retired."""
        row = _row("sid-x", None, state="ESCALATED")
        row.pop("created_at", None)
        row.pop("last_message_at", None)
        row[field] = _iso(days=900)
        disp = _FakeDispatcher(tmp_path, {"t-x": row})

        out = self._gc(disp, tmp_path).collect(
            terminal_states=TERMINAL_STATES, quarantine_states=set(),
        )

        assert out[0]["age_field"] is None
        assert out[0]["outcome"] == "kept"
        assert "unresolvable" in out[0]["reason"]
        assert "t-x" in disp.rows()

    def test_every_dispatcher_terminal_write_is_covered(self):
        """Guard against a new terminal transition adding an unlisted field."""
        from gateway import codex_registry_gc as mod

        source = Path(
            CodexSessionDispatcher.__module__.replace(".", "/") + ".py"
        )
        text = (Path(__file__).parents[2] / source).read_text(encoding="utf-8")
        # Every `row["<x>_at"] = ...` that sits in a terminal transition.
        written = set(re.findall(r'row\["(\w+_at)"\]\s*=', text))
        terminal_writes = written & {
            "released_at", "orphaned_at", "escalated_at", "closed_at",
            "completed_at", "merged_at", "terminal_at",
        }
        assert terminal_writes <= set(mod._TERMINAL_TS_FIELDS)
        # The three the dispatcher provably writes today.
        assert {"orphaned_at", "escalated_at", "closed_at", "merged_at"} <= (
            set(mod._TERMINAL_TS_FIELDS)
        )
        # The missing direction.  Both asserts above are one-directional in the
        # way deletion cannot violate: dropping a `row["…_at"] = …` line only
        # SHRINKS `written`, and a smaller set is still a subset — the grep gets
        # strictly easier every time a stamp is deleted.  The second assert looks
        # like the reverse but reads the GC module's tuple, which the deletion
        # never touches.  This one reads the grep result, so a deleted stamp goes
        # red.  Belt, not braces: a grep still cannot see whether the value is
        # CORRECT — that is
        # TestDispatcherStampsEveryTerminalTransition's job.
        assert {"orphaned_at", "escalated_at", "closed_at", "merged_at"} <= written


class TestDispatcherStampsEveryTerminalTransition:
    """B2, upstream half: no terminal transition may leave the age unknowable."""

    @pytest.mark.asyncio
    async def test_pr_closed_unmerged_writes_closed_at(self, tmp_path):
        d, _broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-a", _row("sid-a", tmp_path / "wt", state="EXECUTING",
                             pr_number=7))

        await d.on_pr_closed_unmerged("t-a", {"closedAt": "2026-08-14T10:00:00Z"})

        row = d._load_state()["sessions"]["t-a"]
        assert row["state"] == "ESCALATED"
        assert row["closed_at"] == "2026-08-14T10:00:00Z"

    @pytest.mark.asyncio
    async def test_restart_orphaning_stamps_orphaned_at(self, tmp_path):
        d, _broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-a", _row("sid-a", tmp_path / "definitely-gone",
                             state="EXECUTING"))

        await d.on_bot_restart()

        row = d._load_state()["sessions"]["t-a"]
        assert row["state"] == "ORPHANED"
        assert row["orphaned_at"], "an ORPHANED row with no stamp has no age"
        assert row["orphaned_reason"]

    @pytest.mark.asyncio
    async def test_escalate_verdict_writes_a_correct_escalated_at(self, tmp_path):
        """The third terminal transition — the one this class omitted.

        Deleting ``row["escalated_at"] = _now_iso()`` from ``_apply_verdict``'s
        ESCALATE branch left all 203 tests green: the parametrised age tests
        hand-write the field on a synthetic dict (they prove the GC *consumes*
        it, never that anything *produces* it), and the source grep only gets
        easier when a stamp is deleted.  Without the stamp the GC finds no
        terminal timestamp, falls to ``age_field=None``/``kept``, and the row is
        immortal in the live registry — the mirror image of the original B2.

        Asserts the instant, not merely truthiness: a mutant writing
        ``row["escalated_at"] = row["created_at"]`` would satisfy a truthiness
        check and reintroduce exactly B2's defect of a non-transition timestamp
        as the age basis.
        """
        d, _broker, _send = _make_dispatcher(tmp_path)
        _seed(d, "t-a", _row("sid-a", tmp_path / "wt", state="EXECUTING"))
        state = d._load_state()

        before = datetime.now(timezone.utc)
        await d._apply_verdict(
            thread_id="t-a",
            row=state["sessions"]["t-a"],
            state=state,
            verdict=SimpleNamespace(
                kind="ESCALATE", rationale="human needed", iteration=3
            ),
        )
        after = datetime.now(timezone.utc)

        persisted = d._load_state()["sessions"]["t-a"]
        assert persisted["state"] == "ESCALATED"
        stamp = persisted.get("escalated_at")
        assert stamp, "an ESCALATED row with no stamp has no age the GC can use"
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        assert before <= parsed <= after, "escalated_at must be the transition instant"

    @pytest.mark.asyncio
    async def test_the_escalate_stamp_is_the_age_basis_the_gc_uses(self, tmp_path):
        """Producer -> consumer, end to end, with no hand-written field anywhere.

        This is the span the source grep was gesturing at: the dispatcher writes
        the stamp and the registry GC picks it — and only it — as the age field,
        even though the row's ``created_at``/``last_message_at`` are 900 days old
        and would authorise collection if either were still the age basis.
        """
        d, _broker, _send = _make_dispatcher(tmp_path)
        _seed(
            d,
            "t-a",
            _row(
                "sid-a", tmp_path / "wt", state="EXECUTING",
                created_at=_iso(days=900), last_message_at=_iso(days=900),
            ),
        )
        state = d._load_state()
        await d._apply_verdict(
            thread_id="t-a",
            row=state["sessions"]["t-a"],
            state=state,
            verdict=SimpleNamespace(kind="ESCALATE", rationale="x", iteration=1),
        )

        out = CodexRegistryGc(
            d, hermes_home=tmp_path, max_terminal_age_days=90
        ).collect(terminal_states=TERMINAL_STATES, quarantine_states=set())

        assert out[0]["age_field"] == "escalated_at"
        assert out[0]["outcome"] == "kept"
        assert "t-a" in d._load_state()["sessions"]


# =========================================================================== #
# B3 — ORPHANED quarantine is never garbage-collected
# =========================================================================== #
class TestQuarantineIsNeverCollected:
    """The docstrings promise "kept forever, until a human looks".

    Pre-fix the registry GC deleted ORPHANED rows at 90 days, so the promise
    and the code disagreed.  Resolved in favour of the promise: quarantine is
    terminal AND exempt.
    """

    def _gc(self, disp, tmp_path, *, days: int = 90) -> CodexRegistryGc:
        return CodexRegistryGc(disp, hermes_home=tmp_path, max_terminal_age_days=days)

    def test_the_two_state_sets_agree(self):
        assert QUARANTINE_STATES == DEFAULT_QUARANTINE_STATES
        assert QUARANTINE_STATES <= TERMINAL_STATES
        assert GC_ELIGIBLE_TERMINAL_STATES == TERMINAL_STATES - QUARANTINE_STATES
        assert "ORPHANED" not in GC_ELIGIBLE_TERMINAL_STATES

    @pytest.mark.parametrize("age_days", [91, 200, 3650])
    def test_an_ancient_orphaned_row_is_kept(self, tmp_path, age_days):
        rows = {"t-x": _row("sid-x", None, state="ORPHANED",
                            orphaned_at=_iso(days=age_days))}
        disp = _FakeDispatcher(tmp_path, rows)

        out = self._gc(disp, tmp_path).collect(terminal_states=TERMINAL_STATES)

        assert out[0]["outcome"] == "kept"
        assert out[0]["quarantined"] is True
        assert "quarantine" in out[0]["reason"]
        assert "t-x" in disp.rows()
        assert disp.writes == 0
        assert load_archived_thread_ids(tmp_path) == set(), (
            "a quarantined row must not even be archived"
        )

    def test_quarantine_survives_a_caller_passing_the_whole_vocabulary(self, tmp_path):
        """Belt-and-braces: even ``terminal_states=TERMINAL_STATES`` is safe."""
        rows = {
            "t-orph": _row("sid-orph", None, state="ORPHANED",
                           orphaned_at=_iso(days=500)),
            "t-rel": _row("sid-rel", None, state="RELEASED",
                          released_at=_iso(days=500)),
        }
        disp = _FakeDispatcher(tmp_path, rows)

        self._gc(disp, tmp_path).collect(terminal_states=TERMINAL_STATES)

        assert set(disp.rows()) == {"t-orph"}

    def test_the_watcher_never_hands_orphaned_to_the_collector(self, tmp_path):
        """Cross-check at the wiring layer, not just the collector."""
        rows = {"t-orph": _row("sid-orph", None, state="ORPHANED",
                               orphaned_at=_iso(days=500))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=disp, worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
            reap_armed=True, reap_confirmed=True,
        )

        import asyncio

        asyncio.run(w._tick())

        assert "t-orph" in disp.rows()

    def test_a_quarantined_worktree_stays_tracked_so_gc_never_sweeps_it(
        self, tmp_path
    ):
        """The disk half of the same promise (already true; pinned here)."""
        rows = {"t-orph": _row("sid-orph", tmp_path / "orph", state="ORPHANED")}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0
        w = CodexGcWatcher(
            dispatcher=disp, worktree_broker=broker,
            gh_list_open_branches=lambda: set(),
        )

        import asyncio

        asyncio.run(w._tick())

        assert broker.gc.call_args.kwargs["tracked_sids"] == {"sid-orph"}

    def test_the_reaper_docstring_promise_is_still_written_down(self):
        """If someone reverses this decision, the prose must change with it."""
        from gateway import codex_session_reaper as mod

        assert "forever, until a human looks" in mod.__doc__


# =========================================================================== #
# HIGH-1 — the row is re-verified at WRITE time, not just at decide time
# =========================================================================== #
class TestWriteTimeReverification:
    """``_decide`` and ``_apply`` are separated by the whole probe suite.

    The stability guard closes the window *inside* ``_decide``; the tombstone is
    stamped later still, in ``_apply``, after a re-read that used to overwrite
    whatever it found.  A Discord message landing in that window flips the row
    back to EXECUTING — and the reaper would tombstone it anyway.
    """

    def _armed_row(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        return _FakeDispatcher(tmp_path, rows)

    def _race(self, tmp_path, mutate_row):
        """Run one reap, applying ``mutate_row`` in the decide->write window."""
        disp = self._armed_row(tmp_path)
        broker = _RecordingBroker(disp)
        reaper = _reaper(disp, broker, ledger=tmp_path / "l.jsonl")
        real_stability = reaper._stability_drift

        def _hook(thread_id, snapshot, worktree, head):
            drift = real_stability(thread_id, snapshot, worktree, head)
            state = disp._load_state()
            mutate_row(state["sessions"])
            disp._write_state(state)
            return drift

        reaper._stability_drift = _hook
        return disp, broker, reaper.reap(reap_idle_hours=6.0, dry_run=False)

    @pytest.mark.parametrize("new_state", ["MERGING", "COMPLETE", "ORPHANED"])
    def test_a_state_change_between_decide_and_write_aborts_the_release(
        self, tmp_path, new_state
    ):
        def _flip(sessions):
            sessions["t-a"]["state"] = new_state

        disp, broker, out = self._race(tmp_path, _flip)

        assert out[0]["outcome"] == "skipped"
        assert "before the tombstone could be written" in out[0]["reason"]
        assert out[0]["write_time_drift"]
        assert broker.nonforce_calls == [], "no disk teardown after an abort"
        assert disp.rows()["t-a"]["state"] == new_state, "the new state survives"
        assert "released_at" not in disp.rows()["t-a"]

    def test_a_message_arriving_in_the_window_aborts_the_release(self, tmp_path):
        """The load-bearing case: ``state`` does NOT change.

        ``on_thread_message`` sets ``state = "EXECUTING"`` on a row that is
        already EXECUTING and stamps ``last_message_*``.  A guard that only
        compared ``state`` would see nothing and tombstone a session that had
        just started talking again — which is the whole point of the race.
        """
        def _message_arrives(sessions):
            sessions["t-a"]["last_message_id"] = "m-new"
            sessions["t-a"]["last_message_at"] = _iso(seconds=1)
            sessions["t-a"]["state"] = "EXECUTING"  # unchanged, as in production

        disp, broker, out = self._race(tmp_path, _message_arrives)

        assert out[0]["outcome"] == "skipped"
        assert "last_message_id" in out[0]["write_time_drift"]
        assert broker.nonforce_calls == []
        row = disp.rows()["t-a"]
        assert row["state"] == "EXECUTING"
        assert row["last_message_id"] == "m-new"
        assert "released_at" not in row

    def test_a_pause_in_the_window_aborts_the_release(self, tmp_path):
        def _paused(sessions):
            sessions["t-a"]["paused"] = True

        disp, broker, out = self._race(tmp_path, _paused)

        assert out[0]["outcome"] == "skipped"
        assert "paused" in out[0]["write_time_drift"]
        assert broker.nonforce_calls == []
        assert disp.rows()["t-a"]["paused"] is True

    def test_a_session_id_swap_between_decide_and_write_aborts(self, tmp_path):
        """The thread_id was reused by a *different* session."""
        def _swap(sessions):
            sessions["t-a"]["session_id"] = "somebody-else"

        disp, broker, out = self._race(tmp_path, _swap)

        assert out[0]["outcome"] == "skipped"
        assert "session_id" in out[0]["write_time_drift"]
        assert broker.nonforce_calls == []
        assert disp.rows()["t-a"]["state"] == "EXECUTING"

    def test_the_quarantine_write_is_re_verified_too(self, tmp_path):
        """An ORPHANED stamp must not clobber a row that finished properly."""
        wt = tmp_path / "gone"  # missing -> custody unprovable -> orphan
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        reaper = _reaper(disp, ledger=tmp_path / "l.jsonl")
        real_branch_only = reaper._branch_only_commits

        def _finish(worktree):
            result = real_branch_only(worktree)
            state = disp._load_state()
            state["sessions"]["t-a"]["state"] = "COMPLETE"
            state["sessions"]["t-a"]["merged_at"] = _iso(hours=0)
            disp._write_state(state)
            return result

        reaper._branch_only_commits = _finish

        out = reaper.reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "skipped"
        assert disp.rows()["t-a"]["state"] == "COMPLETE"
        assert "orphaned_at" not in disp.rows()["t-a"]

    def test_a_vanished_row_still_aborts_cleanly(self, tmp_path):
        def _delete(sessions):
            sessions.pop("t-a", None)

        _disp, broker, out = self._race(tmp_path, _delete)

        assert out[0]["outcome"] == "skipped"
        assert "disappeared" in out[0]["write_time_drift"]
        assert broker.nonforce_calls == []

    def test_the_happy_path_still_releases(self, tmp_path):
        """The guard must not be so tight that nothing can ever be reaped."""
        disp = self._armed_row(tmp_path)
        broker = _RecordingBroker(disp)

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "released"
        assert broker.nonforce_calls == ["a"]

    def test_the_refusal_downgrade_expects_the_released_state(self, tmp_path):
        """The downgrade runs *after* our own RELEASED write — it must not
        re-verify against CLAIMED/EXECUTING and silently no-op."""
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp, refuse=WorktreeReleaseRefused("dirty"))

        out = _reaper(disp, broker, ledger=tmp_path / "l.jsonl").reap(
            reap_idle_hours=6.0, dry_run=False
        )

        assert out[0]["outcome"] == "orphaned"
        row = disp.rows()["t-a"]
        assert row["state"] == "ORPHANED"
        assert "refused" in row["orphaned_reason"]


# =========================================================================== #
# HIGH-3 — the registry GC re-reads and merges instead of clobbering
# =========================================================================== #
class TestRegistryGcDoesNotClobberConcurrentWrites:
    """``collect()`` used to load the whole file, pop across N fsync'd archive
    appends, then write the whole stale snapshot back — the exact hazard the
    reaper's own docstring warns about."""

    def _gc(self, disp, tmp_path, *, days: int = 90) -> CodexRegistryGc:
        return CodexRegistryGc(disp, hermes_home=tmp_path, max_terminal_age_days=days)

    def test_a_row_added_during_the_sweep_survives(self, tmp_path):
        rows = {
            "t-old1": _row("sid-1", None, state="RELEASED", released_at=_iso(days=200)),
            "t-old2": _row("sid-2", None, state="RELEASED", released_at=_iso(days=200)),
        }
        disp = _FakeDispatcher(tmp_path, rows)
        gc = self._gc(disp, tmp_path)

        # The gateway allocates a brand-new session while the GC is archiving.
        added = {"done": False}
        real_archive = gc._archive_row

        def _archive_then_gateway_writes(thread_id, row, reason):
            ok = real_archive(thread_id, row, reason)
            if not added["done"]:
                added["done"] = True
                state = disp._load_state()
                state["sessions"]["t-new"] = _row("sid-new", None, state="EXECUTING")
                disp._write_state(state)
            return ok

        gc._archive_row = _archive_then_gateway_writes

        out = gc.collect(terminal_states=GC_ELIGIBLE_TERMINAL_STATES)

        assert [d["outcome"] for d in out] == ["retired", "retired"]
        assert set(disp.rows()) == {"t-new"}, (
            "the concurrently-created session must not be reverted"
        )

    def test_a_concurrent_edit_to_another_row_survives(self, tmp_path):
        rows = {
            "t-old": _row("sid-1", None, state="RELEASED", released_at=_iso(days=200)),
            "t-live": _row("sid-live", None, state="EXECUTING"),
        }
        disp = _FakeDispatcher(tmp_path, rows)
        gc = self._gc(disp, tmp_path)
        real_archive = gc._archive_row

        def _archive_then_message_arrives(thread_id, row, reason):
            ok = real_archive(thread_id, row, reason)
            state = disp._load_state()
            state["sessions"]["t-live"]["last_message_id"] = "m-999"
            disp._write_state(state)
            return ok

        gc._archive_row = _archive_then_message_arrives

        gc.collect(terminal_states=GC_ELIGIBLE_TERMINAL_STATES)

        assert disp.rows()["t-live"]["last_message_id"] == "m-999"

    def test_a_row_revived_during_the_sweep_is_not_deleted(self, tmp_path):
        """Archive succeeded, then the row stopped being terminal."""
        rows = {"t-old": _row("sid-1", None, state="RELEASED",
                              released_at=_iso(days=200))}
        disp = _FakeDispatcher(tmp_path, rows)
        gc = self._gc(disp, tmp_path)
        real_archive = gc._archive_row

        def _archive_then_revive(thread_id, row, reason):
            ok = real_archive(thread_id, row, reason)
            state = disp._load_state()
            state["sessions"]["t-old"]["state"] = "EXECUTING"
            disp._write_state(state)
            return ok

        gc._archive_row = _archive_then_revive

        out = gc.collect(terminal_states=GC_ELIGIBLE_TERMINAL_STATES)

        assert out[0]["outcome"] == "kept"
        assert "changed under us" in out[0]["reason"]
        assert "t-old" in disp.rows()

    def test_a_sid_swap_during_the_sweep_is_not_deleted(self, tmp_path):
        rows = {"t-old": _row("sid-1", None, state="RELEASED",
                              released_at=_iso(days=200))}
        disp = _FakeDispatcher(tmp_path, rows)
        gc = self._gc(disp, tmp_path)
        real_archive = gc._archive_row

        def _archive_then_swap(thread_id, row, reason):
            ok = real_archive(thread_id, row, reason)
            state = disp._load_state()
            state["sessions"]["t-old"]["session_id"] = "sid-other"
            disp._write_state(state)
            return ok

        gc._archive_row = _archive_then_swap

        out = gc.collect(terminal_states=GC_ELIGIBLE_TERMINAL_STATES)

        assert out[0]["outcome"] == "kept"
        assert "t-old" in disp.rows()

    def test_each_retirement_is_its_own_write(self, tmp_path):
        rows = {
            f"t-{i}": _row(f"sid-{i}", None, state="RELEASED",
                           released_at=_iso(days=200))
            for i in range(3)
        }
        disp = _FakeDispatcher(tmp_path, rows)

        self._gc(disp, tmp_path).collect(terminal_states=GC_ELIGIBLE_TERMINAL_STATES)

        assert disp.writes == 3, "one read-modify-write per retirement"
        assert disp.rows() == {}


# =========================================================================== #
# MED-1 — the watcher's own PR lookup fails closed
# =========================================================================== #
class TestWatcherPrLookupFailsClosed:
    @pytest.mark.asyncio
    async def test_a_failed_lookup_skips_gc_rather_than_passing_an_empty_set(
        self, tmp_path
    ):
        disp = _FakeDispatcher(tmp_path, {"t1": _row("sid-a", None)})
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0

        def _boom():
            raise RuntimeError("gh auth expired")

        w = CodexGcWatcher(
            dispatcher=disp, worktree_broker=broker, gh_list_open_branches=_boom,
        )

        await w._tick()

        broker.gc.assert_not_called()
        broker.reap_deleted.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_failed_lookup_also_blocks_every_release(self, tmp_path):
        """The reaper half: same lookup, same tick, nothing released."""
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=99))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = MagicMock()
        broker.gc.return_value = []
        broker.reap_deleted.return_value = 0

        def _boom():
            raise RuntimeError("gh auth expired")

        w = CodexGcWatcher(
            dispatcher=disp, worktree_broker=broker, gh_list_open_branches=_boom,
            reap_idle_hours=6.0, reap_armed=True, reap_confirmed=True,
        )

        await w._tick()

        assert disp.rows()["t-a"]["state"] == "EXECUTING"
        broker.release_nonforce.assert_not_called()

    def test_the_default_lookup_raises_so_the_reaper_contract_can_fire(self):
        """The reaper's fail-closed path is only reachable if the callable it is
        handed actually raises — the default one used to swallow everything."""
        from gateway import codex_gc_watcher as mod

        assert issubclass(mod.GhLookupError, Exception)
        assert mod.CodexGcWatcher(
            dispatcher=MagicMock(), worktree_broker=MagicMock(),
        )._gh_list_open_branches is mod._gh_list_open_branches


# =========================================================================== #
# MED-2 — surviving-mutant coverage on the process-owner probe
# =========================================================================== #
class TestProcessOwnerProbeMutants:
    """Two conditions in ``_scan_process_owners`` had no test pinning them."""

    def test_zero_readable_pids_is_inconclusive(self, tmp_path, monkeypatch):
        """``probe_ok = readable > 0``: /proc listed pids but none was readable.

        This is the shape of a hardened container or a foreign-user pid space:
        listdir succeeds, every readlink is EPERM.  We cannot prove the absence
        of an owner, and absence is exactly what authorises a release.
        """
        reaper = CodexSessionReaper(
            dispatcher_state=_FakeDispatcher(tmp_path),
            broker=MagicMock(),
            gh_open_branches_fn=lambda: set(),
        )
        monkeypatch.setattr(os, "listdir", lambda path: ["1", "2", "3"])
        monkeypatch.setattr(
            CodexSessionReaper, "_proc_cwd", staticmethod(lambda pid: [])
        )
        monkeypatch.setattr(
            CodexSessionReaper, "_proc_fds", staticmethod(lambda pid: ([], False, False))
        )

        owners, probe_ok = reaper._scan_process_owners({"sid": tmp_path / "wt"})

        assert owners == {"sid": []}
        assert probe_ok is False, "no readable pid means no proof of absence"

    def test_one_readable_pid_is_enough(self, tmp_path, monkeypatch):
        """Boundary pair: readable == 1 must be conclusive."""
        reaper = CodexSessionReaper(
            dispatcher_state=_FakeDispatcher(tmp_path),
            broker=MagicMock(),
            gh_open_branches_fn=lambda: set(),
        )
        monkeypatch.setattr(os, "listdir", lambda path: ["1", "2"])
        monkeypatch.setattr(
            CodexSessionReaper, "_proc_cwd",
            staticmethod(lambda pid: ["/elsewhere"] if pid == "1" else []),
        )
        monkeypatch.setattr(
            CodexSessionReaper, "_proc_fds", staticmethod(lambda pid: ([], False, False))
        )

        _owners, probe_ok = reaper._scan_process_owners({"sid": tmp_path / "wt"})

        assert probe_ok is True

    def test_an_inconclusive_scan_blocks_the_release_end_to_end(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        rows = {"t-a": _row("a", wt, last_message_at=_iso(hours=9))}
        disp = _FakeDispatcher(tmp_path, rows)
        broker = _RecordingBroker(disp)

        out = _reaper(
            disp, broker, owner_probe_ok=False, ledger=tmp_path / "l.jsonl"
        ).reap(reap_idle_hours=6.0, dry_run=False)

        assert out[0]["outcome"] == "skipped"
        assert broker.nonforce_calls == []

    def test_an_oversized_fd_table_disarms_the_probe(self, tmp_path, monkeypatch):
        """The >2048-fd path: we refuse to walk it, so we cannot prove absence."""
        from gateway import codex_session_reaper as mod

        fd_entries = [str(i) for i in range(mod._MAX_FDS_PER_PROC + 1)]
        monkeypatch.setattr(os, "listdir", lambda path: fd_entries)

        targets, truncated, readable = CodexSessionReaper._proc_fds("1")

        assert targets == []
        assert truncated is True
        assert readable is True

    def test_exactly_the_fd_limit_is_still_walked(self, tmp_path, monkeypatch):
        """Boundary pair for the mutant ``>`` vs ``>=``."""
        from gateway import codex_session_reaper as mod

        fd_entries = [str(i) for i in range(mod._MAX_FDS_PER_PROC)]
        monkeypatch.setattr(os, "listdir", lambda path: fd_entries)
        monkeypatch.setattr(os, "readlink", lambda path: "/dev/null")

        targets, truncated, readable = CodexSessionReaper._proc_fds("1")

        assert truncated is False
        assert readable is True
        assert len(targets) == mod._MAX_FDS_PER_PROC

    def test_a_truncated_scan_reports_not_ok_even_with_readable_pids(
        self, tmp_path, monkeypatch
    ):
        """Both halves of ``readable > 0 and not truncated`` are load-bearing."""
        reaper = CodexSessionReaper(
            dispatcher_state=_FakeDispatcher(tmp_path),
            broker=MagicMock(),
            gh_open_branches_fn=lambda: set(),
        )
        monkeypatch.setattr(os, "listdir", lambda path: ["1"])
        monkeypatch.setattr(
            CodexSessionReaper, "_proc_cwd", staticmethod(lambda pid: ["/elsewhere"])
        )
        monkeypatch.setattr(
            CodexSessionReaper, "_proc_fds", staticmethod(lambda pid: ([], True, True))
        )

        _owners, probe_ok = reaper._scan_process_owners({"sid": tmp_path / "wt"})

        assert probe_ok is False
