from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from hermes_cli import dashboard_os as osmod


def _item_by_name(section: dict, name: str) -> dict:
    return next(item for item in section["items"] if item["name"] == name)


def _create_state_db(hermes_home: Path, *, age_h: float) -> Path:
    state_db = hermes_home / "state.db"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    state_db.write_text("state\n", encoding="utf-8")
    t = time.time() - age_h * 3600
    os.utime(state_db, (t, t))
    return state_db


def test_state_db_stale_is_not_green(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(osmod, "HERMES_HOME", tmp_path)

    _create_state_db(tmp_path, age_h=200.0)
    stale_section = osmod._section_memory_stores()
    stale_item = _item_by_name(stale_section, "state_db")

    assert stale_item["status"] == "red"

    _create_state_db(tmp_path, age_h=0.0)
    fresh_section = osmod._section_memory_stores()
    fresh_item = _item_by_name(fresh_section, "state_db")

    assert fresh_item["status"] == "green"


def test_kanban_db_empty_board_is_not_green(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(osmod, "HERMES_HOME", tmp_path)
    kanban_db = tmp_path / "kanban" / "boards" / "hermes" / "kanban.db"
    kanban_db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(kanban_db)
    conn.execute("CREATE TABLE tasks(status TEXT)")
    conn.commit()
    conn.close()

    empty_section = osmod._section_memory_stores()
    empty_item = _item_by_name(empty_section, "kanban_db")

    assert empty_item["status"] == "amber"

    conn = sqlite3.connect(kanban_db)
    conn.execute("INSERT INTO tasks(status) VALUES (?)", ("ready",))
    conn.commit()
    conn.close()

    populated_section = osmod._section_memory_stores()
    populated_item = _item_by_name(populated_section, "kanban_db")

    assert populated_item["status"] == "green"
    assert "1 open / 1 total" in populated_item["detail"]
