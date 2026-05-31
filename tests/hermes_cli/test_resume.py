from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


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


def _write_bridge_log(path: Path, entries: list[dict]) -> None:
    lines = ["not json"]
    lines.extend(json.dumps(entry) for entry in entries)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_select_latest_bridge_entry_filters_by_cwd_and_messages_sent(tmp_path):
    from hermes_cli.resume import select_latest_session_from_bridge_log

    cwd = tmp_path / "project"
    cwd.mkdir()
    log_path = tmp_path / "claude-honcho-bridge.log"
    _write_bridge_log(
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
    _write_bridge_log(
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
    _write_bridge_log(
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
    _write_bridge_log(
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
    _write_bridge_log(
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
