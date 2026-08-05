"""Tests for GHSA-ppp5-vxwm-4cf7 — Host-header validation.

DNS rebinding defence: a victim browser that has the dashboard open
could be tricked into fetching from an attacker-controlled hostname
that TTL-flips to 127.0.0.1. Same-origin / CORS checks won't help —
the browser now treats the attacker origin as same-origin. Validating
the Host header at the application layer rejects the attack.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_repo = str(Path(__file__).resolve().parents[1])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


class TestHostHeaderValidator:
    """Unit test the _is_accepted_host helper directly — cheaper and
    more thorough than spinning up the full FastAPI app."""



    def test_zero_zero_bind_accepts_anything(self):
        """0.0.0.0 means operator explicitly opted into all-interfaces
        (requires --insecure). No Host-layer defence is possible — rely
        on operator network controls."""
        from hermes_cli.web_server import _is_accepted_host

        for host in ("10.0.0.5", "evil.example", "my-server.corp.net"):
            assert _is_accepted_host(host, "0.0.0.0")
            assert _is_accepted_host(host + ":9119", "0.0.0.0")

    def test_explicit_non_loopback_bind_requires_exact_match(self):
        """If the operator bound to a specific non-loopback hostname,
        the Host header must match exactly."""
        from hermes_cli.web_server import _is_accepted_host

        assert _is_accepted_host("my-server.corp.net", "my-server.corp.net")
        assert _is_accepted_host("my-server.corp.net:9119", "my-server.corp.net")
        # Different host — reject
        assert not _is_accepted_host("evil.example", "my-server.corp.net")
        # Loopback — reject (we bound to a specific non-loopback name)
        assert not _is_accepted_host("localhost", "my-server.corp.net")



class TestHostHeaderMiddleware:
    """End-to-end test via the FastAPI app — verify the middleware
    rejects bad Host headers with 400."""

    def test_rebinding_request_rejected(self):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        # Simulate start_server having set the bound_host
        app.state.bound_host = "127.0.0.1"
        try:
            client = TestClient(app)
            # The TestClient sends Host: testserver by default — which is
            # NOT a loopback alias, so the middleware must reject it.
            resp = client.get(
                "/api/status",
                headers={"Host": "evil.example"},
            )
            assert resp.status_code == 400
            assert "Invalid Host header" in resp.json()["detail"]
        finally:
            # Clean up so other tests don't inherit the bound_host
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host


    def test_no_bound_host_skips_validation(self):
        """If app.state.bound_host isn't set (e.g. running under test
        infra without calling start_server), middleware must pass through
        rather than crash."""
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        # Make sure bound_host isn't set
        if hasattr(app.state, "bound_host"):
            del app.state.bound_host

        client = TestClient(app)
        resp = client.get("/api/status")
        # Should get through to the status endpoint, not a 400
        assert resp.status_code != 400


class TestWebSocketHostOriginGuard:
    """WebSocket upgrades must enforce the same dashboard boundary as HTTP."""

    def test_rebinding_websocket_host_is_rejected(self, monkeypatch):
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
        monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)

        client = TestClient(ws.app)
        url = f"/api/events?token={ws._SESSION_TOKEN}&channel=security-test"
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                url,
                headers={
                    "Host": "evil.example",
                    "Origin": "http://evil.example",
                },
            ):
                pass

        assert exc.value.code == 4403


    def test_loopback_websocket_host_and_origin_are_accepted(self, monkeypatch):
        from fastapi.testclient import TestClient

        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
        monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)

        client = TestClient(ws.app)
        url = f"/api/events?token={ws._SESSION_TOKEN}&channel=security-test"
        with client.websocket_connect(
            url,
            headers={
                "Host": "localhost:9119",
                "Origin": "http://localhost:9119",
            },
        ):
            pass


class TestExtraAcceptedHosts:
    """HERMES_DASHBOARD_EXTRA_HOSTS — operator-allowlisted Host values that
    widen the GHSA-ppp5-vxwm-4cf7 DNS-rebinding Host guard.

    Safety rationale (see ``_extra_accepted_hosts`` docstring): forging an
    allowlisted Host via DNS rebinding requires the attacker to control how
    that name resolves for the victim's browser. For a tailnet MagicDNS name
    (``fresh.tailadb109.ts.net``) resolution is tailnet-internal and
    WireGuard-authed, so the operator opt-in does not reopen the hole.
    """

    def test_allowlisted_host_accepted_others_rejected(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.setattr(
            ws, "_EXTRA_ACCEPTED_HOSTS", frozenset({"fresh.tailadb109.ts.net"})
        )

        # Allowlisted name accepted even though it is neither loopback nor the
        # bound interface (127.0.0.1).
        assert ws._is_accepted_host("fresh.tailadb109.ts.net", "127.0.0.1") is True
        # ...and with the dashboard port suffix stripped.
        assert (
            ws._is_accepted_host("fresh.tailadb109.ts.net:9119", "127.0.0.1") is True
        )
        # A non-allowlisted host is still rejected.
        assert ws._is_accepted_host("evil.example", "127.0.0.1") is False

    def test_allowlist_match_is_case_insensitive(self, monkeypatch):
        import hermes_cli.web_server as ws

        # Allowlist stored lowercase; an upper/mixed-case Host header (with
        # port) must still match per RFC case-insensitivity.
        monkeypatch.setattr(
            ws, "_EXTRA_ACCEPTED_HOSTS", frozenset({"fresh.tailadb109.ts.net"})
        )
        assert (
            ws._is_accepted_host("FRESH.TailADB109.TS.NET:9119", "127.0.0.1") is True
        )

    def test_parser_strips_lowercases_and_drops_blanks(self, monkeypatch):
        import hermes_cli.web_server as ws

        monkeypatch.setenv("HERMES_DASHBOARD_EXTRA_HOSTS", " A.test, b.TEST ,, ")
        # Whitespace trimmed, case-folded, empty/blank entries dropped.
        assert ws._extra_accepted_hosts() == frozenset({"a.test", "b.test"})

    def test_import_frozen_global_is_a_footgun(self, monkeypatch):
        """Regression: ``_EXTRA_ACCEPTED_HOSTS`` is evaluated ONCE at import
        from the env var, so a runtime env change does NOT take effect until
        the dashboard process is restarted. This intentionally documents the
        load-bearing gotcha — the parser reads the env live, but the guard
        reads the frozen global.
        """
        import hermes_cli.web_server as ws

        # Set the env var but deliberately do NOT refresh the global.
        monkeypatch.setenv("HERMES_DASHBOARD_EXTRA_HOSTS", "late.example")

        # The frozen global is unaffected (the module was imported before this
        # setenv; ambient env carries no allowlist), so the guard still rejects
        # the would-be-allowlisted host.
        assert ws._EXTRA_ACCEPTED_HOSTS == frozenset()
        assert ws._is_accepted_host("late.example", "127.0.0.1") is False
