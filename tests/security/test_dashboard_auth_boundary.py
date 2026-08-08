"""No registered ``/api`` route is reachable in loopback mode without the dashboard session token unless it is on the shared ``PUBLIC_API_PATHS`` allowlist or under the ``/api/mcp/oauth/callback/`` browser-redirect prefix (both middlewares exempt that prefix so external OAuth providers can redirect the user's browser there; the handler itself fails closed on flow/state matching); ``?token=`` query auth is accepted only for the SSE/download allowlists needed by browser/EventSource/download callers and must not become a universal API bypass."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

# These tests mutate ``web_server.app.state.auth_required`` so they share the
# same xdist group as the other dashboard-auth app.state tests.
pytestmark = pytest.mark.xdist_group("dashboard_auth_app_state")


_PATH_PARAM_RE = re.compile(r"\{[^}:]+(?::[^}]+)?\}")


def _iter_get_api_route_paths() -> list[str]:
    paths: list[str] = []
    for route in web_server.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        methods = getattr(route, "methods", None)
        # WebSocket routes expose no ``methods`` and are intentionally skipped;
        # this boundary test covers registered HTTP GET API routes only.
        if methods is None or "GET" not in methods:
            continue
        paths.append(path)
    return paths


def _concrete_path(path: str) -> str:
    return _PATH_PARAM_RE.sub("1", path)


# The one prefix-public API surface: external MCP OAuth providers redirect
# the user's browser here, so BOTH middlewares exempt it (loopback:
# ``auth_middleware``'s ``is_mcp_oauth_callback``; gated:
# ``_GATE_PUBLIC_PREFIXES``). The trailing slash is load-bearing — near-miss
# paths like ``/api/mcp/oauth/callbackX`` stay token-gated (pinned below).
_MCP_OAUTH_CALLBACK_PUBLIC_PREFIX = "/api/mcp/oauth/callback/"

GET_API_ROUTE_PATHS = _iter_get_api_route_paths()
GATED_GET_API_ROUTE_PATHS = [
    path
    for path in GET_API_ROUTE_PATHS
    if path not in PUBLIC_API_PATHS
    and path not in web_server._QUERY_TOKEN_PATHS
    and not path.startswith(_MCP_OAUTH_CALLBACK_PUBLIC_PREFIX)
]
PUBLIC_GET_API_ROUTE_PATHS = [
    path
    for path in sorted(PUBLIC_API_PATHS)
    if "{" not in path and path in GET_API_ROUTE_PATHS
]


@pytest.fixture
def loopback_client():
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 8080
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:8080")
    yield client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


@pytest.fixture
def known_gated_get_path() -> str:
    if "/api/config" in GATED_GET_API_ROUTE_PATHS:
        return "/api/config"
    for path in GATED_GET_API_ROUTE_PATHS:
        if "{" not in path:
            return path
    pytest.fail("No param-free gated GET /api route was registered")


def _fake_request(path: str, token: str = "", *, header_token: str = ""):
    headers = {}
    if header_token:
        headers[web_server._SESSION_HEADER_NAME] = header_token
    return SimpleNamespace(
        headers=headers,
        url=SimpleNamespace(path=path),
        query_params={"token": token} if token else {},
    )


@pytest.mark.parametrize("path", GATED_GET_API_ROUTE_PATHS)
def test_unauthenticated_api_routes_401(loopback_client, path: str):
    response = loopback_client.get(_concrete_path(path))
    assert response.status_code == 401


@pytest.mark.parametrize("path", PUBLIC_GET_API_ROUTE_PATHS)
def test_public_routes_reachable_without_token(loopback_client, path: str):
    response = loopback_client.get(path)
    assert response.status_code != 401


def test_valid_session_header_grants_access(loopback_client, known_gated_get_path: str):
    assert loopback_client.get(known_gated_get_path).status_code == 401

    session_header_response = loopback_client.get(
        known_gated_get_path,
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    )
    assert session_header_response.status_code != 401

    legacy_bearer_response = loopback_client.get(
        known_gated_get_path,
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert legacy_bearer_response.status_code != 401


def test_bad_session_token_rejected(loopback_client, known_gated_get_path: str):
    response = loopback_client.get(
        known_gated_get_path,
        headers={web_server._SESSION_HEADER_NAME: "nope"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("path", sorted(web_server._QUERY_TOKEN_PATHS))
def test_sse_accepts_query_token(path: str):
    # Drive the auth predicate directly so the EventSource/SSE routes cannot
    # block TestClient while still pinning the middleware's query-token verdict.
    request = cast(Request, _fake_request(path, web_server._SESSION_TOKEN))
    assert web_server._has_valid_session_token(request) is True


def test_query_token_rejected_off_allowlist(loopback_client, known_gated_get_path: str):
    response = loopback_client.get(
        known_gated_get_path,
        params={"token": web_server._SESSION_TOKEN},
    )
    assert response.status_code == 401


def test_download_query_token():
    ok_request = cast(Request, _fake_request("/api/files/download", web_server._SESSION_TOKEN))
    bad_request = cast(Request, _fake_request("/api/files/download", "nope"))

    assert web_server._has_valid_query_token(ok_request, "/api/files/download") is True
    assert web_server._has_valid_query_token(bad_request, "/api/files/download") is False


# ── MCP OAuth callback boundary ─────────────────────────────────────────────
# The callback prefix is browser-redirect public by design; every security
# property lives in the handler: an unauthenticated caller with no matching
# (server_name, authorization_required, constant-time state) flow gets a 404
# shell page and delivers nothing, delivery is single-use, and near-miss
# paths outside the trailing-slash prefix stay token-gated.


@pytest.fixture
def mcp_flow_registry():
    with web_server._mcp_oauth_flows_lock:
        saved = dict(web_server._mcp_oauth_flows)
        web_server._mcp_oauth_flows.clear()
    yield web_server._mcp_oauth_flows
    with web_server._mcp_oauth_flows_lock:
        web_server._mcp_oauth_flows.clear()
        web_server._mcp_oauth_flows.update(saved)


def _register_flow(registry, *, server_name: str, state: str):
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id=f"flow-{server_name}",
        server_name=server_name,
        profile=None,
        hermes_home="/nonexistent-test-home",
        redirect_uri=f"http://127.0.0.1:8080/api/mcp/oauth/callback/{server_name}",
    )
    flow.expected_state = state
    flow.status = "authorization_required"
    with web_server._mcp_oauth_flows_lock:
        registry[flow.flow_id] = flow
    return flow


def test_mcp_oauth_callback_prefix_is_public_in_both_middlewares():
    from hermes_cli.dashboard_auth.middleware import _GATE_PUBLIC_PREFIXES

    assert _MCP_OAUTH_CALLBACK_PUBLIC_PREFIX.endswith("/")
    assert _MCP_OAUTH_CALLBACK_PUBLIC_PREFIX in _GATE_PUBLIC_PREFIXES
    assert any(
        path.startswith(_MCP_OAUTH_CALLBACK_PUBLIC_PREFIX)
        for path in GET_API_ROUTE_PATHS
    ), "callback route no longer registered under the public prefix"


def test_mcp_oauth_callback_no_flow_is_public_but_404(loopback_client, mcp_flow_registry):
    response = loopback_client.get("/api/mcp/oauth/callback/unknown-server")

    assert response.status_code == 404
    assert "expired" in response.text.lower()


def test_mcp_oauth_callback_requires_matching_pre_registered_flow(
    loopback_client, mcp_flow_registry
):
    flow = _register_flow(mcp_flow_registry, server_name="srv", state="expected-state")

    mismatch = loopback_client.get(
        "/api/mcp/oauth/callback/srv",
        params={"code": "auth-code", "state": "attacker-state"},
    )
    assert mismatch.status_code == 404
    assert not flow._callback_ready.is_set(), "state mismatch must not deliver"

    match = loopback_client.get(
        "/api/mcp/oauth/callback/srv",
        params={"code": "auth-code", "state": "expected-state"},
    )
    assert match.status_code == 200
    assert flow._callback_ready.is_set()
    assert flow._callback == ("auth-code", "expected-state")


def test_mcp_oauth_callback_is_single_use(loopback_client, mcp_flow_registry):
    flow = _register_flow(mcp_flow_registry, server_name="srv2", state="one-shot")

    first = loopback_client.get(
        "/api/mcp/oauth/callback/srv2",
        params={"code": "code-1", "state": "one-shot"},
    )
    assert first.status_code == 200

    replay = loopback_client.get(
        "/api/mcp/oauth/callback/srv2",
        params={"code": "code-2", "state": "one-shot"},
    )
    assert replay.status_code == 409
    assert flow._callback == ("code-1", "one-shot"), "replay must not overwrite delivery"


@pytest.mark.parametrize(
    "near_miss",
    (
        "/api/mcp/oauth/callback",
        "/api/mcp/oauth/callbackevil",
        "/api/mcp/oauth/flows/some-flow-id",
    ),
)
def test_mcp_oauth_callback_near_miss_paths_stay_gated(loopback_client, near_miss):
    response = loopback_client.get(near_miss)
    assert response.status_code == 401
