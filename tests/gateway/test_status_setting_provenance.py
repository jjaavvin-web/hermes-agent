"""Native /status reports local settings, not observed requests or provider claims."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionEntry, build_session_key
from tests.gateway.test_status_command import _make_event, _make_runner, _make_source


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["live agent", "cached agent", "no agent", "pending agent"])
async def test_status_setting_provenance_is_read_only_and_not_config_or_provider_claim(monkeypatch, origin):
    from gateway.run import _AGENT_PENDING_SENTINEL

    entry = SessionEntry(
        session_key=build_session_key(_make_source()), session_id="status-test",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="dm",
    )
    runner = _make_runner(entry)
    saved = {"model": {"default": "saved-model", "provider": "saved-provider"},
             "agent": {"reasoning_effort": "low", "service_tier": "normal"}}
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: deepcopy(saved))
    requested = {"enabled": True, "effort": "high"}
    agent = SimpleNamespace(model="requested-model", provider="test-provider",
                            reasoning_config=requested, service_tier="priority",
                            context_compressor=SimpleNamespace(last_prompt_tokens=10, context_length=100),
                            interrupt=MagicMock(), run_conversation=MagicMock())
    if origin == "live agent":
        runner._running_agents[entry.session_key] = agent
    elif origin == "cached agent":
        runner._agent_cache[entry.session_key] = (agent,)
    elif origin == "pending agent":
        runner._running_agents[entry.session_key] = _AGENT_PENDING_SENTINEL
    before = deepcopy(requested)
    result = await runner._handle_status_command(_make_event("/status"))
    if origin in {"live agent", "cached agent"}:
        assert f"**Settings (resolved locally, {origin}):** reasoning=high; service tier=priority" in result
    else:
        assert "**Settings (resolved locally):** UNKNOWN (no live/cached agent)" in result
        assert "reasoning=low" not in result  # saved config is not a runtime request
    assert "**Wire-request settings:** UNKNOWN" in result
    assert "**Provider-applied settings:** UNKNOWN" in result
    assert requested == before
    assert agent.service_tier == "priority"
    agent.interrupt.assert_not_called()
    agent.run_conversation.assert_not_called()
    assert isinstance(runner.session_store.update_session, MagicMock)
    assert isinstance(runner.session_store.append_to_transcript, MagicMock)
    runner.session_store.update_session.assert_not_called()
    runner.session_store.append_to_transcript.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,expected", [
    ({}, "reasoning=UNKNOWN; service tier=UNKNOWN"),
    ({"reasoning_config": None, "service_tier": None}, "reasoning=default (not explicit); service tier=default (not explicit)"),
    ({"reasoning_config": {"enabled": False}, "service_tier": "priority"}, "reasoning=disabled; service tier=priority"),
    ({"reasoning_config": {}, "service_tier": {}}, "reasoning=UNKNOWN; service tier=UNKNOWN"),
])
async def test_status_distinguishes_absent_default_disabled_and_malformed_requests(monkeypatch, fields, expected):
    entry = SessionEntry(session_key=build_session_key(_make_source()), session_id="status-test",
                         created_at=datetime.now(), updated_at=datetime.now(), platform=Platform.TELEGRAM)
    runner = _make_runner(entry)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    runner._running_agents[entry.session_key] = SimpleNamespace(model="test", provider="test", **fields)
    result = await runner._handle_status_command(_make_event("/status"))
    assert expected in result
    assert "**Wire-request settings:** UNKNOWN" in result
    assert "**Provider-applied settings:** UNKNOWN" in result
