"""Focused regression coverage for conversation_loop compression/error paths."""

from __future__ import annotations

import types
from dataclasses import dataclass
from unittest.mock import Mock, patch

import pytest

from agent.conversation_loop import run_conversation


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


class _AssistantMessage:
    def __init__(self, content: str = "ok", *, finish_reason: str = "stop", tool_calls=None):
        self.content = content
        self.finish_reason = finish_reason
        self.tool_calls = tool_calls or []
        self.reasoning = None
        self.reasoning_content = None
        self.reasoning_details = None


class _Transport:
    def validate_response(self, response):
        return response is not None

    def normalize_response(self, response, **_kwargs):
        return _AssistantMessage(
            getattr(response, "content", "ok"),
            finish_reason=getattr(response, "finish_reason", "stop"),
            tool_calls=getattr(response, "tool_calls", []),
        )


class _Budget:
    def __init__(self):
        self.used = 0
        self.remaining = 99
        self.max_total = 99

    def consume(self):
        self.used += 1
        self.remaining -= 1
        return True

    def refund(self):
        self.used = max(0, self.used - 1)
        self.remaining += 1


class _Compressor:
    def __init__(self, *, should=True, context_length=200_000):
        self.protect_first_n = 0
        self.protect_last_n = 0
        self.threshold_tokens = 100
        self.context_length = context_length
        self.last_prompt_tokens = 0
        self.last_real_prompt_tokens = 0
        self.should_calls = []
        self.update_calls = []
        self._context_probed = False
        self._context_probe_persistable = None
        self._should = should

    def should_defer_preflight_to_real_usage(self, _tokens):
        return False

    def should_compress(self, tokens):
        self.should_calls.append(tokens)
        return self._should

    def update_model(self, **kwargs):
        self.update_calls.append(kwargs)
        self.context_length = kwargs.get("context_length", self.context_length)

    def update_from_response(self, response):
        if isinstance(response, dict):
            self.last_prompt_tokens = response.get("prompt_tokens", 0) or 0
            return
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0


