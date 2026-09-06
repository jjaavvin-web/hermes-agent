"""Tests for hermes_cli/resume.py — `hermes resume` live state."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hermes_cli import resume


# ──────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────


class FakeHTTP(resume.HonchoHTTP):
    """Tiny injected Honcho HTTP client for hermetic resume tests."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[tuple[str, str, object | None]] = []

    def request_json(self, method: str, path: str, *, body: object | None = None) -> Any:
        self.calls.append((method, path, body))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def project_cwd(tmp_path: Path) -> Path:
    cwd = tmp_path / "project"
    cwd.mkdir()
    return cwd


def _write_jsonl(path: Path, rows: list[dict[str, Any] | str]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if isinstance(row, str):
                handle.write(row + "\n")
            else:
                handle.write(json.dumps(row) + "\n")


def _write_bridge_log(
    path: Path,
    cwd: Path,
    *,
    session_id: str = "session-123",
    ai_peer: str = "ai-peer-1",
    messages_sent: int = 3,
    ts: str = "2026-06-24T10:00:00Z",
) -> Path:
    _write_jsonl(
        path,
        [
            {
                "msg": "upload_complete",
                "cwd": str(cwd),
                "honcho_session": session_id,
                "ai_peer": ai_peer,
                "messages_sent": messages_sent,
                "ts": ts,
            }
        ],
    )
    return path


def _session_path(session_id: str, suffix: str, *, workspace: str = resume.DEFAULT_WORKSPACE) -> str:
    return f"/v3/workspaces/{workspace}/sessions/{session_id}/{suffix}"


# ──────────────────────────────────────────────────────────────────────
# Bridge log selection
# ──────────────────────────────────────────────────────────────────────


class TestBridgeLogSelection:
    def test_selects_newest_eligible_matching_cwd_and_skips_bad_rows(
        self, tmp_path: Path, project_cwd: Path
    ) -> None:
        other_cwd = tmp_path / "other"
        other_cwd.mkdir()
        log_path = tmp_path / "bridge.log"
        _write_jsonl(
            log_path,
            [
                "not valid json",
                {
                    "msg": "upload_complete",
                    "cwd": str(project_cwd),
                    "honcho_session": "older-session",
                    "ai_peer": "ai-old",
                    "messages_sent": 2,
                    "ts": "2026-06-24T09:00:00Z",
                },
                {
                    "msg": "upload_complete",
                    "cwd": str(project_cwd),
                    "honcho_session": "zero-message-session",
                    "ai_peer": "ai-zero",
                    "messages_sent": 0,
                    "ts": "2026-06-24T12:00:00Z",
                },
                {
                    "msg": "upload_complete",
                    "cwd": str(other_cwd),
                    "honcho_session": "other-cwd-session",
                    "ai_peer": "ai-other",
                    "messages_sent": 9,
                    "ts": "2026-06-24T13:00:00Z",
                },
                {
                    "msg": "upload_complete",
                    "cwd": str(project_cwd),
                    "ai_peer": "ai-missing-session",
                    "messages_sent": 9,
                    "ts": "2026-06-24T14:00:00Z",
                },
                {
                    "msg": "upload_complete",
                    "cwd": str(project_cwd),
                    "honcho_session": "newest-eligible-session",
                    "ai_peer": "ai-new",
                    "messages_sent": 7,
                    "ts": "2026-06-24T11:00:00Z",
                },
            ],
        )

        selected = resume.select_latest_session_from_bridge_log(log_path=log_path, cwd=project_cwd)

        assert selected == resume.BridgeSelection(
            session_id="newest-eligible-session",
            ai_peer="ai-new",
            cwd=str(project_cwd.resolve()),
            ts="2026-06-24T11:00:00Z",
            messages_sent=7,
        )

    def test_missing_log_file_raises_resume_error(self, tmp_path: Path, project_cwd: Path) -> None:
        missing_log = tmp_path / "missing.log"

        with pytest.raises(resume.ResumeError, match="Bridge log not found"):
            resume.select_latest_session_from_bridge_log(log_path=missing_log, cwd=project_cwd)

    def test_no_eligible_row_for_cwd_raises_resume_error(self, tmp_path: Path, project_cwd: Path) -> None:
        other_cwd = tmp_path / "other"
        other_cwd.mkdir()
        log_path = tmp_path / "bridge.log"
        _write_jsonl(
            log_path,
            [
                {
                    "msg": "upload_complete",
                    "cwd": str(other_cwd),
                    "honcho_session": "other-session",
                    "messages_sent": 3,
                    "ts": "2026-06-24T10:00:00Z",
                },
                {
                    "msg": "upload_complete",
                    "cwd": str(project_cwd),
                    "honcho_session": "zero-session",
                    "messages_sent": 0,
                    "ts": "2026-06-24T11:00:00Z",
                },
                {
                    "msg": "upload_complete",
                    "cwd": str(project_cwd),
                    "messages_sent": 5,
                    "ts": "2026-06-24T12:00:00Z",
                },
            ],
        )

        with pytest.raises(resume.ResumeError, match="No Honcho upload_complete entry"):
            resume.select_latest_session_from_bridge_log(log_path=log_path, cwd=project_cwd)


# ──────────────────────────────────────────────────────────────────────
# Timestamp parsing
# ──────────────────────────────────────────────────────────────────────


class TestParseTs:
    def test_zulu_timestamp_parses_to_aware_utc_datetime(self) -> None:
        parsed = resume._parse_ts("2026-06-24T10:00:00Z")

        assert parsed == datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
        assert parsed.tzinfo is not None

    def test_naive_timestamp_gets_utc_timezone(self) -> None:
        parsed = resume._parse_ts("2026-06-24T10:00:00")

        assert parsed == datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
        assert parsed.tzinfo is timezone.utc

    @pytest.mark.parametrize("value", ["", "not-a-date"])
    def test_empty_or_garbage_timestamp_returns_datetime_min_utc(self, value: str) -> None:
        assert resume._parse_ts(value) == datetime.min.replace(tzinfo=timezone.utc)


# ──────────────────────────────────────────────────────────────────────
# read_resume_state
# ──────────────────────────────────────────────────────────────────────


class TestReadResumeState:
    def test_summary_path_uses_short_summary_and_omits_long_summary_when_not_full(
        self, tmp_path: Path, project_cwd: Path
    ) -> None:
        log_path = _write_bridge_log(tmp_path / "bridge.log", project_cwd)
        http = FakeHTTP(
            {
                "/health": {"status": "ok"},
                _session_path("session-123", "summaries"): {
                    "short_summary": {"content": "did X and Y"},
                    "long_summary": {"content": "long..."},
                },
                _session_path("session-123", "context"): {
                    "messages": [
                        {
                            "created_at": "2026-06-24T09:59:00Z",
                            "peer_id": "human",
                            "content": "hello",
                        }
                    ]
                },
            }
        )

        state = resume.read_resume_state(cwd=project_cwd, log_path=log_path, http=http, full=False)

        assert state.source == "summary"
        assert state.freshness == "summary"
        assert state.short_summary == "did X and Y"
        assert state.long_summary is None
        assert state.session_id == "session-123"
        assert state.messages_sent == 3
        assert [path for _, path, _ in http.calls] == [
            "/health",
            _session_path("session-123", "summaries"),
            _session_path("session-123", "context"),
        ]

    def test_summary_path_populates_long_summary_when_full(
        self, tmp_path: Path, project_cwd: Path
    ) -> None:
        log_path = _write_bridge_log(tmp_path / "bridge.log", project_cwd)
        http = FakeHTTP(
            {
                "/health": {"status": "healthy"},
                _session_path("session-123", "summaries"): {
                    "short_summary": {"content": "did X and Y"},
                    "long_summary": {"content": "long..."},
                },
                _session_path("session-123", "context"): {"messages": []},
            }
        )

        state = resume.read_resume_state(cwd=project_cwd, log_path=log_path, http=http, full=True)

        assert state.source == "summary"
        assert state.short_summary == "did X and Y"
        assert state.long_summary == "long..."

    def test_raw_messages_fallback_when_summary_deriver_lags(
        self, tmp_path: Path, project_cwd: Path
    ) -> None:
        log_path = _write_bridge_log(tmp_path / "bridge.log", project_cwd)
        http = FakeHTTP(
            {
                "/health": {"status": "ok"},
                _session_path("session-123", "summaries"): {},
                _session_path("session-123", "context"): {
                    "messages": [
                        {
                            "created_at": "2026-06-24T10:02:00Z",
                            "peer_id": "ai-peer-1",
                            "content": "latest useful answer",
                        }
                    ]
                },
            }
        )

        state = resume.read_resume_state(cwd=project_cwd, log_path=log_path, http=http)

        assert state.source == "raw_messages"
        assert state.freshness == "summaries not ready; showing raw latest session messages"
        assert state.short_summary is None
        assert state.recent_messages == [
            {
                "created_at": "2026-06-24T10:02:00Z",
                "peer_id": "ai-peer-1",
                "content": "latest useful answer",
            }
        ]

    def test_no_summary_and_no_raw_messages_raises_resume_error(
        self, tmp_path: Path, project_cwd: Path
    ) -> None:
        log_path = _write_bridge_log(tmp_path / "bridge.log", project_cwd)
        http = FakeHTTP(
            {
                "/health": {"status": "ok"},
                _session_path("session-123", "summaries"): {},
                _session_path("session-123", "context"): {"messages": []},
            }
        )

        with pytest.raises(resume.ResumeError, match="no summary and no raw messages"):
            resume.read_resume_state(cwd=project_cwd, log_path=log_path, http=http)

    def test_unhealthy_honcho_raises_and_does_not_fetch_session_endpoints(
        self, tmp_path: Path, project_cwd: Path
    ) -> None:
        log_path = _write_bridge_log(tmp_path / "bridge.log", project_cwd)
        http = FakeHTTP({"/health": {"status": "degraded"}})

        with pytest.raises(resume.HonchoUnreachableError, match="health returned"):
            resume.read_resume_state(cwd=project_cwd, log_path=log_path, http=http)

        assert http.calls == [("GET", "/health", None)]


# ──────────────────────────────────────────────────────────────────────
# run exit codes / rendering
# ──────────────────────────────────────────────────────────────────────


class TestRun:
    def test_json_success_returns_zero_and_prints_session_id(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_read_resume_state(**kwargs: object) -> resume.ResumeState:
            return resume.ResumeState(
                cwd="/tmp/project",
                workspace="hermes",
                session_id="session-json",
                ai_peer="ai-peer",
                bridge_ts="2026-06-24T10:00:00Z",
                messages_sent=5,
                session_created_at="2026-06-24T09:00:00Z",
                source="summary",
                freshness="summary",
                short_summary="did X and Y",
                long_summary="long...",
            )

        monkeypatch.setattr(resume, "read_resume_state", fake_read_resume_state)
        args = argparse.Namespace(cwd="/tmp/project", honcho_base_url=None, full=False, json=True)

        exit_code = resume.run(args)
        captured = capsys.readouterr()

        assert exit_code == 0
        payload = json.loads(captured.out)
        assert payload["session_id"] == "session-json"
        assert "long_summary" not in payload
        assert captured.err == ""

    def test_text_success_returns_zero_and_prints_project_and_summary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_read_resume_state(**kwargs: object) -> resume.ResumeState:
            return resume.ResumeState(
                cwd="/tmp/project",
                workspace="hermes",
                session_id="session-text",
                ai_peer="ai-peer",
                bridge_ts="2026-06-24T10:00:00Z",
                messages_sent=5,
                session_created_at="2026-06-24T09:00:00Z",
                source="summary",
                freshness="summary",
                short_summary="did X and Y",
                long_summary="long...",
            )

        monkeypatch.setattr(resume, "read_resume_state", fake_read_resume_state)
        args = argparse.Namespace(cwd="/tmp/project", honcho_base_url=None, full=False, json=False)

        exit_code = resume.run(args)
        captured = capsys.readouterr()

        assert exit_code == 0
        assert "Project: /tmp/project" in captured.out
        assert "did X and Y" in captured.out
        assert captured.err == ""

    def test_honcho_unreachable_returns_two_and_mentions_constant(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_read_resume_state(**kwargs: object) -> resume.ResumeState:
            raise resume.HonchoUnreachableError("down")

        monkeypatch.setattr(resume, "read_resume_state", fake_read_resume_state)
        args = argparse.Namespace(cwd="/tmp/project", honcho_base_url=None, full=False, json=False)

        exit_code = resume.run(args)
        captured = capsys.readouterr()

        assert exit_code == 2
        assert resume.HONCHO_UNREACHABLE in captured.err
        assert captured.out == ""

    def test_resume_error_returns_one_and_prints_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_read_resume_state(**kwargs: object) -> resume.ResumeState:
            raise resume.ResumeError("boom")

        monkeypatch.setattr(resume, "read_resume_state", fake_read_resume_state)
        args = argparse.Namespace(cwd="/tmp/project", honcho_base_url=None, full=False, json=False)

        exit_code = resume.run(args)
        captured = capsys.readouterr()

        assert exit_code == 1
        assert "boom" in captured.err
        assert captured.out == ""


# Fork-main CLI integration coverage.
class FakeHonchoHTTP:
    def __init__(self, responses=None, fail_health: bool = False):
        self.responses = responses or {}
        self.fail_health = fail_health
        self.calls: list[tuple[str, str, object | None]] = []

    def request_json(self, method: str, path: str, *, body=None):
        self.calls.append((method, path, body))
        if path == "/health" and self.fail_health:
            raise OSError("connection refused")
        if path == "/health":
            return {"status": "ok"}
        if (method, path) in self.responses:
            return self.responses[(method, path)]
        raise AssertionError(f"unexpected request: {method} {path}")


def _write_legacy_bridge_log(path: Path, entries: list[dict]) -> None:
    lines = ["not json"]
    lines.extend(json.dumps(entry) for entry in entries)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_select_latest_bridge_entry_filters_by_cwd_and_messages_sent(tmp_path):
    from hermes_cli.resume import select_latest_session_from_bridge_log

    cwd = tmp_path / "project"
    cwd.mkdir()
    log_path = tmp_path / "claude-honcho-bridge.log"
    _write_legacy_bridge_log(
        log_path,
        [
            {
                "ts": "2026-05-30T09:00:00+00:00",
                "msg": "upload_complete",
                "honcho_session": "older-this-project",
                "ai_peer": "claude-code",
                "cwd": str(cwd),
                "messages_sent": 10,
            },
            {
                "ts": "2026-05-31T09:00:00+00:00",
                "msg": "upload_complete",
                "honcho_session": "wrong-project",
                "ai_peer": "claude-code",
                "cwd": str(tmp_path / "other"),
                "messages_sent": 99,
            },
            {
                "ts": "2026-05-31T10:00:00+00:00",
                "msg": "upload_complete",
                "honcho_session": "zero-messages",
                "ai_peer": "claude-code",
                "cwd": str(cwd),
                "messages_sent": 0,
            },
            {
                "ts": "2026-05-31T11:00:00+00:00",
                "msg": "upload_complete",
                "honcho_session": "latest-this-project",
                "ai_peer": "claude-code-opus",
                "cwd": str(cwd),
                "messages_sent": 3,
            },
        ],
    )

    selected = select_latest_session_from_bridge_log(log_path=log_path, cwd=cwd)

    assert selected.session_id == "latest-this-project"
    assert selected.ai_peer == "claude-code-opus"
    assert selected.cwd == str(cwd.resolve())
    assert selected.ts == "2026-05-31T11:00:00+00:00"
    assert selected.messages_sent == 3


def test_run_resume_renders_short_summary_and_never_calls_unscoped_chat(tmp_path, capsys, monkeypatch):
    from hermes_cli import resume

    cwd = tmp_path / "project"
    cwd.mkdir()
    log_path = tmp_path / "claude-honcho-bridge.log"
    _write_legacy_bridge_log(
        log_path,
        [
            {
                "ts": "2026-05-31T11:00:00+00:00",
                "msg": "upload_complete",
                "honcho_session": "session-123",
                "ai_peer": "claude-code",
                "cwd": str(cwd),
                "messages_sent": 42,
            },
        ],
    )
    http = FakeHonchoHTTP(
        {
            (
                "GET",
                "/v3/workspaces/hermes/sessions/session-123/summaries",
            ): {
                "id": "session-123",
                "short_summary": {
                    "content": "Phase-3 webhook sandbox commit 5694a9494; MVMS cleanup 2627->324; Phase-4 relay-goal underway."
                },
                "long_summary": {"content": "Longer detail"},
            },
            (
                "GET",
                "/v3/workspaces/hermes/sessions/session-123/context",
            ): {
                "id": "session-123",
                "messages": [
                    {"peer_id": "josep", "content": "go", "created_at": "2026-05-31T14:16:06Z"}
                ],
            },
        }
    )
    monkeypatch.setattr(resume, "DEFAULT_BRIDGE_LOG", log_path)
    monkeypatch.setattr(resume, "HonchoHTTP", lambda base_url, timeout=10.0: http)

    code = resume.run(
        argparse.Namespace(cwd=str(cwd), json=False, full=False, honcho_base_url="http://honcho.test")
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "Session: session-123" in captured.out
    assert "Created: 2026-05-31T14:16:06Z" in captured.out
    assert "Phase-3 webhook sandbox commit 5694a9494" in captured.out
    assert "/peers/claude-code/chat" not in [call[1] for call in http.calls]


def test_run_resume_json_full_includes_long_summary(tmp_path, capsys, monkeypatch):
    from hermes_cli import resume

    cwd = tmp_path / "project"
    cwd.mkdir()
    log_path = tmp_path / "bridge.log"
    _write_legacy_bridge_log(
        log_path,
        [
            {
                "ts": "2026-05-31T11:00:00+00:00",
                "msg": "upload_complete",
                "honcho_session": "session-json",
                "ai_peer": "claude-code",
                "cwd": str(cwd),
                "messages_sent": 5,
            }
        ],
    )
    http = FakeHonchoHTTP(
        {
            ("GET", "/v3/workspaces/hermes/sessions/session-json/summaries"): {
                "id": "session-json",
                "created_at": "2026-05-31T14:00:00Z",
                "short_summary": {"content": "Short live state"},
                "long_summary": {"content": "Long live state"},
            },
            ("GET", "/v3/workspaces/hermes/sessions/session-json/context"): {
                "id": "session-json",
                "messages": [
                    {"peer_id": "josep", "content": "hello", "created_at": "2026-05-31T13:59:00Z"}
                ],
            },
        }
    )
    monkeypatch.setattr(resume, "DEFAULT_BRIDGE_LOG", log_path)
    monkeypatch.setattr(resume, "HonchoHTTP", lambda base_url, timeout=10.0: http)

    code = resume.run(
        argparse.Namespace(cwd=str(cwd), json=True, full=True, honcho_base_url="http://honcho.test")
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "session-json"
    assert payload["source"] == "summary"
    assert payload["short_summary"] == "Short live state"
    assert payload["long_summary"] == "Long live state"
    assert payload["session_created_at"] == "2026-05-31T13:59:00Z"


def test_empty_summary_falls_back_to_raw_recent_messages_with_freshness_label(tmp_path, capsys, monkeypatch):
    from hermes_cli import resume

    cwd = tmp_path / "project"
    cwd.mkdir()
    log_path = tmp_path / "bridge.log"
    _write_legacy_bridge_log(
        log_path,
        [
            {
                "ts": "2026-05-31T11:00:00+00:00",
                "msg": "upload_complete",
                "honcho_session": "session-lagging",
                "ai_peer": "claude-code",
                "cwd": str(cwd),
                "messages_sent": 2,
            }
        ],
    )
    http = FakeHonchoHTTP(
        {
            ("GET", "/v3/workspaces/hermes/sessions/session-lagging/summaries"): {
                "id": "session-lagging",
                "short_summary": None,
                "long_summary": None,
            },
            ("GET", "/v3/workspaces/hermes/sessions/session-lagging/context"): {
                "id": "session-lagging",
                "messages": [
                    {"peer_id": "josep", "content": "Start phase 4", "created_at": "2026-05-31T12:00:00Z"},
                    {"peer_id": "claude-code", "content": "Building relay-goal orchestrator", "created_at": "2026-05-31T12:01:00Z"},
                ],
            },
        }
    )
    monkeypatch.setattr(resume, "DEFAULT_BRIDGE_LOG", log_path)
    monkeypatch.setattr(resume, "HonchoHTTP", lambda base_url, timeout=10.0: http)

    code = resume.run(
        argparse.Namespace(cwd=str(cwd), json=False, full=False, honcho_base_url="http://honcho.test")
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "Freshness: summaries not ready; showing raw latest session messages" in captured.out
    assert "Start phase 4" in captured.out
    assert "Building relay-goal orchestrator" in captured.out


def test_honcho_unreachable_fails_loud_without_stale_fallback(tmp_path, capsys, monkeypatch):
    from hermes_cli import resume

    cwd = tmp_path / "project"
    cwd.mkdir()
    log_path = tmp_path / "bridge.log"
    _write_legacy_bridge_log(
        log_path,
        [
            {
                "ts": "2026-05-31T11:00:00+00:00",
                "msg": "upload_complete",
                "honcho_session": "session-down",
                "ai_peer": "claude-code",
                "cwd": str(cwd),
                "messages_sent": 1,
            }
        ],
    )
    monkeypatch.setattr(resume, "DEFAULT_BRIDGE_LOG", log_path)
    monkeypatch.setattr(
        resume,
        "HonchoHTTP",
        lambda base_url, timeout=10.0: FakeHonchoHTTP(fail_health=True),
    )

    code = resume.run(
        argparse.Namespace(cwd=str(cwd), json=False, full=False, honcho_base_url="http://honcho.test")
    )

    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert "Honcho unreachable - cannot read live state" in captured.err
    assert "MEMORY" not in captured.err


def test_resume_is_declared_as_builtin_subcommand():
    from hermes_cli.main import _BUILTIN_SUBCOMMANDS

    assert "resume" in _BUILTIN_SUBCOMMANDS


def test_main_registers_resume_subcommand(monkeypatch, capsys):
    from hermes_cli import main as hermes_main
    from hermes_cli import resume

    called = {}

    def fake_run(args):
        called["args"] = args
        print("resume called")
        return 0

    monkeypatch.setattr(resume, "run", fake_run)
    with patch("sys.argv", ["hermes", "resume", "--cwd", "/tmp/project", "--json", "--full"]):
        with pytest.raises(SystemExit) as exc:
            hermes_main.main()

    assert exc.value.code == 0
    assert called["args"].cwd == "/tmp/project"
    assert called["args"].json is True
    assert called["args"].full is True
    assert "resume called" in capsys.readouterr().out
