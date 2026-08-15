"""Life-tab task counter: read the LIVE current kanban board, never fabricate 0/0.

Regression coverage for the fix on ``fix/life-tab-kanban-counts-v0201``
(spec: ~/.hermes/audits/20260815T1300Z-state-db-recover/lanes/p0/SPEC.md).

Defect (5f66e091c7 and every serving tree since 2026-07-30):
``_read_life_kanban_counts`` hardcoded the retired board ``hermes``, caught the
resulting ``OperationalError`` in a bare ``except Exception`` and returned a
fabricated ``(0, 0)``. ``/api/life/agenda`` served it while ``/api/life/state``
kept the operator's real numbers, and the compiled SPA (``LifePage``:
``tasksDone: i.tasksDone`` with no fallback) overwrote the real numbers with
the fake zero on every 30 s poll.

RED against 5f66e091c7: every test except ``test_import_target_is_this_tree``
must FAIL. GREEN after the fix: all pass.

Target install location: ``tests/hermes_cli/test_life_agenda_kanban_counts.py``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import hermes_cli.web_server as web_server
from hermes_cli import kanban_db
from tests.conftest import PROJECT_ROOT

# Minimal ``tasks`` table: the only columns the counter is allowed to depend on.
_TASKS_DDL = (
    "CREATE TABLE tasks ("
    " id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,"
    " created_at INTEGER NOT NULL)"
)

# 4 done + 4 open (triage, triage, blocked, running) + 5 archived  ->  (4, 8)
_LIVE_STATUSES = ["done"] * 4 + ["triage", "triage", "blocked", "running"] + ["archived"] * 5
_LIVE_EXPECTED = (4, 8)

# The operator's saved numbers (what /api/life/state serves). Distinct from the
# board (4/8), from the fabricated (0/0) and from _LIFE_DEFAULT_VALUES (3/6).
_STATE_EXPECTED = (7, 11)


def _write_board(kanban_home: Path, slug: str, statuses: list[str] | None, *, current: bool = True) -> Path:
    """Create ``<kanban_home>/kanban/boards/<slug>/{board.json,kanban.db}``.

    ``statuses=None`` -> board.json only, NO kanban.db (board_exists() is
    still True because board.json is present).
    """
    board_dir = kanban_home / "kanban" / "boards" / slug
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / "board.json").write_text(json.dumps({"slug": slug, "name": slug}), encoding="utf-8")
    db_path = board_dir / "kanban.db"
    if statuses is not None:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(_TASKS_DDL)
            conn.executemany(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
                [(f"t{i}", f"task {i}", status, 1_700_000_000 + i) for i, status in enumerate(statuses)],
            )
            conn.commit()
        finally:
            conn.close()
    if current:
        _point_current_at(kanban_home, slug)
    return db_path


def _point_current_at(kanban_home: Path, slug: str) -> Path:
    current = kanban_home / "kanban" / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(slug + "\n", encoding="utf-8")
    return current


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Hermetic kanban root; also clears any leaked board/db pins and the
    once-per-reason log latch (raising=False so the fixture also works against
    the baseline tree, where the latch does not exist yet)."""
    home = tmp_path / "kanban-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(web_server, "_LIFE_KANBAN_LAST_ERROR", None, raising=False)
    return home


@pytest.fixture
def life_state(tmp_path, monkeypatch):
    """life-dashboard.json holding the operator's saved counts (7/11)."""
    path = tmp_path / "life-dashboard.json"
    path.write_text(json.dumps({"tasksDone": 7, "tasksTotal": 11}), encoding="utf-8")
    monkeypatch.setattr(web_server, "_LIFE_STATE_PATH", path)
    return path


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_server, "_read_google_calendar_agenda", lambda: [])
    return TestClient(web_server.app)


def _headers() -> dict[str, str]:
    return {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}


