from __future__ import annotations

from hermes_cli.dashboard_action_preflight import REQUIRED_KEYS, compute_preflight


def assert_required_keys(payload: dict) -> None:
    assert set(payload) == set(REQUIRED_KEYS)


def test_gateway_restart_preflight_shape_and_scope() -> None:
    payload = compute_preflight("gateway-restart", None, None)

    assert_required_keys(payload)
    assert payload["action"] == "gateway-restart"
    assert payload["action_class"] == "write-high"
    assert payload["scope"] == "machine-global"
    assert isinstance(payload["active_sessions"], int)
    assert isinstance(payload["dirty_source"], bool)
    assert isinstance(payload["cron_impact"], int)
    assert payload["rollback_hint"] == "gateway auto-restarts; sessions resume"


def test_sessions_bulk_delete_preflight_destructive_no_auto_rollback() -> None:
    payload = compute_preflight("sessions-bulk-delete", "session-a,session-b", "default")

    assert_required_keys(payload)
    assert payload["action"] == "sessions-bulk-delete"
    assert payload["target"] == "session-a,session-b"
    assert payload["action_class"] == "destructive"
    assert payload["scope"] == "profile:default"
    assert payload["cron_impact"] == 0
    assert "no automatic rollback" in payload["rollback_hint"]


def test_unknown_action_graceful_and_idempotent() -> None:
    first = compute_preflight("unknown-action", "target-x", "brain")
    second = compute_preflight("unknown-action", "target-x", "brain")

    assert_required_keys(first)
    assert_required_keys(second)
    assert first["action"] == "unknown-action"
    assert first["action_class"] == "write-high"
    assert first["scope"] == "profile:brain"
    assert first["rollback_hint"] == "unknown action; require review before mutation"
    assert first["action_class"] == second["action_class"]
    assert first["scope"] == second["scope"]
