"""Dedicated tests for the `hermes checkpoints` CLI subcommand."""

from __future__ import annotations

import argparse
import builtins
from datetime import datetime

import pytest

from hermes_cli import checkpoints


def _status(total_size_bytes: int = 4096, legacy_archives: list[dict] | None = None) -> dict:
    return {
        "base": "/fake/base",
        "store_size_bytes": total_size_bytes,
        "legacy_size_bytes": 0,
        "total_size_bytes": total_size_bytes,
        "project_count": 2 if total_size_bytes else 0,
        "projects": [],
        "legacy_archives": legacy_archives or [],
    }


def test_fmt_bytes_uses_expected_unit_thresholds() -> None:
    assert checkpoints._fmt_bytes(0) == "0 B"
    assert checkpoints._fmt_bytes(512) == "512 B"
    assert checkpoints._fmt_bytes(1024) == "1.0 KB"
    assert checkpoints._fmt_bytes(1536) == "1.5 KB"
    assert checkpoints._fmt_bytes(1048576) == "1.0 MB"
    assert checkpoints._fmt_bytes(1073741824) == "1.0 GB"


def test_fmt_age_uses_expected_relative_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000_000.0
    monkeypatch.setattr(checkpoints.time, "time", lambda: now)

    assert checkpoints._fmt_age(now + 1) == "now"
    assert checkpoints._fmt_age(now - 30) == "30s ago"
    assert checkpoints._fmt_age(now - 5 * 60) == "5m ago"
    assert checkpoints._fmt_age(now - 2 * 3600) == "2h ago"
    assert checkpoints._fmt_age(now - 3 * 86400) == "3d ago"
    assert checkpoints._fmt_age("garbage") == "—"
    assert checkpoints._fmt_age(None) == "—"


def test_fmt_ts_handles_missing_invalid_and_valid_epochs() -> None:
    epoch = 1_700_000_000

    assert checkpoints._fmt_ts(None) == "—"
    assert checkpoints._fmt_ts("nan-ish") == "—"
    assert checkpoints._fmt_ts(epoch) == datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("y", True),
        ("yes", True),
        ("n", False),
        ("", False),
    ],
)
def test_confirm_accepts_only_explicit_yes(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt: response)

    assert checkpoints._confirm("x") is expected


def test_confirm_returns_false_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", raise_eof)

    assert checkpoints._confirm("x") is False


def test_cmd_clear_aborts_without_calling_clear_all(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("tools.checkpoint_manager.store_status", lambda: _status())
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", "/fake/base")
    monkeypatch.setattr(checkpoints, "_confirm", lambda *_args: False)
    monkeypatch.setattr("tools.checkpoint_manager.clear_all", lambda: calls.append("clear_all"))

    rc = checkpoints.cmd_clear(argparse.Namespace(force=False))

    assert rc == 1
    assert calls == []
    assert "Aborted." in capsys.readouterr().out


def test_cmd_clear_force_bypasses_prompt_and_clears(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("tools.checkpoint_manager.store_status", lambda: _status())
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", "/fake/base")
    monkeypatch.setattr(checkpoints, "_confirm", lambda *_args: pytest.fail("prompt should be bypassed"))

    def fake_clear_all() -> dict[str, object]:
        calls.append("clear_all")
        return {"deleted": True, "bytes_freed": 4096}

    monkeypatch.setattr("tools.checkpoint_manager.clear_all", fake_clear_all)

    rc = checkpoints.cmd_clear(argparse.Namespace(force=True))

    assert rc == 0
    assert calls == ["clear_all"]
    assert "Cleared." in capsys.readouterr().out


def test_cmd_clear_empty_missing_base_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    calls: list[str] = []
    missing_base = tmp_path / "missing-checkpoints-base"
    monkeypatch.setattr("tools.checkpoint_manager.store_status", lambda: _status(total_size_bytes=0))
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", missing_base)
    monkeypatch.setattr("tools.checkpoint_manager.clear_all", lambda: calls.append("clear_all"))

    rc = checkpoints.cmd_clear(argparse.Namespace(force=False))

    assert rc == 0
    assert calls == []
    assert "Nothing to clear" in capsys.readouterr().out


def test_cmd_clear_legacy_no_archives_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("tools.checkpoint_manager.store_status", lambda: _status(legacy_archives=[]))
    monkeypatch.setattr("tools.checkpoint_manager.clear_legacy", lambda: calls.append("clear_legacy"))

    rc = checkpoints.cmd_clear_legacy(argparse.Namespace(force=False))

    assert rc == 0
    assert calls == []
    assert "No legacy archives" in capsys.readouterr().out
