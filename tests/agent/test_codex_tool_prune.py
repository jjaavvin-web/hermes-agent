"""Codex tool-prune scoping.

Boilerplate MCP tools are pruned for ALL codex turns; Notion/Context7 only for
non-Discord (loki/webhook) turns — the Discord MOTHERSHIP keeps them.  The loki
prune is what eliminates the recurring HTTP 400 'Unsupported content type'; the
gate is fail-safe (anything not explicitly Discord prunes) so loki can never
re-ship the full tool set.
"""
from agent.codex_responses_adapter import (
    _CODEX_PRUNE_TOOL_PREFIXES,
    _CODEX_PRUNE_TOOL_SUFFIXES,
    _CODEX_TOOL_POLICIES,
    _CodexToolPolicy,
    _codex_tool_policy,
    _preflight_codex_api_kwargs,
)
from gateway.session_context import set_session_vars, clear_session_vars


def _kw():
    mk = lambda n: {"type": "function", "name": n, "parameters": {"type": "object", "properties": {}}}
    return {
        "model": "gpt-5.5",
        "instructions": "x",
        "input": [],
        "store": False,
        "tools": [
            mk("mcp_notion_API_post_search"),
            mk("mcp_context7_query_docs"),
            mk("mcp_mvms_mvms_search"),     # MVMS data tool — always kept
            mk("mcp_mvms_list_prompts"),    # boilerplate — pruned globally
            mk("mcp_custom_sensitive_read"),
            mk("terminal"),                 # core — always kept
        ],
    }


def _names_for(platform):
    tokens = set_session_vars(platform=platform)
    try:
        return {t["name"] for t in _preflight_codex_api_kwargs(_kw())["tools"]}
    finally:
        clear_session_vars(tokens)


def test_mothership_discord_keeps_notion_and_context7():
    names = _names_for("discord")
    assert "mcp_notion_API_post_search" in names      # KEPT for MOTHERSHIP
    assert "mcp_context7_query_docs" in names          # KEPT for MOTHERSHIP
    assert "mcp_mvms_mvms_search" in names
    assert "terminal" in names
    assert "mcp_mvms_list_prompts" not in names        # boilerplate pruned GLOBALLY


def test_loki_webhook_gets_slim_set():
    names = _names_for("webhook")
    assert "mcp_notion_API_post_search" not in names   # PRUNED for loki
    assert "mcp_context7_query_docs" not in names       # PRUNED for loki
    assert "mcp_mvms_list_prompts" not in names         # boilerplate pruned
    assert "mcp_mvms_mvms_search" in names              # data tool kept
    assert "terminal" in names


def test_failsafe_empty_platform_prunes_prefix():
    # cli/cron/unknown -> not "discord" -> fail-safe prune of Notion/Context7
    names = _names_for("")
    assert "mcp_notion_API_post_search" not in names
    assert "mcp_context7_query_docs" not in names
    assert "mcp_mvms_mvms_search" in names


def test_failsafe_relay_webhook_prunes():
    # a non-loki webhook/relay lane still prunes (correct: also risks the 400)
    names = _names_for("webhook")
    assert "mcp_context7_query_docs" not in names


def _legacy_pruned_names(platform):
    """Model the pre-allowlist Codex prune behavior for exact fallback proof."""

    keep_mcp_prefix = platform == "discord"
    names = set()
    for tool in _kw()["tools"]:
        name = tool["name"]
        if name.endswith(_CODEX_PRUNE_TOOL_SUFFIXES):
            continue
        if not keep_mcp_prefix and name.startswith(_CODEX_PRUNE_TOOL_PREFIXES):
            continue
        names.add(name)
    return names


def test_route_with_narrow_allowlist_drops_out_of_policy_mcp_tool():
    route = "unit-narrow"
    _CODEX_TOOL_POLICIES[route] = _CodexToolPolicy(allowed_mcp_prefixes=("mcp_mvms_",))
    try:
        assert _codex_tool_policy(route) is not None
        names = _names_for(route)
    finally:
        del _CODEX_TOOL_POLICIES[route]

    assert _codex_tool_policy(route) is None
    assert "mcp_custom_sensitive_read" not in names
    assert "mcp_notion_API_post_search" not in names
    assert "mcp_mvms_mvms_search" in names
    assert "terminal" in names


def test_route_without_allowlist_entry_preserves_legacy_400_safe_behavior():
    platform = "unlisted-route"
    names = _names_for(platform)
    assert names == _legacy_pruned_names(platform)
    assert names == {"mcp_mvms_mvms_search", "mcp_custom_sensitive_read", "terminal"}


def test_config_policy_preserves_discord_vs_webhook_split():
    assert _codex_tool_policy("discord") is not None
    assert _codex_tool_policy("webhook") is not None

    discord_names = _names_for("discord")
    webhook_names = _names_for("webhook")

    assert "mcp_notion_API_post_search" in discord_names
    assert "mcp_context7_query_docs" in discord_names
    assert "mcp_notion_API_post_search" not in webhook_names
    assert "mcp_context7_query_docs" not in webhook_names
    assert "mcp_mvms_mvms_search" in discord_names & webhook_names
    assert "terminal" in discord_names & webhook_names
