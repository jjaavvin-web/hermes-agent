"""Tests for dashboard Content-Security-Policy report-only headers."""

from __future__ import annotations

import re

import pytest


@pytest.fixture()
def dashboard_client(monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli import web_server

    monkeypatch.delattr(web_server.app.state, "bound_host", raising=False)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)

    with TestClient(web_server.app) as client:
        yield client


def test_spa_index_sets_report_only_csp_header(dashboard_client):
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["Content-Security-Policy-Report-Only"]
    assert "default-src 'self'" in policy
    assert re.search(r"script-src 'self' 'nonce-[^']+'", policy)
    assert "style-src 'self' 'unsafe-inline'" in policy
    assert "img-src 'self' data: blob:" in policy
    assert "connect-src 'self' ws: wss:" in policy
    assert "font-src 'self' data:" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy


def test_spa_index_does_not_set_enforcing_csp_header(dashboard_client):
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Content-Security-Policy-Report-Only" in response.headers
    assert "Content-Security-Policy" not in response.headers


def test_nexus_exact_path_can_be_same_origin_framed_without_widening_api_or_root(dashboard_client):
    nexus = dashboard_client.get("/nexus")

    assert nexus.status_code == 200
    assert nexus.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in nexus.headers["Content-Security-Policy-Report-Only"]
    assert "frame-ancestors 'none'" not in nexus.headers["Content-Security-Policy-Report-Only"]

    for path in ("/api/dashboard/nexus", "/api/dashboard/nexus/actions/registry", "/"):
        response = dashboard_client.get(path)
        assert response.headers["X-Frame-Options"] == "DENY", path
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy-Report-Only"], path
        assert "frame-ancestors 'self'" not in response.headers["Content-Security-Policy-Report-Only"], path

    gitnexus = dashboard_client.get("/_gitnexus-app/")
    assert gitnexus.headers["X-Frame-Options"] == "SAMEORIGIN"
