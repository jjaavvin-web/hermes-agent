"""Tests for the System Health (nexus-health) read-only graph + node endpoint."""
import ast
from pathlib import Path


def _mock_health(
    monkeypatch,
    *,
    runtimes=None,
    topology=None,
    infra=None,
    hives=None,
    codex=None,
    pulse=None,
):
    """Wire dashboard_health probes to controlled fixtures and clear the cache."""
    from hermes_cli import dashboard_health

    monkeypatch.setattr(
        dashboard_health,
        "_get_snapshot",
        lambda: {
            "runtimes": runtimes or [],
            "model": "test-model",
            "recentSessions": [],
            "nextCron": None,
            "spendToday": 0.0,
            "spendWeek": 0.0,
            "streakDays": 0,
        },
    )
    monkeypatch.setattr(
        dashboard_health,
        "_get_gitnexus_runtime_snapshot",
        lambda: topology
        or {
            "agents": [],
            "swarms": [],
            "hives": [],
            "mcp": [],
            "gateways": [],
            "cron": [],
            "edges": [],
        },
    )
    monkeypatch.setattr(
        dashboard_health,
        "_get_infra_snapshot",
        lambda: infra or {"services": [], "containers": [], "ports": []},
    )
    monkeypatch.setattr(
        dashboard_health,
        "_get_hives_snapshot",
        lambda: hives
        or {
            "hives": [],
            "scanned_at": "2026-05-28T00:00:00Z",
            "active_count": 0,
            "completed_count": 0,
            "stale_count": 0,
        },
    )
    monkeypatch.setattr(
        dashboard_health,
        "_get_codex_sessions_snapshot",
        lambda: codex
        or {
            "scanned_at": "2026-05-28T00:00:00Z",
            "sessions": [],
            "counts": {"total": 0, "by_state": {}, "ports_claimed": 0, "ports_free": 0},
            "review_pool": {},
        },
    )
    monkeypatch.setattr(
        dashboard_health,
        "_get_pulse_kpis_snapshot",
        lambda: pulse
        or {
            "active_hives": 0,
            "pending_cards": 0,
            "max_usage_pct": None,
            "today_spend_usd": 0.0,
            "today_pr_merges": 0,
            "last_completion": None,
        },
    )
    monkeypatch.setattr(dashboard_health, "_NEXUS_CACHE", None)
    return dashboard_health


def test_nexus_health_endpoint_returns_required_schema(monkeypatch):
    d = _mock_health(
        monkeypatch,
        runtimes=[
            {"name": "hermes", "label": "Hermes", "status": "online", "detail": "gateway running"},
            {"name": "kanban", "label": "Kanban", "status": "degraded", "detail": "DB reachable"},
            {"name": "cron", "label": "Cron", "status": "unknown"},
            {"name": "codex", "label": "Codex", "status": "online"},
            {"name": "ruflo", "label": "Ruflo", "status": "unknown"},
            {"name": "claude-code", "label": "Claude Code", "status": "unknown"},
        ],
        topology={
            "gateways": [{"id": "default", "status": "running", "platforms": ["telegram"]}],
            "agents": [{"id": "default", "status": "running"}],
            "mcp": [{"id": "notion", "name": "notion", "status": "enabled"}],
            "cron": [{"id": "job", "name": "job"}],
            "hives": [],
            "swarms": [],
            "edges": [],
        },
    )

    paths = {route.path for route in d.router.routes}
    assert "/api/dashboard/nexus-health" in paths
    assert "/api/dashboard/nexus-health/node/{node_id}" in paths

    data = d._build_nexus_health()
    assert {
        "generated_at",
        "posture",
        "summary",
        "counts",
        "nodes",
        "edges",
        "sectors",
        "needs_joseph",
        "safe_actions",
        "locked_actions",
        "evidence",
    } <= set(data)
    assert data["posture"] == "caution"
    assert data["nodes"] and data["edges"]
    assert data["safe_actions"] and data["locked_actions"]

    # Every node carries a layout group, and every edge resolves to a node.
    node_ids = {n["id"] for n in data["nodes"]}
    assert all(n["group"] for n in data["nodes"])
    for edge in data["edges"]:
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids


def test_nexus_health_includes_required_topology_nodes(monkeypatch):
    d = _mock_health(monkeypatch)
    data = d._build_nexus_health()
    node_ids = {node["id"] for node in data["nodes"]}

    assert {
        "hermes",
        "dashboard",
        "gateway",
        "kanban",
        "cron-watchdogs",
        "gitnexus-explorer",
        "mcp-memory",
        "agent-lanes",
        "audit-store",
        "source-tree",
        "systemd-units",
        "ports",
        "containers",
    } <= node_ids


def test_nexus_health_enriches_infrastructure(monkeypatch):
    d = _mock_health(
        monkeypatch,
        topology={
            "agents": [],
            "swarms": [],
            "hives": [],
            "cron": [],
            "edges": [],
            "gateways": [],
            "mcp": [{"id": "notion", "name": "notion", "status": "enabled"}],
        },
        infra={
            "services": [
                {
                    "name": "hermes-dashboard.service",
                    "load": "loaded",
                    "active": "active",
                    "sub": "running",
                    "description": "Dashboard",
                    "status": "ok",
                },
                {
                    "name": "hermes-gitnexus-runtime.service",
                    "load": "loaded",
                    "active": "failed",
                    "sub": "failed",
                    "description": "Runtime topology",
                    "status": "error",
                },
            ],
            "containers": [
                {
                    "name": "supabase_db_demo",
                    "state": "running",
                    "status_text": "Up 2 days (healthy)",
                    "image": "postgres",
                    "ports": "",
                    "status": "ok",
                },
            ],
            "ports": [
                {
                    "port": 9119,
                    "label": "Dashboard API",
                    "description": "dashboard",
                    "online": True,
                    "latencyMs": 1.0,
                    "status": "ok",
                },
            ],
        },
    )
    data = d._build_nexus_health()
    ids = {n["id"] for n in data["nodes"]}
    assert "svc:hermes-dashboard.service" in ids
    assert "svc:hermes-gitnexus-runtime.service" in ids
    assert "ctr:supabase_db_demo" in ids
    assert "port:9119" in ids
    assert "mcp:notion" in ids

    kinds = {n["kind"] for n in data["nodes"]}
    assert {"service", "container", "port", "mcp"} <= kinds

    # A failed systemd unit surfaces as an error node.
    failed = next(n for n in data["nodes"] if n["id"] == "svc:hermes-gitnexus-runtime.service")
    assert failed["status"] == "error"


def test_nexus_health_node_detail(monkeypatch):
    d = _mock_health(
        monkeypatch,
        runtimes=[{"name": "kanban", "label": "Kanban", "status": "online"}],
        infra={
            "services": [
                {
                    "name": "hermes-gateway.service",
                    "load": "loaded",
                    "active": "failed",
                    "sub": "failed",
                    "description": "Gateway",
                    "status": "error",
                }
            ],
            "containers": [],
            "ports": [],
        },
    )

    # A healthy node yields optimization recommendations.
    ok_detail = d._build_node_detail("dashboard")
    assert ok_detail is not None
    assert ok_detail["metric_cards"]
    assert ok_detail["recommendations"]
    assert all(r["kind"] == "optimization" for r in ok_detail["recommendations"])
    assert "connections" in ok_detail

    # An unhealthy node yields fix recommendations, at least one with a command.
    bad_detail = d._build_node_detail("svc:hermes-gateway.service")
    assert bad_detail is not None
    assert any(r["kind"] == "fix" for r in bad_detail["recommendations"])
    assert any(r["command"] for r in bad_detail["recommendations"])

    # An unknown node returns None (the endpoint maps this to a 404).
    assert d._build_node_detail("does-not-exist") is None


def test_nexus_health_node_history_uses_real_series(monkeypatch):
    d = _mock_health(
        monkeypatch,
        runtimes=[{"name": "kanban", "label": "Kanban", "status": "online"}],
    )
    monkeypatch.setattr(
        d,
        "_get_queue_depth",
        lambda rng: {
            "range": rng,
            "openNow": 3,
            "points": [{"date": "2026-05-20", "count": 2}],
        },
    )
    detail = d._build_node_detail("kanban")
    assert detail is not None
    assert any(h["kind"] == "queue" for h in detail["history"])