def _agenda(client) -> dict:
    resp = client.get("/api/life/agenda", headers=_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _state(client) -> dict:
    resp = client.get("/api/life/state", headers=_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 0. Which tree is under test (guards the RED/GREEN runs against a stray import)
# ---------------------------------------------------------------------------

def test_import_target_is_this_tree():
    assert Path(web_server.__file__).resolve() == (PROJECT_ROOT / "hermes_cli" / "web_server.py").resolve()
    assert Path(kanban_db.__file__).resolve() == (PROJECT_ROOT / "hermes_cli" / "kanban_db.py").resolve()


# ---------------------------------------------------------------------------
# 1. Happy path: the LIVE current board is read, not a hardcoded slug
# ---------------------------------------------------------------------------

def test_counts_read_live_current_board_not_hardcoded_hermes(kanban_home, life_state):
    # Decoy: a board literally named 'hermes' with DIFFERENT counts (1/2).
    # The baseline hardcodes board="hermes" and would report (1, 2).
    _write_board(kanban_home, "hermes", ["done", "triage"], current=False)
    _write_board(kanban_home, "lifetest", _LIVE_STATUSES, current=True)

    counts = web_server._read_life_kanban_counts()

    assert (counts[0], counts[1]) == _LIVE_EXPECTED
    assert counts.source == "kanban"
    assert counts.board == "lifetest"
    assert counts.error is None


@pytest.mark.parametrize(
    "statuses",
    [pytest.param([], id="empty-board"), pytest.param(["archived"] * 3, id="archived-only")],
)
def test_real_zero_from_a_live_board_is_allowed_and_labelled(kanban_home, life_state, statuses):
    _write_board(kanban_home, "lifetest", statuses)

    counts = web_server._read_life_kanban_counts()

    assert (counts[0], counts[1]) == (0, 0)
    assert counts.source == "kanban"          # a REAL zero, not a fabricated one
    assert counts.error is None


def test_board_is_opened_read_only_and_left_byte_identical(kanban_home, life_state, monkeypatch):
    db_path = _write_board(kanban_home, "lifetest", _LIVE_STATUSES)
    before = _sha256(db_path)
    seen: list[tuple[tuple, dict]] = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)

    counts = web_server._read_life_kanban_counts()

    assert (counts[0], counts[1]) == _LIVE_EXPECTED
    assert seen, "board was never opened"
    for args, kwargs in seen:
        assert kwargs.get("uri") is True
        assert str(args[0]).startswith("file:") and "mode=ro" in str(args[0])
    assert _sha256(db_path) == before


# ---------------------------------------------------------------------------
# 2. Failure classes: fall back to the operator's numbers, never (0, 0)
# ---------------------------------------------------------------------------

def test_fallback_when_current_pointer_missing(kanban_home, life_state):
    # boards/ exists but kanban/current does not -> no live board.
    _write_board(kanban_home, "lifetest", _LIVE_STATUSES, current=False)

    counts = web_server._read_life_kanban_counts()

    assert (counts[0], counts[1]) == _STATE_EXPECTED
    assert counts.source == "life-state"
    assert counts.board is None
    assert counts.error and "current" in counts.error


def test_fallback_when_current_names_a_board_that_does_not_exist(kanban_home, life_state):
    _point_current_at(kanban_home, "ghost")

    counts = web_server._read_life_kanban_counts()

    assert (counts[0], counts[1]) == _STATE_EXPECTED
    assert counts.source == "life-state"
    assert counts.board is None
    assert counts.error and "ghost" in counts.error


def _break_missing_db(db_path: Path) -> None:
    db_path.unlink()


def _break_garbage_db(db_path: Path) -> None:
    db_path.write_bytes(b"this is not a sqlite database\n" * 8)


def _break_no_tasks_table(db_path: Path) -> None:
    db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "breaker, error_marker",
    [
        pytest.param(_break_missing_db, "OperationalError", id="db-missing"),
        pytest.param(_break_garbage_db, "DatabaseError", id="db-garbage"),
        pytest.param(_break_no_tasks_table, "no such table", id="no-tasks-table"),
    ],
)
def test_fallback_when_board_db_unreadable(kanban_home, life_state, breaker, error_marker):
    db_path = _write_board(kanban_home, "lifetest", _LIVE_STATUSES)
    breaker(db_path)

    counts = web_server._read_life_kanban_counts()

    assert (counts[0], counts[1]) == _STATE_EXPECTED
    assert counts.source == "life-state"
    assert counts.board == "lifetest"
    assert counts.error and error_marker in counts.error


def _board_dir(kanban_home: Path) -> Path:
    return kanban_home / "kanban" / "boards" / "lifetest"


def _boards_dir(kanban_home: Path) -> Path:
    return kanban_home / "kanban" / "boards"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize(
    "locked",
    [pytest.param(_board_dir, id="board-dir-000"), pytest.param(_boards_dir, id="boards-dir-000")],
)
def test_fallback_when_board_dir_unreadable(kanban_home, life_state, client, locked):
    # Skeptic probes 'board dir mode 000' / 'kanban/boards mode 000' (round 0, 2 BAD):
    # board_exists() -> Path.exists() -> stat() raised PermissionError, which
    # escaped _life_kanban_target's `except ValueError` and 500'd /api/life/agenda.
    # Contract: ANY board-read failure falls back to the operator's numbers.
    _write_board(kanban_home, "lifetest", _LIVE_STATUSES)
    target = locked(kanban_home)
    target.chmod(0)
    try:
        counts = web_server._read_life_kanban_counts()

        assert (counts[0], counts[1]) == _STATE_EXPECTED
        assert counts.source == "life-state"
        assert counts.board is None
        assert counts.error and "PermissionError" in counts.error and "lifetest" in counts.error

        body = _agenda(client)                    # HTTP 200, never a 500
        assert (body["tasksDone"], body["tasksTotal"]) == _STATE_EXPECTED
        assert body["tasksSource"] == "life-state"
        assert body["tasksError"] and "PermissionError" in body["tasksError"]
    finally:
        target.chmod(0o755)                       # pytest tmp cleanup needs it back


