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


def test_quick_snapshot_goal_anchor_has_no_literal_secret_file_names():
    from hermes_cli import backup

    source = backup.Path(backup.__file__).read_text()

    assert '".env"' not in source
    assert '"auth.json"' not in source


def test_quick_snapshot_cli_reuses_helpers_for_nightly_snapshot(monkeypatch, capsys):
    from hermes_cli import backup

    calls = []

    def fake_create_quick_snapshot(*, label=None, hermes_home=None, keep=None):
        calls.append(("create", label, hermes_home, keep))
        return "snapshot-id"

    def fake_prune_quick_snapshots(*, keep=None, hermes_home=None):
        calls.append(("prune", keep, hermes_home))
        return 2

    monkeypatch.setattr(backup, "create_quick_snapshot", fake_create_quick_snapshot)
    monkeypatch.setattr(backup, "prune_quick_snapshots", fake_prune_quick_snapshots)

    rc = backup.main(["quick-snapshot", "--retain", "7"])

    assert rc == 0
    assert calls == [
        ("create", "nightly", None, None),
        ("prune", 7, None),
    ]
    out = capsys.readouterr().out
    assert "State snapshot created: snapshot-id" in out
    assert "Pruned 2 old snapshot" in out
