"""Offline result-contract and rejected-background ownership regressions."""
import json
import threading
from unittest.mock import MagicMock

import pytest

import tools.delegate_tool as dt
from tests.tools.test_delegate import _make_mock_parent


@pytest.fixture
def delegation(monkeypatch):
    parent = _make_mock_parent()
    parent.session_id = "opt-parent"
    parent._interrupt_requested = False
    child = MagicMock()
    child.session_id = "opt-child"
    child._subagent_id = None
    child._credential_pool = None
    child._delegate_output_schema = None
    child._delegate_role = "leaf"
    child.tool_progress_callback = None
    child._interrupt_requested = False
    child.model = "test-model"
    child.run_conversation.return_value = {
        "final_response": "done", "completed": True, "api_calls": 1,
    }

    def build(**kwargs):
        parent._active_children.append(child)
        return child

    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.setattr(dt, "_get_worktree_isolation", lambda: False)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: {
        "model": "test-model", "provider": None, "base_url": None,
        "api_key": None, "api_mode": None, "command": None, "args": None,
    })
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    monkeypatch.setattr("tools.async_delegation._current_origin_session_id", lambda: "")
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *a, **k: (None, [], []))
    monkeypatch.setattr("tools.delegation_live_log.update_manifest_statuses", lambda *a, **k: None)
    return parent, child


@pytest.mark.parametrize("flags,status,reason", [
    ({"completed": False, "partial": True, "error": "stream ended early"}, "failed", "error"),
    ({"completed": False, "partial": True, "failed": True}, "failed", "error"),
    ({"completed": False}, "completed", "max_iterations"),
    ({"completed": True, "error": ""}, "completed", "completed"),
])
def test_nonempty_child_summary_obeys_structured_result(delegation, flags, status, reason):
    parent, child = delegation
    summary = "BLOCKED is a quoted label; keep this useful partial report."
    child.run_conversation.return_value = {"final_response": summary, "api_calls": 2, **flags}
    result = json.loads(dt.delegate_task(goal="Check result contract", parent_agent=parent, background=False))
    entry = result["results"][0]
    assert entry["summary"] == summary
    assert entry["status"] == status
    assert entry["exit_reason"] == reason
    assert entry["truncated"] is (reason == "max_iterations")
    assert parent._active_children == []


@pytest.mark.parametrize("with_lock", [False, True])
@pytest.mark.parametrize("rejection", ["pool at capacity", "schedule failed"])
def test_rejected_dispatch_restores_tracking_during_inline_run(delegation, monkeypatch, with_lock, rejection):
    parent, child = delegation
    parent._active_children_lock = threading.Lock() if with_lock else None
    unrelated = object()
    parent._active_children.append(unrelated)
    observations = []

    def reject(**kwargs):
        # No async unit took ownership; children must be restored before inline.
        assert child not in parent._active_children
        return {"status": "rejected", "error": rejection}

    def run(**kwargs):
        observations.append(child in parent._active_children)
        # Model the parent's interrupt fan-out over its actual tracking list.
        for active in parent._active_children:
            if active is child:
                active.interrupt("Parent stopped during inline fallback")
        return {"final_response": "partial report", "completed": False, "interrupted": True, "api_calls": 1}

    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation_batch", reject)
    child.run_conversation.side_effect = run
    result = json.loads(dt.delegate_task(goal="Check rejected background ownership", parent_agent=parent, background=True))
    assert observations == [True]
    child.interrupt.assert_called_once_with("Parent stopped during inline fallback")
    assert result["results"][0]["status"] == "interrupted"
    assert "SYNCHRONOUSLY" in result["note"]
    assert parent._active_children == [unrelated]


def test_accepted_dispatch_stays_detached_and_preserves_payload(delegation, monkeypatch):
    parent, child = delegation
    captured = {}

    def accept(**kwargs):
        captured.update(kwargs)
        assert child not in parent._active_children
        return {"status": "dispatched", "delegation_id": "test-batch"}

    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation_batch", accept)
    result = json.loads(dt.delegate_task(goal="Check accepted background ownership", parent_agent=parent, background=True))
    assert result["status"] == "dispatched"
    assert result["mode"] == "background"
    assert result["count"] == 1
    assert result["delegation_id"] == "test-batch"
    child.run_conversation.assert_not_called()
    assert parent._active_children == []
    parent._interrupt_requested = True
    finished = captured["runner"]()
    assert finished["results"][0]["status"] == "completed"
    child.interrupt.assert_not_called()
    assert parent._active_children == []
