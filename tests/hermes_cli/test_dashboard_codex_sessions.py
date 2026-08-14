"""Tests for hermes_cli.dashboard_codex_sessions (P4)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from hermes_cli import dashboard_codex_sessions as mod


@pytest.fixture
def app_with_router(tmp_path, monkeypatch):
    """Build a fresh FastAPI app with just the codex-sessions router
    + redirect all state paths to ``tmp_path``."""
    from fastapi import FastAPI
    app = FastAPI()
    monkeypatch.setattr(mod, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(mod, "_SESSIONS_PATH", tmp_path / "codex_sessions.json")
    monkeypatch.setattr(mod, "_REVIEW_STATE_PATH", tmp_path / "codex-review-state.json")
    monkeypatch.setattr(mod, "_PORTS_PATH", tmp_path / "codex-ports.json")
    monkeypatch.setattr(mod, "_AGENT_LOG_PATH", tmp_path / "agent.log")
    # Reset the cache so each test sees fresh state.
    monkeypatch.setattr(mod, "_SNAPSHOT_CACHE", None)
    app.include_router(mod.router)
    return app


def _seed(tmp_path: Path, threads: dict, ports_claimed: int = 0, review: dict = None):
    (tmp_path / "codex_sessions.json").write_text(
        json.dumps({"version": 1, "sessions": threads}),
        encoding="utf-8",
    )
    ports = {str(p): None for p in range(50000, 50008)}
    for i, (_, row) in enumerate(threads.items()):
        if i < ports_claimed and row.get("port"):
            ports[str(row["port"])] = row["session_id"]
    (tmp_path / "codex-ports.json").write_text(
        json.dumps(ports), encoding="utf-8",
    )
    if review is not None:
        (tmp_path / "codex-review-state.json").write_text(
            json.dumps({"version": 1, "sessions": review}),
            encoding="utf-8",
        )


def _make_row(sid: str, thread_id: str, **overrides) -> dict:
    row = {
        "session_id": sid,
        "thread_id": thread_id,
        "channel_id": "c1",
        "kanban_card_id": None,
        "worktree_path": "",  # off-disk by default; tests override per case
        "tmux_session": None,
        "isa_id": "test",
        "isa_path": "",
        "state": "EXECUTING",
        "paused": False,
        "queued_messages": [],
        "last_message_id": None,
        "last_message_at": None,
        "created_at": "2026-05-25T00:00:00+00:00",
        "review_round": 0,
        "port": 50000,
    }
    row.update(overrides)
    return row


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env=merged_env,
    )
    return result.stdout.strip()


def _git_date(ts: int) -> str:
    return f"{ts} +0000"


def _commit(repo: Path, filename: str, body: str, message: str, ts: int) -> str:
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", filename)
    env = {"GIT_AUTHOR_DATE": _git_date(ts), "GIT_COMMITTER_DATE": _git_date(ts)}
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")


def _river_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "river-repo"
    _init_git_repo(repo)
    _commit(repo, "trunk.txt", "one\n", "initial trunk", 1_700_000_000)
    _commit(repo, "trunk.txt", "one\ntwo\n", "second trunk", 1_700_000_100)

    _git(repo, "checkout", "-q", "-b", "feature/merged-pr")
    _commit(repo, "merged.txt", "merged work\n", "merged branch work", 1_700_000_200)
    _git(repo, "checkout", "-q", "main")
    _git(
        repo,
        "merge",
        "--no-ff",
        "feature/merged-pr",
        "-m",
        "Merge pull request #123 from feature/merged-pr",
        env={"GIT_COMMITTER_DATE": _git_date(1_700_000_300)},
    )

    _git(repo, "checkout", "-q", "-b", "feature/closed-lane")
    _commit(repo, "closed.txt", "closed lane\n", "closed lane work", 1_700_000_400)
    _git(repo, "checkout", "-q", "main")

    _git(repo, "checkout", "-q", "-b", "feature/open-lane")
    _commit(repo, "open.txt", "open lane\n", "open lane work", 1_700_000_500)
    _git(repo, "checkout", "-q", "main")
    return repo


def _point_river_at_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    sessions: dict | None = None,
) -> None:
    monkeypatch.setattr(mod, "__file__", str(repo / "hermes_cli" / "dashboard_codex_sessions.py"))
    sessions_path = tmp_path / "codex_sessions.json"
    sessions_path.write_text(
        json.dumps({"version": 1, "sessions": sessions or {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(mod, "_RIVER_CACHE", None)


# ── git river ─────────────────────────────────────────────────────────


def test_build_river_returns_current_shape_and_parses_pr_number(monkeypatch, tmp_path):
    repo = _river_repo(tmp_path)
    _point_river_at_repo(monkeypatch, tmp_path, repo)

    river = mod._build_river(trunk_n=8, branch_n=8)

    assert set(river) == {"scanned_at", "base", "trunk", "branches", "counts"}
    assert set(river["base"]) == {"ref", "sha", "total_commits"}
    assert river["base"]["ref"] == "main"
    assert river["base"]["total_commits"] == 4
    assert river["counts"] == {"trunk": 3, "branches": 2, "active": 0}

    trunk = river["trunk"]
    assert len(trunk) == 3
    assert {"sha", "full", "ts", "author", "subject", "pr", "age_rank"} <= set(trunk[0])
    assert trunk[0]["subject"] == "Merge pull request #123 from feature/merged-pr"
    assert trunk[0]["pr"] == 123
    assert [commit["age_rank"] for commit in trunk] == [0, 1, 2]

    branches = river["branches"]
    assert {branch["name"] for branch in branches} == {"feature/open-lane", "feature/closed-lane"}
    first_branch = branches[0]
    assert {
        "name",
        "short",
        "ahead",
        "fork_rank",
        "lead_commits",
        "ts",
        "recency",
        "active",
        "is_current",
        "thread_id",
        "pr_number",
        "pr_url",
        "merged",
    } <= set(first_branch)


def test_build_river_orders_branches_by_most_recent_commit_timestamp(monkeypatch, tmp_path):
    repo = _river_repo(tmp_path)
    _point_river_at_repo(monkeypatch, tmp_path, repo)

    branches = mod._build_river(trunk_n=8, branch_n=8)["branches"]

    assert [branch["name"] for branch in branches] == ["feature/open-lane", "feature/closed-lane"]
    assert [branch["ts"] for branch in branches] == [1_700_000_500, 1_700_000_400]
    assert all(branch["fork_rank"] == 0 for branch in branches)
    assert all(branch["ahead"] == 1 for branch in branches)


def test_build_river_sets_active_and_merged_flags_from_session_metadata(monkeypatch, tmp_path):
    repo = _river_repo(tmp_path)
    sessions = {
        "thread-closed": _make_row(
            "sid-closed",
            "thread-closed",
            head_branch="feature/closed-lane",
            pr_number=456,
            pr_url="https://example.invalid/pr/456",
            merged_at="2026-05-25T17:00:00Z",
        ),
        "thread-open": _make_row(
            "sid-open",
            "thread-open",
            head_branch="feature/open-lane",
            pr_number=789,
            pr_url="https://example.invalid/pr/789",
        ),
    }
    _point_river_at_repo(monkeypatch, tmp_path, repo, sessions)

    river = mod._build_river(trunk_n=8, branch_n=8)
    by_name = {branch["name"]: branch for branch in river["branches"]}

    closed = by_name["feature/closed-lane"]
    assert closed["active"] is True
    assert closed["merged"] is True
    assert closed["thread_id"] == "thread-closed"
    assert closed["pr_number"] == 456
    assert closed["pr_url"] == "https://example.invalid/pr/456"

    open_branch = by_name["feature/open-lane"]
    assert open_branch["active"] is True
    assert open_branch["merged"] is False
    assert open_branch["thread_id"] == "thread-open"
    assert open_branch["pr_number"] == 789
    assert open_branch["pr_url"] == "https://example.invalid/pr/789"

    # The current implementation only includes local branches ahead of trunk;
    # a branch already merged into first-parent trunk is represented by the
    # trunk merge commit, not by a branch lane.
    assert "feature/merged-pr" not in by_name
    assert river["counts"]["active"] == 2


def test_build_river_empty_repo_degrades_without_raising(monkeypatch, tmp_path):
    repo = tmp_path / "empty-repo"
    _init_git_repo(repo)
    _point_river_at_repo(monkeypatch, tmp_path, repo)

    river = mod._build_river(trunk_n=8, branch_n=8)

    assert set(river) == {"scanned_at", "base", "trunk", "branches", "error"}
    assert river["base"] is None
    assert river["trunk"] == []
    assert river["branches"] == []
    assert river["error"] == "no trunk ref found"


# ── expensive git endpoint caches ─────────────────────────────────────


def test_git_health_reuses_cached_shortstats(monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    sessions_path = tmp_path / "codex_sessions.json"
    sessions_path.write_text(
        json.dumps({
            "version": 1,
            "sessions": {
                "thread-1": _make_row(
                    "sid-1",
                    "thread-1",
                    worktree_path=str(wt),
                ),
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(mod, "_GIT_HEALTH_CACHE", None, raising=False)
    calls = []

    def fake_run(args, capture_output, text, timeout, **kwargs):
        calls.append(tuple(args))
        if args[3:] == ("status", "--porcelain"):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[3:] == ("diff", "--shortstat", "fork/main...HEAD"):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="1 file changed, 2 insertions(+), 1 deletion(-)\n",
                stderr="",
            )
        if args[3:] == ("rev-list", "--count", "fork/main..HEAD"):
            return subprocess.CompletedProcess(args, 0, stdout="1\n", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="unexpected")

    monkeypatch.setattr(subprocess, "run", fake_run)

    first = mod.git_health()
    second = mod.git_health()

    assert first == second
    shortstats = [call for call in calls if call[3:] == ("diff", "--shortstat", "fork/main...HEAD")]
    assert len(shortstats) == 1


def test_git_graph_reuses_cached_lane_shortstats(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    (repo / "hermes_cli").mkdir(parents=True)
    wt.mkdir()
    sessions_path = tmp_path / "codex_sessions.json"
    sessions_path.write_text(
        json.dumps({
            "version": 1,
            "sessions": {
                "thread-1": _make_row(
                    "sid-1",
                    "thread-1",
                    worktree_path=str(wt),
                    isa_slug="sid-one",
                ),
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "__file__", str(repo / "hermes_cli" / "dashboard_codex_sessions.py"))
    monkeypatch.setattr(mod, "_SESSIONS_PATH", sessions_path)
    monkeypatch.setattr(mod, "_GIT_GRAPH_CACHE", None, raising=False)
    calls = []

    def fake_run(args, capture_output, text, timeout, **kwargs):
        calls.append(tuple(args))
        git_args = tuple(args[3:])
        if git_args == ("rev-parse", "--verify", "main"):
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if git_args[:2] == ("rev-parse", "--verify"):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        if git_args == ("log", "--max-count=6", "--pretty=%h\x1f%s\x1f%cI", "main"):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="abc123\x1ftrunk\x1f2026-01-01T00:00:00+00:00\n",
                stderr="",
            )
        if git_args == ("rev-parse", "--short", "main"):
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
        if git_args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return subprocess.CompletedProcess(args, 0, stdout="feature/test\n", stderr="")
        if git_args == ("rev-parse", "--short", "HEAD"):
            return subprocess.CompletedProcess(args, 0, stdout="def456\n", stderr="")
        if git_args == ("merge-base", "main", "HEAD"):
            return subprocess.CompletedProcess(args, 0, stdout="abc123456\n", stderr="")
        if git_args == ("rev-list", "--count", "main..HEAD"):
            return subprocess.CompletedProcess(args, 0, stdout="1\n", stderr="")
        if git_args == ("rev-list", "--count", "HEAD..main"):
            return subprocess.CompletedProcess(args, 0, stdout="0\n", stderr="")
        if git_args == ("diff", "--shortstat", "main...HEAD"):
            return subprocess.CompletedProcess(args, 0, stdout="1 file changed, 2 insertions(+)\n", stderr="")
        if git_args == ("status", "--porcelain"):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if git_args == ("log", "--max-count=6", "--pretty=%h\x1f%s\x1f%cI", "main..HEAD"):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="def456\x1fwork\x1f2026-01-01T00:01:00+00:00\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="unexpected")

    monkeypatch.setattr(subprocess, "run", fake_run)

    first = mod.git_graph()
    second = mod.git_graph()

    assert first == second
    shortstats = [call for call in calls if call[3:] == ("diff", "--shortstat", "main...HEAD")]
    assert len(shortstats) == 2


# ── snapshot ───────────────────────────────────────────────────────────



class TestSnapshot:
    def test_empty_state(self, app_with_router, tmp_path):
        _seed(tmp_path, {})
        client = TestClient(app_with_router)
        r = client.get("/api/dashboard/codex-sessions")
        assert r.status_code == 200
        data = r.json()
        assert data["sessions"] == []
        assert data["counts"]["total"] == 0
        assert data["counts"]["ports_free"] == 8

    def test_two_sessions_aggregate(self, app_with_router, tmp_path):
        _seed(tmp_path, {
            "t1": _make_row("sid-a", "t1", state="EXECUTING", port=50000),
            "t2": _make_row("sid-b", "t2", state="MERGING", port=50001),
        }, ports_claimed=2)
        client = TestClient(app_with_router)
        data = client.get("/api/dashboard/codex-sessions").json()
        assert data["counts"]["total"] == 2
        assert data["counts"]["ports_claimed"] == 2
        assert data["counts"]["by_state"]["EXECUTING"] == 1
        assert data["counts"]["by_state"]["MERGING"] == 1

    def test_review_state_merged(self, app_with_router, tmp_path):
        _seed(
            tmp_path,
            {"t1": _make_row("sid-r", "t1")},
            review={"sid-r": {"iterations": 2, "reviews_today": 3,
                              "last_verdict": "REVISE"}},
        )
        client = TestClient(app_with_router)
        sess = client.get("/api/dashboard/codex-sessions").json()["sessions"][0]
        assert sess["review_iterations"] == 2
        assert sess["reviews_today"] == 3
        assert sess["last_verdict"] == "REVISE"

    def test_pr_meta_surfaced(self, app_with_router, tmp_path):
        """P4 Wave 2: PR meta fields (pr_number/pr_url/pr_state/etc.)
        must surface in the snapshot for the SPA tab."""
        _seed(tmp_path, {"t1": _make_row("sid-x", "t1",
            state="MERGING",
            pr_number=99,
            pr_url="https://example/pr/99",
            pr_state="OPEN",
            head_branch="codex/sid-x/task",
            merge_label="auto-merge",
            merge_requested_at="2026-05-25T16:00:00Z",
        )})
        client = TestClient(app_with_router)
        sess = client.get("/api/dashboard/codex-sessions").json()["sessions"][0]
        assert sess["pr_number"] == 99
        assert sess["pr_url"] == "https://example/pr/99"
        assert sess["pr_state"] == "OPEN"
        assert sess["head_branch"] == "codex/sid-x/task"
        assert sess["merge_label"] == "auto-merge"
        assert sess["merge_requested_at"] == "2026-05-25T16:00:00Z"

    def test_pr_meta_absent_for_pre_p35_rows(self, app_with_router, tmp_path):
        """Old rows without PR meta still come back; missing fields = None."""
        _seed(tmp_path, {"t1": _make_row("sid-y", "t1", state="EXECUTING")})
        client = TestClient(app_with_router)
        sess = client.get("/api/dashboard/codex-sessions").json()["sessions"][0]
        assert sess["pr_number"] is None
        assert sess["pr_url"] is None
        assert sess["pr_state"] is None

    def test_merged_session_carries_merged_at_and_oid(self, app_with_router, tmp_path):
        _seed(tmp_path, {"t1": _make_row("sid-m", "t1",
            state="COMPLETE",
            pr_number=42,
            pr_state="MERGED",
            merged_at="2026-05-25T17:00:00Z",
            merge_commit_oid="deadbeef",
        )})
        client = TestClient(app_with_router)
        sess = client.get("/api/dashboard/codex-sessions").json()["sessions"][0]
        assert sess["pr_state"] == "MERGED"
        assert sess["merged_at"] == "2026-05-25T17:00:00Z"
        assert sess["merge_commit_oid"] == "deadbeef"


# ── detail ────────────────────────────────────────────────────────────


class TestDetail:
    def test_404_for_unknown_sid(self, app_with_router, tmp_path):
        _seed(tmp_path, {})
        client = TestClient(app_with_router)
        assert client.get("/api/dashboard/codex-sessions/nope").status_code == 404

    def test_returns_isa_verbatim(self, app_with_router, tmp_path):
        isa = tmp_path / "isa.md"
        isa.write_text("---\nphase: execute\n---\n## Problem\nbody", encoding="utf-8")
        _seed(tmp_path, {
            "t1": _make_row("sid-d", "t1", isa_path=str(isa)),
        })
        client = TestClient(app_with_router)
        data = client.get("/api/dashboard/codex-sessions/sid-d").json()
        assert "phase: execute" in data["isa_verbatim"]
        assert data["row"]["session_id"] == "sid-d"
        assert data["thread_id"] == "t1"


# ── log tail ──────────────────────────────────────────────────────────


class TestLog:
    def test_404_for_unknown_sid(self, app_with_router, tmp_path):
        _seed(tmp_path, {})
        client = TestClient(app_with_router)
        assert client.get("/api/dashboard/codex-sessions/nope/log").status_code == 404

    def test_filters_to_thread_id(self, app_with_router, tmp_path):
        _seed(tmp_path, {
            "1234567890": _make_row("sid-l", "1234567890"),
        })
        (tmp_path / "agent.log").write_text(
            "line not for us\n"
            "chat=999 something\n"
            "chat=1234567890 first match\n"
            "chat=1234567890 second match\n"
            "other line\n",
            encoding="utf-8",
        )
        client = TestClient(app_with_router)
        data = client.get("/api/dashboard/codex-sessions/sid-l/log").json()
        assert data["sid"] == "sid-l"
        assert len(data["lines"]) == 2
        assert all("1234567890" in line for line in data["lines"])


# ── pause / resume ────────────────────────────────────────────────────


class TestPauseResume:
    def test_pause_sets_flag(self, app_with_router, tmp_path):
        _seed(tmp_path, {"t1": _make_row("sid-p", "t1")})
        client = TestClient(app_with_router)
        assert client.post("/api/dashboard/codex-sessions/sid-p/pause").status_code == 200
        # State file should reflect the change.
        sessions = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert sessions["sessions"]["t1"]["paused"] is True

    def test_resume_clears_flag(self, app_with_router, tmp_path):
        _seed(tmp_path, {"t1": _make_row("sid-r", "t1", paused=True)})
        client = TestClient(app_with_router)
        assert client.post("/api/dashboard/codex-sessions/sid-r/resume").status_code == 200
        sessions = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert sessions["sessions"]["t1"]["paused"] is False


# ── destructive: kill / force-merge ────────────────────────────────────


class TestKill:
    def test_wrong_token_returns_422_with_expected_in_body(self, app_with_router, tmp_path):
        _seed(tmp_path, {"t1": _make_row("sid-k", "t1")})
        client = TestClient(app_with_router)
        r = client.post(
            "/api/dashboard/codex-sessions/sid-k/kill",
            json={"confirm": "WRONG"},
        )
        assert r.status_code == 422
        body = r.json()
        # FastAPI wraps the detail under "detail"
        detail = body.get("detail")
        assert detail["expected"] == "KILL_CODEX_SESSION"
        assert detail["example"] == {"confirm": "KILL_CODEX_SESSION"}

    def test_correct_token_drops_row(self, app_with_router, tmp_path):
        _seed(tmp_path, {"t1": _make_row("sid-k", "t1")})
        client = TestClient(app_with_router)
        with patch("hermes_cli.dashboard_codex_sessions.subprocess.run"):
            r = client.post(
                "/api/dashboard/codex-sessions/sid-k/kill",
                json={"confirm": "KILL_CODEX_SESSION"},
            )
        assert r.status_code == 200
        sessions = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert "t1" not in sessions["sessions"]


class TestForceMerge:
    def test_wrong_token_returns_422(self, app_with_router, tmp_path):
        _seed(tmp_path, {"t1": _make_row("sid-fm", "t1")})
        client = TestClient(app_with_router)
        r = client.post(
            "/api/dashboard/codex-sessions/sid-fm/force-merge",
            json={"confirm": "wrong"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["expected"] == "FORCE_MERGE_CODEX_SESSION"

    def test_correct_token_marks_merging(self, app_with_router, tmp_path):
        _seed(tmp_path, {"t1": _make_row("sid-fm", "t1", state="EXECUTING")})
        client = TestClient(app_with_router)
        r = client.post(
            "/api/dashboard/codex-sessions/sid-fm/force-merge",
            json={"confirm": "FORCE_MERGE_CODEX_SESSION"},
        )
        assert r.status_code == 200
        sessions = json.loads((tmp_path / "codex_sessions.json").read_text())
        assert sessions["sessions"]["t1"]["state"] == "MERGING"
        assert sessions["sessions"]["t1"]["force_merge_requested_at"]
