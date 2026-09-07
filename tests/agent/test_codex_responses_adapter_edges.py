"""Edge-case and golden-fixture coverage for codex_responses_adapter.

These tests intentionally stay hermetic: no SDK clients, no network, no model calls.
The golden cases are sanitized/minimized from real local Hermes Codex session rows
and pin the chat-history -> Responses input rewrite byte-for-byte.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.codex_responses_adapter import (
    _CODEX_TOOL_POLICIES,
    _CodexToolPolicy,
    _chat_messages_to_responses_input,
    _derive_responses_function_call_id,
    _preflight_codex_api_kwargs,
    _split_responses_tool_id,
)
from gateway.session_context import clear_session_vars, set_session_vars

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex_responses_adapter"


@pytest.fixture(autouse=True)
def _stable_codex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep preflight tests independent of operator kill-switch env vars."""
    monkeypatch.delenv("HERMES_CODEX_DROP_ENCRYPTED_REASONING", raising=False)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "model": "gpt-5.5",
        "instructions": "test instructions",
        "input": [],
        "store": False,
    }
    kwargs.update(overrides)
    return kwargs


def _function_tool(name: str, *, strict: bool = False, parameters: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "type": "function",
        "name": name,
        "description": f"tool {name}",
        "strict": strict,
        "parameters": parameters or {"type": "object", "properties": {}},
    }


def _preflight_names_for_route(route: str, tools: list[dict[str, object]] | None = None) -> set[str]:
    tokens = set_session_vars(platform=route)
    try:
        out = _preflight_codex_api_kwargs(_base_kwargs(tools=tools or _policy_tools()))
        return {tool["name"] for tool in out["tools"] if tool.get("type") == "function"}
    finally:
        clear_session_vars(tokens)


def _policy_tools() -> list[dict[str, object]]:
    return [
        _function_tool("mcp_notion_API_post_search"),
        _function_tool("mcp_context7_query_docs"),
        _function_tool("mcp_mvms_mvms_search"),
        _function_tool("mcp_mvms_list_prompts"),
        _function_tool("mcp_custom_sensitive_read"),
        _function_tool("terminal"),
    ]


# ---------------------------------------------------------------------------
# _split_responses_tool_id / _derive_responses_function_call_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, None)),
        ("", (None, None)),
        ("   ", (None, None)),
        ("call_abc", ("call_abc", None)),
        ("fc_item", (None, "fc_item")),
        ("call_abc|fc_item", ("call_abc", "fc_item")),
        (" call_abc | fc_item ", ("call_abc", "fc_item")),
        ("call_abc|", ("call_abc", None)),
        ("|fc_item", (None, "fc_item")),
        ("custom-id", ("custom-id", None)),
    ],
)
def test_split_responses_tool_id_edges(raw: object, expected: tuple[str | None, str | None]) -> None:
    assert _split_responses_tool_id(raw) == expected


@pytest.mark.parametrize(
    ("call_id", "response_item_id", "expected"),
    [
        ("call_abc123", None, "fc_abc123"),
        ("fc_already", None, "fc_already"),
        ("custom id!?", None, "fc_customid"),
        ("call_abc123", "fc_response_wins", "fc_response_wins"),
        ("  call_spaced  ", "  fc_trimmed  ", "fc_trimmed"),
        ("call_under-score", None, "fc_under-score"),
    ],
)
def test_derive_responses_function_call_id_round_trips(call_id: str, response_item_id: str | None, expected: str) -> None:
    assert _derive_responses_function_call_id(call_id, response_item_id) == expected
    assert expected.startswith("fc_")


def test_split_and_derive_round_trip_embedded_tool_id() -> None:
    call_id, response_item_id = _split_responses_tool_id("call_abc|fc_response_item")

    assert call_id == "call_abc"
    assert response_item_id == "fc_response_item"
    assert _derive_responses_function_call_id(call_id or "", response_item_id) == "fc_response_item"


def test_derive_response_item_id_prevents_same_call_id_collision() -> None:
    left = _derive_responses_function_call_id("call_shared", "fc_left_response")
    right = _derive_responses_function_call_id("call_shared", "fc_right_response")
    fallback = _derive_responses_function_call_id("call_shared")

    assert left == "fc_left_response"
    assert right == "fc_right_response"
    assert fallback == "fc_shared"
    assert len({left, right, fallback}) == 3


# ---------------------------------------------------------------------------
# _chat_messages_to_responses_input
# ---------------------------------------------------------------------------


def test_chat_messages_empty_and_none_input_are_safe() -> None:
    assert _chat_messages_to_responses_input([]) == []
    assert _chat_messages_to_responses_input(None) == []  # type: ignore[arg-type]
    assert _chat_messages_to_responses_input(["bad", 123, {"role": "system", "content": "skip"}]) == []  # type: ignore[list-item]


