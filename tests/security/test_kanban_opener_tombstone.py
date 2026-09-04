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
(f) ``kanban_transfer.export_board()`` — the archive *exporter*, which was
    ungated and opened the source board read-write.
(g) Hostile tombstone SHAPES: the gate must fail CLOSED on a dangling
    symlink, a symlink to a directory or to a real DB, a symlink loop, a
    fifo, and an unreadable path — only an absent path or an ordinary
    regular file may read as "not retired".
(h) The gateway watchers (notifier + dispatcher), which were gated by
    ``config.yaml`` rather than by the tombstone and wrote a singleton
    ``.dispatcher.lock`` into the retired tree before any opener could
    refuse.

Nothing under the real ``~/.hermes`` is ever opened or modified — every
scenario builds its own isolated ``HERMES_HOME`` under ``tmp_path``.
"""
from __future__ import annotations

import contextlib
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



class TestExportBoardRefuses:
    """(f) ``kanban_transfer.export_board()`` — the one public opener that was
    left ungated and that opened the source board **read-write**.

    Export resolves a board, opens its ``kanban.db`` and mints a
    redistributable ``tar.gz`` from it. With the canonical tombstone present
    it ran to completion and returned ``EXPORT_OK`` — so ``HERMES_KANBAN_DB``
    pointed at the preserved retired history (``~/.hermes/retired/
    kanban-*/kanban.db``) opened read-only history read-write (WAL recovery /
    checkpoint on open is a write) and published a copy of it.

    Two properties are pinned here: the refusal itself, and the fact that the
    refusal happens BEFORE anything is opened or written.
    """

    def _seed_board_db(self, path: Path) -> bytes:
        import hashlib
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
            conn.execute("INSERT INTO tasks (title) VALUES ('history')")
            conn.commit()
        finally:
            conn.close()
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_export_board_refuses_and_writes_no_archive(
        self, tombstone_home, monkeypatch, tmp_path
    ):
        import hashlib

        from hermes_cli import kanban_db as kdb
        from hermes_cli import kanban_transfer as kt

        source_root = tmp_path / "retired-history"
        source_db = source_root / "kanban.db"
        before_sha = self._seed_board_db(source_db)
        before_tree = _tree_snapshot(source_root)

        monkeypatch.setenv("HERMES_KANBAN_DB", str(source_db))
        out_root = tmp_path / "exported"
        out = out_root / "board.tar.gz"

        with pytest.raises(kdb.KanbanRetiredError):
            kt.export_board(None, str(out))

        # No archive, and not even the parent directory export would create.
        assert not out.exists()
        assert not out_root.exists(), "export_board created its output directory"
        # The source was never opened at all: same bytes, no new WAL/SHM sidecar.
        assert hashlib.sha256(source_db.read_bytes()).hexdigest() == before_sha
        assert _tree_snapshot(source_root) == before_tree

    def test_export_board_refuses_for_explicit_board_and_home_override(
        self, tombstone_home, monkeypatch, tmp_path
    ):
        """The gate is not reachable around via board / umbrella-root overrides."""
        from hermes_cli import kanban_db as kdb
        from hermes_cli import kanban_transfer as kt

        alt_home = tmp_path / "alt-home"  # a fresh root with NO tombstone
        (alt_home / "kanban" / "boards" / "side-door").mkdir(parents=True)
        self._seed_board_db(alt_home / "kanban" / "boards" / "side-door" / "kanban.db")
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(alt_home))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "side-door")

        out = tmp_path / "side-door-export"
        with pytest.raises(kdb.KanbanRetiredError):
            kt.export_board("side-door", str(out))
        assert not out.exists()
        assert not Path(str(out) + ".tar.gz").exists()

    def test_snapshot_source_is_opened_read_only(self, tmp_path, monkeypatch):
        """``_snapshot_db`` must open the SOURCE through ``file:…?mode=ro``.

        Asserted on the actual connect arguments (not just on an end-to-end
        success) because SQLite silently downgrades a read-write open of a
        read-only file to read-only, which would let a plain ``connect()``
        masquerade as safe on a chmod-444 fixture.
        """
        import sqlite3

        from hermes_cli import kanban_transfer as kt

        source = tmp_path / "src" / "kanban.db"
        self._seed_board_db(source)
        target = tmp_path / "snap.db"

        calls: list[tuple[tuple, dict]] = []
        real_connect = sqlite3.connect

        def _recording_connect(*args, **kwargs):
            calls.append((args, kwargs))
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(kt.sqlite3, "connect", _recording_connect)
        kt._snapshot_db(source, target)

        src_args, src_kwargs = calls[0]
        assert src_kwargs.get("uri") is True, "source was not opened as a URI"
        assert src_args[0].startswith("file:"), src_args[0]
        assert src_args[0].endswith("?mode=ro"), src_args[0]
        assert str(source) not in src_args[0] or "?mode=ro" in src_args[0]
        # and the snapshot is real, not an empty file
        with contextlib.closing(real_connect(str(target))) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_readonly_uri_connection_rejects_writes(self, tmp_path):
        """The URI the exporter uses must produce a genuinely read-only handle.

        Discriminating on purpose: the fixture DB is mode-0644 in a writable
        directory, so a plain ``sqlite3.connect()`` would happily write to it
        (SQLite only downgrades to read-only when the *file* is read-only,
        which is why a chmod-444 fixture cannot tell the two openers apart).
        """
        import sqlite3

        from hermes_cli import kanban_transfer as kt

        source = tmp_path / "board" / "kanban.db"
        self._seed_board_db(source)
        uri = kt._readonly_uri(source)
        assert uri.startswith("file:") and uri.endswith("?mode=ro")

        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO tasks (title) VALUES ('smuggled')")

        # Sanity: the same file IS writable through an ordinary connection,
        # so the refusal above came from the URI, not from the filesystem.
        with contextlib.closing(sqlite3.connect(str(source))) as conn:
            conn.execute("INSERT INTO tasks (title) VALUES ('control')")
            conn.commit()


class TestTombstoneShapeIsFailClosed:
    """(g) ``kanban_retired()`` must fail CLOSED on every ambiguous shape.

    The gate used to decide retirement with ``Path.is_dir()``, which swallows
    every ``OSError`` and reports ``False`` — so the function's own documented
    fail-closed branch ("permissions, broken symlink, unreadable home") was
    dead code for exactly those three cases. A **dangling symlink** at the
    canonical ``kanban.db`` path read as "not retired" and let ``init_db()``
    create a full board database straight through the tombstone.

    Only two shapes may read as not-retired: an absent path, and an ordinary
    regular file (a real board DB).
    """

    @pytest.fixture
    def bare_home(self, tmp_path, monkeypatch):
        """A HERMES_HOME with NO tombstone — the shape under test is planted
        at the canonical ``kanban.db`` path by each test."""
        home = tmp_path / ".hermes"
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        return home

    def test_absent_path_is_not_retired(self, bare_home):
        """Control: a never-created board is not a retired one."""
        assert kb.kanban_retired() is False

    def test_regular_file_is_not_retired(self, bare_home):
        """Control: an ordinary board DB file must keep working."""
        (bare_home / "kanban.db").write_bytes(b"SQLite format 3\x00")
        assert kb.kanban_retired() is False

    def test_dangling_symlink_is_retired(self, bare_home, tmp_path):
        """The live bypass: a symlink to a path that does not exist."""
        link = bare_home / "kanban.db"
        try:
            link.symlink_to(tmp_path / "nowhere" / "kanban.db")
        except OSError as exc:  # pragma: no cover - symlink-less filesystems
            pytest.skip(f"symlinks unavailable: {exc}")
        assert link.is_file() is False and link.is_dir() is False
        assert kb.kanban_retired() is True

    def test_symlink_to_directory_is_retired(self, bare_home, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = bare_home / "kanban.db"
        try:
            link.symlink_to(elsewhere)
        except OSError as exc:  # pragma: no cover
            pytest.skip(f"symlinks unavailable: {exc}")
        assert kb.kanban_retired() is True

    def test_symlink_to_a_real_database_is_still_retired(self, bare_home, tmp_path):
        """Even a symlink that resolves to a valid DB is refused: following it
        is the bypass, and a symlink at the canonical path is never a shape a
        live installation produces."""
        real = tmp_path / "real" / "kanban.db"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"SQLite format 3\x00")
        link = bare_home / "kanban.db"
        try:
            link.symlink_to(real)
        except OSError as exc:  # pragma: no cover
            pytest.skip(f"symlinks unavailable: {exc}")
        assert kb.kanban_retired() is True

    def test_symlink_loop_is_retired(self, bare_home):
        link = bare_home / "kanban.db"
        try:
            link.symlink_to(link)
        except OSError as exc:  # pragma: no cover
            pytest.skip(f"symlinks unavailable: {exc}")
        assert kb.kanban_retired() is True

    def test_fifo_is_retired(self, bare_home):
        import os as _os

        try:
            _os.mkfifo(bare_home / "kanban.db")
        except (AttributeError, OSError) as exc:  # pragma: no cover
            pytest.skip(f"mkfifo unavailable: {exc}")
        assert kb.kanban_retired() is True

    def test_permission_error_is_retired(self, bare_home, monkeypatch):
        """The unreadable-home case the docstring promised and never covered."""
        import os as _os

        real_lstat = _os.lstat

        def _denied(path, *a, **kw):
            if str(path).endswith("kanban.db"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_lstat(path, *a, **kw)

        monkeypatch.setattr(kb.os, "lstat", _denied)
        assert kb.kanban_retired() is True

    def test_dangling_symlink_blocks_init_db(self, bare_home, tmp_path):
        """End-to-end: the shape that used to mint a 118 KB DB with a ``tasks``
        table must now refuse and leave the tree untouched."""
        link = bare_home / "kanban.db"
        try:
            link.symlink_to(tmp_path / "nowhere" / "kanban.db")
        except OSError as exc:  # pragma: no cover
            pytest.skip(f"symlinks unavailable: {exc}")
        before = _tree_snapshot(bare_home)
        with pytest.raises(kb.KanbanRetiredError):
            kb.init_db()
        assert _tree_snapshot(bare_home) == before, "init_db wrote through the tombstone"
        assert not (tmp_path / "nowhere").exists()


class TestGatewayKanbanWatchersRefuse:
    """(h) The gateway's Kanban watchers were gated by CONFIG, not by the
    tombstone.

    ``gateway/run.py`` spawns both watchers unconditionally; the dispatcher's
    only brake was ``kanban.dispatch_in_gateway`` (plus an env escape hatch),
    and before either was consulted it wrote a singleton
    ``kanban/.dispatcher.lock`` **into the retired tree**. A lost or defaulted
    config key must not resurrect a tick — and nothing may be written under a
    tombstoned kanban home.
    """

    def _run(self, coro_fn):
        import asyncio

        from gateway.kanban_watchers import GatewayKanbanWatchersMixin

        class _Stub(GatewayKanbanWatchersMixin):
            _running = True

        stub = _Stub()
        asyncio.run(coro_fn(stub))
        return stub

    def test_dispatcher_watcher_refuses_and_writes_no_lock(
        self, tombstone_home, monkeypatch
    ):
        # Dispatch explicitly ENABLED both ways: only the tombstone may stop it.
        monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "true")
        (tombstone_home / "config.yaml").write_text(
            "kanban:\n  dispatch_in_gateway: true\n  dispatch_interval_seconds: 1\n",
            encoding="utf-8",
        )
        before = _tree_snapshot(tombstone_home)
        self._run(lambda s: s._kanban_dispatcher_watcher())
        after = _tree_snapshot(tombstone_home)
        assert after == before, f"dispatcher wrote under a retired kanban home: {after - before}"
        assert not (tombstone_home / "kanban").exists()
        assert not (tombstone_home / "kanban" / ".dispatcher.lock").exists()

    def test_notifier_watcher_refuses_and_writes_nothing(self, tombstone_home):
        before = _tree_snapshot(tombstone_home)
        self._run(lambda s: s._kanban_notifier_watcher())
        assert _tree_snapshot(tombstone_home) == before

    def test_control_dispatcher_is_not_blocked_without_a_tombstone(
        self, tmp_path, monkeypatch
    ):
        """Negative control: the gate is retirement-specific. Without a
        tombstone the dispatcher gets PAST the tombstone check and is stopped
        by its ordinary config brake instead."""
        home = tmp_path / ".hermes-live"
        home.mkdir()
        (home / "config.yaml").write_text(
            "kanban:\n  dispatch_in_gateway: false\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert kb.kanban_retired() is False
        self._run(lambda s: s._kanban_dispatcher_watcher())
