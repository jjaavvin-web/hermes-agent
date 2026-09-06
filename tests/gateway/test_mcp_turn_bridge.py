"""Actual TurnRunner wiring, no model or network."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from tests.gateway.test_mcp_include_operation import fixture

@pytest.mark.asyncio
@pytest.mark.parametrize('control_allowed', [True, False])
async def test_actual_turnrunner_registers_and_cleans_proposal(fixture, control_allowed):
    from unittest.mock import MagicMock
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.turn_context import TurnContext
    from gateway.run import GatewayRunner
    from tools import mcp_include_tool
    observed = []
    class _ExhaustedAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.session_id = kwargs["session_id"]
            self.tools = []
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=200_000,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0

        def run_conversation(self, _message, **_kwargs):
            from tools.mcp_include_tool import propose_tool
            observed.append(json.loads(propose_tool({"server": "mvms", "include": ["status", "get"]})))
            return {
                "final_response": "Proposal submitted",
                "failed": True,
                "compression_exhausted": True,
                "messages": [],
            }

    gateway_runner = MagicMock()
    gateway_runner._resume_caller_is_admin = lambda source: source.user_id == 'human'
    gateway_runner._mcp_operation_lock = None
    gateway_runner._slash_confirm_counter = None
    gateway_runner._session_key_for_source = fixture.runner._session_key_for_source
    gateway_runner._request_slash_confirm = GatewayRunner._request_slash_confirm.__get__(gateway_runner)
    gateway_runner._adapter_for_source = fixture.runner._adapter_for_source
    gateway_runner.config = SimpleNamespace(streaming=None)
    gateway_runner._provider_routing = {}
    gateway_runner._agent_cache_lock = None
    gateway_runner._agent_cache = {}
    gateway_runner._session_db = None
    gateway_runner._prefill_messages = None
    gateway_runner._pending_model_notes = {}
    gateway_runner._pending_skills_reload_notes = {}
    gateway_runner.session_store._entries = {}
    gateway_runner._get_system_prompt_for_channel.return_value = None
    gateway_runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    gateway_runner._resolve_session_reasoning_config.return_value = None
    gateway_runner._resolve_session_service_tier.return_value = None
    gateway_runner._resolve_turn_agent_config.return_value = {
        "model": "test-model",
        "runtime": {},
    }
    gateway_runner._agent_config_signature.return_value = ("test-signature",)
    gateway_runner._extract_cache_busting_config.return_value = {}
    gateway_runner._refresh_fallback_model.return_value = None
    gateway_runner._consume_pending_native_image_paths.return_value = []
    gateway_runner._consume_pending_turn_sidecar_notes.return_value = []
    gateway_runner._is_telegram_topic_lane.return_value = False
    gateway_runner._is_discord_auto_thread_lane.return_value = False
    gateway_runner._is_relay_discord_channel_lane.return_value = False

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="test-chat",
        user_id="human",
    )
    ctx = TurnContext(
        source=source,
        _loop_for_step=asyncio.get_running_loop(),
        mcp_control_allowed=control_allowed,
        message="continue",
        history=[],
        session_id="test-session",
        session_key="test-session-key",
        user_config={},
        AIAgent=_ExhaustedAgent,
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )

    from gateway.run import TurnRunner

    result = await asyncio.to_thread(TurnRunner(gateway_runner, ctx).run_sync)

    assert observed[0]['status'] == ('awaiting_confirmation' if control_allowed else 'blocked')
    assert 'test-session-key' not in mcp_include_tool._callbacks
    assert json.loads(mcp_include_tool.propose_tool({'server': 'mvms', 'include': ['get']}))['status'] == 'blocked'