def test_fallback_when_current_pointer_is_not_utf8(kanban_home, life_state, client):
    # read_text(encoding="utf-8") raises UnicodeDecodeError (a ValueError, NOT an
    # OSError) on a garbage pointer -- same "never crash" contract as above.
    _write_board(kanban_home, "lifetest", _LIVE_STATUSES)
    (kanban_home / "kanban" / "current").write_bytes(b"\xff\xfe lifetest\n")

    counts = web_server._read_life_kanban_counts()

    assert (counts[0], counts[1]) == _STATE_EXPECTED
    assert counts.source == "life-state"
    assert counts.board is None
    assert counts.error and "cannot read" in counts.error and "codec" in counts.error

    body = _agenda(client)
    assert (body["tasksDone"], body["tasksTotal"]) == _STATE_EXPECTED
    assert body["tasksSource"] == "life-state"


def test_non_numeric_life_state_gives_visible_sentinel_not_zero(kanban_home, life_state):
    # No live board AND the fallback file is corrupt: the only path left is a
    # sentinel that can never be mistaken for a real count.
    life_state.write_text(json.dumps({"tasksDone": "seven", "tasksTotal": 11}), encoding="utf-8")

    counts = web_server._read_life_kanban_counts()

    assert (counts[0], counts[1]) == (-1, -1)
    assert counts.source == "life-state"
    assert counts.error and "non-numeric" in counts.error


def test_repeated_failure_logs_one_warning_then_recovery(kanban_home, life_state, monkeypatch, caplog):
    test_logger = logging.getLogger("test.life_agenda_kanban_counts")
    monkeypatch.setattr(web_server, "_log", test_logger)
    _write_board(kanban_home, "lifetest", _LIVE_STATUSES, current=False)   # no pointer -> failing

    with caplog.at_level(logging.DEBUG, logger=test_logger.name):
        for _ in range(3):
            web_server._read_life_kanban_counts()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "kanban count read failed" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]

        _point_current_at(kanban_home, "lifetest")                        # heal
        counts = web_server._read_life_kanban_counts()
        assert (counts[0], counts[1]) == _LIVE_EXPECTED
        recovered = [r for r in caplog.records if r.levelno == logging.INFO and "recovered" in r.getMessage()]
        assert len(recovered) == 1


# ---------------------------------------------------------------------------
# 3. HTTP contract: /api/life/agenda vs /api/life/state
# ---------------------------------------------------------------------------

def test_agenda_endpoint_serves_live_board_counts(kanban_home, life_state, client):
    _write_board(kanban_home, "lifetest", _LIVE_STATUSES)

    body = _agenda(client)

    assert (body["tasksDone"], body["tasksTotal"]) == _LIVE_EXPECTED
    assert body["tasksSource"] == "kanban"
    assert body["tasksBoard"] == "lifetest"
    assert body["tasksError"] is None
    assert body["agenda"] == []


def test_agenda_endpoint_never_disagrees_with_state_when_board_unreadable(kanban_home, life_state, client):
    # THE defect: agenda said 0/0 while state said the operator's numbers.
    _write_board(kanban_home, "lifetest", _LIVE_STATUSES, current=False)

    agenda = _agenda(client)
    state = _state(client)

    assert (state["tasksDone"], state["tasksTotal"]) == _STATE_EXPECTED
    assert (agenda["tasksDone"], agenda["tasksTotal"]) == (state["tasksDone"], state["tasksTotal"])
    assert (agenda["tasksDone"], agenda["tasksTotal"]) != (0, 0)
    assert agenda["tasksSource"] == "life-state"
    assert agenda["tasksError"]


def test_agenda_endpoint_contract_keys_and_types(kanban_home, life_state, client):
    _write_board(kanban_home, "lifetest", _LIVE_STATUSES)

    body = _agenda(client)

    # Keys the compiled SPA reads (must stay) + the additive provenance keys.
    assert set(body) >= {"agenda", "tasksDone", "tasksTotal", "tasksSource", "tasksBoard", "tasksError"}
    assert isinstance(body["tasksDone"], int) and isinstance(body["tasksTotal"], int)
    assert isinstance(body["agenda"], list)
    assert body["tasksSource"] in {"kanban", "life-state"}
