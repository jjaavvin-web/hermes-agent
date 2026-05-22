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
