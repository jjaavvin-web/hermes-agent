"""Dedicated tests for hermes_cli.model_switch public behavior.

The module is shared by CLI, gateway, TUI, and ACP /model handlers, so these
unit tests keep the model-switch core isolated from real config files,
credential stores, provider registries, and network-backed model discovery.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

import hermes_cli.model_switch as ms

_REAL_RESOLVE_ALIAS = ms.resolve_alias


@pytest.fixture(autouse=True)
def isolated_hermes_home(tmp_path, monkeypatch):
    """Keep every test away from the user's real ~/.hermes state."""
    hermes_home = tmp_path / "hermes-home"
    user_home = tmp_path / "home"
    hermes_home.mkdir()
    user_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(user_home))

    for env_name in (
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "KIMI_API_KEY",
        "DASHSCOPE_API_KEY",
        "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY",
        "OLLAMA_API_KEY",
        "LM_API_KEY",
        "LM_BASE_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
    ):
        monkeypatch.delenv(env_name, raising=False)

    monkeypatch.setattr(ms, "DIRECT_ALIASES", {})
    ms._picker_prewarm_done.clear()


@pytest.fixture
def switch_dependencies(monkeypatch):
    """Patch switch_model dependencies so tests never hit live providers."""
    import hermes_cli.models as model_mod
    import hermes_cli.runtime_provider as runtime_provider

    calls: dict[str, list] = {
        "normalize": [],
        "runtime": [],
        "validate": [],
    }

    def normalize(model: str, provider: str) -> str:
        calls["normalize"].append((model, provider))
        return model

    def validate(model: str, provider: str, **kwargs):
        calls["validate"].append((model, provider, kwargs))
        return {
            "accepted": True,
            "persist": True,
            "recognized": True,
            "message": "",
        }

    def resolve_runtime_provider(requested=None, **kwargs):
        calls["runtime"].append((requested, kwargs))
        provider = requested or "auto"
        return {
            "provider": provider,
            "api_key": f"{provider}-key",
            "base_url": f"https://{provider}.example/v1",
            "api_mode": "",
        }

    monkeypatch.setattr(ms, "resolve_alias", lambda _raw, _provider: None)
    monkeypatch.setattr(ms, "is_aggregator", lambda _provider: False)
    monkeypatch.setattr(ms, "list_provider_models", lambda _provider: [])
    monkeypatch.setattr(ms, "normalize_model_for_provider", normalize)
    monkeypatch.setattr(ms, "get_label", lambda provider: f"Label {provider}")
    monkeypatch.setattr(ms, "determine_api_mode", lambda provider, _base_url: f"{provider}-mode")
    monkeypatch.setattr(
        ms,
        "get_model_capabilities",
        lambda provider, model: SimpleNamespace(provider=provider, model=model),
    )
    monkeypatch.setattr(
        ms,
        "get_model_info",
        lambda provider, model: SimpleNamespace(id=model, context_window=12345),
    )
    monkeypatch.setattr(model_mod, "detect_provider_for_model", lambda _model, _current: None)
    monkeypatch.setattr(model_mod, "validate_requested_model", validate)
    monkeypatch.setattr(model_mod, "copilot_model_api_mode", lambda _model, api_key="": "copilot-mode")
    monkeypatch.setattr(model_mod, "opencode_model_api_mode", lambda _provider, _model: "opencode-mode")
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve_runtime_provider)
    return calls