def test_chat_messages_multi_tool_turn_and_matching_outputs() -> None:
    messages = [
        {"role": "user", "content": "run both"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_alpha|fc_alpha_item",
                    "type": "function",
                    "function": {"name": "first_tool", "arguments": {"x": 1}},
                },
                {
                    "id": "fc_beta_item",
                    "type": "function",
                    "function": {"name": "second_tool", "arguments": " {\"y\":2} "},
                },
                {"id": "call_bad", "type": "function", "function": {"arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_alpha|fc_alpha_item", "content": "alpha out"},
        {"role": "tool", "tool_call_id": "call_beta_item", "content": [{"type": "text", "text": "beta text"}]},
    ]

    out = _chat_messages_to_responses_input(messages)

    assert out[0] == {"role": "user", "content": "run both"}
    assert out[1]["type"] == "function_call"
    assert out[1]["call_id"] == "call_alpha"
    assert out[1]["name"] == "first_tool"
    assert json.loads(out[1]["arguments"]) == {"x": 1}
    assert out[2] == {
        "type": "function_call",
        "call_id": "call_beta_item",
        "name": "second_tool",
        "arguments": "{\"y\":2}",
    }
    assert out[3] == {"type": "function_call_output", "call_id": "call_alpha", "output": "alpha out"}
    assert out[4] == {
        "type": "function_call_output",
        "call_id": "call_beta_item",
        "output": [{"type": "input_text", "text": "beta text"}],
    }
    assert [item.get("call_id") for item in out if item.get("type") == "function_call"] == ["call_alpha", "call_beta_item"]
    assert [item.get("call_id") for item in out if item.get("type") == "function_call_output"] == ["call_alpha", "call_beta_item"]


def test_chat_messages_drops_orphaned_tool_result_without_matching_call() -> None:
    out = _chat_messages_to_responses_input([
        {"role": "tool", "tool_call_id": "call_missing", "content": "orphan"},
        {"role": "user", "content": "continue"},
    ])

    assert out == [{"role": "user", "content": "continue"}]
    assert all(item.get("type") != "function_call_output" for item in out)


def test_chat_messages_reasoning_replay_dedup_cross_issuer_and_message_items() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "fallback text should not duplicate replayed item",
            "codex_reasoning_items": [
                {"type": "reasoning", "id": "rs_keep", "encrypted_content": "enc-keep", "summary": [], "_issuer_kind": "codex_backend"},
                {"type": "reasoning", "id": "rs_keep", "encrypted_content": "enc-dupe", "summary": []},
                {"type": "reasoning", "id": "rs_drop", "encrypted_content": "enc-drop", "summary": [], "_issuer_kind": "xai_responses"},
            ],
            "codex_message_items": [
                {
                    "type": "message",
                    "role": "assistant",
                    "id": "msg_1",
                    "phase": "final",
                    "status": "in-progress",
                    "content": [{"type": "text", "text": "cached answer"}, {"type": "refusal", "text": "skip"}],
                }
            ],
        }
    ]

    out = _chat_messages_to_responses_input(messages, current_issuer_kind="codex_backend")

    assert out[0] == {"type": "reasoning", "encrypted_content": "enc-keep", "summary": []}
    assert out[1] == {
        "type": "message",
        "role": "assistant",
        "status": "in_progress",
        "content": [{"type": "output_text", "text": "cached answer"}],
        "id": "msg_1",
        "phase": "final",
    }
    assert len(out) == 2
    assert "enc-dupe" not in _stable_json(out)
    assert "enc-drop" not in _stable_json(out)
    assert "_issuer_kind" not in _stable_json(out)


# ---------------------------------------------------------------------------
# _preflight_codex_api_kwargs
# ---------------------------------------------------------------------------


def test_preflight_empty_and_none_input_validation() -> None:
    out = _preflight_codex_api_kwargs(_base_kwargs(input=[]))

    assert out["model"] == "gpt-5.5"
    assert out["instructions"] == "test instructions"
    assert out["input"] == []
    assert out["store"] is False

    with pytest.raises(ValueError, match="must be a list"):
        _preflight_codex_api_kwargs(_base_kwargs(input=None))
    with pytest.raises(ValueError, match="must be a dict"):
        _preflight_codex_api_kwargs(None)
    with pytest.raises(ValueError, match="missing required"):
        _preflight_codex_api_kwargs({"model": "gpt-5.5", "store": False})


