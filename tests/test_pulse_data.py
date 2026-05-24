"""Tests for hermes_cli.pulse_data — DoD scenarios 1-4."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import pulse_data


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _empty_hives_snapshot():
    return {"hives": [], "scanned_at": "", "active_count": 0}


def _empty_nexus_snapshot():
    return {"agents": [], "swarms": [], "hives": [], "mcp": [],
            "gateways": [], "cron": [], "edges": []}


def _empty_build_snapshot():
    return {"spendToday": 0.0, "swarm": {"workerCount": 0}}


def _patch_dashboard_helpers(monkeypatch, *,
                              hives_snapshot=None,
                              nexus_snapshot=None,
                              build_snapshot=None,
                              active_model="test-model"):
    """Monkeypatch the four dashboard_health helpers used by pulse_data."""
    import hermes_cli.dashboard_health as dh
    if hives_snapshot is None:
        hives_snapshot = _empty_hives_snapshot()
    if nexus_snapshot is None:
        nexus_snapshot = _empty_nexus_snapshot()
    if build_snapshot is None:
        build_snapshot = _empty_build_snapshot()
    monkeypatch.setattr(dh, "_get_hives_snapshot", lambda: hives_snapshot)
    monkeypatch.setattr(dh, "_get_gitnexus_runtime_snapshot",
                        lambda: nexus_snapshot)
    monkeypatch.setattr(dh, "_build_snapshot", lambda: build_snapshot)
    monkeypatch.setattr(dh, "_get_active_model", lambda: active_model)


def _create_tasks_table(db_path: Path) -> None:
    """Create the minimal `tasks` table that pulse_data queries."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tasks ("
        "  id TEXT PRIMARY KEY,"
        "  title TEXT NOT NULL,"
        "  status TEXT NOT NULL,"
        "  priority INTEGER DEFAULT 0,"
        "  assignee TEXT,"
        "  created_at INTEGER"
        ")"
    )
    conn.commit()
    conn.close()


def _insert_card(db_path: Path, **kwargs) -> None:
    """Insert a single row into the tasks table."""
    defaults = {
        "id": "card-x",
        "title": "Test card",
        "status": "ready",
        "priority": 5,
        "assignee": "alice",
        "created_at": int(time.time()) - 60,
    }
    defaults.update(kwargs)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tasks(id,title,status,priority,assignee,created_at)"
        " VALUES (:id,:title,:status,:priority,:assignee,:created_at)",
        defaults,
    )
    conn.commit()
    conn.close()


