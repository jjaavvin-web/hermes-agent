"""Fake-adapter integration test for the CodexSessionDispatcher.

This test exercises CodexSessionDispatcher against a *real* WorktreeBroker
talking to a *real* (temp) git repo and a *real* filesystem-backed
codex_sessions.json — only tmux subprocess calls and the discord_send
callable are mocked. It is the highest-fidelity substitute available
for ISC-7, ISC-8, ISC-9, and ISC-14 of isas/P1-mvp.md when the runtime
hive cannot drive a live Discord bot.

Coverage:
  * ISC-7-equivalent: a "thread_create" event allocates a worktree + writes
    a session row + records a tmux launch call.
  * ISC-8-equivalent: a follow-up "thread_message" event forwards the text
    to the session via tmux send-keys (mocked) and updates last_message_at.
  * ISC-9-equivalent: a "thread_archive" event removes the worktree, frees
    the port, and removes the session row.
  * ISC-14-equivalent: four concurrent thread_create events produce four
    distinct worktrees, four distinct branches (codex/<sid>/<slug>), and
    four distinct port assignments — verified by `git worktree list` and
    by inspecting codex-ports.json.

Live-Discord verification (the original spec's Test Strategy probes) is
still required for full ISC sign-off and is the operator's responsibility
after this PR ships behind HERMES_CODEX_DISPATCHER=1.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure scripts/ is importable (so the dispatcher's siblings load cleanly).
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from agent.worktree_broker import WorktreeBroker  # noqa: E402
from gateway.codex_session_dispatcher import (  # noqa: E402
    CodexSessionDispatcher,
    ThreadEvent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_repo(repo_root: Path) -> None:
    """Initialise a minimal git repo with a `main` branch and one commit."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "p1-test@example"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "p1-test"],
        check=True,
        capture_output=True,
    )
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo_root), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-q", "-m", "seed"],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def env(tmp_path):
    """Real git repo + real hermes_home + dispatcher with mocked tmux/discord."""
    repo_root = tmp_path / "repo"
    hermes_home = tmp_path / "hermes_home"
    repo_root.mkdir()
    hermes_home.mkdir()
    _init_repo(repo_root)

    broker = WorktreeBroker(repo_root=repo_root, hermes_home=hermes_home)
    discord_send = AsyncMock()

    dispatcher = CodexSessionDispatcher(
        hermes_home=hermes_home,
        worktree_broker=broker,
        peer_review_orchestrator=None,
        merge_broker=None,
        discord_send=discord_send,
        kanban_complete=None,
        base_branch="main",  # temp repo has no `origin/main`
    )

    yield {
        "repo_root": repo_root,
        "hermes_home": hermes_home,
        "broker": broker,
        "dispatcher": dispatcher,
        "discord_send": discord_send,
    }


def _fake_subprocess_run(captured: List[list]):
    """Builder for a subprocess.run shim that records tmux calls.

    Real git calls (from the broker's `_git` helper) still go through to
    the real subprocess; tmux calls are intercepted, recorded, and given
    a synthetic CompletedProcess(returncode=0).
    """
    real = subprocess.run

    def shim(cmd, *args, **kwargs):
        if cmd and cmd[0] == "tmux":
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real(cmd, *args, **kwargs)

    return shim


