"""Tests for hermes_cli.dashboard_codex_sessions (P4)."""

from __future__ import annotations

import json
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
