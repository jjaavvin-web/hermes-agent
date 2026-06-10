"""Tests for hermes_cli.curator public CLI entry points.

All curator-backup interactions are path-isolated and monkeypatched so tests do
not touch the live ~/.hermes tree or live agent logs.
"""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


class _NoopLogger:
    def debug(self, *args, **kwargs):  # noqa: D401 - logging shim
        pass

    info = debug
    warning = debug
    error = debug
    exception = debug


@pytest.fixture(autouse=True)
def isolated_curator_environment(tmp_path, monkeypatch):
    """Redirect Hermes home/log-related state to tmp_path for every test."""
    original_hermes_home = os.environ.get("HERMES_HOME")
    original_path_home = Path.home
    home = tmp_path / "hermes-home"
    skills = home / "skills"
    logs = home / "logs"
    cron = home / "cron"
    skills.mkdir(parents=True)
    logs.mkdir()
    cron.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    import hermes_constants

    importlib.reload(hermes_constants)

    # curator_backup logs at info/debug on snapshot/rollback; replace that logger
    # before any command under test can call into it, preventing writes through a
    # Hermes-configured file logger if one exists in the parent process.
    from agent import curator_backup

    monkeypatch.setattr(curator_backup, "logger", _NoopLogger())

    try:
        yield {"home": home, "skills": skills, "logs": logs, "cron": cron}
    finally:
        if original_hermes_home is None:
            monkeypatch.delenv("HERMES_HOME", raising=False)
        else:
            monkeypatch.setenv("HERMES_HOME", original_hermes_home)
        monkeypatch.setattr(Path, "home", original_path_home)
        importlib.reload(hermes_constants)


def _args(**kwargs):
    return SimpleNamespace(**kwargs)


def _snapshot_dir(skills: Path, name: str = "2026-05-01T12-00-00Z") -> Path:
    path = skills / ".curator_backups" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_cli_main_dispatches_registered_status_entry_point(monkeypatch, capsys):
    from hermes_cli import curator as curator_cli

    calls = []

    def fake_status(args):
        calls.append(args.curator_command)
        print("status handled")
        return 7

    monkeypatch.setattr(curator_cli, "_cmd_status", fake_status)

    assert curator_cli.cli_main(["status"]) == 7
    assert calls == ["status"]
    assert "status handled" in capsys.readouterr().out


def test_register_cli_exposes_backup_and_rollback_options():
    from hermes_cli import curator as curator_cli

    parser = argparse.ArgumentParser(prog="hermes curator")
    curator_cli.register_cli(parser)

    backup_args = parser.parse_args(["backup", "--reason", "manual-check"])
    assert backup_args.reason == "manual-check"
    assert backup_args.func.__name__ == "_cmd_backup"

    rollback_args = parser.parse_args(["rollback", "--id", "2026-05-01T12-00-00Z", "-y"])
    assert rollback_args.backup_id == "2026-05-01T12-00-00Z"
    assert rollback_args.yes is True
    assert rollback_args.list is False
    assert rollback_args.func.__name__ == "_cmd_rollback"

    list_args = parser.parse_args(["rollback", "--list"])
    assert list_args.list is True
    assert list_args.func.__name__ == "_cmd_rollback"


def test_run_disabled_returns_error_without_review(monkeypatch, capsys):
    from agent import curator as curator_state
    from hermes_cli import curator as curator_cli

    monkeypatch.setattr(curator_state, "is_enabled", lambda: False)
    monkeypatch.setattr(
        curator_state,
        "run_curator_review",
        lambda **_kwargs: pytest.fail("disabled curator must not start review"),
    )

    rc = curator_cli._cmd_run(_args(dry_run=False, background=False, synchronous=False))

    assert rc == 1
    assert "disabled via config" in capsys.readouterr().out


def test_backup_disabled_returns_error_without_snapshot(monkeypatch, capsys):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    monkeypatch.setattr(curator_backup, "is_enabled", lambda: False)
    monkeypatch.setattr(
        curator_backup,
        "snapshot_skills",
        lambda **_kwargs: pytest.fail("disabled backup must not snapshot"),
    )

    rc = curator_cli._cmd_backup(_args(reason="manual"))

    assert rc == 1
    assert "backups are disabled" in capsys.readouterr().out


def test_backup_passes_reason_and_reports_snapshot(monkeypatch, capsys, isolated_curator_environment):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    snap = _snapshot_dir(isolated_curator_environment["skills"])
    calls = []
    monkeypatch.setattr(curator_backup, "is_enabled", lambda: True)
    monkeypatch.setattr(
        curator_backup,
        "snapshot_skills",
        lambda *, reason: calls.append(reason) or snap,
    )

    rc = curator_cli._cmd_backup(_args(reason="manual-check"))

    assert rc == 0
    assert calls == ["manual-check"]
    out = capsys.readouterr().out
    assert "snapshot created" in out
    assert snap.name in out


