"""Dashboard security header regression coverage."""

import os
import subprocess
import sys
import textwrap


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_dashboard_security_headers_and_bootstrap_nonce(tmp_path):
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
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")

    script = textwrap.dedent(
        """
        import re
        from fastapi.testclient import TestClient
        import hermes_cli.web_server as ws

        client = TestClient(ws.app)

        first = client.get("/")
        second = client.get("/")
        api = client.get("/api/status")
        protected_api = client.get("/api/not-public")

        for response in (first, second, api, protected_api):
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "content-security-policy-report-only" in response.headers

        assert first.status_code == 200
        assert api.status_code == 200
        assert protected_api.status_code == 401

        html_nonce = re.search(r'<script nonce="([^"]+)"', first.text).group(1)
        csp_nonce = re.search(
            r"script-src 'self' 'nonce-([^']+)'",
            first.headers["content-security-policy-report-only"],
        ).group(1)
        second_nonce = re.search(r'<script nonce="([^"]+)"', second.text).group(1)

        assert html_nonce == csp_nonce
        assert html_nonce
        assert second_nonce
        assert html_nonce != second_nonce
        assert first.headers["content-security-policy-report-only"].count("script-src") == 1
        assert "frame-ancestors 'none'" in first.headers["content-security-policy-report-only"]
        assert sum(
            key.lower() == b"x-frame-options" for key, _value in first.headers.raw
        ) == 1
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