@pytest.fixture
def picker_registry(monkeypatch):
    """Minimal provider/model registry for picker tests without network calls."""
    import agent.models_dev as models_dev
    import hermes_cli.auth as auth_mod
    import hermes_cli.models as model_mod
    import hermes_cli.providers as provider_mod

    monkeypatch.setattr(models_dev, "PROVIDER_TO_MODELS_DEV", {}, raising=False)
    monkeypatch.setattr(models_dev, "fetch_models_dev", lambda: {})
    monkeypatch.setattr(models_dev, "get_provider_info", lambda _provider: None)
    monkeypatch.setattr(auth_mod, "PROVIDER_REGISTRY", {}, raising=False)
    monkeypatch.setattr(model_mod, "OPENROUTER_MODELS", [], raising=False)
    monkeypatch.setattr(model_mod, "_PROVIDER_MODELS", {}, raising=False)
    monkeypatch.setattr(model_mod, "_MODELS_DEV_PREFERRED", set(), raising=False)
    monkeypatch.setattr(model_mod, "_merge_with_models_dev", lambda _provider, ids: ids, raising=False)
    monkeypatch.setattr(model_mod, "cached_provider_model_ids", lambda _slug: [], raising=False)
    monkeypatch.setattr(model_mod, "get_curated_nous_model_ids", lambda: [], raising=False)
    monkeypatch.setattr(model_mod, "fetch_ollama_cloud_models", lambda: [], raising=False)
    monkeypatch.setattr(model_mod, "CANONICAL_PROVIDERS", [], raising=False)
    monkeypatch.setattr(model_mod, "fetch_api_models", lambda *_args, **_kwargs: pytest.fail("network fetch_api_models called"), raising=False)
    monkeypatch.setattr(provider_mod, "HERMES_OVERLAYS", {}, raising=False)


# ---------------------------------------------------------------------------
# Public dataclasses / named tuples and flag parsing
# ---------------------------------------------------------------------------


def test_public_result_and_alias_types_have_expected_defaults():
    identity = ms.ModelIdentity("anthropic", "claude-sonnet")
    direct = ms.DirectAlias("llama3", "custom", "http://localhost:11434/v1")
    result = ms.ModelSwitchResult(success=True)

    assert identity.vendor == "anthropic"
    assert identity.family == "claude-sonnet"
    assert direct.model == "llama3"
    assert direct.provider == "custom"
    assert direct.base_url == "http://localhost:11434/v1"
    assert result.success is True
    assert result.new_model == ""
    assert result.provider_changed is False
    assert result.is_global is False
    assert result.capabilities is None
    assert result.model_info is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sonnet", ("sonnet", "", False, False, False)),
        ("sonnet --global", ("sonnet", "", True, False, False)),
        ("sonnet --session", ("sonnet", "", False, False, True)),
        (
            "sonnet --provider anthropic --session",
            ("sonnet", "anthropic", False, False, True),
        ),
        ("sonnet --provider anthropic", ("sonnet", "anthropic", False, False, False)),
        ("--provider my-ollama", ("", "my-ollama", False, False, False)),
        ("--refresh", ("", "", False, True, False)),
        (
            "sonnet --provider anthropic --global --refresh",
            ("sonnet", "anthropic", True, True, False),
        ),
        (
            "sonnet \u2014provider anthropic \u2013global \u2015refresh",
            ("sonnet", "anthropic", True, True, False),
        ),
    ],
)
def test_parse_model_flags_extracts_provider_global_refresh_and_unicode_dashes(raw, expected):
    assert ms.parse_model_flags(raw) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("NousResearch/Hermes-3-Llama-3.1-70B", True),
        ("openrouter/hermes4:405b", True),
        ("hermes-4-405b", True),
        ("hermes-brain:qwen3-14b-ctx16k", False),
        ("anthropic/claude-sonnet-4-6", False),
        ("", False),
    ],
)
def test_is_nous_hermes_non_agentic_matches_only_real_hermes_3_and_4_models(model, expected):
    assert ms.is_nous_hermes_non_agentic(model) is expected


# ---------------------------------------------------------------------------
# Alias resolution and model-name normalization paths
# ---------------------------------------------------------------------------


