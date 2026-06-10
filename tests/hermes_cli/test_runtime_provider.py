from __future__ import annotations

import pytest

from hermes_cli import runtime_provider as rp
from hermes_cli.auth import AuthError


class _EmptyPool:
    def has_credentials(self) -> bool:
        return False

    def select(self):  # pragma: no cover - defensive if selection order regresses
        raise AssertionError("empty pool should not be selected")


@pytest.fixture(autouse=True)
def _isolate_runtime_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep runtime-provider tests off real config, env, pools, and credentials."""
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "AZURE_ANTHROPIC_KEY",
        "CUSTOM_BASE_URL",
        "HERMES_INFERENCE_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(rp, "_get_model_config", lambda: {})
    monkeypatch.setattr(
        rp,
        "load_config",
        lambda: {"model": {}, "auth": {"disable_paid_api_fallback": False}},
    )


def _configure_model_and_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_cfg: dict[str, object],
    disable_paid_api_fallback: bool,
) -> None:
    monkeypatch.setattr(rp, "_get_model_config", lambda: dict(model_cfg))
    monkeypatch.setattr(
        rp,
        "load_config",
        lambda: {
            "model": dict(model_cfg),
            "auth": {"disable_paid_api_fallback": disable_paid_api_fallback},
        },
    )


def test_disable_paid_api_fallback_blocks_anthropic_when_pool_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_model_and_auth(
        monkeypatch,
        model_cfg={"provider": "anthropic", "default": "claude-sonnet-4"},
        disable_paid_api_fallback=True,
    )
    monkeypatch.setattr(rp, "resolve_provider", lambda *args, **kwargs: "anthropic")

    def _pool_load_fails(provider: str):
        assert provider == "anthropic"
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(rp, "load_pool", _pool_load_fails)

    with pytest.raises(AuthError) as exc_info:
        rp.resolve_runtime_provider(requested="anthropic")

    assert exc_info.value.provider == "anthropic"
    assert exc_info.value.code == "anthropic_oauth_missing"
    assert exc_info.value.relogin_required is True
    assert "auth.disable_paid_api_fallback=true" in str(exc_info.value)


def test_disable_paid_api_fallback_uses_anthropic_oauth_pool_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_model_and_auth(
        monkeypatch,
        model_cfg={"provider": "anthropic", "default": "claude-sonnet-4"},
        disable_paid_api_fallback=True,
    )
    monkeypatch.setattr(rp, "resolve_provider", lambda *args, **kwargs: "anthropic")

    class _OAuthEntry:
        access_token = "fake-anthropic-oauth-token"
        base_url = "https://api.anthropic.com"
        source = "claude_code_oauth"

    class _Pool:
        selected_oauth_only = False

        def select_anthropic_oauth_only(self):
            self.selected_oauth_only = True
            return _OAuthEntry()

    pool = _Pool()
    monkeypatch.setattr(rp, "load_pool", lambda provider: pool)

    resolved = rp.resolve_runtime_provider(requested="anthropic")

    assert pool.selected_oauth_only is True
    assert resolved["provider"] == "anthropic"
    assert resolved["api_mode"] == "anthropic_messages"
    assert resolved["api_key"] == "fake-anthropic-oauth-token"
    assert resolved["source"] == "claude_code_oauth"
    assert resolved["credential_pool"] is pool


def test_disable_paid_api_fallback_only_guards_anthropic_KNOWN_GAP(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current behavior: the flag does not block non-Anthropic fallback routes."""
    _configure_model_and_auth(
        monkeypatch,
        model_cfg={"provider": "auto", "default": "some-model"},
        disable_paid_api_fallback=True,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-key")
    monkeypatch.setattr(rp, "resolve_provider", lambda *args, **kwargs: "openrouter")
    monkeypatch.setattr(rp, "load_pool", lambda provider: _EmptyPool())

    resolved = rp.resolve_runtime_provider(requested="auto")

    assert resolved["provider"] == "openrouter"
    assert resolved["requested_provider"] == "auto"
    assert resolved["api_key"] == "fake-openrouter-key"


def test_resolve_requested_provider_prefers_explicit_over_config_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "nous")
    monkeypatch.setattr(rp, "_get_model_config", lambda: {"provider": "anthropic"})

    assert rp.resolve_requested_provider("  openrouter  ") == "openrouter"


def test_resolve_requested_provider_prefers_config_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")
    monkeypatch.setattr(rp, "_get_model_config", lambda: {"provider": "anthropic"})

    assert rp.resolve_requested_provider() == "anthropic"


def test_resolve_requested_provider_uses_env_then_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rp, "_get_model_config", lambda: {})
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "qwen-oauth")
    assert rp.resolve_requested_provider() == "qwen-oauth"

    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    assert rp.resolve_requested_provider() == "auto"


