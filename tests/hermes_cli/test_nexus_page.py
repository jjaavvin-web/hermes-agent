"""Tests for the W2C /nexus V6 page binding."""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ASSET = Path(__file__).resolve().parents[2] / "hermes_cli" / "nexus_slice_assets" / "nexus.html"
SERVER = Path(__file__).resolve().parents[2] / "hermes_cli" / "web_server.py"
ROOT = Path(__file__).resolve().parents[2]

TERRITORIES = ["MEMORY", "SURFACES", "CONTROL", "PROVIDERS", "INGEST", "PROTECTION", "LEARNING", "GIT", "HOST"]
SERVER_FIELDS = [
    "label",
    "reason",
    "evidence_refs",
    "safe_next_action",
    "locked_actions",
    "what_would_prove_green",
    "what_breaks_if_false",
    "last_checked",
    "observed",
    "title",
    "action_verb",
    "finding_label",
    "success_condition",
    "workspace_mode",
    "write_allowlist",
    "forbidden_actions",
    "budget",
]


def _env(tmp_path: Path) -> dict[str, str]:
    web_dist = tmp_path / "web_dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "assets" / "index.js").write_text("console.log('ok');", encoding="utf-8")
    (web_dist / "index.html").write_text("<!doctype html><html><body><div id='root'></div></body></html>", encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    env["HERMES_WEB_DIST"] = str(web_dist)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_script(tmp_path: Path, script: str) -> None:
    subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=ROOT,
        env=_env(tmp_path),
        check=True,
        text=True,
        capture_output=True,
    )


def test_nexus_page_loopback_token_nonce_and_content(tmp_path: Path) -> None:
    _run_script(
        tmp_path,
        """
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        from hermes_cli.web_server import _SESSION_TOKEN

        client = TestClient(web_server.app)
        response = client.get('/nexus')
        body = response.text
        assert response.status_code == 200
        assert response.content
        assert 'no-store' in response.headers.get('Cache-Control', '')
        assert f'window.__HERMES_SESSION_TOKEN__="{_SESSION_TOKEN}"' in body
        assert '__HERMES_CSP_NONCE__' not in body
        assert body.count('data-qa-territory') >= 9
        for name in ['MEMORY','SURFACES','CONTROL','PROVIDERS','INGEST','PROTECTION','LEARNING','GIT','HOST']:
            assert name in body
        route = next((r for r in web_server.app.routes if getattr(r, 'path', None) == '/nexus'), None)
        assert route is not None
        assert getattr(route, 'endpoint', None).__name__ != 'serve_spa'
        """,
    )


def test_nexus_page_gated_anonymous_redirects_to_login(tmp_path: Path) -> None:
    _run_script(
        tmp_path,
        """
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        web_server.app.state.auth_required = True
        client = TestClient(web_server.app)
        response = client.get('/nexus', follow_redirects=False)
        assert response.status_code == 302
        assert response.headers.get('location', '').startswith('/login')
        """,
    )


def test_nexus_page_gated_mode_withholds_token(tmp_path: Path) -> None:
    _run_script(
        tmp_path,
        """
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        from hermes_cli.dashboard_auth import middleware as auth_middleware
        from hermes_cli.web_server import _SESSION_TOKEN

        async def pass_through(request, call_next):
            return await call_next(request)

        auth_middleware.gated_auth_middleware = pass_through
        web_server.app.state.auth_required = True
        client = TestClient(web_server.app)
        response = client.get('/nexus')
        body = response.text
        assert response.status_code == 200
        assert _SESSION_TOKEN not in body
        assert 'window.__HERMES_AUTH_REQUIRED__=true' in body
        assert 'data-qa-territory' in body
        """,
    )


def test_page_source_qa_hooks() -> None:
    text = ASSET.read_text(encoding="utf-8")
    assert text.count("data-qa-territory") >= 9
    for name in TERRITORIES:
        assert name in text
    for needle in [
        "data-qa-satellite",
        'id="drawer"',
        'id="ttoggle"',
        "data-qa-truth-toggle",
        'id="truthParity"',
        'data-parity="probe_state"',
        'data-parity="freshness_age_s"',
        "data-qa-queue-chip",
        "data-qa-edge-ledger",
        "data-qa-action",
        'data-live-bound="true"',
        "sample-label",
        "__NEXUS_PERF__",
    ]:
        assert needle in text


def test_no_mutation_and_no_side_channel() -> None:
    text = ASSET.read_text(encoding="utf-8")
    api_hits = set(re.findall(r"/api/[A-Za-z0-9_./:-]+", text))
    assert api_hits == {"/api/dashboard/nexus", "/api/dashboard/nexus/actions/registry"}
    for forbidden in [
        "/slice/backup-offbox",
        "/api/dashboard/os",
        "/api/dashboard/connectome",
        "/object/",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "method:",
        "method=",
        "<form",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
    ]:
        assert forbidden not in text