def test_infra_status_mappers():
    from hermes_cli import dashboard_health as d

    assert d._systemd_status("active", "running") == "ok"
    assert d._systemd_status("failed", "failed") == "error"
    assert d._systemd_status("activating", "start") == "warn"
    assert d._docker_status("running", "Up 2 days (healthy)") == "ok"
    assert d._docker_status("running", "Up (unhealthy)") == "warn"
    assert d._docker_status("exited", "Exited (1) 3 days ago") == "error"
    assert d._rollup_status(["ok", "ok"]) == "ok"
    assert d._rollup_status(["ok", "error"]) == "error"
    assert d._rollup_status(["ok", "warn"]) == "warn"
    assert d._rollup_status([]) == "unknown"


def test_nexus_health_does_not_import_gitnexus_ingest_path():
    source = Path("hermes_cli/dashboard_health.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert "hermes_cli.gitnexus_runtime_adapter" not in imported_modules
    assert ".ingest(" not in source

def test_nexus_health_summary_points_to_attention_chips_without_ellipsis(monkeypatch):
    d = _mock_health(
        monkeypatch,
        runtimes=[
            {"name": "hermes", "label": "Hermes", "status": "online"},
            {"name": "kanban", "label": "Kanban", "status": "degraded"},
            {"name": "cron", "label": "Cron", "status": "degraded"},
            {"name": "codex", "label": "Codex", "status": "offline"},
            {"name": "ruflo", "label": "Ruflo", "status": "offline"},
            {"name": "claude-code", "label": "Claude Code", "status": "offline"},
        ],
        infra={
            "services": [
                {"name": "svc-one", "status": "error", "active": "failed", "sub": "failed"},
                {"name": "svc-two", "status": "error", "active": "failed", "sub": "failed"},
            ],
            "containers": [],
            "ports": [],
        },
    )

    data = d._build_nexus_health()

    assert "…" not in data["summary"]
    assert "+" in data["summary"]
    assert "shown as chips" in data["summary"]


def test_nexus_health_exposes_consolidated_sector_snapshot(monkeypatch):
    d = _mock_health(
        monkeypatch,
        pulse={
            "active_hives": 2,
            "pending_cards": 5,
            "max_usage_pct": 68,
            "today_spend_usd": 1.25,
            "today_pr_merges": 1,
            "last_completion": {"slug": "system-health"},
        },
        hives={
            "hives": [
                {"id": "hive-live", "status": "running"},
                {"id": "hive-stale", "status": "stale"},
            ],
            "scanned_at": "2026-05-28T00:00:00Z",
            "active_count": 1,
            "completed_count": 0,
            "stale_count": 1,
        },
        codex={
            "scanned_at": "2026-05-28T00:00:00Z",
            "sessions": [
                {"id": "codex-a", "state": "EXECUTING", "paused": False, "worktree_alive": True},
                {"id": "codex-b", "state": "ESCALATED", "paused": False, "worktree_alive": True},
            ],
            "counts": {
                "total": 2,
                "by_state": {"EXECUTING": 1, "ESCALATED": 1},
                "ports_claimed": 1,
                "ports_free": 3,
            },
            "review_pool": {},
        },
    )

    data = d._build_nexus_health()
    sectors = {sector["id"]: sector for sector in data["sectors"]}

    assert set(sectors) == {"pulse", "hives", "codex"}
    assert sectors["pulse"]["kind"] == "read_only_drilldown"
    assert sectors["pulse"]["href"] == "/pulse"
    assert sectors["pulse"]["status"] == "warn"
    assert any(
        metric["label"] == "Pending cards" and metric["value"] == "5"
        for metric in sectors["pulse"]["metrics"]
    )
    assert "No dispatch" in sectors["pulse"]["guardrail"]

    assert sectors["hives"]["href"] == "/hives"
    assert sectors["hives"]["status"] == "warn"
    assert "No Ruflo launch" in sectors["hives"]["guardrail"]

    assert sectors["codex"]["href"] == "/codex-sessions"
    assert sectors["codex"]["status"] == "error"
    assert any(
        metric["label"] == "Escalated" and metric["value"] == "1"
        for metric in sectors["codex"]["metrics"]
    )
    assert "No launch or merge controls" in sectors["codex"]["guardrail"]