def test_resolve_alias_uses_direct_alias_before_catalog(monkeypatch):
    monkeypatch.setattr(
        ms,
        "DIRECT_ALIASES",
        {"sonnet": ms.DirectAlias("private-sonnet", "custom:edge", "http://edge/v1")},
    )
    monkeypatch.setattr(ms, "list_provider_models", lambda _provider: pytest.fail("catalog should not be queried"))

    assert ms.resolve_alias("  SONNET  ", "openrouter") == (
        "custom:edge",
        "private-sonnet",
        "sonnet",
    )


def test_resolve_alias_reverse_matches_direct_alias_model_id_case_insensitively(monkeypatch):
    monkeypatch.setattr(
        ms,
        "DIRECT_ALIASES",
        {"glm": ms.DirectAlias("GLM-4.7", "custom", "https://ollama.example/v1")},
    )

    assert ms.resolve_alias("glm-4.7", "openrouter") == ("custom", "GLM-4.7", "glm")


def test_resolve_alias_picks_latest_aggregator_model_from_catalog(monkeypatch):
    monkeypatch.setattr(ms, "_load_direct_aliases", lambda: {})
    monkeypatch.setattr(ms, "is_aggregator", lambda provider: provider == "openrouter")
    monkeypatch.setattr(
        ms,
        "list_provider_models",
        lambda provider: [
            "anthropic/claude-sonnet-4-1",
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-5",
        ]
        if provider == "openrouter"
        else [],
    )

    assert ms.resolve_alias("sonnet", "openrouter") == (
        "openrouter",
        "anthropic/claude-sonnet-4-6",
        "sonnet",
    )


def test_resolve_alias_picks_highest_quality_non_aggregator_model(monkeypatch):
    monkeypatch.setattr(ms, "_load_direct_aliases", lambda: {})
    monkeypatch.setattr(ms, "is_aggregator", lambda _provider: False)
    monkeypatch.setattr(
        ms,
        "list_provider_models",
        lambda provider: ["mimo-v2.4-max", "mimo-v2.5", "mimo-v2.5-pro"]
        if provider == "xiaomi"
        else [],
    )

    assert ms.resolve_alias("mimo", "xiaomi") == ("xiaomi", "mimo-v2.5-pro", "mimo")


def test_resolve_alias_unknown_name_returns_none_without_catalog_lookup(monkeypatch):
    monkeypatch.setattr(ms, "_load_direct_aliases", lambda: {})
    monkeypatch.setattr(ms, "list_provider_models", lambda _provider: pytest.fail("unknown aliases should not query catalog"))

    assert ms.resolve_alias("not-a-real-alias", "openrouter") is None


def test_switch_model_converts_vendor_colon_model_to_aggregator_slug(monkeypatch, switch_dependencies):
    monkeypatch.setattr(ms, "is_aggregator", lambda provider: provider == "openrouter")

    result = ms.switch_model(
        "anthropic:claude-sonnet-4-6",
        current_provider="openrouter",
        current_model="openai/gpt-5",
    )

    assert result.success is True
    assert result.new_model == "anthropic/claude-sonnet-4-6"
    assert switch_dependencies["normalize"] == [("anthropic/claude-sonnet-4-6", "openrouter")]
    assert switch_dependencies["validate"][0][0] == "anthropic/claude-sonnet-4-6"


def test_switch_model_preserves_openrouter_variant_colon_when_slug_already_has_vendor(monkeypatch, switch_dependencies):
    monkeypatch.setattr(ms, "is_aggregator", lambda provider: provider == "openrouter")

    result = ms.switch_model(
        "anthropic/claude-sonnet-4-6:free",
        current_provider="openrouter",
        current_model="openai/gpt-5",
    )

    assert result.success is True
    assert result.new_model == "anthropic/claude-sonnet-4-6:free"
    assert switch_dependencies["validate"][0][0] == "anthropic/claude-sonnet-4-6:free"


# ---------------------------------------------------------------------------
# Public helper functions
# ---------------------------------------------------------------------------