def test_escaping_pinned_at_every_server_sink() -> None:
    text = ASSET.read_text(encoding="utf-8")
    assert len(re.findall(r"function\s+esc\s*\(", text)) == 1
    for field in SERVER_FIELDS:
        bad = re.findall(r"\$\{(?!esc\()[^}]*\." + re.escape(field) + r"\b", text)
        bad = [item for item in bad if not item.startswith("${esc(")]
        bad = [item for item in bad if not item.startswith("${String(")]
        bad = [item for item in bad if not item.startswith("${(")]
        bad = [item for item in bad if not item.startswith("${Array.isArray(")]
        bad = [item for item in bad if "map(esc)" not in item]
        assert bad == [], (field, bad[:5])
    assert ".map(esc)" in text


def test_enum_and_stale_literal_hygiene() -> None:
    text = ASSET.read_text(encoding="utf-8")
    for forbidden in [
        "missing",
        "79 ●",
        "live 29",
        "35 systems",
        "2026-07-03T03:02:10Z",
        "STATIC SNAPSHOT · ZERO MUTATION",
    ]:
        assert forbidden not in text
    assert "prefers-reduced-motion" in text
    assert "@media (prefers-reduced-motion: reduce)" in text


def test_route_table_get_only_and_slice_untouched(tmp_path: Path) -> None:
    _run_script(
        tmp_path,
        """
        from hermes_cli import web_server
        paths = {getattr(route, 'path', None): route for route in web_server.app.routes}
        assert '/nexus' in paths
        assert getattr(paths['/nexus'], 'methods', set()) == {'GET'}
        assert '/nexus-slice' in paths
        new_api = [p for p in paths if isinstance(p, str) and p.startswith('/api/') and p == '/api/dashboard/nexus/page']
        assert new_api == []
        """,
    )


def test_nexus_slice_token_bootstrap_keeps_csp_nonce(tmp_path: Path) -> None:
    _run_script(
        tmp_path,
        """
        import re
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        from hermes_cli.web_server import _SESSION_TOKEN

        client = TestClient(web_server.app)
        response = client.get('/nexus-slice')
        body = response.text
        assert response.status_code == 200
        match = re.search(r'<script nonce="([^"]+)">window\.__HERMES_SESSION_TOKEN__="([^"]+)";</script>', body)
        assert match, body[body.find('HERMES_SESSION_TOKEN')-80:body.find('HERMES_SESSION_TOKEN')+120]
        assert match.group(1)
        assert match.group(2) == _SESSION_TOKEN
        assert '<script>window.__HERMES_SESSION_TOKEN__=' not in body
        assert '__HERMES_CSP_NONCE__' not in body
        """,
    )


def test_deploy_artery_has_healthy_green_branch() -> None:
    text = ASSET.read_text(encoding="utf-8")
    start = text.index("function drawDeployArtery")
    end = text.index("function scheduleDraw", start)
    hunk = text[start:end]
    assert "head_equals_serving===true" in hunk
    assert "rgba(67,224,154" in hunk or "#43E09A" in hunk
    assert "HEAD=serving" in hunk
    assert "head_equals_serving!==false" not in hunk


def test_page_anti_wedge_both_modes(tmp_path: Path) -> None:
    _run_script(
        tmp_path,
        """
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        from hermes_cli.dashboard_auth import middleware as auth_middleware
        from hermes_cli.web_server import _SESSION_TOKEN

        client = TestClient(web_server.app)
        response = client.get('/nexus')
        assert response.status_code == 200
        assert response.content
        assert 'HERMES' in response.text and 'NEXUS' in response.text

        web_server.app.state.auth_required = True
        anon = client.get('/nexus', follow_redirects=False)
        assert anon.status_code == 302
        assert anon.headers.get('location', '').startswith('/login')

        async def pass_through(request, call_next):
            return await call_next(request)
        auth_middleware.gated_auth_middleware = pass_through
        passed = client.get('/nexus')
        assert passed.status_code == 200
        assert 'HERMES' in passed.text and 'NEXUS' in passed.text
        assert _SESSION_TOKEN not in passed.text
        """,
    )


def test_plw1514_new_hunk() -> None:
    server = SERVER.read_text(encoding="utf-8")
    hunk = server[server.find("_NEXUS_SLICE_HTML") : server.find("# GitNexus Explorer")]
    assert ".read_text()" not in hunk
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"read_text", "write_text"}:
                assert any(kw.arg == "encoding" for kw in node.keywords), f"line {node.lineno} missing encoding"