def test_named_custom_runtime_is_selected_before_builtin_provider_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rp,
        "_get_named_custom_provider",
        lambda requested: {
            "name": "Local Gateway",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "fake-custom-key",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(rp, "_try_resolve_from_custom_pool", lambda *args, **kwargs: None)

    def _provider_resolver_must_not_run(*args, **kwargs):
        raise AssertionError("named custom provider should resolve before resolve_provider")

    monkeypatch.setattr(rp, "resolve_provider", _provider_resolver_must_not_run)

    resolved = rp.resolve_runtime_provider(requested="custom:local-gateway")

    assert resolved["provider"] == "custom"
    assert resolved["api_key"] == "fake-custom-key"
    assert resolved["base_url"] == "http://127.0.0.1:8080/v1"
    assert resolved["source"] == "custom_provider:Local Gateway"
    assert resolved["requested_provider"] == "custom:local-gateway"


def test_explicit_runtime_is_selected_before_credential_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rp, "resolve_provider", lambda *args, **kwargs: "xai")

    def _pool_must_not_load(provider: str):
        raise AssertionError(f"explicit runtime should not load {provider} pool")

    monkeypatch.setattr(rp, "load_pool", _pool_must_not_load)

    resolved = rp.resolve_runtime_provider(
        requested="xai",
        explicit_api_key="fake-xai-key",
        explicit_base_url="https://api.x.ai/v1",
    )

    assert resolved["provider"] == "xai"
    assert resolved["api_mode"] == "codex_responses"
    assert resolved["api_key"] == "fake-xai-key"
    assert resolved["base_url"] == "https://api.x.ai/v1"
    assert resolved["source"] == "explicit"


def test_pool_entry_without_api_key_exhausts_pool_then_uses_provider_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rp, "resolve_provider", lambda *args, **kwargs: "openai-codex")

    class _EmptyEntry:
        access_token = ""
        runtime_api_key = ""
        base_url = "https://chatgpt.com/backend-api/codex"
        source = "pool-empty"

    class _Pool:
        def has_credentials(self) -> bool:
            return True

        def select(self):
            return _EmptyEntry()

    monkeypatch.setattr(rp, "load_pool", lambda provider: _Pool())
    monkeypatch.setattr(
        rp,
        "resolve_codex_runtime_credentials",
        lambda: {
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "fake-codex-runtime-token",
            "source": "codex-auth-store",
            "last_refresh": "2026-06-10T00:00:00Z",
        },
    )

    resolved = rp.resolve_runtime_provider(requested="openai-codex")

    assert resolved["provider"] == "openai-codex"
    assert resolved["api_mode"] == "codex_responses"
    assert resolved["api_key"] == "fake-codex-runtime-token"
    assert resolved["source"] == "codex-auth-store"
    assert resolved.get("credential_pool") is None


def test_auto_oauth_failure_falls_through_to_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-key")
    monkeypatch.setattr(rp, "resolve_provider", lambda *args, **kwargs: "qwen-oauth")
    monkeypatch.setattr(rp, "load_pool", lambda provider: _EmptyPool())

    def _qwen_credentials_fail():
        raise AuthError("stale qwen credentials", provider="qwen-oauth", code="qwen_auth_missing")

    monkeypatch.setattr(rp, "resolve_qwen_runtime_credentials", _qwen_credentials_fail)

    resolved = rp.resolve_runtime_provider(requested="auto")

    assert resolved["provider"] == "openrouter"
    assert resolved["requested_provider"] == "auto"
    assert resolved["api_key"] == "fake-openrouter-key"


def test_explicit_oauth_failure_exhausts_without_openrouter_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-key")
    monkeypatch.setattr(rp, "resolve_provider", lambda *args, **kwargs: "qwen-oauth")
    monkeypatch.setattr(rp, "load_pool", lambda provider: _EmptyPool())

    def _qwen_credentials_fail():
        raise AuthError("stale qwen credentials", provider="qwen-oauth", code="qwen_auth_missing")

    def _openrouter_fallback_must_not_run(*args, **kwargs):
        raise AssertionError("explicit qwen failure should not fall back to OpenRouter")

    monkeypatch.setattr(rp, "resolve_qwen_runtime_credentials", _qwen_credentials_fail)
    monkeypatch.setattr(rp, "_resolve_openrouter_runtime", _openrouter_fallback_must_not_run)

    with pytest.raises(AuthError) as exc_info:
        rp.resolve_runtime_provider(requested="qwen-oauth")

    assert exc_info.value.provider == "qwen-oauth"
    assert exc_info.value.code == "qwen_auth_missing"