def test_get_authenticated_provider_slugs_forwards_inputs_and_returns_slugs(monkeypatch):
    calls = []

    def fake_list_authenticated_providers(**kwargs):
        calls.append(kwargs)
        return [{"slug": "openrouter"}, {"slug": "custom:local"}]

    monkeypatch.setattr(ms, "list_authenticated_providers", fake_list_authenticated_providers)

    assert ms.get_authenticated_provider_slugs(
        current_provider="anthropic",
        user_providers={"local": {}},
        custom_providers=[{"name": "Local"}],
    ) == ["openrouter", "custom:local"]
    assert calls == [
        {
            "current_provider": "anthropic",
            "user_providers": {"local": {}},
            "custom_providers": [{"name": "Local"}],
            "max_models": 0,
        }
    ]


def test_get_authenticated_provider_slugs_returns_empty_on_listing_errors(monkeypatch):
    monkeypatch.setattr(
        ms,
        "list_authenticated_providers",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
    )

    assert ms.get_authenticated_provider_slugs(current_provider="openrouter") == []


def test_resolve_display_context_length_prefers_provider_aware_metadata(monkeypatch):
    calls = []

    def fake_context_length(model, **kwargs):
        calls.append((model, kwargs))
        return "2048"

    monkeypatch.setattr("agent.model_metadata.get_model_context_length", fake_context_length)

    assert ms.resolve_display_context_length(
        "gpt-5",
        "openai",
        base_url="https://api.openai.example/v1",
        api_key="key",
        model_info=cast(Any, SimpleNamespace(context_window=999)),
        custom_providers=[{"name": "custom"}],
        config_context_length=777,
    ) == 2048
    assert calls == [
        (
            "gpt-5",
            {
                "base_url": "https://api.openai.example/v1",
                "api_key": "key",
                "provider": "openai",
                "custom_providers": [{"name": "custom"}],
                "config_context_length": 777,
            },
        )
    ]


def test_resolve_display_context_length_falls_back_to_model_info(monkeypatch):
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *_args, **_kwargs: None)

    assert ms.resolve_display_context_length(
        "unknown-model",
        "custom",
        model_info=cast(Any, SimpleNamespace(context_window=32768)),
    ) == 32768


# ---------------------------------------------------------------------------
# switch_model validation, provider mismatch, persistence flags, and errors
# ---------------------------------------------------------------------------


def test_switch_model_resolves_alias_normalizes_validates_and_preserves_global_flag(monkeypatch, switch_dependencies):
    monkeypatch.setattr(
        ms,
        "resolve_alias",
        lambda raw, provider: ("openrouter", "anthropic/claude-sonnet-4-6", "sonnet")
        if raw.strip().lower() == "sonnet" and provider == "openrouter"
        else None,
    )

    import hermes_cli.models as model_mod

    def validate_with_warning(model, provider, **kwargs):
        switch_dependencies["validate"].append((model, provider, kwargs))
        return {
            "accepted": True,
            "persist": True,
            "recognized": True,
            "message": "catalog warning",
        }

    monkeypatch.setattr(model_mod, "validate_requested_model", validate_with_warning)

    result = ms.switch_model(
        "sonnet",
        current_provider="openrouter",
        current_model="openai/gpt-5",
        current_base_url="https://old.example/v1",
        current_api_key="old-key",
        is_global=True,
    )

    assert result.success is True
    assert result.new_model == "anthropic/claude-sonnet-4-6"
    assert result.target_provider == "openrouter"
    assert result.provider_changed is False
    assert result.resolved_via_alias == "sonnet"
    assert result.is_global is True
    assert result.api_key == "openrouter-key"
    assert result.base_url == "https://openrouter.example/v1"
    assert result.api_mode == "openrouter-mode"
    assert result.warning_message == "catalog warning"
    assert switch_dependencies["normalize"] == [("anthropic/claude-sonnet-4-6", "openrouter")]
    assert switch_dependencies["validate"][0][0:2] == ("anthropic/claude-sonnet-4-6", "openrouter")


