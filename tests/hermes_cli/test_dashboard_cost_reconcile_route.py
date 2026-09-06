from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import cost_reconcile, dashboard_cost, web_server

pytestmark = pytest.mark.xdist_group("dashboard_auth_app_state")


def _sample_reconciliation() -> dict[str, Any]:
    return {
        "generatedAt": "2026-06-24T00:00:00+00:00",
        "dbPath": "/tmp/state.db",
        "window": {"days": 7, "sinceTs": 1.0, "sinceIso": "2026-06-17T00:00:00+00:00"},
        "byBillingMode": {
            "subscription_included": {"turns": 1, "total_tokens": 10, "estimated_cost_usd": 0.0},
            "paid_api": {"turns": 1, "total_tokens": 20, "estimated_cost_usd": 0.13},
            "local_free": {"turns": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
        },
        "budgetRollup": [
            {
                "lane_key": "default",
                "billing_provider": "openrouter",
                "billing_mode": "paid_api",
                "turns": 1,
                "input_tokens": 5,
                "output_tokens": 15,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 20,
                "estimated_cost_usd": 0.13,
            }
        ],
        "paidFallbackViolations": [
            {
                "turn_id": "turn-openrouter",
                "session_id": "session-openrouter",
                "ts": "2026-06-22T00:00:00+00:00",
                "provider": "openrouter",
                "model": "google/gemini-3.5-flash",
                "est_cost_usd": 0.13,
                "why": "openrouter_fallback_providers",
            }
        ],
        "violationCount": 1,
        "violationCostUsd": 0.13,
    }


def test_cost_reconcile_route_returns_reconciler_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_reconcile, "build_reconciliation", _sample_reconciliation)

    app = FastAPI()
    app.include_router(dashboard_cost.router)
    response = TestClient(app).get("/api/dashboard/cost/reconcile")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) >= {
        "byBillingMode",
        "budgetRollup",
        "paidFallbackViolations",
        "violationCount",
        "violationCostUsd",
    }
    assert payload["violationCount"] == 1
    assert payload["paidFallbackViolations"][0]["why"] == "openrouter_fallback_providers"


def test_cost_reconcile_route_returns_503_when_lock_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = Path("/tmp/missing-provider-stack.lock.yaml")

    def raise_missing() -> dict[str, Any]:
        raise FileNotFoundError(f"Provider-stack lock not found at {missing}. Regenerate it.")

    monkeypatch.setattr(cost_reconcile, "build_reconciliation", raise_missing)

    app = FastAPI()
    app.include_router(dashboard_cost.router)
    response = TestClient(app).get("/api/dashboard/cost/reconcile")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "provider_stack_lock_missing"
    assert "Regenerate provider-stack lock" in detail["hint"]
    assert "guessed" not in str(detail).lower()


def test_web_server_mounts_cost_reconcile_route_and_requires_session_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cost_reconcile, "build_reconciliation", _sample_reconciliation)
    assert any(getattr(route, "path", "") == "/api/dashboard/cost/reconcile" for route in web_server.app.routes)

    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 8080
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:8080")
    try:
        assert client.get("/api/dashboard/cost/reconcile").status_code == 401
        response = client.get(
            "/api/dashboard/cost/reconcile",
            headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
        )
    finally:
        web_server.app.state.bound_host = prev_host
        web_server.app.state.bound_port = prev_port
        web_server.app.state.auth_required = prev_required

    assert response.status_code == 200
    assert response.json()["paidFallbackViolations"][0]["model"] == "google/gemini-3.5-flash"
