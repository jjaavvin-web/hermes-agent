"""Tests for dashboard Content-Security-Policy report-only headers."""

from __future__ import annotations

import re

import pytest

_CSP_HEADER = "Content-Security-Policy-Report-Only"
_EXPECTED_CSP_DIRECTIVES = {
    "default-src": "'self'",
    "script-src": None,
    "style-src": "'self' 'unsafe-inline'",
    "img-src": "'self' data: blob:",
    "connect-src": "'self' ws: wss:",
    "font-src": "'self' data:",
    "object-src": "'none'",
    "frame-ancestors": "'none'",
}
_SCRIPT_SRC_RE = re.compile(r"'self' 'nonce-[A-Za-z0-9+/]{22}=='")


def _csp_directives(response) -> dict[str, str]:
    policy = response.headers[_CSP_HEADER]
    directives: dict[str, str] = {}
    for raw_directive in policy.split("; "):
        name, value = raw_directive.split(" ", 1)
        assert name not in directives, name
        directives[name] = value
    return directives


def _assert_dashboard_csp(response, *, frame_ancestors: str = "'none'") -> None:
    expected = dict(_EXPECTED_CSP_DIRECTIVES)
    expected["frame-ancestors"] = frame_ancestors
    directives = _csp_directives(response)

    assert directives.keys() == expected.keys()
    assert _SCRIPT_SRC_RE.fullmatch(directives["script-src"])
    for name, value in expected.items():
        if name == "script-src":
            continue
        assert directives[name] == value


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
    _assert_dashboard_csp(response)


def test_spa_index_does_not_set_enforcing_csp_header(dashboard_client):
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert _CSP_HEADER in response.headers
    assert "Content-Security-Policy" not in response.headers


def test_nexus_exact_path_can_be_same_origin_framed_without_widening_api_or_root(dashboard_client):
    nexus = dashboard_client.get("/nexus")

    assert nexus.status_code == 200
    assert nexus.headers["X-Frame-Options"] == "SAMEORIGIN"
    _assert_dashboard_csp(nexus, frame_ancestors="'self'")

    for path in ("/api/dashboard/nexus", "/api/dashboard/nexus/actions/registry", "/"):
        response = dashboard_client.get(path)
        assert response.headers["X-Frame-Options"] == "DENY", path
        _assert_dashboard_csp(response)

    gitnexus = dashboard_client.get("/_gitnexus-app/")
    assert gitnexus.headers["X-Frame-Options"] == "SAMEORIGIN"