def test_switch_model_falls_back_to_authenticated_provider_for_known_alias(monkeypatch, switch_dependencies):
    def fake_resolve_alias(raw, provider):
        if raw == "sonnet" and provider == "anthropic":
            return ("anthropic", "claude-sonnet-4-6", "sonnet")
        return None

    monkeypatch.setattr(ms, "resolve_alias", fake_resolve_alias)
    monkeypatch.setattr(ms, "get_authenticated_provider_slugs", lambda **_kwargs: ["anthropic"])

    result = ms.switch_model(
        "sonnet",
        current_provider="openai",
        current_model="gpt-5",
    )

    assert result.success is True
    assert result.target_provider == "anthropic"
    assert result.provider_changed is True
    assert result.new_model == "claude-sonnet-4-6"
    assert result.resolved_via_alias == "sonnet"
    assert switch_dependencies["runtime"][0][0] == "anthropic"


def test_switch_model_explicit_provider_resolves_credentials_for_target_provider(monkeypatch, switch_dependencies):
    monkeypatch.setattr(
        ms,
        "resolve_provider_full",
        lambda explicit, _user_providers=None, _custom_providers=None: SimpleNamespace(
            id="anthropic",
            name="Anthropic",
            base_url="",
        )
        if explicit == "anthropic"
        else None,
    )

    result = ms.switch_model(
        "claude-sonnet-4-6",
        current_provider="openrouter",
        current_model="openai/gpt-5",
        explicit_provider="anthropic",
    )

    assert result.success is True
    assert result.target_provider == "anthropic"
    assert result.provider_changed is True
    assert result.new_model == "claude-sonnet-4-6"
    assert result.api_key == "anthropic-key"
    assert result.base_url == "https://anthropic.example/v1"
    assert result.api_mode == "anthropic-mode"
    assert result.provider_label == "Label anthropic"
    assert switch_dependencies["runtime"][0][0] == "anthropic"
    assert switch_dependencies["normalize"] == [("claude-sonnet-4-6", "anthropic")]


def test_switch_model_unknown_provider_returns_actionable_error(monkeypatch):
    monkeypatch.setattr(ms, "resolve_provider_full", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.config.validate_config_structure",
        lambda: [SimpleNamespace(message="providers.openai must be a mapping")],
        raising=False,
    )

    result = ms.switch_model(
        "gpt-5",
        current_provider="openrouter",
        current_model="openai/gpt-5",
        explicit_provider="missing-provider",
        is_global=True,
    )

    assert result.success is False
    assert result.is_global is True
    assert "Unknown provider 'missing-provider'" in result.error_message
    assert "Run 'hermes doctor'" in result.error_message
    assert "providers.openai must be a mapping" in result.error_message


def test_switch_model_rejects_explicit_vendor_alias_when_aggregator_has_no_credentials(monkeypatch):
    import hermes_cli.models as model_mod
    import hermes_cli.providers as provider_mod

    monkeypatch.setattr(
        ms,
        "resolve_provider_full",
        lambda explicit, _user_providers=None, _custom_providers=None: SimpleNamespace(
            id="openrouter",
            name="OpenRouter",
            base_url="",
        )
        if explicit == "openai"
        else None,
    )
    monkeypatch.setattr(model_mod, "_AGGREGATOR_PROVIDERS", {"openrouter"}, raising=False)
    monkeypatch.setattr(provider_mod, "ALIASES", {"openai": "openrouter"}, raising=False)
    monkeypatch.setattr(ms, "get_authenticated_provider_slugs", lambda **_kwargs: ["openai-api"])
    monkeypatch.setattr(ms, "get_label", lambda provider: "OpenRouter" if provider == "openrouter" else provider)

    result = ms.switch_model(
        "gpt-5",
        current_provider="anthropic",
        current_model="claude-sonnet-4-6",
        explicit_provider="openai",
    )

    assert result.success is False
    assert result.target_provider == "openrouter"
    assert result.provider_label == "OpenRouter"
    assert "Provider 'openai' is an alias that routes through OpenRouter" in result.error_message
    assert "has no credentials configured" in result.error_message
    assert "Did you mean: openai-api?" in result.error_message


