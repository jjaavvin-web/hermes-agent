import time

from hermes_state import SessionDB, main as hermes_state_main


def _set_session_started_at(db: SessionDB, session_id: str, started_at: float) -> None:
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (started_at, session_id),
        )
        db._conn.commit()


def _set_message_timestamp(db: SessionDB, message_id: int, timestamp: float) -> None:
    with db._lock:
        db._conn.execute(
            "UPDATE messages SET timestamp = ? WHERE id = ?",
            (timestamp, message_id),
        )
        db._conn.commit()


def _make_db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def test_reap_zombie_sessions_sets_ended_at_to_last_message_and_reason(tmp_path):
    db = _make_db(tmp_path)
    try:
        now = time.time()
        started = now - 40 * 86400
        last_message = now - 20 * 86400
        db.create_session(session_id="old-zombie", source="cli")
        _set_session_started_at(db, "old-zombie", started)
        msg_id = db.append_message("old-zombie", "user", "old zombie")
        _set_message_timestamp(db, msg_id, last_message)

        assert db.reap_zombie_sessions(grace_days=7, inactive_days=7) == 1

        session = db.get_session("old-zombie")
        assert session["end_reason"] == "reaped-zombie"
        assert session["ended_at"] == last_message
    finally:
        db.close()


def test_reap_zombie_sessions_leaves_recent_or_active_sessions_untouched(tmp_path):
    db = _make_db(tmp_path)
    try:
        now = time.time()
        db.create_session(session_id="recent-start", source="cli")
        _set_session_started_at(db, "recent-start", now - 2 * 86400)

        db.create_session(session_id="recent-message", source="cli")
        _set_session_started_at(db, "recent-message", now - 40 * 86400)
        msg_id = db.append_message("recent-message", "user", "still active")
        _set_message_timestamp(db, msg_id, now - 2 * 86400)

        db.create_session(session_id="already-ended", source="cli")
        _set_session_started_at(db, "already-ended", now - 40 * 86400)
        db.end_session("already-ended", "user_exit")
        ended_before = db.get_session("already-ended")["ended_at"]

        assert db.reap_zombie_sessions(grace_days=7, inactive_days=7) == 0
        assert db.get_session("recent-start")["ended_at"] is None
        assert db.get_session("recent-message")["ended_at"] is None
        assert db.get_session("already-ended")["ended_at"] == ended_before
        assert db.get_session("already-ended")["end_reason"] == "user_exit"
    finally:
        db.close()


def test_reap_zombie_sessions_falls_back_to_started_at_without_messages(tmp_path):
    db = _make_db(tmp_path)
    try:
        started = time.time() - 40 * 86400
        db.create_session(session_id="empty-zombie", source="cli")
        _set_session_started_at(db, "empty-zombie", started)

        assert db.reap_zombie_sessions(grace_days=7, inactive_days=7) == 1

        session = db.get_session("empty-zombie")
        assert session["ended_at"] == started
        assert session["end_reason"] == "reaped-zombie"
    finally:
        db.close()


def test_reap_zombie_sessions_dry_run_reports_count_without_writes(tmp_path, capsys):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        started = time.time() - 40 * 86400
        db.create_session(session_id="dry-run-zombie", source="cli")
        _set_session_started_at(db, "dry-run-zombie", started)
    finally:
        db.close()

    rc = hermes_state_main([
        "reap-zombies",
        "--db-path",
        str(db_path),
        "--grace-days",
        "7",
        "--inactive-days",
        "7",
        "--dry-run",
    ])

    assert rc == 0
    assert "would reap 1 zombie session" in capsys.readouterr().out

    db = SessionDB(db_path=db_path)
    try:
        session = db.get_session("dry-run-zombie")
        assert session["ended_at"] is None
        assert session["end_reason"] is None
    finally:
        db.close()


def test_reap_zombie_sessions_preserves_trigram_fts_table_and_triggers(tmp_path):
    db = _make_db(tmp_path)
    try:
        started = time.time() - 40 * 86400
        db.create_session(session_id="fts-zombie", source="cli")
        _set_session_started_at(db, "fts-zombie", started)
        msg_id = db.append_message("fts-zombie", "user", "recall trigram survives")
        _set_message_timestamp(db, msg_id, started + 10)

        def _trigram_objects():
            return {
                row["name"]
                for row in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'messages_fts_trigram%'"
                ).fetchall()
            }

        def _fts_hits(term):
            return len(
                db._conn.execute(
                    "SELECT rowid FROM messages_fts_trigram WHERE messages_fts_trigram MATCH ?",
                    (term,),
                ).fetchall()
            )

        before = _trigram_objects()
        assert {
            "messages_fts_trigram",
            "messages_fts_trigram_insert",
            "messages_fts_trigram_delete",
            "messages_fts_trigram_update",
        } <= before
        # the append fired trigram_insert -> the row is genuinely indexed
        assert _fts_hits("trigram") == 1

        assert db.reap_zombie_sessions(grace_days=7, inactive_days=7) == 1

        # reap only marks sessions ended; it must drop NEITHER the trigram
        # schema (exact set, not a subset) NOR the indexed FTS rows. The old
        # `before <= after` could never fail; this guards the rail with content.
        assert _trigram_objects() == before
        assert _fts_hits("trigram") == 1
    finally:
        db.close()
