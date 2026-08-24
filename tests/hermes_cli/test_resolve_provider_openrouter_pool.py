"""Regression tests for issue #42130.

A credential added via `hermes auth add openrouter` lives in the credential
pool, NOT as an OPENROUTER_API_KEY env var. Before the fix, resolve_provider()
auto-detection only checked env vars, so such a credential was invisible:
the provider failed to resolve (AuthError) or resolved without a key, and
requests went out with no Authorization header — OpenRouter's
"HTTP 401: Missing Authentication header".

These tests lock in that auto-detection consults the OpenRouter pool.
"""

import uuid

import pytest


@pytest.fixture(autouse=True)
def _clean_inference_env(monkeypatch):
    """Strip credential-shaped env vars so the pool is the only source."""
    for key in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "NOUS_API_KEY",
        "HERMES_INFERENCE_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)


def _seed_openrouter_pool(token: str = "sk-or-FAKEKEY123") -> None:
    """Mimic `hermes auth add openrouter <token>` — a manual pool entry."""
    from agent.credential_pool import (
        AUTH_TYPE_API_KEY,
        SOURCE_MANUAL,
        PooledCredential,
        load_pool,
    )

    pool = load_pool("openrouter")
    pool.add_entry(
        PooledCredential(
            provider="openrouter",
            id=uuid.uuid4().hex[:6],
            label="api-key-1",
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source=SOURCE_MANUAL,
            access_token=token,
            base_url="https://openrouter.ai/api/v1",
        )
    )


def test_auto_detects_openrouter_from_pool(tmp_path, monkeypatch):
    """With only a pool credential (no env var), auto-detection finds it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
    _seed_openrouter_pool()

    from hermes_cli.auth import resolve_provider

    assert resolve_provider("auto") == "openrouter"


def test_auto_does_not_detect_openrouter_from_pool_when_paid_fallback_disabled(
    tmp_path, monkeypatch
):
    """Safety net: auth.disable_paid_api_fallback=true must stop the auto path's
    OpenRouter-pool auto-detection (issue #42130's tier 4), not just the
    Anthropic-specific guard in runtime_provider.py. With no other provider
    configured, the auto path falls through to "no provider configured"
    rather than silently returning openrouter."""
    from hermes_cli.auth import AuthError, resolve_provider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
    _seed_openrouter_pool()
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"auth": {"disable_paid_api_fallback": True}},
    )

    with pytest.raises(AuthError) as exc_info:
        resolve_provider("auto")
    assert exc_info.value.code == "no_provider_configured"


def test_auto_does_not_detect_openrouter_from_env_when_paid_fallback_disabled(
    tmp_path, monkeypatch
):
    """Safety net: auth.disable_paid_api_fallback=true must stop the auto
    path's OPENROUTER_API_KEY/OPENAI_API_KEY env-var tier too (tier 3)."""
    from hermes_cli.auth import AuthError, resolve_provider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-FAKEKEY123")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"auth": {"disable_paid_api_fallback": True}},
    )

    with pytest.raises(AuthError) as exc_info:
        resolve_provider("auto")
    assert exc_info.value.code == "no_provider_configured"


