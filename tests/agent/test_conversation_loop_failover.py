"""Focused retry/failover coverage for :mod:`agent.conversation_loop`.

These tests intentionally mock the provider boundary while driving the real
``AIAgent.run_conversation`` loop.  They cover retry/fallback branches that are
otherwise easy to fake-green with source-shape assertions only.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


class _APIError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _chat_response(content: str, *, finish_reason: str = "stop") -> SimpleNamespace:
    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        model_extra={},
    )
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None, model="mock-response-model")


def _make_agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://primary.example/v1",
            provider="primary-provider",
            api_mode="chat_completions",
            model="primary-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 1
    return agent


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import conversation_loop

    monkeypatch.setattr(conversation_loop, "jittered_backoff", lambda *a, **k: 0.0)


@contextmanager
def _patch_quiet_side_effects(agent: AIAgent):
    with ExitStack() as stack:
        stack.enter_context(patch.object(agent, "_persist_session"))
        stack.enter_context(patch.object(agent, "_save_trajectory"))
        stack.enter_context(patch.object(agent, "_cleanup_task_resources"))
        stack.enter_context(
            patch.object(
                agent,
                "_handle_max_iterations",
                return_value="max-iterations-summary",
            )
        )
        yield


def _install_build_kwargs_spy(monkeypatch: pytest.MonkeyPatch, agent: AIAgent) -> list[tuple[str, str]]:
    built_routes: list[tuple[str, str]] = []

    def _build_api_kwargs(api_messages: list[dict]) -> dict:
        built_routes.append((agent.provider, agent.model))
        return {"model": agent.model, "messages": api_messages}

    monkeypatch.setattr(agent, "_build_api_kwargs", _build_api_kwargs)
    return built_routes


def _install_one_step_fallback(monkeypatch: pytest.MonkeyPatch, agent: AIAgent) -> list[object]:
    fallback_reasons: list[object] = []
    agent._fallback_chain = [{"provider": "fallback-provider", "model": "fallback-model"}]
    agent._fallback_index = 0

    def _activate(reason=None) -> bool:
        fallback_reasons.append(reason)
        if agent._fallback_index >= len(agent._fallback_chain):
            return False
        agent._fallback_index += 1
        agent.provider = "fallback-provider"
        agent.model = "fallback-model"
        agent.base_url = "https://fallback.example/v1"
        return True

    monkeypatch.setattr(agent, "_try_activate_fallback", _activate)
    return fallback_reasons


def test_invalid_response_switches_to_fallback_and_rebuilds_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed/empty provider response should activate fallback immediately.

    The important assertion is not just that the turn succeeds: the second API
    request must be rebuilt after the provider/model switch, otherwise fallback
    uses stale primary-shaped kwargs.
    """
    agent = _make_agent()
    built_routes = _install_build_kwargs_spy(monkeypatch, agent)
    fallback_reasons = _install_one_step_fallback(monkeypatch, agent)
    api_calls = MagicMock(side_effect=[None, _chat_response("fallback recovered")])
    monkeypatch.setattr(agent, "_interruptible_api_call", api_calls)

    with _patch_quiet_side_effects(agent):
        result = agent.run_conversation("please answer")

    assert result["completed"] is True
    assert result["final_response"] == "fallback recovered"
    assert api_calls.call_count == 2
    assert fallback_reasons == [None]
    assert built_routes[:2] == [
        ("primary-provider", "primary-model"),
        ("fallback-provider", "fallback-model"),
    ]


def test_content_policy_exception_uses_fallback_before_terminal_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic provider safety block gets one fallback attempt, not retries."""
    agent = _make_agent()
    built_routes = _install_build_kwargs_spy(monkeypatch, agent)
    fallback_reasons = _install_one_step_fallback(monkeypatch, agent)
    api_calls = MagicMock(
        side_effect=[
            _APIError("request flagged for possible cybersecurity risk", status_code=400),
            _chat_response("fallback allowed the request"),
        ]
    )
    monkeypatch.setattr(agent, "_interruptible_api_call", api_calls)

    with _patch_quiet_side_effects(agent):
        result = agent.run_conversation("benign security analysis request")

    assert result["completed"] is True
    assert result["final_response"] == "fallback allowed the request"
    assert api_calls.call_count == 2
    assert fallback_reasons == [None]
    assert built_routes[:2] == [
        ("primary-provider", "primary-model"),
        ("fallback-provider", "fallback-model"),
    ]


def test_retry_exhaustion_rebuilds_primary_transport_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient server errors first get primary transport recovery, then fallback.

    This exercises the max-retries branch where ``_try_recover_primary_transport``
    resets ``retry_count`` once.  If the rebuilt primary still fails, the loop
    must then activate the fallback provider and rebuild the request for it.
    """
    agent = _make_agent()
    built_routes = _install_build_kwargs_spy(monkeypatch, agent)
    fallback_reasons = _install_one_step_fallback(monkeypatch, agent)
    recover_calls: list[str] = []

    def _recover_primary(api_error: Exception, *, retry_count: int, max_retries: int) -> bool:
        recover_calls.append(f"{type(api_error).__name__}:{retry_count}/{max_retries}")
        return len(recover_calls) == 1

    monkeypatch.setattr(agent, "_try_recover_primary_transport", _recover_primary)
    api_calls = MagicMock(
        side_effect=[
            _APIError("upstream server error", status_code=500),
            _APIError("upstream server error again", status_code=500),
            _chat_response("fallback after transport recovery failed"),
        ]
    )
    monkeypatch.setattr(agent, "_interruptible_api_call", api_calls)

    with _patch_quiet_side_effects(agent):
        result = agent.run_conversation("please answer after transient errors")

    assert result["completed"] is True
    assert result["final_response"] == "fallback after transport recovery failed"
    assert api_calls.call_count == 3
    assert recover_calls == ["_APIError:1/1"]
    assert len(fallback_reasons) == 1
    assert built_routes[:3] == [
        ("primary-provider", "primary-model"),
        ("primary-provider", "primary-model"),
        ("fallback-provider", "fallback-model"),
    ]


def test_empty_content_retries_three_times_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful-but-empty model responses use the empty-retry path, then fallback."""
    agent = _make_agent()
    built_routes = _install_build_kwargs_spy(monkeypatch, agent)
    fallback_reasons = _install_one_step_fallback(monkeypatch, agent)
    api_calls = MagicMock(
        side_effect=[
            _chat_response(""),
            _chat_response(""),
            _chat_response(""),
            _chat_response(""),
            _chat_response("fallback supplied visible content"),
        ]
    )
    monkeypatch.setattr(agent, "_interruptible_api_call", api_calls)

    with _patch_quiet_side_effects(agent):
        result = agent.run_conversation("please do not return empty text")

    assert result["completed"] is True
    assert result["final_response"] == "fallback supplied visible content"
    assert api_calls.call_count == 5
    assert agent._empty_content_retries == 0
    assert fallback_reasons == [None]
    assert built_routes[:5] == [
        ("primary-provider", "primary-model"),
        ("primary-provider", "primary-model"),
        ("primary-provider", "primary-model"),
        ("primary-provider", "primary-model"),
        ("fallback-provider", "fallback-model"),
    ]