class _ApiError(Exception):
    def __init__(self, message, *, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.message = message


class _FakeAgent:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.api_mode = "chat_completions"
        self.provider = "openrouter"
        self.model = "test/model"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_key = "sk-test"
        self.platform = "cli"
        self.session_id = "sess-test"
        self.log_prefix = "[test] "
        self.max_iterations = 10
        self.iteration_budget = _Budget()
        self.context_compressor = _Compressor(should=False)
        self.compression_enabled = False
        self.quiet_mode = True
        self.verbose_logging = False
        self.tools = []
        self.valid_tool_names = set()
        self.prefill_messages = []
        self.client = Mock()
        self.max_tokens = 4096
        self._api_max_retries = 3
        self._force_ascii_payload = False
        self._disable_streaming = False
        self._should_start_quiet_spinner = lambda: False
        self._cached_system_prompt = "SYSTEM"
        self._compression_warning = None
        self._memory_manager = None
        self._memory_store = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = types.SimpleNamespace(has_items=lambda: True)
        self._tool_guardrails = types.SimpleNamespace(reset_for_turn=lambda: None)
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        self._interrupt_requested = False
        self._interrupt_thread_signal_pending = False
        self._incomplete_scratchpad_retries = 0
        self._codex_incomplete_retries = 0
        self._invalid_tool_retries = 0
        self._invalid_json_retries = 0
        self._thinking_prefill_retries = 0
        self._empty_content_retries = 0
        self._post_tool_empty_retried = False
        self._last_content_with_tools = None
        self._last_content_tools_all_housekeeping = False
        self._mute_post_response = False
        self._fallback_chain = []
        self._fallback_index = 0
        self._credential_pool = None
        self._rate_limit_state = None
        self._vision_supported = True
        self._current_streamed_assistant_text = ""
        self._response_was_previewed = False
        self._delegate_depth = 0
        self.tool_progress_callback = None
        self.thinking_callback = None
        self.stream_delta_callback = None
        self.event_callback = None
        self._session_messages = []
        self._budget_grace_call = False
        self._checkpoint_mgr = types.SimpleNamespace(new_turn=lambda: None)
        self.step_callback = None
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self._pending_steer = None
        self._pending_steer_lock = None
        self.ephemeral_system_prompt = None
        self._use_prompt_caching = False
        self._cache_ttl = None
        self._use_native_cache_layout = False
        self.statuses = []
        self.vprints = []
        self.persisted = []
        self.cleaned = []
        self.error_hooks = []
        self.compression_calls = []
        self.ephemeral_caps = []
        self._ephemeral_max_output_tokens = None
        self.session_api_calls = 0
        self._session_db = None
        self._session_db_created = False
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "test"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None

    def _ensure_db_session(self): pass
    def _restore_primary_runtime(self): pass
    def _cleanup_dead_connections(self): return False
    def _replay_compression_warning(self): pass
    def _hydrate_todo_store(self, *_a, **_k): pass
    def _safe_print(self, *_a, **_k): pass
    def _emit_status(self, msg): self.statuses.append(msg)
    def _buffer_status(self, msg): self.statuses.append(msg)
    def _buffer_vprint(self, msg): self.vprints.append(msg)
    def _vprint(self, msg, *_, **__): self.vprints.append(msg)
    def _flush_status_buffer(self): pass
    def _clear_status_buffer(self): pass
    def _touch_activity(self, *_a, **_k): pass
    def _drain_pending_steer(self): return None
    def _reset_stream_delivery_tracking(self): pass
    def _reapply_reasoning_echo_for_provider(self, *_a, **_k): pass
    def _api_request_payload_for_hook(self, *_a, **_k): return {}
    def _sanitize_tool_call_arguments(self, *_a, **_k): return 0
    def _copy_reasoning_content_for_api(self, *_a, **_k): pass
    def _should_sanitize_tool_calls(self): return False
    def _sanitize_tool_calls_for_strict_api(self, *_a, **_k): pass
    def _sanitize_api_messages(self, messages): return messages
    def _drop_thinking_only_and_merge_users(self, messages, **_k): return messages
    def _client_log_context(self): return "test-context"
    def _get_transport(self): return _Transport()
    def _get_api_max_retries(self): return 3
    def _build_api_kwargs(self, messages, active_system_prompt=None, **_kwargs):
        cap = self._ephemeral_max_output_tokens
        if cap is not None:
            self.ephemeral_caps.append(cap)
        return {"messages": list(messages), "model": self.model, "max_tokens": cap or 4096}
    def _interruptible_api_call(self, _api_kwargs):
        if not self.responses:
            return types.SimpleNamespace(content="ok", finish_reason="stop", usage=_Usage(prompt_tokens=10))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
    def _interruptible_streaming_api_call(self, api_kwargs, *args, **kwargs):
        return self._interruptible_api_call(api_kwargs)
    def _api_response_payload_for_hook(self, *_a, **_k): return {}
    def _usage_summary_for_api_request_hook(self, _response): return {}
    def _invoke_api_request_error_hook(self, **kwargs): self.error_hooks.append(kwargs)
    def _extract_api_error_context(self, _error): return {}
    def _recover_with_credential_pool(self, **_kwargs): return False, False
    def _try_refresh_codex_client_credentials(self, **_kwargs): return False
    def _try_refresh_nous_client_credentials(self, **_kwargs): return False
    def _try_refresh_copilot_client_credentials(self): return False
    def _try_refresh_anthropic_client_credentials(self): return False
    def _try_recover_primary_transport(self, *_a, **_k): return False
    def _try_activate_fallback(self, *_, **__): return False
    def _has_pending_fallback(self): return False
    def _is_openrouter_url(self): return "openrouter.ai" in self.base_url
    def _summarize_api_error(self, error): return str(error)
    def _clean_error_message(self, error): return str(error)
    def _dump_api_request_debug(self, *_a, **_k): pass
    def _try_shrink_image_parts_in_messages(self, *_a, **_k): return False
    def _try_strip_image_parts_from_tool_messages(self, *_a, **_k): return False
    def _disable_codex_reasoning_replay(self, _messages): return {"items": 0, "messages": 0}
    def _compress_context(self, messages, system_message, *, approx_tokens=None, task_id=None, **_kwargs):
        self.compression_calls.append({"len": len(messages), "approx_tokens": approx_tokens, "task_id": task_id})
        keep = messages[:1] + messages[-1:] if len(messages) > 2 else list(messages)
        self.context_compressor.last_prompt_tokens = -1
        return keep, self._cached_system_prompt
    def _requested_output_cap_from_api_kwargs(self, api_kwargs): return api_kwargs.get("max_tokens")
    def _build_assistant_message(self, assistant_message, finish_reason):
        return {"role": "assistant", "content": assistant_message.content or "", "finish_reason": finish_reason}
    def _strip_think_blocks(self, text): return text or ""
    def _has_content_after_think_block(self, text): return bool((text or "").strip())
    def _should_treat_stop_as_truncated(self, *_a, **_k): return False
    def _extract_reasoning(self, _assistant_message): return ""
    def _drop_trailing_empty_response_scaffolding(self, _messages): pass
    def _save_trajectory(self, *_a, **_k): pass
    def _cleanup_task_resources(self, task_id): self.cleaned.append(task_id)
    def _persist_session(self, messages, conversation_history=None): self.persisted.append(list(messages))
    def _turn_completion_explainer_enabled(self): return False
    def _file_mutation_verifier_enabled(self): return False
    def _format_file_mutation_failure_footer(self, _failed): return ""
    def _handle_max_iterations(self, *_a, **_k): return "summary"
    def _has_stream_consumers(self): return False
    def _should_emit_quiet_tool_messages(self): return False
    def _looks_like_codex_intermediate_ack(self, **_kwargs): return False
    def _emit_interim_assistant_message(self, *_a, **_k): pass
    def _repair_tool_call(self, _name): return None
    def _cap_delegate_task_calls(self, calls): return calls
    def _deduplicate_tool_calls(self, calls): return calls
    def _execute_tool_calls(self, *_a, **_k): pass
    def clear_interrupt(self):
        self._interrupt_requested = False
        self._interrupt_message = None
    def _sync_external_memory_for_turn(self, **_kwargs): pass
    def _spawn_background_review(self, **_kwargs): pass


def _run(agent, history=None):
    with patch("agent.conversation_loop.estimate_request_tokens_rough", return_value=1_000), \
         patch("agent.conversation_loop.jittered_backoff", return_value=0), \
         patch("agent.conversation_loop.time.sleep", lambda *_a, **_k: None), \
         patch("agent.turn_finalizer.emit_turn_trace", lambda *a, **k: None), \
         patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        return run_conversation(agent, "hello", conversation_history=history or [], task_id="task-1")


def test_preflight_compression_compacts_large_history_and_resets_empty_retry_state():
    agent = _FakeAgent([types.SimpleNamespace(content="done", finish_reason="stop", usage=_Usage(prompt_tokens=20))])
    agent.compression_enabled = True
    agent.context_compressor = _Compressor(should=True)
    agent._empty_content_retries = 2
    agent._thinking_prefill_retries = 1
    agent._last_content_with_tools = "stale narration"
    agent._last_content_tools_all_housekeeping = True
    agent._mute_post_response = True
    history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]

    result = _run(agent, history)

    assert result["completed"] is True
    assert result["final_response"] == "done"
    assert agent.compression_calls, "preflight should call _compress_context before the API call"
    assert agent.compression_calls[0]["approx_tokens"] is not None
    assert agent.compression_calls[0]["task_id"] == "task-1"
    assert agent._empty_content_retries == 0
    assert agent._thinking_prefill_retries == 0
    assert agent._last_content_with_tools is None
    assert agent._last_content_tools_all_housekeeping is False
    assert agent._mute_post_response is False
    assert any("Preflight compression" in s for s in agent.statuses)