def test_preflight_normalizes_passthroughs_and_rejects_bad_stream() -> None:
    kwargs = _base_kwargs(
        model="  gpt-5.5  ",
        instructions="   ",
        input=[{"role": "user", "content": None}],
        max_output_tokens=10.7,
        timeout=2,
        temperature=0,
        tool_choice={"type": "function", "name": "terminal"},
        parallel_tool_calls=False,
        prompt_cache_key="cache-key",
        service_tier=" priority ",
        include=["reasoning.encrypted_content"],
        reasoning={"effort": "low"},
        extra_headers={" X-Test ": 123, "drop": None},
        extra_body={"prompt_cache_key": "body-key"},
        stream=True,
    )

    out = _preflight_codex_api_kwargs(kwargs, allow_stream=True)

    assert out["model"] == "gpt-5.5"
    assert isinstance(out["instructions"], str) and out["instructions"]
    assert out["input"] == [{"role": "user", "content": ""}]
    assert out["max_output_tokens"] == 10
    assert out["timeout"] == 2.0
    assert out["temperature"] == 0.0
    assert out["tool_choice"] == {"type": "function", "name": "terminal"}
    assert out["parallel_tool_calls"] is False
    assert out["prompt_cache_key"] == "cache-key"
    assert out["service_tier"] == "priority"
    assert out["include"] == ["reasoning.encrypted_content"]
    assert out["reasoning"] == {"effort": "low"}
    assert out["extra_headers"] == {"X-Test": "123"}
    assert out["extra_body"] == {"prompt_cache_key": "body-key"}
    assert out["stream"] is True

    with pytest.raises(ValueError, match="stream flag"):
        _preflight_codex_api_kwargs(_base_kwargs(stream=True), allow_stream=False)
    with pytest.raises(ValueError, match="must be true"):
        _preflight_codex_api_kwargs(_base_kwargs(stream=False), allow_stream=True)


def test_preflight_function_tools_builtins_and_strict_nested_schema() -> None:
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {
                    "inner": {"type": "object", "properties": {"leaf": {"type": "string", "format": "uri"}}},
                    "arr": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "number"}}}},
                },
            }
        },
    }
    kwargs = _base_kwargs(tools=[_function_tool("strict_tool", strict=True, parameters=schema), {"type": "web_search"}])

    out = _preflight_codex_api_kwargs(kwargs, allow_stream=True)
    strict_tool, builtin = out["tools"]
    params = strict_tool["parameters"]

    assert strict_tool["type"] == "function"
    assert strict_tool["name"] == "strict_tool"
    assert strict_tool["strict"] is True
    assert params["additionalProperties"] is False
    assert params["properties"]["outer"]["additionalProperties"] is False
    assert params["properties"]["outer"]["properties"]["inner"]["additionalProperties"] is False
    assert params["properties"]["outer"]["properties"]["inner"]["properties"]["leaf"] == {"type": "string"}
    assert params["properties"]["outer"]["properties"]["arr"]["items"]["additionalProperties"] is False
    assert builtin == {"type": "web_search"}


def test_preflight_tool_policy_route_branches() -> None:
    discord = _preflight_names_for_route("discord")
    webhook = _preflight_names_for_route("webhook")
    unknown = _preflight_names_for_route("unlisted-route")
    empty = _preflight_names_for_route("")

    assert "mcp_notion_API_post_search" in discord
    assert "mcp_context7_query_docs" in discord
    assert "mcp_mvms_mvms_search" in discord
    assert "terminal" in discord
    assert "mcp_mvms_list_prompts" not in discord

    assert "mcp_notion_API_post_search" not in webhook
    assert "mcp_context7_query_docs" not in webhook
    assert "mcp_mvms_mvms_search" in webhook
    assert "mcp_custom_sensitive_read" not in webhook
    assert "terminal" in webhook

    assert unknown == {"mcp_mvms_mvms_search", "mcp_custom_sensitive_read", "terminal"}
    assert empty == {"mcp_mvms_mvms_search", "mcp_custom_sensitive_read", "terminal"}

    _CODEX_TOOL_POLICIES["unit-profile:reader"] = _CodexToolPolicy(allowed_mcp_prefixes=("mcp_context7_",))
    try:
        assert _CODEX_TOOL_POLICIES["unit-profile:reader"].allowed_mcp_prefixes == ("mcp_context7_",)
        assert _preflight_names_for_route("unit-profile") == unknown
    finally:
        del _CODEX_TOOL_POLICIES["unit-profile:reader"]


# ---------------------------------------------------------------------------
# Golden fixtures: real captured chat rows -> Responses input payloads.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "assistant_reasoning_tool_call",
        "assistant_parallel_mvms_calls",
    ],
)
def test_chat_to_responses_golden_fixtures_byte_for_byte(fixture_name: str) -> None:
    source = json.loads((FIXTURES / f"{fixture_name}.input.json").read_text(encoding="utf-8"))
    expected = (FIXTURES / f"{fixture_name}.expected.json").read_text(encoding="utf-8")

    actual = _stable_json(_chat_messages_to_responses_input(source))

    assert actual == expected
    assert _stable_json(json.loads(actual)) == expected
