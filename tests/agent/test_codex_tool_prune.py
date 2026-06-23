"""Codex tool-prune scoping.

Boilerplate MCP tools are pruned for ALL codex turns; Notion/Context7 only for
non-Discord (loki/webhook) turns — the Discord MOTHERSHIP keeps them.  The loki
prune is what eliminates the recurring HTTP 400 'Unsupported content type'; the
gate is fail-safe (anything not explicitly Discord prunes) so loki can never
re-ship the full tool set.
"""
from agent.codex_responses_adapter import _preflight_codex_api_kwargs
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