# ---------------------------------------------------------------------------
# ISC-7 equivalent: thread_create allocates worktree + tmux + state row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_create_allocates_worktree_and_writes_row(env):
    tmux_calls: List[list] = []
    with patch(
        "gateway.codex_session_dispatcher.subprocess.run",
        side_effect=_fake_subprocess_run(tmux_calls),
    ):
        await env["dispatcher"].on_thread_create(
            ThreadEvent(thread_id="t1", channel_id="c1", isa_slug="p1-mvp")
        )

    state = json.loads((env["hermes_home"] / "codex_sessions.json").read_text())
    assert "t1" in state["sessions"], "session row not written for thread t1"
    row = state["sessions"]["t1"]
    sid = row["session_id"]

    wt_path = Path(row["worktree_path"])
    assert wt_path.exists(), f"worktree dir {wt_path} not created"
    assert (wt_path / ".git").exists(), "worktree .git missing"

    branches = subprocess.run(
        ["git", "-C", str(env["repo_root"]), "branch", "--list", "codex/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"codex/{sid}/p1-mvp" in branches, f"expected branch missing: {branches!r}"

    # Pivot Phase A: dispatcher no longer spawns tmux.
    tmux_new = [c for c in tmux_calls if len(c) > 1 and c[1] == "new-session"]
    assert not tmux_new, f"dispatcher should NOT call tmux new-session after pivot; got {tmux_calls!r}"
    assert row["tmux_session"] is None


# ---------------------------------------------------------------------------
# ISC-8 equivalent: follow-up message forwards via tmux send-keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_up_message_routes_to_existing_session(env):
    tmux_calls: List[list] = []
    with patch(
        "gateway.codex_session_dispatcher.subprocess.run",
        side_effect=_fake_subprocess_run(tmux_calls),
    ):
        await env["dispatcher"].on_thread_create(
            ThreadEvent(thread_id="t2", channel_id="c2", isa_slug="p1")
        )
        tmux_calls.clear()
        await env["dispatcher"].on_thread_message(
            ThreadEvent(
                thread_id="t2",
                channel_id="c2",
                message_id="m1",
                text="hello codex",
                author_id="u1",
            )
        )

    # Pivot Phase A: dispatcher does NOT forward via tmux send-keys.
    # The regular Hermes agent processes the message; dispatcher just
    # records metadata.
    send_keys = [c for c in tmux_calls if len(c) > 1 and c[1] == "send-keys"]
    assert not send_keys, f"dispatcher should NOT send-keys after pivot; got {tmux_calls!r}"

    state = json.loads((env["hermes_home"] / "codex_sessions.json").read_text())
    assert state["sessions"]["t2"]["last_message_id"] == "m1"
    assert state["sessions"]["t2"]["last_message_at"] is not None
    assert state["sessions"]["t2"]["state"] == "EXECUTING"


# ---------------------------------------------------------------------------
# ISC-9 equivalent: archive removes worktree + frees port + removes row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_releases_worktree_and_removes_row(env):
    tmux_calls: List[list] = []
    with patch(
        "gateway.codex_session_dispatcher.subprocess.run",
        side_effect=_fake_subprocess_run(tmux_calls),
    ):
        await env["dispatcher"].on_thread_create(
            ThreadEvent(thread_id="t3", channel_id="c3", isa_slug="p1")
        )
        state = json.loads((env["hermes_home"] / "codex_sessions.json").read_text())
        wt_path = Path(state["sessions"]["t3"]["worktree_path"])
        assert wt_path.exists()

        await env["dispatcher"].on_thread_archive(
            ThreadEvent(thread_id="t3", channel_id="c3")
        )

    state = json.loads((env["hermes_home"] / "codex_sessions.json").read_text())
    assert "t3" not in state["sessions"], "session row not removed on archive"
    assert not wt_path.exists(), f"worktree dir not removed: {wt_path}"

    ports = json.loads((env["hermes_home"] / "codex-ports.json").read_text())
    assert all(v is None for v in ports.values()), (
        f"ports not freed after archive: {ports!r}"
    )


# ---------------------------------------------------------------------------
# ISC-14 equivalent: four concurrent threads -> 4 worktrees + 4 branches + 4 ports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_concurrent_threads_no_collision(env):
    tmux_calls: List[list] = []
    with patch(
        "gateway.codex_session_dispatcher.subprocess.run",
        side_effect=_fake_subprocess_run(tmux_calls),
    ):
        for i in range(1, 5):
            await env["dispatcher"].on_thread_create(
                ThreadEvent(
                    thread_id=f"tc{i}",
                    channel_id=f"cc{i}",
                    isa_slug=f"slot{i}",
                )
            )

    state = json.loads((env["hermes_home"] / "codex_sessions.json").read_text())
    sessions = state["sessions"]
    assert len(sessions) == 4, f"expected 4 rows, got {len(sessions)}"

    # Distinct worktrees, distinct branches, distinct ports.
    sids = {row["session_id"] for row in sessions.values()}
    paths = {row["worktree_path"] for row in sessions.values()}
    ports = {row["port"] for row in sessions.values()}
    assert len(sids) == 4
    assert len(paths) == 4
    assert len(ports) == 4
    assert None not in ports, f"expected real ports for 4 sessions; got {ports!r}"

    # All four worktrees exist on disk.
    for row in sessions.values():
        assert Path(row["worktree_path"]).exists()

    # git -C repo branch --list 'codex/*' lists 4 branches.
    branches = subprocess.run(
        ["git", "-C", str(env["repo_root"]), "branch", "--list", "codex/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()
    assert len(branches) == 4, f"expected 4 codex/* branches, got {branches!r}"

    # codex-ports.json: exactly 4 ports claimed.
    ports_file = json.loads((env["hermes_home"] / "codex-ports.json").read_text())
    claimed = [v for v in ports_file.values() if v is not None]
    assert len(claimed) == 4, f"expected 4 claimed ports, got {ports_file!r}"


# ---------------------------------------------------------------------------
# Cleanup helper: extra coverage for the spec's idempotency claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_is_idempotent(env):
    tmux_calls: List[list] = []
    with patch(
        "gateway.codex_session_dispatcher.subprocess.run",
        side_effect=_fake_subprocess_run(tmux_calls),
    ):
        # Archive an unknown thread — must be a no-op, no exception.
        await env["dispatcher"].on_thread_archive(
            ThreadEvent(thread_id="never-existed", channel_id="?")
        )

    state = json.loads((env["hermes_home"] / "codex_sessions.json").read_text())
    assert state["sessions"] == {}


# ---------------------------------------------------------------------------
# TESTS-5 coverage: merge/finalize/revive/restart ordering edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_under_merge_conflict_keeps_session_merging_without_pr_meta(env):
    """Fake-adapter path: an APPROVE whose merge broker reports a conflict
    must not fake-finalize or leave watcher-visible PR metadata behind."""
    from agent.peer_review import Verdict

    dispatcher = env["dispatcher"]
    await dispatcher.on_thread_create(
        ThreadEvent(thread_id="merge-conflict", channel_id="chan", isa_slug="merge-conflict")
    )

    peer_review = MagicMock()
    peer_review.start = AsyncMock()
    peer_review.review = AsyncMock(
        return_value=Verdict(
            kind="APPROVE",
            rationale="looks mergeable except conflict",
            iteration=1,
            raw_capture="VERDICT: APPROVE",
            duration_sec=0.1,
            pane_id="review-pane",
        )
    )

    merge_broker = MagicMock()

    async def conflict_merge(**_kwargs):
        from agent.merge_broker import MergeResult

        return MergeResult(ok=False, error="merge conflict in gateway/codex_session_dispatcher.py")

    merge_broker.merge = conflict_merge
    dispatcher._peer_review = peer_review
    dispatcher._merge_broker = merge_broker

    await dispatcher.on_phase_verify("merge-conflict")

    row = dispatcher._load_state()["sessions"]["merge-conflict"]
    assert row["state"] == "MERGING"
    assert "pr_number" not in row
    assert "pr_url" not in row
    assert "pr_state" not in row
    assert any(
        "Merge failed" in call.args[1] and "merge conflict" in call.args[1]
        for call in env["discord_send"].await_args_list
    )


@pytest.mark.asyncio
async def test_revive_after_crash_archives_previous_isa_and_allocates_fresh_session(env):
    """Fake-adapter revive: after restart marks a missing worktree ORPHANED,
    /revive archives the old ISA and allocates a new row on the same thread."""
    await env["dispatcher"].on_thread_create(
        ThreadEvent(thread_id="revive-crash", channel_id="chan", isa_slug="revive-crash")
    )
    original = env["dispatcher"]._load_state()["sessions"]["revive-crash"]
    old_sid = original["session_id"]
    old_isa = Path(original["isa_path"])
    old_isa.write_text(old_isa.read_text(encoding="utf-8") + "\nold progress\n", encoding="utf-8")

    # Simulate a crash/manual cleanup where the registered worktree vanished.
    env["broker"].release(old_sid)
    restart_results = await env["dispatcher"].on_bot_restart()
    assert [r.status for r in restart_results] == ["orphaned"]
    assert env["dispatcher"]._load_state()["sessions"]["revive-crash"]["state"] == "ORPHANED"

    from gateway.codex_session_dispatcher import SlashContext

    response = await env["dispatcher"].slash_command(
        "revive",
        SlashContext(thread_id="revive-crash", channel_id="chan", options={}),
    )

    revived = env["dispatcher"]._load_state()["sessions"]["revive-crash"]
    assert "revived" in response.content.lower()
    assert revived["session_id"] != old_sid
    assert revived["state"] == "CLAIMED"
    assert Path(revived["worktree_path"]).exists()
    archive_files = list((old_isa.parent / "_ephemeral").glob("orphaned-*.md"))
    assert len(archive_files) == 1
    assert "old progress" in archive_files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_reaper_vs_in_flight_merge_ordering_preserves_merging_session(env):
    """Restart/reaper ordering: a MERGING row without a worktree is in-flight
    merge state and must not be downgraded to ORPHANED before the watcher can
    finalize it."""
    await env["dispatcher"].on_thread_create(
        ThreadEvent(thread_id="reaper-merge", channel_id="chan", isa_slug="reaper-merge")
    )
    state = env["dispatcher"]._load_state()
    row = state["sessions"]["reaper-merge"]
    sid = row["session_id"]
    row["state"] = "MERGING"
    row["pr_number"] = 77
    row["pr_url"] = "https://example/pr/77"
    row["pr_state"] = "OPEN"
    env["dispatcher"]._write_state(state)

    env["broker"].release(sid)

    restart_results = await env["dispatcher"].on_bot_restart()
    persisted = env["dispatcher"]._load_state()["sessions"]["reaper-merge"]
    assert restart_results == []
    assert persisted["state"] == "MERGING"
    assert persisted["pr_state"] == "OPEN"

    await env["dispatcher"].on_pr_merged(
        "reaper-merge",
        {
            "state": "MERGED",
            "mergedAt": "2026-06-24T12:00:00Z",
            "mergeCommit": {"oid": "facefeed"},
            "url": "https://example/pr/77",
            "number": 77,
        },
    )
    finalized = env["dispatcher"]._load_state()["sessions"]["reaper-merge"]
    assert finalized["state"] == "COMPLETE"
    assert finalized["pr_state"] == "MERGED"
    assert finalized["merge_commit_oid"] == "facefeed"
