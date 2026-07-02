"""Tests for dashboard Content-Security-Policy report-only headers."""

from __future__ import annotations

import pytest


EXPECTED_CSP_REPORT_ONLY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' ws: wss:; "
    "font-src 'self' data:; "
    "frame-ancestors 'none'"
)


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
    assert (
        response.headers["Content-Security-Policy-Report-Only"]
        == EXPECTED_CSP_REPORT_ONLY_POLICY
    )


def test_spa_index_does_not_set_enforcing_csp_header(dashboard_client):
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Content-Security-Policy-Report-Only" in response.headers
    assert "Content-Security-Policy" not in response.headers
