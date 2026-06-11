"""Tests for hermes_cli.providers provider identity resolution.

The provider module is the single registry surface used by CLI model switching,
runtime provider resolution, and picker labels. These tests keep it isolated
from the live models.dev cache/network by monkeypatching get_provider_info.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import providers


@pytest.fixture(autouse=True)
def no_models_dev_network(monkeypatch):
    """Default all models.dev lookups to cache-miss so tests never hit I/O."""
    import agent.models_dev as models_dev

    monkeypatch.setattr(models_dev, "get_provider_info", lambda _provider_id: None)


def _provider_info(
    provider_id: str,
    *,
    name: str | None = None,
    env: tuple[str, ...] = (),
    api: str = "https://catalog.example/v1",
    doc: str = "https://docs.example/provider",
):
    return SimpleNamespace(
        id=provider_id,
        name=name or provider_id.title(),
        env=env,
        api=api,
        doc=doc,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GLM", "zai"),
        ("z.ai", "zai"),
        ("claude-code", "anthropic"),
        ("github-copilot-acp", "copilot-acp"),
        ("lm_studio", "lmstudio"),
        ("unknown-provider", "unknown-provider"),
    ],
)
def test_normalize_provider_trims_lowercases_and_resolves_aliases(raw, expected):
    assert providers.normalize_provider(raw) == expected


def test_normalize_provider_maps_openai_to_openrouter_KNOWN_GAP_C2():
    """Current behavior: vendor-to-aggregator aliasing is pinned, not endorsed."""
    assert providers.normalize_provider(" OpenAI ") == "openrouter"


def test_registry_contains_load_bearing_overlay_capabilities():
    """Essential Hermes-only metadata remains available without models.dev."""
    assert providers.HERMES_OVERLAYS["openrouter"].is_aggregator is True
    assert providers.HERMES_OVERLAYS["openrouter"].base_url_env_var == "OPENROUTER_BASE_URL"

    anthropic = providers.HERMES_OVERLAYS["anthropic"]
    assert anthropic.transport == "anthropic_messages"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in anthropic.extra_env_vars

    codex = providers.HERMES_OVERLAYS["openai-codex"]
    assert codex.transport == "codex_responses"
    assert codex.auth_type == "oauth_external"

    assert providers.TRANSPORT_TO_API_MODE["anthropic_messages"] == "anthropic_messages"
    assert providers.TRANSPORT_TO_API_MODE["codex_responses"] == "codex_responses"
    assert providers.TRANSPORT_TO_API_MODE["bedrock_converse"] == "bedrock_converse"


def test_get_provider_merges_models_dev_catalog_with_hermes_overlay(monkeypatch):
    import agent.models_dev as models_dev

    def fake_get_provider_info(provider_id: str):
        assert provider_id == "openrouter"
        return _provider_info(
            "openrouter",
            name="OpenRouter Catalog",
            env=("OPENROUTER_API_KEY",),
            api="https://openrouter.ai/api/v1",
            doc="https://openrouter.ai/docs",
        )

    monkeypatch.setattr(models_dev, "get_provider_info", fake_get_provider_info)

    pdef = providers.get_provider("openai")

    assert pdef is not None
    assert pdef.id == "openrouter"
    assert pdef.name == "OpenRouter Catalog"
    assert pdef.source == "models.dev"
    assert pdef.transport == "openai_chat"
    assert pdef.api_key_env_vars == ("OPENROUTER_API_KEY",)
    assert pdef.base_url == "https://openrouter.ai/api/v1"
    assert pdef.base_url_env_var == "OPENROUTER_BASE_URL"
    assert pdef.is_aggregator is True
    assert pdef.auth_type == "api_key"
    assert pdef.doc == "https://openrouter.ai/docs"


def test_get_provider_dedupes_overlay_extra_env_vars(monkeypatch):
    import agent.models_dev as models_dev

    monkeypatch.setattr(
        models_dev,
        "get_provider_info",
        lambda provider_id: _provider_info(
            provider_id,
            name="Anthropic Catalog",
            env=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"),
            api="https://api.anthropic.com",
        ) if provider_id == "anthropic" else None,
    )

    pdef = providers.get_provider("claude")

    assert pdef is not None
    assert pdef.id == "anthropic"
    assert pdef.transport == "anthropic_messages"
    assert pdef.api_key_env_vars.count("ANTHROPIC_TOKEN") == 1
    assert pdef.api_key_env_vars == (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    )


def test_get_provider_resolves_hermes_only_overlay_when_catalog_misses():
    pdef = providers.get_provider("github-copilot-acp")

    assert pdef is not None
    assert pdef.id == "copilot-acp"
    assert pdef.name == "GitHub Copilot ACP"
    assert pdef.source == "hermes"
    assert pdef.transport == "codex_responses"
    assert pdef.auth_type == "external_process"
    assert pdef.base_url == "acp://copilot"
    assert pdef.base_url_env_var == "COPILOT_ACP_BASE_URL"


def test_label_lookup_prefers_overrides_then_catalog(monkeypatch):
    import agent.models_dev as models_dev

    assert providers.get_label("github-copilot-acp") == "GitHub Copilot ACP"

    monkeypatch.setattr(
        models_dev,
        "get_provider_info",
        lambda provider_id: _provider_info(provider_id, name="Catalog Label")
        if provider_id == "catalog-only"
        else None,
    )

    assert providers.get_label("catalog-only") == "Catalog Label"
    assert providers.get_label("  missing-provider  ") == "missing-provider"


def test_capability_helpers_and_api_mode_use_registry_and_url_heuristics():
    assert providers.is_aggregator("opencode-zen") is True
    assert providers.is_aggregator("claude") is False
    assert providers.is_aggregator("missing-provider") is False

    assert providers.determine_api_mode("claude") == "anthropic_messages"
    assert providers.determine_api_mode("openai-codex") == "codex_responses"
    assert providers.determine_api_mode("bedrock") == "bedrock_converse"

    assert (
        providers.determine_api_mode("custom", "https://api.anthropic.com/v1")
        == "anthropic_messages"
    )
    assert (
        providers.determine_api_mode("custom", "https://api.kimi.com/coding/v1")
        == "anthropic_messages"
    )
    assert (
        providers.determine_api_mode("custom", "https://api.openai.com/v1")
        == "codex_responses"
    )
    assert (
        providers.determine_api_mode(
            "custom",
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/provider.model/converse",
        )
        == "bedrock_converse"
    )
    assert providers.determine_api_mode("custom", "https://proxy.example/v1") == "chat_completions"


def test_resolve_provider_full_prefers_raw_user_provider_before_alias():
    user_providers = {
        "openai": {
            "name": "OpenAI Direct",
            "api": "https://api.openai.com/v1",
            "key_env": "OPENAI_API_KEY",
            "transport": "codex_responses",
        }
    }

    pdef = providers.resolve_provider_full("openai", user_providers=user_providers)

    assert pdef is not None
    assert pdef.id == "openai"
    assert pdef.source == "user-config"
    assert pdef.name == "OpenAI Direct"
    assert pdef.transport == "codex_responses"
    assert pdef.api_key_env_vars == ("OPENAI_API_KEY",)
    assert pdef.base_url == "https://api.openai.com/v1"


def test_resolve_custom_provider_matches_display_slug_and_bare_custom_fallback():
    custom_providers = [
        {"name": "missing-url"},
        {"name": "Local Ollama", "base_url": "http://127.0.0.1:11434/v1"},
    ]

    assert providers.custom_provider_slug(" Local Ollama ") == "custom:local-ollama"

    by_name = providers.resolve_custom_provider("local ollama", custom_providers)
    assert by_name is not None
    assert by_name.id == "custom:local-ollama"
    assert by_name.name == "Local Ollama"
    assert by_name.base_url == "http://127.0.0.1:11434/v1"
    assert by_name.source == "user-config"

    by_slug = providers.resolve_custom_provider("custom:local-ollama", custom_providers)
    assert by_slug is not None
    assert by_slug.id == "custom:local-ollama"

    healed = providers.resolve_custom_provider("custom", custom_providers)
    assert healed is not None
    assert healed.id == "custom:local-ollama"


def test_unknown_provider_handling_tolerates_catalog_errors(monkeypatch):
    import agent.models_dev as models_dev

    def explode(_provider_id: str):
        raise RuntimeError("models.dev disabled in tests")

    monkeypatch.setattr(models_dev, "get_provider_info", explode)

    assert providers.get_provider("mystery-provider") is None
    assert providers.resolve_provider_full("mystery-provider", {}, []) is None
    assert providers.get_label(" Mystery-Provider ") == "mystery-provider"
    assert providers.is_aggregator("mystery-provider") is False


def test_user_provider_resolution_rejects_malformed_entries():
    assert providers.resolve_user_provider("x", {}) is None
    assert providers.resolve_user_provider("x", []) is None  # type: ignore[arg-type]
    assert providers.resolve_user_provider("x", {"x": "not-a-dict"}) is None
    assert providers.resolve_custom_provider("x", {}) is None  # type: ignore[arg-type]
    assert providers.resolve_custom_provider("x", [{"name": "no-url"}]) is None