def test_switch_model_rejects_unknown_model_from_validation(monkeypatch, switch_dependencies):
    import hermes_cli.models as model_mod

    monkeypatch.setattr(
        model_mod,
        "validate_requested_model",
        lambda *_args, **_kwargs: {
            "accepted": False,
            "persist": False,
            "recognized": False,
            "message": "Unknown model 'typo-model' for OpenRouter",
        },
    )

    result = ms.switch_model(
        "typo-model",
        current_provider="openrouter",
        current_model="openai/gpt-5",
    )

    assert result.success is False
    assert result.new_model == "typo-model"
    assert result.target_provider == "openrouter"
    assert result.provider_label == "Label openrouter"
    assert result.error_message == "Unknown model 'typo-model' for OpenRouter"


def test_switch_model_turns_validation_exceptions_into_error_result(monkeypatch, switch_dependencies):
    import hermes_cli.models as model_mod

    def broken_validate(*_args, **_kwargs):
        raise RuntimeError("registry offline")

    monkeypatch.setattr(model_mod, "validate_requested_model", broken_validate)

    result = ms.switch_model(
        "gpt-5",
        current_provider="openai",
        current_model="gpt-4",
    )

    assert result.success is False
    assert result.error_message == "Could not validate `gpt-5`: registry offline"


def test_switch_model_accepts_models_declared_in_user_provider_config_despite_validation_rejection(monkeypatch, switch_dependencies):
    import hermes_cli.models as model_mod

    monkeypatch.setattr(
        model_mod,
        "validate_requested_model",
        lambda *_args, **_kwargs: {
            "accepted": False,
            "persist": False,
            "recognized": False,
            "message": "not present in live /models",
        },
    )

    result = ms.switch_model(
        "private-model",
        current_provider="local-gateway",
        current_model="old-private-model",
        user_providers={
            "local-gateway": {
                "base_url": "http://127.0.0.1:9999/v1",
                "models": {"private-model": {"context_length": 8192}},
            }
        },
    )

    assert result.success is True
    assert result.new_model == "private-model"
    assert result.target_provider == "local-gateway"
    assert result.warning_message == "not present in live /models"


def test_switch_model_returns_error_when_explicit_provider_has_no_model_or_base_url(monkeypatch):
    monkeypatch.setattr(
        ms,
        "resolve_provider_full",
        lambda *_args, **_kwargs: SimpleNamespace(id="anthropic", name="Anthropic", base_url=""),
    )

    result = ms.switch_model(
        "",
        current_provider="openrouter",
        current_model="openai/gpt-5",
        explicit_provider="anthropic",
    )

    assert result.success is False
    assert result.target_provider == "anthropic"
    assert result.provider_label == "Anthropic"
    assert "Provider 'Anthropic' has no base URL configured" in result.error_message
    assert "/model <model-name> --provider anthropic" in result.error_message


def test_switch_model_uses_direct_alias_base_url_override(monkeypatch, switch_dependencies):
    monkeypatch.setattr(
        ms,
        "DIRECT_ALIASES",
        {"local": ms.DirectAlias("llama3.1", "custom", "http://127.0.0.1:11434/v1")},
    )
    monkeypatch.setattr(ms, "resolve_alias", _REAL_RESOLVE_ALIAS)

    result = ms.switch_model(
        "local",
        current_provider="openrouter",
        current_model="openai/gpt-5",
    )

    assert result.success is True
    assert result.target_provider == "custom"
    assert result.new_model == "llama3.1"
    assert result.base_url == "http://127.0.0.1:11434/v1"
    assert result.api_key == "custom-key"
    assert result.resolved_via_alias == "local"