def test_backup_snapshot_failure_returns_error(monkeypatch, capsys):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    monkeypatch.setattr(curator_backup, "is_enabled", lambda: True)
    monkeypatch.setattr(curator_backup, "snapshot_skills", lambda *, reason: None)

    rc = curator_cli._cmd_backup(_args(reason="manual-check"))

    assert rc == 1
    assert "snapshot failed" in capsys.readouterr().out


def test_rollback_list_prints_summary_without_resolving_or_restoring(monkeypatch, capsys):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    monkeypatch.setattr(curator_backup, "summarize_backups", lambda: "snapshot summary")
    monkeypatch.setattr(
        curator_backup,
        "_resolve_backup",
        lambda _backup_id: pytest.fail("--list should not resolve a snapshot"),
    )
    monkeypatch.setattr(
        curator_backup,
        "rollback",
        lambda **_kwargs: pytest.fail("--list should not restore a snapshot"),
    )

    rc = curator_cli._cmd_rollback(_args(list=True, backup_id=None, yes=False))

    assert rc == 0
    assert capsys.readouterr().out.strip() == "snapshot summary"


def test_rollback_no_snapshots_prints_guidance(monkeypatch, capsys):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    monkeypatch.setattr(curator_backup, "_resolve_backup", lambda _backup_id: None)
    monkeypatch.setattr(curator_backup, "list_backups", lambda: [])

    rc = curator_cli._cmd_rollback(_args(list=False, backup_id=None, yes=True))

    assert rc == 1
    out = capsys.readouterr().out
    assert "no snapshots exist yet" in out
    assert "hermes curator backup" in out


def test_rollback_unknown_snapshot_prints_available_list(monkeypatch, capsys):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    monkeypatch.setattr(curator_backup, "_resolve_backup", lambda _backup_id: None)
    monkeypatch.setattr(curator_backup, "list_backups", lambda: [{"id": "2026-05-01T12-00-00Z"}])
    monkeypatch.setattr(curator_backup, "summarize_backups", lambda: "available snapshots")

    rc = curator_cli._cmd_rollback(_args(list=False, backup_id="missing", yes=True))

    assert rc == 1
    out = capsys.readouterr().out
    assert "no snapshot matching id 'missing'" in out
    assert "Available:" in out
    assert "available snapshots" in out


def test_rollback_cancelled_confirmation_does_not_restore(
    monkeypatch,
    capsys,
    isolated_curator_environment,
):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    target = _snapshot_dir(isolated_curator_environment["skills"])
    monkeypatch.setattr(curator_backup, "_resolve_backup", lambda _backup_id: target)
    monkeypatch.setattr(
        curator_backup,
        "_read_manifest",
        lambda _target: {"reason": "pre-run", "created_at": "2026-05-01T12:00:00Z", "skill_files": 3},
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    monkeypatch.setattr(
        curator_backup,
        "rollback",
        lambda **_kwargs: pytest.fail("cancelled rollback must not mutate"),
    )

    rc = curator_cli._cmd_rollback(_args(list=False, backup_id=None, yes=False))

    assert rc == 1
    assert "cancelled" in capsys.readouterr().out


def test_rollback_yes_calls_rollback_with_resolved_snapshot_name(
    monkeypatch,
    capsys,
    isolated_curator_environment,
):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    target = _snapshot_dir(isolated_curator_environment["skills"], "2026-05-02T12-00-00Z")
    calls = []
    monkeypatch.setattr(curator_backup, "_resolve_backup", lambda _backup_id: target)
    monkeypatch.setattr(
        curator_backup,
        "_read_manifest",
        lambda _target: {
            "reason": "pre-curator-run",
            "created_at": "2026-05-02T12:00:00Z",
            "skill_files": 4,
            "cron_jobs": {"backed_up": True, "jobs_count": 2},
        },
    )
    monkeypatch.setattr(
        curator_backup,
        "rollback",
        lambda *, backup_id: calls.append(backup_id) or (True, "restored from snapshot", target),
    )

    rc = curator_cli._cmd_rollback(_args(list=False, backup_id="partial-id", yes=True))

    assert rc == 0
    assert calls == [target.name]
    out = capsys.readouterr().out
    assert f"Rollback target: {target.name}" in out
    assert "reason:      pre-curator-run" in out
    assert "cron jobs:   2" in out
    assert "curator: restored from snapshot" in out


def test_rollback_reports_restore_failure(monkeypatch, capsys, isolated_curator_environment):
    from agent import curator_backup
    from hermes_cli import curator as curator_cli

    target = _snapshot_dir(isolated_curator_environment["skills"])
    monkeypatch.setattr(curator_backup, "_resolve_backup", lambda _backup_id: target)
    monkeypatch.setattr(curator_backup, "_read_manifest", lambda _target: {})
    monkeypatch.setattr(
        curator_backup,
        "rollback",
        lambda *, backup_id: (False, "extract failed", None),
    )

    rc = curator_cli._cmd_rollback(_args(list=False, backup_id=None, yes=True))

    assert rc == 1
    assert "rollback failed — extract failed" in capsys.readouterr().out
