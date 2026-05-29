from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

from hermes_state import SessionDB


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, *args, **kwargs):
        self.statements.append(statement)
        return []


class RecordingSessionDB(SessionDB):
    def __init__(self) -> None:
        self._conn = RecordingConnection()
        self._lock = nullcontext()


def test_db_maintenance_runs_checkpoint_optimize_incremental_vacuum_in_order():
    db = RecordingSessionDB()

    db.run_db_maintenance()

    assert db._conn.statements == [
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "PRAGMA optimize",
        "PRAGMA incremental_vacuum",
    ]


def test_db_maintenance_preserves_messages_fts_trigram(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db._conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "test", 1.0),
        )
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("s1", "user", "hello trigram", 2.0),
        )

        db.run_db_maintenance()

        table = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts_trigram'"
        ).fetchone()
        hits = db._conn.execute(
            "SELECT rowid FROM messages_fts_trigram WHERE messages_fts_trigram MATCH ?",
            ("hello",),
        ).fetchall()
    finally:
        db.close()

    assert table is not None
    assert hits


def test_db_init_sets_auto_vacuum_incremental(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        mode = db._conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    finally:
        db.close()

    assert mode == 2  # INCREMENTAL


def test_db_maintenance_cli_smoke_preserves_trigram_table(tmp_path: Path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db._conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("s1", "test", 1.0),
        )
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("s1", "user", "cli smoke trigram", 2.0),
        )
    finally:
        db.close()

    result = subprocess.run(
        [sys.executable, "hermes_state.py", "db-maintenance", "--db-path", str(db_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "wal_checkpoint(TRUNCATE)" in result.stdout
    assert "optimize" in result.stdout
    assert "incremental_vacuum" in result.stdout

    conn = sqlite3.connect(db_path)
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts_trigram'"
        ).fetchone()
    finally:
        conn.close()
    assert table is not None
