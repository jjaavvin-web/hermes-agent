"""Static checks for the System Health frontend wiring."""
from pathlib import Path


WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"

_SYSTEM_HEALTH_FILES = (
    "pages/NexusHealthPage.tsx",
    "components/system-health/HealthGraph.tsx",
    "components/system-health/FilterRail.tsx",
    "components/system-health/DetailPanel.tsx",
    "components/system-health/constants.ts",
)


def _read(relative: str) -> str:
    return (WEB_SRC / relative).read_text(encoding="utf-8")


def _read_all() -> str:
    return "".join(_read(path) for path in _SYSTEM_HEALTH_FILES)


def test_api_exposes_nexus_health_endpoints():
    api = _read("lib/api.ts")
    assert "getNexusHealth" in api
    assert '"/api/dashboard/nexus-health"' in api
    assert "getNexusHealthNode" in api
    assert "/api/dashboard/nexus-health/node/" in api
    assert "NexusHealthNodeDetail" in api
    assert "NexusHealthSector" in api
    assert "sectors: NexusHealthSector[]" in api


def test_system_health_route_and_nav_exist():
    app = _read("App.tsx")
    assert "NexusHealthPage" in app
    assert '"/nexus-health"' in app
    assert 'label: "System Health"' in app


def test_cockpit_route_is_retired():
    app = _read("App.tsx")
    assert '"/cockpit"' not in app
    assert "MissionControlPage" not in app


def test_system_health_uses_interactive_graph():
    page = _read("pages/NexusHealthPage.tsx")
    graph = _read("components/system-health/HealthGraph.tsx")
    assert "HealthGraph" in page
    assert "FilterRail" in page
    assert "DetailPanel" in page
    assert "@xyflow/react" in graph
    assert "ReactFlow" in graph


def test_system_health_renders_required_surfaces():
    text = _read_all()
    for token in (
        "nodes",
        "edges",
        "sectors",
        "needs_joseph",
        "locked_actions",
        "recommendations",
        "metric_cards",
    ):
        assert token in text, token


def test_system_health_is_read_only():
    """The System Health UI must not call any state-changing dashboard API."""
    text = _read_all()
    forbidden = (
        "api.restartGateway",
        "api.rescanPlugins",
        "api.triggerCronJob",
        "api.setEnvVar",
        "api.saveConfig",
        "api.setModelAssignment",
        "api.installPlugin",
        "api.deletePlugin",
    )
    offenders = [name for name in forbidden if name in text]
    assert offenders == [], offenders

def test_system_health_renders_attention_targets_and_accessible_filter_state():
    page = _read("pages/NexusHealthPage.tsx")
    detail = _read("components/system-health/DetailPanel.tsx")
    filters = _read("components/system-health/FilterRail.tsx")

    assert 'aria-label="Critical summary"' in page
    assert 'aria-label="attention-targets"' in page
    assert "Inspect attention target" in page
    assert "ATTENTION_SCORE" in page
    assert "Command posture" in detail
    assert "Approval locked" in detail
    assert "aria-pressed={active}" in filters
    assert "Reset System Health filters" in filters


def test_system_health_renders_consolidated_sector_drilldowns():
    page = _read("pages/NexusHealthPage.tsx")
    detail = _read("components/system-health/DetailPanel.tsx")

    assert "consolidated-sector-status" in page
    assert "Consolidated sectors" in detail
    assert "read-only drilldown" in detail
    assert "Open ${sector.label} read-only drilldown" in detail
    assert "sector.guardrail" in detail
    assert "onNavigate(sector.href)" in detail


def test_system_health_avoids_known_react_compiler_pitfalls():
    page = _read("pages/NexusHealthPage.tsx")
    detail = _read("components/system-health/DetailPanel.tsx")
    constants = _read("components/system-health/constants.ts")

    assert "Date.now() - lastLiveAt" not in page
    assert "setIsMobile(mq.matches)" not in page
    assert "setLastLiveAt(Date.now())" not in page
    assert "queueMicrotask" in page
    assert "liveUntil" in page
    assert "const Icon = kindIcon" not in detail
    assert "kindIconElement" in detail
    assert "createElement(kindIcon(kind)" in constants