def _setup_kanban_home(tmp_path: Path, monkeypatch) -> Path:
    """Create ~/.hermes/kanban.db under tmp_path and redirect Path.home."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = hermes_home / "kanban.db"
    _create_tasks_table(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Scenario 1: empty state — no hives, no tasks
# ---------------------------------------------------------------------------

def test_build_pulse_graph_empty_state_returns_empty_nodes(monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)
    result = pulse_data.build_pulse_graph(now=time.time())
    assert result["nodes"] == []


def test_build_pulse_graph_empty_state_returns_empty_edges(monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)
    result = pulse_data.build_pulse_graph(now=time.time())
    assert result["edges"] == []


def test_build_pulse_graph_empty_state_returns_empty_degraded_mode(monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)
    result = pulse_data.build_pulse_graph(now=time.time())
    assert result["degraded_mode"] == []


def test_build_pulse_queue_empty_state_returns_empty_cards(monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)
    result = pulse_data.build_pulse_queue(now=time.time())
    assert result["cards"] == []


def test_build_pulse_kpis_empty_state_active_hives_is_zero(monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)
    result = pulse_data.build_pulse_kpis(now=time.time())
    assert result["active_hives"] == 0


def test_build_pulse_kpis_empty_state_pending_cards_is_zero(monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)
    result = pulse_data.build_pulse_kpis(now=time.time())
    assert result["pending_cards"] == 0


def test_build_pulse_kpis_empty_state_last_completion_is_none(monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)
    result = pulse_data.build_pulse_kpis(now=time.time())
    assert result["last_completion"] is None


# ---------------------------------------------------------------------------
# Scenario 2: single active hive + tracking card
# ---------------------------------------------------------------------------

def test_build_pulse_graph_single_hive_produces_two_nodes(monkeypatch, tmp_path):
    db_path = _setup_kanban_home(tmp_path, monkeypatch)
    _insert_card(db_path, id="card-abc", title="Build pulse tab",
                 status="running", priority=5, assignee="queen",
                 created_at=int(time.time()) - 120)

    hive = {
        "id": "hive-1",
        "status": "running",
        "tmux_alive": True,
        "tracking_card": "card-abc",
        "track_title": "Build pulse tab",
        "log_mtime": time.time() - 60,
    }
    _patch_dashboard_helpers(monkeypatch,
                             hives_snapshot={"hives": [hive], "active_count": 1})

    result = pulse_data.build_pulse_graph(now=time.time())
    assert len(result["nodes"]) == 2


def test_build_pulse_graph_single_hive_produces_tracking_edge(monkeypatch, tmp_path):
    db_path = _setup_kanban_home(tmp_path, monkeypatch)
    _insert_card(db_path, id="card-abc", title="Build pulse tab",
                 status="running", priority=5, assignee="queen",
                 created_at=int(time.time()) - 120)

    hive = {
        "id": "hive-1",
        "status": "running",
        "tmux_alive": True,
        "tracking_card": "card-abc",
        "track_title": "Build pulse tab",
        "log_mtime": time.time() - 60,
    }
    _patch_dashboard_helpers(monkeypatch,
                             hives_snapshot={"hives": [hive], "active_count": 1})

    result = pulse_data.build_pulse_graph(now=time.time())
    tracking_edges = [e for e in result["edges"] if e.get("kind") == "tracking"]
    assert len(tracking_edges) == 1


def test_build_pulse_graph_hive_node_has_hive_active_group(monkeypatch, tmp_path):
    db_path = _setup_kanban_home(tmp_path, monkeypatch)
    _insert_card(db_path, id="card-abc", title="Build pulse tab",
                 status="running", priority=5, assignee="queen",
                 created_at=int(time.time()) - 120)

    hive = {
        "id": "hive-1",
        "status": "running",
        "tmux_alive": True,
        "tracking_card": "card-abc",
        "track_title": "Build pulse tab",
        "log_mtime": time.time() - 60,
    }
    _patch_dashboard_helpers(monkeypatch,
                             hives_snapshot={"hives": [hive], "active_count": 1})

    result = pulse_data.build_pulse_graph(now=time.time())
    hive_nodes = [n for n in result["nodes"] if n.get("kind") == "hive"]
    assert hive_nodes[0]["group"] == "hive-active"


def test_build_pulse_graph_card_node_has_card_running_group(monkeypatch, tmp_path):
    db_path = _setup_kanban_home(tmp_path, monkeypatch)
    _insert_card(db_path, id="card-abc", title="Build pulse tab",
                 status="running", priority=5, assignee="queen",
                 created_at=int(time.time()) - 120)

    hive = {
        "id": "hive-1",
        "status": "running",
        "tmux_alive": True,
        "tracking_card": "card-abc",
        "track_title": "Build pulse tab",
        "log_mtime": time.time() - 60,
    }
    _patch_dashboard_helpers(monkeypatch,
                             hives_snapshot={"hives": [hive], "active_count": 1})

    result = pulse_data.build_pulse_graph(now=time.time())
    card_nodes = [n for n in result["nodes"] if n.get("kind") == "card"]
    assert card_nodes[0]["group"] == "card-running"


# ---------------------------------------------------------------------------
# Scenario 3: GitNexus unreachable → degraded_mode populated
# ---------------------------------------------------------------------------

def test_build_pulse_graph_gitnexus_error_dict_adds_degraded_flag(
        monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    nexus_with_error = {"_error": "wedged", "agents": [], "swarms": [],
                        "hives": [], "mcp": [], "gateways": [], "cron": [],
                        "edges": []}
    _patch_dashboard_helpers(monkeypatch, nexus_snapshot=nexus_with_error)

    result = pulse_data.build_pulse_graph(now=time.time())
    assert "gitnexus_unreachable" in result["degraded_mode"]


def test_build_pulse_graph_gitnexus_exception_adds_degraded_flag(
        monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)

    import hermes_cli.dashboard_health as dh
    _patch_dashboard_helpers(monkeypatch)  # patch others to safe defaults
    monkeypatch.setattr(
        dh, "_get_gitnexus_runtime_snapshot",
        lambda: (_ for _ in ()).throw(ConnectionRefusedError("no service"))
    )

    result = pulse_data.build_pulse_graph(now=time.time())
    assert "gitnexus_unreachable" in result["degraded_mode"]


def test_build_pulse_graph_gitnexus_unreachable_still_has_nodes_key(
        monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    nexus_with_error = {"_error": "wedged", "agents": [], "swarms": [],
                        "hives": [], "mcp": [], "gateways": [], "cron": [],
                        "edges": []}
    _patch_dashboard_helpers(monkeypatch, nexus_snapshot=nexus_with_error)

    result = pulse_data.build_pulse_graph(now=time.time())
    assert "nodes" in result


def test_build_pulse_graph_gitnexus_unreachable_still_has_edges_key(
        monkeypatch, tmp_path):
    _setup_kanban_home(tmp_path, monkeypatch)
    nexus_with_error = {"_error": "wedged", "agents": [], "swarms": [],
                        "hives": [], "mcp": [], "gateways": [], "cron": [],
                        "edges": []}
    _patch_dashboard_helpers(monkeypatch, nexus_snapshot=nexus_with_error)

    result = pulse_data.build_pulse_graph(now=time.time())
    assert "edges" in result


# ---------------------------------------------------------------------------
# Scenario 4: malformed kanban row → skip with warning, valid row still present
# ---------------------------------------------------------------------------

def test_build_pulse_queue_malformed_row_logs_warning(monkeypatch, tmp_path, caplog):
    db_path = _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)

    # Valid card
    _insert_card(db_path, id="card-good", title="Good card",
                 status="ready", priority=3, assignee="alice",
                 created_at=int(time.time()) - 60)

    # Malformed card: priority is a non-numeric string (sqlite is loose-typed)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tasks(id,title,status,priority,assignee,created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("card-bad", "Bad card", "ready", "NOT_A_NUMBER", "bob",
         int(time.time()) - 60),
    )
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.pulse_data"):
        result = pulse_data.build_pulse_queue(now=time.time())

    assert any("malformed" in rec.message.lower() or "skipping" in rec.message.lower()
               for rec in caplog.records)


def test_build_pulse_queue_malformed_row_valid_card_still_appears(
        monkeypatch, tmp_path, caplog):
    db_path = _setup_kanban_home(tmp_path, monkeypatch)
    _patch_dashboard_helpers(monkeypatch)

    # Valid card
    _insert_card(db_path, id="card-good", title="Good card",
                 status="ready", priority=3, assignee="alice",
                 created_at=int(time.time()) - 60)

    # Malformed card: non-numeric priority
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tasks(id,title,status,priority,assignee,created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("card-bad", "Bad card", "ready", "NOT_A_NUMBER", "bob",
         int(time.time()) - 60),
    )
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.pulse_data"):
        result = pulse_data.build_pulse_queue(now=time.time())

    card_ids = [c["id"] for c in result["cards"]]
    assert "card-good" in card_ids
