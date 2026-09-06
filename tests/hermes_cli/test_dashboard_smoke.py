"""Route-level dashboard smoke-test regression coverage."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute

from hermes_cli import dashboard_smoke

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTE_MANIFEST = _REPO_ROOT / "tests" / "fixtures" / "dashboard_route_manifest.json"


def test_dashboard_router_expected_count_matches_mounted_routes():
    from hermes_cli import web_server

    comparison = dashboard_smoke.compare_dashboard_router_mounts(web_server.app)

    assert comparison["import_errors"] == {}
    assert comparison["missing"] == []
    assert comparison["unexpected"] == []
    assert comparison["expected_count"] == comparison["mounted_count"]
    # Anti-fake-green floor: currently this covers mission/codex/os/connectome/
    # learning/cost/get-some/command-center router GETs, not just /api/status.
    assert comparison["mounted_count"] >= 25


def test_dashboard_router_import_failure_goes_red(monkeypatch):
    """Prove the mount-count gate catches the try/except silent-drop class."""
    expected, errors = dashboard_smoke.expected_dashboard_router_paths()
    assert errors == {}
    assert "/api/dashboard/mission" in expected

    app = FastAPI()
    # Simulate web_server's broad try/except swallowing dashboard_health import:
    # include every other dashboard router, but omit mission-control routes.
    for module_name in dashboard_smoke.DASHBOARD_ROUTER_MODULES:
        if module_name == "hermes_cli.dashboard_health":
            continue
        module = importlib.import_module(module_name)
        app.include_router(module.router)

    comparison = dashboard_smoke.compare_dashboard_router_mounts(app, expected_paths=expected)

    assert comparison["expected_count"] == len(expected)
    assert comparison["mounted_count"] < comparison["expected_count"]
    assert "/api/dashboard/mission" in comparison["missing"]
    assert comparison["ok"] is False


def test_dashboard_smoke_extracts_injected_spa_token(tmp_path):
    web_dist = tmp_path / "web_dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "assets" / "index.js").write_text("console.log('ok');", encoding="utf-8")
    (web_dist / "index.html").write_text(
        '<!doctype html><html><head><script type="module" src="/assets/index.js"></script></head><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    env["HERMES_WEB_DIST"] = str(web_dist)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    script = textwrap.dedent(
        """
        from fastapi.testclient import TestClient
        from hermes_cli import dashboard_smoke
        import hermes_cli.web_server as ws

        with TestClient(ws.app) as client:
            token = dashboard_smoke.get_spa_session_token(client)
        assert token == ws._SESSION_TOKEN
        assert token
        """
    )
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def test_dashboard_stream_query_token_allowed_and_missing_token_rejected():
    """Mission Control EventSource uses ?token= because it cannot set headers."""
    from hermes_cli import web_server as ws

    class FakeURL:
        path = "/api/dashboard/stream"

    class FakeQuery:
        def __init__(self, token: str = ""):
            self._token = token

        def get(self, _name: str, default: str = "") -> str:
            return self._token or default

    class FakeHeaders:
        def get(self, _name: str, default: str = "") -> str:
            return default

    class FakeRequest:
        headers = FakeHeaders()
        url = FakeURL()

        def __init__(self, token: str = ""):
            self.query_params = FakeQuery(token)

    assert "/api/dashboard/stream" in ws._QUERY_TOKEN_PATHS
    assert ws._has_valid_session_token(cast(Request, FakeRequest(ws._SESSION_TOKEN))) is True
    assert ws._has_valid_session_token(cast(Request, FakeRequest(""))) is False
    response = dashboard_smoke._probe_stream_route(
        cast(APIRoute, next(route for route in ws.app.routes if getattr(route, "path", "") == "/api/dashboard/stream")),
        "/api/dashboard/stream",
    )
    assert response.ok is True
    assert response.status_code == 200
    assert response.content_type == "text/event-stream"


# Probes ~383 GET routes through the real app: measured 17-32 s unloaded on a
# 4-vCPU box. Under the sliced CI job (8 parallel per-file pytest workers on a
# 4-vCPU runner, 8x oversubscription of the thread-based timeout) it exceeds
# the fork's 30 s addopts ceiling even though the standalone smoke gate step
# passes. Same 180 s bump as tests/docker/conftest.py; assertions unchanged.
@pytest.mark.timeout(180)
def test_dashboard_smoke_enumerates_full_app_and_probes_all_api_get_routes(tmp_path, monkeypatch):
    from hermes_cli import cost_reconcile, web_server
    from hermes_state import SessionDB

    # Exercise the real cost route with an initialized, isolated ledger. An
    # absent database would return early and never exercise policy loading.
    ledger_path = tmp_path / "cost-smoke.db"
    SessionDB(db_path=ledger_path).close()
    monkeypatch.setattr(cost_reconcile, "_state_db_path", lambda: ledger_path)

    policy_path = tmp_path / "provider-stack.lock.yaml"
    policy_path.write_text(
        "lock:\n"
        "  default_lane: {provider: fixture-provider, model: fixture-model}\n"
        "  forbidden: [paid_fallback]\n",
        encoding="utf-8",
    )
    load_lane_policy = cost_reconcile.load_lane_policy
    policy_reads = []

    def load_smoke_policy(_path):
        policy_reads.append(policy_path)
        return load_lane_policy(policy_path)

    monkeypatch.setattr(cost_reconcile, "load_lane_policy", load_smoke_policy)

    report = dashboard_smoke.run_dashboard_smoke(
        web_server.app,
        status_file=tmp_path / "dashboard-smoke.json",
        expected_manifest=dashboard_smoke.load_route_manifest(_ROUTE_MANIFEST),
    )

    cost_route = next(route for route in report.route_results if route.path == "/api/dashboard/cost/reconcile")
    assert policy_reads, "cost reconciliation did not exercise the temporary policy"
    assert cost_route.probe is not None
    assert cost_route.probe.status_code == 200, cost_route.probe.error
    assert report.ok is True
    assert report.route_count_total == len(web_server.app.routes)
    assert report.route_count_total >= 273
    assert len(report.route_results) == report.route_count_total
    assert report.api_get_route_count_total >= 120
    assert report.mutating_route_count_total >= 130
    assert report.missing_app_routes == []
    assert report.unexpected_app_routes == []
    assert report.route_manifest_actual_count is not None
    assert report.route_manifest_expected_count is not None
    assert report.route_manifest_actual_count >= report.route_manifest_expected_count
    assert report.expected_import_errors == {}
    assert report.missing_dashboard_routes == []
    assert report.unexpected_dashboard_router_routes == []

    api_get_results = [
        route for route in report.route_results if route.is_api_route and route.is_get_route
    ]
    assert len(api_get_results) == report.api_get_route_count_total
    assert api_get_results
    for route in api_get_results:
        assert route.probed or route.skipped, f"{route.methods} {route.path} had no probe/skip result"
        if route.skipped:
            assert route.skip_reason
        else:
            assert route.probe is not None
            assert route.probe.ok, route.probe.error

    mutating_results = [route for route in report.route_results if route.is_mutating_route]
    assert len(mutating_results) == report.mutating_route_count_total
    for route in mutating_results:
        assert route.registered is True
        assert route.handler_importable is True, route.handler_import_error
        assert route.probed is False
        assert route.skipped is True
        assert "without executing side effects" in (route.skip_reason or "")

    written = json.loads((tmp_path / "dashboard-smoke.json").read_text(encoding="utf-8"))
    assert len(written["route_results"]) == report.route_count_total
    assert written["api_get_route_count_total"] == report.api_get_route_count_total


def test_dashboard_smoke_declared_4xx_seed_for_parameterized_get():
    from hermes_cli import web_server

    route = cast(
        APIRoute,
        next(
            route
            for route in web_server.app.routes
            if isinstance(route, APIRoute) and route.path == "/api/sessions/{session_id}"
        ),
    )
    expected, reason = dashboard_smoke._declared_expected_for_route(route)

    assert 404 in expected
    assert 422 in expected
    assert reason is not None
    assert "path seed" in reason


def test_dashboard_smoke_accepts_inactive_ssh_ownership():
    from hermes_cli import web_server

    route = cast(
        APIRoute,
        next(
            route
            for route in web_server.app.routes
            if isinstance(route, APIRoute) and route.path == "/api/ssh/ownership"
        ),
    )
    expected, reason = dashboard_smoke._declared_expected_for_route(route)

    assert expected == {200, 404}
    assert reason == "declared inactive SSH ownership response accepted"


def test_dashboard_route_manifest_gate_fails_on_missing_and_unexpected_route():
    from hermes_cli import web_server

    expected = dashboard_smoke.load_route_manifest(_ROUTE_MANIFEST)
    assert expected
    dropped = expected[1:]
    comparison = dashboard_smoke.compare_route_manifest(web_server.app, dropped)

    assert comparison["ok"] is False
    assert comparison["unexpected"]

    with_extra = list(expected) + [
        {
            "path": "/api/dashboard-smoke/nonexistent",
            "methods": ["GET"],
            "name": "missing_for_test",
            "route_type": "APIRoute",
        }
    ]
    comparison = dashboard_smoke.compare_route_manifest(web_server.app, with_extra)

    assert comparison["ok"] is False
    assert "GET /api/dashboard-smoke/nonexistent" in comparison["missing"]


def test_dashboard_route_manifest_fixture_matches_live_app():
    from hermes_cli import web_server

    expected = dashboard_smoke.load_route_manifest(_ROUTE_MANIFEST)
    comparison = dashboard_smoke.compare_route_manifest(web_server.app, expected)

    assert comparison == {
        "ok": True,
        "expected_count": len(expected),
        "actual_count": len(dashboard_smoke.route_manifest(web_server.app)),
        "missing": [],
        "unexpected": [],
    }