def test_context_overflow_with_auto_compaction_disabled_returns_terminal_error_without_compressing():
    error = _ApiError("maximum context length exceeded", status_code=400)
    agent = _FakeAgent([error])
    agent.compression_enabled = False
    agent.context_compressor = _Compressor(should=False)

    result = _run(agent, [{"role": "user", "content": "old"}])

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["compaction_disabled"] is True
    assert "auto-compaction is disabled" in result["error"]
    assert agent.compression_calls == []
    assert agent.error_hooks[-1]["reason"] == "context_overflow"


def test_context_overflow_output_cap_error_retries_with_ephemeral_max_tokens_without_compressing():
    error = _ApiError(
        "max_tokens: 32768 > context_window: 200000 - input_tokens: 190000 = available_tokens: 10000",
        status_code=400,
    )
    agent = _FakeAgent([error, types.SimpleNamespace(content="recovered", finish_reason="stop", usage=_Usage(prompt_tokens=50))])
    agent.compression_enabled = True
    agent.context_compressor = _Compressor(should=False, context_length=200_000)

    result = _run(agent, [{"role": "user", "content": "old"}])

    assert result["completed"] is True
    assert result["final_response"] == "recovered"
    assert agent.compression_calls == []
    assert agent._ephemeral_max_output_tokens == 9_936
    assert agent.ephemeral_caps[-1] == 9_936
    assert agent.context_compressor.context_length == 200_000
    assert agent.error_hooks[-1]["reason"] == "context_overflow"


def test_content_policy_error_is_classified_terminal_and_uses_policy_result_shape():
    error = _ApiError("violates our usage policies", status_code=400)
    agent = _FakeAgent([error])

    result = _run(agent, [{"role": "user", "content": "old"}])

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["error"].startswith("content_policy_blocked:")
    assert "safety filter blocked" in result["final_response"]
    assert agent.error_hooks[-1]["reason"] == "content_policy_blocked"
    assert agent.compression_calls == []
