from datetime import datetime
import json
import os
import sqlite3
import stat


def _make_home(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: openai-codex\n")
    (home / ".env").write_text("PLACEHOLDER_SECRET_SOURCE=present-only\n")
    (home / "auth.json").write_text('{"credential_pool": {"x": "present-only"}}\n')
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'nightly')")
    conn.commit()
    conn.close()
    return home


def _snapshot_dir(home, snapshot_id):
    return home / "state-snapshots" / snapshot_id


def test_daily_quick_snapshot_creates_0600_state_db_file(tmp_path):
    from hermes_cli.backup import create_daily_quick_snapshot

    home = _make_home(tmp_path)
    os.chmod(home / "state.db", 0o644)

    snapshot_id = create_daily_quick_snapshot(hermes_home=home, retain=5)

    mode = stat.S_IMODE((_snapshot_dir(home, snapshot_id) / "state.db").stat().st_mode)
    assert mode == 0o600


def test_daily_quick_snapshot_excludes_live_secret_files(tmp_path):
    from hermes_cli.backup import _QUICK_STATE_FILES, create_daily_quick_snapshot

    home = _make_home(tmp_path)

    assert ".env" not in _QUICK_STATE_FILES
    assert "auth.json" not in _QUICK_STATE_FILES

    snapshot_id = create_daily_quick_snapshot(hermes_home=home, retain=5)
    snap_dir = _snapshot_dir(home, snapshot_id)

    assert not (snap_dir / ".env").exists()
    assert not (snap_dir / "auth.json").exists()
    manifest = json.loads((snap_dir / "manifest.json").read_text())
    assert ".env" not in manifest["files"]
    assert "auth.json" not in manifest["files"]


def test_daily_quick_snapshot_prunes_to_exact_retain_count(tmp_path, monkeypatch):
    from hermes_cli import backup

    home = _make_home(tmp_path)
    root = home / "state-snapshots"
    root.mkdir()
    for i in range(5):
        old = root / f"2026010{i}-000000-nightly"
        old.mkdir()
        (old / "manifest.json").write_text(json.dumps({"id": old.name, "files": {}}))

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime.strptime("20260110-000000", "%Y%m%d-%H%M%S").replace(tzinfo=tz)

    monkeypatch.setattr(backup, "datetime", FakeDateTime)

    snapshot_id = backup.create_daily_quick_snapshot(hermes_home=home, retain=3)

    names = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert snapshot_id == "20260110-000000-nightly"
    assert names == [
        "20260103-000000-nightly",
        "20260104-000000-nightly",
        "20260110-000000-nightly",
    ]


def test_daily_quick_snapshot_retain_above_twenty_is_not_capped(tmp_path, monkeypatch):
    """Regression: retain > 20 must keep exactly ``retain``, not silently cap at 20.

    A prior shape listed prune candidates through a default ``limit=20`` helper,
    so retain>20 quietly behaved like retain=20. Build 25 snapshots, keep 22, and
    assert all 22 (not 20) survive.
    """
    from hermes_cli import backup

    home = _make_home(tmp_path)
    root = home / "state-snapshots"
    root.mkdir()
    for i in range(25):
        old = root / f"202601{i:02d}-000000-nightly"
        old.mkdir()
        (old / "manifest.json").write_text(json.dumps({"id": old.name, "files": {}}))

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime.strptime("20260201-000000", "%Y%m%d-%H%M%S").replace(tzinfo=tz)

    monkeypatch.setattr(backup, "datetime", FakeDateTime)

    snapshot_id = backup.create_daily_quick_snapshot(hermes_home=home, retain=22)

    names = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert snapshot_id == "20260201-000000-nightly"
    # 25 old + 1 new = 26; retain=22 -> 4 oldest pruned, 22 survive (NOT capped at 20).
    assert len(names) == 22, f"retain=22 should keep 22, got {len(names)} (silently capped?)"
    assert "20260201-000000-nightly" in names   # the new snapshot survives
    assert "20260124-000000-nightly" in names   # a recent old one survives
    assert "20260100-000000-nightly" not in names  # the oldest is pruned


def test_quick_snapshot_cli_creates_nightly_snapshot_and_reports_actual_pruning(tmp_path, monkeypatch, capsys):
    from hermes_cli import backup

    home = _make_home(tmp_path)
    root = home / "state-snapshots"
    root.mkdir()
    for i in range(3):
        old = root / f"2026010{i + 1}-000000-nightly"
        old.mkdir()
        (old / "manifest.json").write_text(json.dumps({"id": old.name, "files": {}}), encoding="utf-8")
    monkeypatch.setattr(backup, "get_hermes_home", lambda: home)

    assert backup.main(["quick-snapshot", "--retain", "2"]) == 0

    snapshots = [p for p in root.iterdir() if p.is_dir()]
    assert len(snapshots) == 2
    newest = max(snapshots, key=lambda p: p.name)
    assert newest.name.endswith("-nightly")
    assert (newest / "state.db").is_file()
    assert not (newest / ".env").exists()
    assert not (newest / "auth.json").exists()
    out = capsys.readouterr().out
    assert "State snapshot created:" in out
    assert "Pruned 2 old snapshot" in out


def test_daily_quick_snapshot_preserves_recovery_when_database_copy_fails(tmp_path, monkeypatch):
    from hermes_cli import backup

    home = _make_home(tmp_path)
    old = home / "state-snapshots" / "20260101-000000-nightly"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text(json.dumps({"id": old.name, "files": {}}), encoding="utf-8")
    monkeypatch.setattr(backup, "_safe_copy_db", lambda *args, **kwargs: False)

    snapshot_id = backup.create_daily_quick_snapshot(hermes_home=home, retain=1)

    assert snapshot_id is not None
    manifest = json.loads((_snapshot_dir(home, snapshot_id) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["failed_dbs"] == ["state.db"]
    assert old.is_dir(), "The last recovery snapshot must survive an incomplete nightly backup"
