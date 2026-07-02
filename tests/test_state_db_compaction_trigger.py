"""Compaction trigger decoupled from prune-count (GWR-1 / ARCH-9).

Historically ``maybe_auto_prune_and_vacuum`` only ran optimize_fts/VACUUM when
``pruned > 0``. In production every session is inside the retention window, so
prune always returns 0 and compaction never fired — the state.db file bloated
without bound. These tests pin the *new* contract: compaction fires on real
fragmentation/growth even when ``pruned == 0`` (the exact live condition).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db():
    """Fresh SessionDB backed by a temp file (matches test_hermes_state.py)."""
    with tempfile.TemporaryDirectory() as d:
        yield SessionDB(db_path=Path(d) / "state.db")


def _freelist_ratio(db: SessionDB) -> float:
    fl = db._conn.execute("PRAGMA freelist_count").fetchone()[0]
    pc = db._conn.execute("PRAGMA page_count").fetchone()[0]
    return (fl / pc) if pc else 0.0


def _make_high_freelist(db: SessionDB) -> None:
    """Bloat the DB: insert many messages then delete them WITHOUT vacuuming.

    The parent session stays recent + active so it is never prunable — this
    reproduces the live condition where ``pruned == 0`` but the file is full of
    free pages that the old ``pruned > 0`` gate would never reclaim.
    """
    db.create_session(session_id="fresh", source="cli")  # recent, active
    for i in range(2000):
        db.append_message(
            session_id="fresh",
            role="user",
            content=("payload " * 60) + str(i),
        )
    with db._lock:
        db._conn.execute("DELETE FROM messages")
        db._conn.commit()


class TestCompactionDecoupledFromPrune:
    def test_high_freelist_with_zero_prune_still_vacuums(self, db):
        """The exact live condition: pruned == 0, freelist ratio > 0.10."""
        _make_high_freelist(db)
        ratio_before = _freelist_ratio(db)
        assert ratio_before > 0.10, f"setup must bloat the DB (got {ratio_before})"

        result = db.maybe_auto_prune_and_vacuum(retention_days=90)

        # Nothing was prunable, yet compaction ran anyway.
        assert result["skipped"] is False
        assert result["pruned"] == 0
        assert result["vacuumed"] is True
        assert result.get("error") is None

        # Free pages were actually reclaimed and provenance recorded.
        assert _freelist_ratio(db) < ratio_before
        assert db.get_meta("last_vacuum") is not None
        assert db.get_meta("last_fts_optimize") is not None
        assert db.get_meta("last_fts_optimize_rowcount") is not None

    def test_tight_db_zero_prune_does_not_vacuum(self, db):
        """Small, fresh, low-fragmentation DB must NOT pay the VACUUM cost.

        Guards against the new trigger over-firing on every startup.
        """
        db.create_session(session_id="fresh", source="cli")
        db.append_message(session_id="fresh", role="user", content="hi")
        assert _freelist_ratio(db) <= 0.10

        result = db.maybe_auto_prune_and_vacuum(retention_days=90)

        assert result["pruned"] == 0
        assert result["vacuumed"] is False
        # Marker still recorded so we don't retry every startup.
        assert db.get_meta("last_auto_prune") is not None

    def test_growth_threshold_records_rowcount_baseline(self, db):
        """After a compaction pass the rowcount baseline is persisted so the
        next idle run does not re-fire on the growth branch."""
        _make_high_freelist(db)
        db.maybe_auto_prune_and_vacuum(retention_days=90)
        baseline = db.get_meta("last_fts_optimize_rowcount")
        assert baseline is not None
        assert int(baseline) == 0  # all messages were deleted

    def test_vacuum_flag_false_disables_compaction(self, db):
        """vacuum=False must short-circuit even with a bloated DB."""
        _make_high_freelist(db)
        result = db.maybe_auto_prune_and_vacuum(retention_days=90, vacuum=False)
        assert result["vacuumed"] is False