# ---------------------------------------------------------------------------
# Picker/listing public surface
# ---------------------------------------------------------------------------


def test_list_authenticated_providers_includes_user_config_and_grouped_custom_models(picker_registry):
    providers = ms.list_authenticated_providers(
        current_provider="local-gateway",
        user_providers={
            "local-gateway": {
                "name": "Local Gateway",
                "base_url": "http://127.0.0.1:9999/v1",
                "default_model": "qwen3-coder",
                "models": {"deepseek-v4-flash": {"context_length": 65536}},
            }
        },
        custom_providers=[
            {
                "name": "Ollama — Llama 3",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "llama3.1",
                "models": {"qwen3:14b": {"context_length": 16384}},
            },
            {
                "name": "Ollama — DeepSeek",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "deepseek-r1",
            },
        ],
        max_models=3,
        current_model="qwen3-coder",
    )

    local = next(row for row in providers if row["slug"] == "local-gateway")
    ollama = next(row for row in providers if row["slug"] == "custom:ollama")

    assert local["is_current"] is True
    assert local["is_user_defined"] is True
    assert local["source"] == "user-config"
    assert local["api_url"] == "http://127.0.0.1:9999/v1"
    assert local["models"] == ["qwen3-coder", "deepseek-v4-flash"]
    assert ollama["name"] == "Ollama"
    assert ollama["models"] == ["llama3.1", "qwen3:14b", "deepseek-r1"]
    assert ollama["total_models"] == 3


def test_list_picker_providers_filters_openrouter_against_live_catalog_and_keeps_custom_empty_rows(monkeypatch):
    monkeypatch.setattr(
        ms,
        "list_authenticated_providers",
        lambda **_kwargs: [
            {
                "slug": "openrouter",
                "name": "OpenRouter",
                "models": ["stale/model"],
                "total_models": 1,
                "is_user_defined": False,
            },
            {
                "slug": "anthropic",
                "name": "Anthropic",
                "models": [],
                "total_models": 0,
                "is_user_defined": False,
            },
            {
                "slug": "local",
                "name": "Local",
                "models": [],
                "total_models": 0,
                "is_user_defined": True,
                "api_url": "http://127.0.0.1:9999/v1",
            },
        ],
    )
    monkeypatch.setattr(
        "hermes_cli.models.fetch_openrouter_models",
        lambda: [("openai/gpt-5", "GPT-5"), ("anthropic/claude-sonnet", "Claude")],
    )

    providers = ms.list_picker_providers(max_models=1)

    assert [row["slug"] for row in providers] == ["openrouter", "local"]
    assert providers[0]["models"] == ["openai/gpt-5"]
    assert providers[0]["total_models"] == 2
    assert providers[1]["models"] == []


def test_prewarm_picker_cache_async_runs_once_and_uses_picker_context(monkeypatch):
    fake_inventory = ModuleType("hermes_cli.inventory")
    setattr(
        fake_inventory,
        "load_picker_context",
        lambda: SimpleNamespace(
            current_provider="openrouter",
            current_base_url="https://openrouter.example/v1",
            current_model="openai/gpt-5",
            user_providers={"local": {}},
            custom_providers=[{"name": "Local"}],
        ),
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.inventory", fake_inventory)

    calls = []
    monkeypatch.setattr(ms, "list_authenticated_providers", lambda **kwargs: calls.append(kwargs) or [])

    thread = ms.prewarm_picker_cache_async()
    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert calls == [
        {
            "current_provider": "openrouter",
            "current_base_url": "https://openrouter.example/v1",
            "current_model": "openai/gpt-5",
            "user_providers": {"local": {}},
            "custom_providers": [{"name": "Local"}],
            "max_models": 50,
        }
    ]

    assert ms.prewarm_picker_cache_async() is None
