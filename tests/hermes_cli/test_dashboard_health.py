"""Dedicated tests for hermes_cli.dashboard_health.

The module defaults to live process, socket, and ~/.hermes probes.  These tests
redirect state to tmp_path and monkeypatch every probe boundary so they stay
fully offline and never inspect the real user profile.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes_cli import dashboard_health as d


@pytest.fixture(autouse=True)
def isolated_dashboard_health(tmp_path, monkeypatch):
    """Keep dashboard_health away from real HOME, sockets, subprocesses and caches."""
    hermes_home = tmp_path / ".hermes"
    claude_projects = tmp_path / ".claude" / "projects"
    ruflo_work = hermes_home / "ruflo-work"

    monkeypatch.setattr(d, "HOME", tmp_path)
    monkeypatch.setattr(d, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(d, "CLAUDE_PROJECTS_DIR", claude_projects)
    monkeypatch.setattr(d, "RUFLO_WORK_DIR", ruflo_work)
    monkeypatch.setattr(d, "_SNAPSHOT_CACHE", None)
    monkeypatch.setattr(d, "_SPEND_CACHE", {})
    monkeypatch.setattr(d, "_NEXUS_CACHE", None)
    monkeypatch.setattr(d, "_INFRA_CACHE", None)
    monkeypatch.setattr(d, "_HIVES_CACHE", None)

    def forbidden_subprocess(cmd, *args, **kwargs):
        raise AssertionError(f"unexpected subprocess.run call: {cmd!r}")

    def forbidden_socket(*args, **kwargs):
        raise AssertionError("unexpected real socket probe")

    monkeypatch.setattr(d.subprocess, "run", forbidden_subprocess)
    monkeypatch.setattr(d.socket, "create_connection", forbidden_socket)


def _run(coro):
    return asyncio.run(coro)


def _runtime(name: str, status: str = "online", **overrides) -> dict:
    row = {"name": name, "label": name.title(), "status": status, "latencyMs": 1.5}
    row.update(overrides)
    return row


def _healthy_mission() -> dict:
    return {
        "model": "test-model",
        "spendToday": 0.5,
        "spendWeek": 2.5,
        "streakDays": 4,
        "recentSessions": [{"id": "s1"}],
        "nextCron": {"name": "daily", "schedule": "0 9 * * *", "nextRun": "2026-06-10T09:00:00+00:00"},
        "lastDream": None,
        "swarm": None,
        "runtimes": [
            _runtime("hermes", "online", label="Hermes", detail="telegram:connected"),
            _runtime("kanban", "online", label="Kanban", detail="0 open tasks", port=9119),
            _runtime("cron", "online", label="Cron", detail="1/1 jobs enabled"),
            _runtime("codex", "online", label="Codex"),
            _runtime("ruflo", "online", label="Ruflo"),
            _runtime("claude-code", "online", label="Claude Code"),
        ],
    }


def _healthy_topology() -> dict:
    return {
        "gateways": [{"id": "gateway", "status": "running", "platforms": ["telegram"], "active_agents": 1}],
        "agents": [{"id": "codex", "status": "running"}],
        "mcp": [{"id": "memory", "name": "memory", "status": "enabled"}],
        "cron": [{"id": "daily", "name": "daily"}],
        "hives": [],
        "swarms": [],
        "edges": [],
    }


def _healthy_infra() -> dict:
    return {
        "services": [
            {
                "name": "hermes-dashboard.service",
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "description": "Dashboard",
                "status": "ok",
            }
        ],
        "containers": [
            {
                "name": "supabase_db_test",
                "state": "running",
                "status_text": "Up 1 hour (healthy)",
                "image": "postgres",
                "ports": "5434/tcp",
                "status": "ok",
            }
        ],
        "ports": [
            {
                "port": 9119,
                "label": "Dashboard API",
                "description": "Dashboard API",
                "online": True,
                "latencyMs": 2.0,
                "status": "ok",
            }
        ],
    }


def _healthy_hives() -> dict:
    return {"hives": [{"id": "done", "status": "completed"}], "active_count": 0, "completed_count": 1, "stale_count": 0}


def _healthy_codex() -> dict:
    return {
        "sessions": [{"id": "done", "state": "COMPLETE", "worktree_alive": True}],
        "counts": {"total": 1, "by_state": {"COMPLETE": 1}, "ports_claimed": 0, "ports_free": 2},
        "review_pool": {},
    }


def _healthy_pulse() -> dict:
    return {
        "active_hives": 0,
        "pending_cards": 0,
        "max_usage_pct": 10,
        "today_spend_usd": 0.25,
        "today_pr_merges": 1,
        "last_completion": {"slug": "green-run"},
    }


def _wire_nexus_inputs(monkeypatch, *, mission=None, topology=None, infra=None, hives=None, codex=None, pulse=None) -> None:
    monkeypatch.setattr(d, "_get_snapshot", lambda: mission or _healthy_mission())
    monkeypatch.setattr(d, "_get_gitnexus_runtime_snapshot", lambda: topology or _healthy_topology())
    monkeypatch.setattr(d, "_get_infra_snapshot", lambda: infra or _healthy_infra())
    monkeypatch.setattr(d, "_get_hives_snapshot", lambda: hives or _healthy_hives())
    monkeypatch.setattr(d, "_get_codex_sessions_snapshot", lambda: codex or _healthy_codex())
    monkeypatch.setattr(d, "_get_pulse_kpis_snapshot", lambda: pulse or _healthy_pulse())
    monkeypatch.setattr(d, "_NEXUS_CACHE", None)


def _make_kanban_db(path: Path, rows: list[tuple[str, int | None]] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (status TEXT, created_at INTEGER)")
    for status, created_at in rows or []:
        conn.execute("INSERT INTO tasks (status, created_at) VALUES (?, ?)", (status, created_at))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Router and public endpoint surface
# ---------------------------------------------------------------------------


def test_router_registers_dashboard_health_public_routes():
    paths = {route.path for route in d.router.routes}

    assert {
        "/api/dashboard/mission",
        "/api/dashboard/nexus-health",
        "/api/dashboard/nexus-health/node/{node_id}",
        "/api/dashboard/health/runtime/{name}",
        "/api/dashboard/spend",
        "/api/dashboard/queue",
        "/api/dashboard/swarm",
        "/api/dashboard/cron",
        "/api/dashboard/dreams/latest",
        "/api/dashboard/stream",
        "/api/dashboard/hives",
        "/api/dashboard/hives/{hive_id}/log",
    } <= paths


def test_public_endpoints_delegate_to_cached_builders(monkeypatch):
    mission = {"model": "fake", "runtimes": []}
    monkeypatch.setattr(d, "_get_snapshot", lambda: mission)
    monkeypatch.setattr(d, "_get_spend", lambda range_str: {"range": range_str, "points": []})
    monkeypatch.setattr(d, "_get_queue_depth", lambda range_str: {"range": range_str, "points": [], "openNow": 0})
    monkeypatch.setattr(d, "_get_all_cron_jobs", lambda: [{"id": "job-1"}])
    monkeypatch.setattr(d, "_get_swarm_status", lambda: {"id": "swarm-1", "workerCount": 2})

    assert _run(d.get_mission_snapshot()) is mission
    assert _run(d.get_spend("bogus"))["range"] == "7d"
    assert _run(d.get_queue("bogus"))["range"] == "7d"
    assert _run(d.get_cron()) == {"jobs": [{"id": "job-1"}], "count": 1}
    assert _run(d.get_swarm()) == {"active": True, "id": "swarm-1", "workerCount": 2}


def test_public_endpoint_error_paths_return_http_exceptions(monkeypatch):
    monkeypatch.setattr(d, "_build_node_detail", lambda node_id: None)
    monkeypatch.setattr(d, "_get_hive_log_tail", lambda hive_id, tail: None)

    with pytest.raises(HTTPException) as node_exc:
        _run(d.get_nexus_health_node("missing-node"))
    assert node_exc.value.status_code == 404
    assert "missing-node" in node_exc.value.detail

    with pytest.raises(HTTPException) as hive_exc:
        _run(d.get_hive_log("missing-hive", tail=10))
    assert hive_exc.value.status_code == 404
    assert "missing-hive" in hive_exc.value.detail


def test_timeout_wrapped_endpoints_surface_503(monkeypatch):
    monkeypatch.setattr(d, "_get_nexus_health", lambda: {"nodes": []})
    monkeypatch.setattr(d, "_get_hives_snapshot", lambda: {"hives": []})

    async def always_timeout(awaitable, timeout):
        awaitable.cancel()
        raise asyncio.TimeoutError

    monkeypatch.setattr(d.asyncio, "wait_for", always_timeout)

    with pytest.raises(HTTPException) as nexus_exc:
        _run(d.get_nexus_health())
    assert nexus_exc.value.status_code == 503
    assert "timed out" in nexus_exc.value.detail

    with pytest.raises(HTTPException) as hives_exc:
        _run(d.get_hives_snapshot())
    assert hives_exc.value.status_code == 503
    assert "timed out" in hives_exc.value.detail


def test_stream_health_returns_sse_response_without_starting_generator():
    response = _run(d.stream_health())

    assert response.media_type == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Accel-Buffering"] == "no"


# ---------------------------------------------------------------------------
# Status computation and classification helpers
# ---------------------------------------------------------------------------


def test_status_mappers_cover_ok_warn_error_unknown_and_auth_gated():
    assert d._nexus_status("online") == "ok"
    assert d._nexus_status("running") == "ok"
    assert d._nexus_status("enabled") == "ok"
    assert d._nexus_status("degraded") == "warn"
    assert d._nexus_status("stopped") == "warn"
    assert d._nexus_status("offline") == "error"
    assert d._nexus_status("error") == "error"
    assert d._nexus_status("auth_gated") == "auth_gated"
    assert d._nexus_status(None) == "unknown"
    assert d._nexus_status("surprising") == "unknown"

    assert d._systemd_status("active", "running") == "ok"
    assert d._systemd_status("active", "listening") == "ok"
    assert d._systemd_status("failed", "running") == "error"
    assert d._systemd_status("active", "failed") == "error"
    assert d._systemd_status("reloading", "reload") == "warn"
    assert d._systemd_status("inactive", "dead") == "warn"
    assert d._systemd_status("mystery", "mystery") == "unknown"

    assert d._docker_status("running", "Up 2 hours (healthy)") == "ok"
    assert d._docker_status("running", "Up 2 hours (unhealthy)") == "warn"
    assert d._docker_status("paused", "Paused") == "warn"
    assert d._docker_status("created", "Created") == "warn"
    assert d._docker_status("exited", "Exited (1)") == "error"
    assert d._docker_status("dead", "Dead") == "error"
    assert d._docker_status("unknown", "") == "unknown"


def test_rollup_status_precedence_for_dashboard_groups():
    assert d._rollup_status([]) == "unknown"
    assert d._rollup_status([None, ""]) == "unknown"
    assert d._rollup_status(["ok", "ok"]) == "ok"
    assert d._rollup_status(["ok", "unknown"]) == "warn"
    assert d._rollup_status(["unknown"]) == "unknown"
    assert d._rollup_status(["ok", "auth_gated"]) == "warn"
    assert d._rollup_status(["ok", "warn", "error"]) == "error"


def test_nexus_health_safe_caution_and_stop_postures(monkeypatch):
    _wire_nexus_inputs(monkeypatch)
    safe = d._build_nexus_health()
    assert safe["posture"] == "safe"
    assert safe["summary"] == "All observed systems are safe for read-only inspection."
    assert safe["counts"]["error"] == 0
    assert safe["counts"]["warn"] == 0
    assert safe["counts"]["auth_gated"] == 0
    assert safe["needs_joseph"] == []

    caution_mission = _healthy_mission()
    for runtime in caution_mission["runtimes"]:
        if runtime["name"] == "codex":
            runtime["status"] = "degraded"
    _wire_nexus_inputs(monkeypatch, mission=caution_mission)
    caution = d._build_nexus_health()
    assert caution["posture"] == "caution"
    assert "need attention" in caution["summary"]
    assert "Codex / Claude Lanes" in caution["summary"]

    gated_topology = _healthy_topology()
    gated_topology["mcp"] = [{"id": "honcho", "name": "honcho", "status": "auth_gated"}]
    _wire_nexus_inputs(monkeypatch, topology=gated_topology)
    stop = d._build_nexus_health()
    assert stop["posture"] == "stop"
    assert any(item["id"] == "mcp-memory" for item in stop["needs_joseph"])
    assert any(item["id"] == "mcp:honcho" for item in stop["needs_joseph"])
    assert "need Joseph" in stop["summary"]


def test_nexus_health_build_aggregates_nodes_edges_counts_and_sectors(monkeypatch):
    _wire_nexus_inputs(monkeypatch)

    data = d._build_nexus_health()
    node_ids = {node["id"] for node in data["nodes"]}
    edge_pairs = {(edge["source"], edge["target"]) for edge in data["edges"]}
    sectors = {sector["id"]: sector for sector in data["sectors"]}

    assert {"dashboard", "hermes", "gateway", "kanban", "cron-watchdogs", "agent-lanes"} <= node_ids
    assert {"systemd-units", "ports", "containers", "mcp-memory", "source-tree", "audit-store"} <= node_ids
    assert ("dashboard", "hermes") in edge_pairs
    assert ("hermes", "gateway") in edge_pairs
    assert sum(data["counts"].values()) == len(data["nodes"])
    assert set(sectors) == {"pulse", "hives", "codex"}
    assert sectors["pulse"]["summary"].endswith("Last completion: green-run.")


def test_build_nexus_sectors_classifies_degraded_dashboard_sources():
    sectors = {sector["id"]: sector for sector in d._build_nexus_sectors(
        pulse={
            "active_hives": 2,
            "pending_cards": 3,
            "today_pr_merges": 1,
            "today_spend_usd": 1.25,
            "last_completion": {"slug": "packet-a"},
        },
        hives={
            "hives": [{"id": "blocked", "status": "blocked"}],
            "active_count": 0,
            "completed_count": 1,
            "stale_count": 0,
        },
        codex={
            "sessions": [
                {"id": "active", "state": "EXECUTING", "worktree_alive": True},
                {"id": "bad", "state": "ESCALATED", "worktree_alive": True},
            ],
            "counts": {"total": 2, "by_state": {"EXECUTING": 1, "ESCALATED": 1}, "ports_claimed": 1},
        },
    )}

    assert sectors["pulse"]["status"] == "warn"
    assert sectors["hives"]["status"] == "error"
    assert sectors["codex"]["status"] == "error"
    assert "3 queued card(s)" in sectors["pulse"]["summary"]
    assert any(metric == {"label": "Spend today", "value": "$1.25"} for metric in sectors["pulse"]["metrics"])
    assert any(metric == {"label": "States", "value": "ESCALATED:1, EXECUTING:1"} for metric in sectors["codex"]["metrics"])
    assert "No dispatch" in sectors["pulse"]["guardrail"]
    assert "Ruflo is retired" in sectors["hives"]["guardrail"]
    assert "No launch or merge" in sectors["codex"]["guardrail"]


def test_build_nexus_sectors_distinguishes_empty_stale_active_and_missing_worktrees():
    empty = {sector["id"]: sector for sector in d._build_nexus_sectors(
        pulse={"pending_cards": 0, "active_hives": 0, "today_pr_merges": 0, "today_spend_usd": 0},
        hives={"hives": [], "active_count": 0, "completed_count": 0, "stale_count": 0},
        codex={"sessions": [], "counts": {"total": 0, "by_state": {}}},
    )}
    assert empty["pulse"]["status"] == "ok"
    assert empty["hives"]["status"] == "unknown"
    assert empty["codex"]["status"] == "unknown"

    degraded = {sector["id"]: sector for sector in d._build_nexus_sectors(
        pulse={"_error": "pulse failed", "pending_cards": 0, "active_hives": 0, "today_pr_merges": 0},
        hives={"hives": [{"id": "old", "status": "stale"}], "active_count": 0, "completed_count": 0, "stale_count": 1},
        codex={
            "sessions": [
                {"id": "paused", "state": "PAUSED", "paused": True, "worktree_alive": True},
                {"id": "missing", "state": "COMPLETE", "worktree_alive": False},
            ],
            "counts": {"total": 2, "by_state": {"PAUSED": 1, "COMPLETE": 1}},
        },
    )}
    assert degraded["pulse"]["status"] == "warn"
    assert degraded["hives"]["status"] == "warn"
    assert degraded["codex"]["status"] == "error"


# ---------------------------------------------------------------------------
# Probe boundaries and error paths
# ---------------------------------------------------------------------------


def test_probe_all_uses_runtime_order_and_unknown_runtime_endpoint(monkeypatch):
    monkeypatch.setattr(d, "_RUNTIME_ORDER", ["b", "a"])
    monkeypatch.setattr(
        d,
        "_PROBE_MAP",
        {
            "a": lambda: {"name": "a", "status": "online"},
            "b": lambda: {"name": "b", "status": "offline"},
        },
    )

    assert [row["name"] for row in d._probe_all()] == ["b", "a"]
    assert _run(d.get_runtime_health("missing"))["status"] == "unknown"
    assert _run(d.get_runtime_health("a")) == {"name": "a", "status": "online"}


def test_run_capture_never_raises_and_systemd_parser_handles_failed_bullets(monkeypatch):
    def fake_run(cmd, capture_output, text=False, timeout=None):
        assert cmd == ["safe", "status"]
        assert capture_output is True
        assert text is True
        return SimpleNamespace(returncode=0, stdout="captured")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    assert d._run_capture(["safe", "status"], timeout=1.0) == (0, "captured")

    monkeypatch.setattr(d.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert d._run_capture(["safe", "status"]) == (1, "")

    systemd_out = "\n".join(
        [
            "hermes-dashboard.service loaded active running Dashboard API",
            "● hermes-gateway.service loaded failed failed Gateway adapter",
            "not-hermes.service loaded active running ignored",
            "garbage",
        ]
    )
    monkeypatch.setattr(d, "_run_capture", lambda cmd, timeout: (0, systemd_out))
    units = d._probe_systemd_units()

    assert [unit["name"] for unit in units] == ["hermes-dashboard.service", "hermes-gateway.service"]
    assert [unit["status"] for unit in units] == ["ok", "error"]
    assert units[1]["description"] == "Gateway adapter"

    monkeypatch.setattr(d, "_run_capture", lambda cmd, timeout: (1, ""))
    assert d._probe_systemd_units() == []


def test_docker_and_port_probes_are_filtered_and_offline_without_real_network(monkeypatch):
    docker_out = "\n".join(
        [
            "supabase_db_test\trunning\tUp 2 hours (healthy)\tpostgres\t5434/tcp",
            "supabase_api_test\trunning\tUp 2 hours (unhealthy)\tkong\t54321/tcp",
            "redis_test\trunning\tUp 2 hours\tredis\t6379/tcp",
            "supabase_dead_test\texited\tExited (1)\tpostgres\t",
        ]
    )
    monkeypatch.setattr(d, "_run_capture", lambda cmd, timeout: (0, docker_out))
    containers = d._probe_docker_containers()

    assert [container["name"] for container in containers] == [
        "supabase_db_test",
        "supabase_api_test",
        "supabase_dead_test",
    ]
    assert [container["status"] for container in containers] == ["ok", "warn", "error"]

    tcp_calls: list[int] = []
    monkeypatch.setattr(d, "_probe_port_9119_http", lambda: ("online", 2.5))

    def fake_tcp(host, port, timeout):
        tcp_calls.append(port)
        return "offline", None

    monkeypatch.setattr(d, "_tcp_latency", fake_tcp)
    ports = d._probe_ports()
    by_port = {entry["port"]: entry for entry in ports}

    assert 9119 not in tcp_calls
    assert by_port[9119]["status"] == "ok"
    assert by_port[9119]["online"] is True
    assert "application-level HTTP ping" in by_port[9119]["description"]
    assert all(by_port[port]["status"] == "error" for port in tcp_calls)


def test_runtime_process_probes_classify_missing_and_degraded_tools(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("retired Ruflo probe must not shell out")

    monkeypatch.setattr(d.subprocess, "run", fail_if_called)
    retired = d._probe_ruflo()

    assert retired["status"] == "retired"
    assert retired["active"] is False
    assert retired["latencyMs"] is None
    assert "retired" in retired["detail"]

    monkeypatch.setattr(d, "_process_alive", lambda name: ("offline", 7.0))
    monkeypatch.setattr(d.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="123\n"))
    codex = d._probe_codex()
    assert codex["status"] == "online"
    assert codex["latencyMs"] == 7.0


def test_probe_hermes_gateway_state_classifies_alive_dead_and_unreadable(monkeypatch):
    import gateway.status as gateway_status

    state_path = d.HERMES_HOME / "gateway_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    alive_pids = {123}
    monkeypatch.setattr(gateway_status, "_pid_exists", lambda pid: pid in alive_pids)

    state_path.write_text(json.dumps({
        "gateway_state": "running",
        "pid": 123,
        "platforms": {"telegram": {"state": "connected"}, "discord": {"state": "error"}},
    }))
    online = d._probe_hermes()
    assert online["status"] == "online"
    assert online["detail"] == "telegram:connected, discord:error"

    state_path.write_text(json.dumps({"gateway_state": "starting", "pid": 123, "platforms": {}}))
    assert d._probe_hermes()["status"] == "degraded"

    state_path.write_text(json.dumps({"gateway_state": "running", "pid": 999, "platforms": {}}))
    assert d._probe_hermes()["status"] == "offline"

    state_path.write_text("not json")
    unknown = d._probe_hermes()
    assert unknown["status"] == "unknown"
    assert unknown["detail"] is None


def test_probe_cron_classifies_enabled_disabled_and_missing_state():
    jobs_path = d.HERMES_HOME / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)

    jobs_path.write_text(json.dumps({"jobs": [{"enabled": False}, {"enabled": False}]}))
    disabled = d._probe_cron()
    assert disabled["status"] == "degraded"
    assert disabled["detail"] == "0/2 jobs enabled"

    jobs_path.write_text(json.dumps({"jobs": [{"enabled": True}, {"enabled": False}]}))
    enabled = d._probe_cron()
    assert enabled["status"] == "online"
    assert enabled["detail"] == "1/2 jobs enabled"

    jobs_path.unlink()
    missing = d._probe_cron()
    assert missing["status"] == "unknown"
    assert missing["latencyMs"] is None


def test_kanban_probe_and_queue_depth_use_only_tmp_sqlite_state():
    assert d._probe_kanban()["status"] == "unknown"

    legacy_db = d.HERMES_HOME / "kanban.db"
    _make_kanban_db(
        legacy_db,
        [("todo", None), ("in_progress", None), ("done", None), ("archived", None), ("complete", None)],
    )
    legacy = d._probe_kanban()
    assert legacy["status"] == "online"
    assert legacy["detail"] == "2 open tasks"

    board_db = d.HERMES_HOME / "kanban" / "boards" / "main" / "kanban.db"
    now_ts = int(time.time())
    _make_kanban_db(board_db, [("todo", now_ts), ("done", now_ts), ("archived", now_ts - 90 * 86400)])
    queue = d._get_queue_depth("7d")
    assert queue["openNow"] == 1
    assert sum(point["count"] for point in queue["points"]) == 2


# ---------------------------------------------------------------------------
# Dashboard formatting helpers and data readers
# ---------------------------------------------------------------------------


def test_node_metric_cards_format_values_for_dashboard_display():
    cards = d._node_metric_cards(
        {
            "metrics": {
                "latency_ms": 12.345,
                "online": True,
                "platforms": ["telegram", "discord", "slack", "sms", "matrix"],
                "missing": None,
                "empty": [],
            }
        }
    )

    assert {"label": "Latency Ms", "value": "12.35"} in cards
    assert {"label": "Online", "value": "yes"} in cards
    assert {"label": "Platforms", "value": "telegram, discord, slack, sms"} in cards
    assert all(card["label"] not in {"Missing", "Empty"} for card in cards)


def test_node_recommendations_format_fix_and_optimization_commands():
    unhealthy = d._node_recommendations(
        {
            "id": "svc:hermes-gateway.service",
            "status": "error",
            "kind": "service",
            "label": "gateway",
            "metrics": {"unit": "hermes-gateway.service"},
            "needs_joseph": True,
        }
    )
    assert any(rec["kind"] == "fix" and "journalctl --user -u hermes-gateway.service" in (rec["command"] or "") for rec in unhealthy)
    assert any(rec["title"] == "Human gate is active" for rec in unhealthy)

    healthy = d._node_recommendations(
        {
            "id": "port:9119",
            "status": "ok",
            "kind": "port",
            "label": "Dashboard API",
            "metrics": {"port": 9119, "latency_ms": 75.0},
        }
    )
    assert healthy == [
        {
            "kind": "optimization",
            "title": "Reachable — latency is elevated",
            "detail": "TCP connect succeeded in 75.0 ms. Sub-50 ms localhost latency is healthy.",
            "command": None,
        }
    ]


def test_build_node_detail_attaches_metric_cards_history_recommendations_and_connections(monkeypatch):
    health = {
        "generated_at": "2026-06-10T00:00:00+00:00",
        "nodes": [
            {"id": "kanban", "label": "Kanban", "status": "ok", "kind": "kanban", "metrics": {"active_tasks": 2}},
            {"id": "hermes", "label": "Hermes", "status": "ok", "kind": "runtime", "metrics": {}},
        ],
        "edges": [{"id": "hermes->kanban", "source": "hermes", "target": "kanban", "label": "queue", "status": "ok"}],
    }
    monkeypatch.setattr(d, "_get_nexus_health", lambda: health)
    monkeypatch.setattr(d, "_get_queue_depth", lambda range_str: {"points": [{"date": "2026-06-10", "count": 2}], "openNow": 2})

    detail = d._build_node_detail("kanban")

    assert detail is not None
    assert detail["generated_at"] == health["generated_at"]
    assert detail["metric_cards"] == [{"label": "Active Tasks", "value": "2"}]
    assert detail["history"][0]["kind"] == "queue"
    assert detail["recommendations"][0]["kind"] == "optimization"
    assert detail["connections"] == [{"id": "hermes->kanban", "label": "queue", "status": "ok", "direction": "in", "peer": "hermes"}]
    assert d._build_node_detail("missing") is None


def test_spend_points_ignore_bad_old_and_missing_usage_rows():
    project = d.CLAUDE_PROJECTS_DIR / "project-a"
    project.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=45)
    rows = [
        {
            "timestamp": now.isoformat(),
            "message": {
                "model": "claude-haiku-4-5",
                "usage": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 500_000,
                    "cache_read_input_tokens": 100_000,
                    "cache_creation_input_tokens": 10_000,
                },
            },
        },
        {"timestamp": old.isoformat(), "message": {"usage": {"input_tokens": 9_000_000}}},
        {"timestamp": now.isoformat(), "message": {}},
        "not-json",
    ]
    session = project / "session.jsonl"
    session.write_text("\n".join(json.dumps(row) if isinstance(row, dict) else row for row in rows))

    points = d._compute_spend_points(30)

    assert points == [
        {
            "date": now.strftime("%Y-%m-%d"),
            "model": "claude-haiku-4-5",
            "amountUsd": 2.818,
            "tokenCount": 1_500_000,
        }
    ]


def test_recent_sessions_are_sorted_and_bad_state_is_empty():
    sessions_dir = d.HERMES_HOME / "sessions"
    sessions_dir.mkdir(parents=True)
    sessions_path = sessions_dir / "sessions.json"
    sessions_path.write_text(json.dumps({
        "old": {
            "session_id": "old",
            "display_name": "old session",
            "created_at": "2026-06-09T00:00:00+00:00",
            "updated_at": "2026-06-09T00:00:00+00:00",
            "model": "old-model",
        },
        "new": {
            "session_id": "new",
            "platform": "telegram",
            "created_at": "2026-06-10T00:00:00+00:00",
            "updated_at": "2026-06-10T00:00:00+00:00",
            "model": "new-model",
        },
    }))

    assert d._get_recent_sessions(limit=1) == [
        {"id": "new", "preview": "telegram", "createdAt": "2026-06-10T00:00:00+00:00", "modelUsed": "new-model"}
    ]

    sessions_path.write_text("not json")
    assert d._get_recent_sessions() == []


def test_cron_and_dream_readers_handle_missing_staged_and_latest_files():
    cron_dir = d.HERMES_HOME / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(json.dumps({
        "jobs": [
            {
                "id": "later",
                "name": "later",
                "enabled": True,
                "state": "idle",
                "schedule": {"expr": "0 10 * * *"},
                "next_run_at": "2026-06-10T10:00:00+00:00",
                "last_run_at": None,
                "last_status": None,
            },
            {
                "id": "soon",
                "name": "soon",
                "enabled": True,
                "state": "idle",
                "schedule_display": "every hour",
                "next_run_at": "2026-06-10T09:00:00+00:00",
                "last_run_at": "2026-06-09T09:00:00+00:00",
                "last_status": "ok",
            },
        ]
    }))
    assert d._get_next_cron() == {"name": "soon", "schedule": "every hour", "nextRun": "2026-06-10T09:00:00+00:00"}
    assert d._get_all_cron_jobs()[0]["id"] == "later"

    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": []}))
    cron_d = d.HERMES_HOME / "cron.d"
    cron_d.mkdir()
    (cron_d / "dream-reflect.cron").write_text("# dream-reflect\n15 3 * * * hermes dream\n")
    staged = d._get_next_cron()
    assert staged is not None
    assert staged["name"] == "dream-reflect (staged)"

    assert d._get_last_dream() is None
    latest = _run(d.get_latest_dream())
    assert latest == {"dream": None, "date": None, "message": "No dreams directory yet"}

    dreams = d.HERMES_HOME / "dreams"
    dreams.mkdir()
    assert _run(d.get_latest_dream()) == {"dream": None, "date": None, "message": "No dream files yet"}
    (dreams / "2026-06-10.md").write_text("# Title\n\nInsight line\n")
    assert d._get_last_dream() == "Insight line"
    assert _run(d.get_latest_dream())["filename"] == "2026-06-10.md"

def test_swarm_status_no_fabrication_from_stale_ruflo_dirs(monkeypatch):
    ruflo_work = d.HERMES_HOME / "ruflo-work"
    (ruflo_work / "test-hive").mkdir(parents=True)

    def failed_ruflo_status(cmd, *args, **kwargs):
        assert cmd == ["ruflo", "swarm", "status", "--json"]
        return SimpleNamespace(returncode=1, stdout="", stderr="ruflo retired")

    monkeypatch.setattr(d.subprocess, "run", failed_ruflo_status)

    import inspect

    source = inspect.getsource(d._get_swarm_status)
    assert d._get_swarm_status() is None
    assert _run(d.get_swarm()) == {"active": False, "message": "No active swarm detected"}
    assert "Hive Mind Swarm" not in source
    assert "hierarchical-mesh" not in source




def test_retired_ruflo_probe_and_hives_ignore_stale_workdir(monkeypatch):
    stale_hive = d.RUFLO_WORK_DIR / "dead-but-live-hive"
    stale_hive.mkdir(parents=True)
    (stale_hive / "LAUNCH.sh").write_text(
        '#!/bin/bash\nTRACK_TITLE="Do Not Fabricate"\n',
        encoding="utf-8",
    )
    (stale_hive / ".ruflo-status.json").write_text(
        json.dumps({"session": "dead-session", "tracking_card": "card-123"}),
        encoding="utf-8",
    )
    (stale_hive / "hive-mind.log").write_text("old log line\n", encoding="utf-8")

    def no_ruflo_status(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ruflo":
            raise FileNotFoundError("ruflo retired")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(d.subprocess, "run", no_ruflo_status)
    monkeypatch.setattr(d, "_HIVES_CACHE", None)
    monkeypatch.setattr(d, "_NEXUS_CACHE", None)

    probe = d._probe_ruflo()
    snapshot = d._build_hives_snapshot()
    endpoint = _run(d.get_hives_snapshot())
    sectors = {sector["id"]: sector for sector in d._build_nexus_sectors(
        pulse={"pending_cards": 0, "active_hives": 0, "today_pr_merges": 0, "today_spend_usd": 0},
        hives=snapshot,
        codex={"sessions": [], "counts": {"total": 0, "by_state": {}}},
    )}

    assert probe["status"] == "retired"
    assert probe["active"] is False
    assert probe["latencyMs"] is None
    assert snapshot["status"] == "retired"
    assert snapshot["active"] is False
    assert snapshot["hives"] == []
    assert snapshot["active_count"] == 0
    assert snapshot["completed_count"] == 0
    assert snapshot["stale_count"] == 0
    assert endpoint["hives"] == []
    assert endpoint["source"] == "ruflo-retired"
    assert sectors["hives"]["status"] == "unknown"
    assert sectors["hives"]["summary"] == "Ruflo hives are retired; no live hive data is available."
    assert sectors["hives"]["metrics"] == [
        {"label": "Status", "value": "retired"},
        {"label": "Live source", "value": "no data"},
    ]
    assert "dead-but-live-hive" not in json.dumps(endpoint)
    assert "Do Not Fabricate" not in json.dumps(endpoint)
