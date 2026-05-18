def test_nexus_health_endpoint_returns_required_schema(monkeypatch):
    from hermes_cli import dashboard_health

    monkeypatch.setattr(
        dashboard_health,
        "_get_snapshot",
        lambda: {
            "model": "test-model",
            "runtimes": [
                {
                    "name": "hermes",
                    "label": "Hermes",
                    "status": "online",
                    "detail": "gateway running",
                },
                {
                    "name": "kanban",
                    "label": "Kanban",
                    "status": "degraded",
                    "detail": "DB reachable",
                },
                {"name": "cron", "label": "Cron", "status": "unknown"},
                {"name": "codex", "label": "Codex", "status": "online"},
                {"name": "ruflo", "label": "Ruflo", "status": "unknown"},
                {"name": "claude-code", "label": "Claude Code", "status": "unknown"},
            ],
            "recentSessions": [],
            "nextCron": None,
        },
    )
    monkeypatch.setattr(
        dashboard_health,
        "_get_gitnexus_runtime_snapshot",
        lambda: {
            "gateways": [{"id": "default", "status": "running", "platforms": ["telegram"]}],
            "agents": [{"id": "default", "status": "running"}],
            "mcp": [{"id": "notion", "status": "enabled"}],
            "cron": [{"id": "job", "name": "job"}],
            "hives": [],
            "swarms": [],
            "edges": [{"source": "default", "target": "notion", "type": "USES_MCP"}],
        },
    )

    paths = {route.path for route in dashboard_health.router.routes}
    assert "/api/dashboard/nexus-health" in paths

    data = dashboard_health._build_nexus_health()
    assert {
        "generated_at",
        "posture",
        "summary",
        "nodes",
        "edges",
        "needs_joseph",
        "safe_actions",
        "locked_actions",
        "evidence",
    } <= set(data)
    assert data["posture"] == "caution"
    assert data["nodes"]
    assert data["edges"]
    assert data["safe_actions"]
    assert data["locked_actions"]


def test_nexus_health_includes_required_topology_nodes(monkeypatch):
    from hermes_cli import dashboard_health

    monkeypatch.setattr(dashboard_health, "_get_snapshot", lambda: {"runtimes": []})
    monkeypatch.setattr(
        dashboard_health,
        "_get_gitnexus_runtime_snapshot",
        lambda: {"agents": [], "swarms": [], "hives": [], "mcp": [], "gateways": [], "cron": [], "edges": []},
    )

    data = dashboard_health._build_nexus_health()
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
    } <= node_ids


def test_nexus_health_does_not_import_gitnexus_ingest_path():
    import ast
    from pathlib import Path

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
