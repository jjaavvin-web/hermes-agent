"""Certification that every public Kanban DB opener refuses to bypass the
canonical tombstone via ``HERMES_KANBAN_DB``, an explicit ``db_path``, a
non-default board, or ``HERMES_KANBAN_HOME`` (P7 item 8 follow-up).

Kanban was fully retired live on 2026-09-01: the former ``kanban.db`` file
path is now a mode-0555 **directory** containing a single read-only
``RETIRED`` marker file (see ``tests/security/test_kanban_tombstone_inert.py``,
which certifies the *canonical-path* openers stay inert). This module is
the opener-level counterpart: it certifies the DB-opener chokepoint itself
(``hermes_cli.kanban_db.connect`` — and therefore ``connect_closing``,
``init_db``, ``create_board``, all of which funnel through it) refuses to
create or open an ALTERNATE ``kanban.db`` when the canonical tombstone is
present, regardless of how the caller tries to redirect it:

(a) ``HERMES_KANBAN_DB`` pointed at a fresh temp path.
(b) An explicit ``db_path=`` argument pointed at a fresh temp path.
(c) A non-default ``board=`` name (or ``create_board()`` with a fresh
    slug).
(d) ``HERMES_KANBAN_HOME`` pointed at a fresh temp root that has NO
    tombstone of its own, while the canonical ``HERMES_HOME``-anchored
    root DOES. **Ruling (this module encodes it):** the canonical
    tombstone always wins — retirement is a global property of the
    installation, not something an umbrella-root override can undo for
    itself. :func:`hermes_cli.kanban_db.kanban_retired` checks the
    ``HERMES_HOME``-anchored root unconditionally (ignoring
    ``HERMES_KANBAN_HOME``) in addition to the resolved
    :func:`hermes_cli.kanban_db.kanban_home`, and reports retired if
    EITHER location is tombstoned.
(e) Control: with no tombstone anywhere, the ordinary open still works and
    creates the schema — proving the guard is retirement-specific, not a
    blanket break of Kanban.

Nothing under the real ``~/.hermes`` is ever opened or modified — every
scenario builds its own isolated ``HERMES_HOME`` under ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _make_tombstone_home(root: Path) -> Path:
    """Build a temp HERMES_HOME mirroring the live retired Kanban shape."""
    root.mkdir(parents=True, exist_ok=True)
    # Minimal config.yaml — no kanban section, so every kanban.* flag falls
    # through to its fail-closed code default.
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")

    kanban_db_dir = root / "kanban.db"
    kanban_db_dir.mkdir()
    (kanban_db_dir / "RETIRED").write_text(
        "Kanban retired 2026-09-01. Board history is read-only.\n",
        encoding="utf-8",
    )
    kanban_db_dir.chmod(0o555)
    return root


def _tree_snapshot(root: Path) -> set[str]:
    """Every path under root, relative — for a before/after diff."""
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.fixture
def tombstone_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    _make_tombstone_home(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield home
    # Restore write bits so pytest's own tmp_path cleanup can rmtree these
    # directories — the read-only permission is the thing under test, not a
    # constraint on teardown.
    try:
        (home / "kanban.db").chmod(0o755)
    except OSError:
        pass


class TestHermesKanbanDbEnvBypass:
    """(a) ``HERMES_KANBAN_DB`` pointed at a fresh temp path must refuse."""

    def test_connect_refuses_and_creates_nothing(self, tombstone_home, monkeypatch, tmp_path):
        alt_root = tmp_path / "alt-db-env"
        alt_db = alt_root / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(alt_db))
        with pytest.raises(kb.KanbanRetiredError):
            kb.connect()
        assert not alt_root.exists(), "HERMES_KANBAN_DB bypass created files"

    def test_connect_closing_refuses_and_creates_nothing(self, tombstone_home, monkeypatch, tmp_path):
        alt_root = tmp_path / "alt-db-env-cc"
        alt_db = alt_root / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(alt_db))
        with pytest.raises(kb.KanbanRetiredError):
            with kb.connect_closing():
                pass  # pragma: no cover — never reached, connect() raises first
        assert not alt_root.exists()

    def test_init_db_refuses_and_creates_nothing(self, tombstone_home, monkeypatch, tmp_path):
        alt_root = tmp_path / "alt-db-env-init"
        alt_db = alt_root / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(alt_db))
        with pytest.raises(kb.KanbanRetiredError):
            kb.init_db()
        assert not alt_root.exists()


class TestExplicitDbPathBypass:
    """(b) an explicit ``db_path=`` argument must refuse."""

    def test_connect_refuses_and_creates_nothing(self, tombstone_home, tmp_path):
        alt_db = tmp_path / "alt-db-path" / "kanban.db"
        with pytest.raises(kb.KanbanRetiredError):
            kb.connect(db_path=alt_db)
        assert not alt_db.parent.exists()

    def test_init_db_refuses_and_creates_nothing(self, tombstone_home, tmp_path):
        alt_db = tmp_path / "alt-db-path-init" / "kanban.db"
        with pytest.raises(kb.KanbanRetiredError):
            kb.init_db(db_path=alt_db)
        assert not alt_db.parent.exists()


class TestNonDefaultBoardBypass:
    """(c) a non-default ``board=`` name (or ``create_board()``) must refuse."""

    def test_connect_refuses_and_creates_nothing(self, tombstone_home):
        with pytest.raises(kb.KanbanRetiredError):
            kb.connect(board="side-door")
        assert not (tombstone_home / "kanban" / "boards" / "side-door").exists()

    def test_init_db_refuses_and_creates_nothing(self, tombstone_home):
        with pytest.raises(kb.KanbanRetiredError):
            kb.init_db(board="side-door-init")
        assert not (tombstone_home / "kanban" / "boards" / "side-door-init").exists()

    def test_create_board_refuses_and_leaves_no_ghost_metadata(self, tombstone_home):
        # create_board() writes board.json BEFORE touching the DB — it must
        # not leave a "ghost" board behind that list_boards() would surface
        # even though the DB write itself is refused.
        with pytest.raises(kb.KanbanRetiredError):
            kb.create_board("ghost-board")
        assert not (tombstone_home / "kanban" / "boards" / "ghost-board").exists()


class TestHermesKanbanHomeBypass:
    """(d) ``HERMES_KANBAN_HOME`` must not be able to un-retire the board.

    Ruling encoded here: the canonical ``HERMES_HOME``-anchored tombstone
    always wins, even when ``HERMES_KANBAN_HOME`` points at a fresh root
    with no tombstone of its own. Kanban retirement is a global property
    of the installation, not a per-umbrella-root one.
    """

    def test_connect_refuses_and_creates_nothing(self, tombstone_home, monkeypatch, tmp_path):
        alt_home = tmp_path / "alt-kanban-home-connect"
        alt_home.mkdir()
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(alt_home))
        # Sanity: the alternate root really is clean before the call — the
        # bypass under test is specifically that it has NO tombstone.
        assert not (alt_home / "kanban.db").exists()
        with pytest.raises(kb.KanbanRetiredError):
            kb.connect()
        assert _tree_snapshot(alt_home) == set(), "HERMES_KANBAN_HOME bypass created files"

    def test_kanban_retired_reports_true_despite_clean_override_root(
        self, tombstone_home, monkeypatch, tmp_path
    ):
        alt_home = tmp_path / "alt-kanban-home-flag"
        alt_home.mkdir()
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(alt_home))
        # Confirm the override really does redirect kanban_home() (i.e. this
        # is testing the intended bypass shape, not a no-op).
        assert kb.kanban_home() == alt_home
        assert kb.kanban_retired() is True


class TestControlNoTombstone:
    """(e) control: with no tombstone, the ordinary open still works."""

    def test_connect_creates_schema(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert kb.kanban_retired() is False

        conn = kb.connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()
        assert (home / "kanban.db").is_file()


class TestRepairDbRefuses:
    """repair_db() opens via _sqlite_connect() (not connect()) and used to write
    the cross-process .init.lock sidecar before failing — it must refuse first."""

    def test_repair_db_refuses_and_writes_nothing(self, tombstone_home, tmp_path):
        from hermes_cli import kanban_db as kdb

        alt = tmp_path / "alt-root" / "kanban.db"
        alt.parent.mkdir(parents=True)
        with pytest.raises(kdb.KanbanRetiredError):
            kdb.repair_db(alt)
        assert sorted(p.name for p in alt.parent.iterdir()) == []


class TestSiblingWritersRefuse:
    """Openers that bypass connect(): archive import (stages + moves a full DB
    before init_db) and the standalone board.json writer (rename / workdir /
    projects sync)."""

    def test_import_board_refuses_before_staging(self, tombstone_home, tmp_path):
        from hermes_cli import kanban_db as kdb
        from hermes_cli import kanban_transfer as kt

        archive = tmp_path / "smuggled.tar.gz"
        archive.write_bytes(b"not-even-a-real-archive")  # must be refused before it is opened
        with pytest.raises(kdb.KanbanRetiredError):
            kt.import_board(archive)
        root = kdb.boards_root() if hasattr(kdb, "boards_root") else None
        if root is not None and root.exists():
            assert sorted(p.name for p in root.iterdir()) == []

    def test_write_board_metadata_refuses(self, tombstone_home):
        from hermes_cli import kanban_db as kdb

        with pytest.raises(kdb.KanbanRetiredError):
            kdb.write_board_metadata("default", name="pwned")

