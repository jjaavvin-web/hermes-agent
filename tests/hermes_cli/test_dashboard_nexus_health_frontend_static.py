from pathlib import Path


WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"


def _read(relative: str) -> str:
    return (WEB_SRC / relative).read_text(encoding="utf-8")


def test_api_get_nexus_health_exists():
    api = _read("lib/api.ts")

    assert "getNexusHealth" in api
    assert '"/api/dashboard/nexus-health"' in api


def test_nexus_health_route_and_nav_exist():
    app = _read("App.tsx")

    assert "NexusHealthPage" in app
    assert '"/nexus-health"' in app
    assert 'label: "Nexus Health"' in app


def test_nexus_health_page_renders_required_surfaces():
    page = _read("pages/NexusHealthPage.tsx")

    for token in ("nodes", "edges", "needs_joseph", "locked_actions"):
        assert token in page
    assert "<svg" in page


def test_nexus_health_page_avoids_mutation_apis():
    page = _read("pages/NexusHealthPage.tsx")
    forbidden = (
        "restartGateway",
        "rescanPlugins",
        "dispatch",
        "triggerCronJob",
        "setEnvVar",
        "saveConfig",
        "setModelAssignment",
        "enable",
        "disable",
    )

    offenders = [token for token in forbidden if token in page]
    assert offenders == []
