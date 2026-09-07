"""Integration regression for the safe-provider filter under
``auth.disable_paid_api_fallback`` (post-audit fix closing the
anthropic_oauth_spoof_provider gap left by the initial
fable-safe-provider-filter-v1 change).

Exercises ``resolve_runtime_provider()`` end-to-end — not just
``is_safe_provider()`` in isolation — so a future call-site regression
(reordering the gate, exempting a provider, restoring a removed branch)
is caught here at the security-review level, not just in a unit test that
could be edited alongside the regression.
"""

from __future__ import annotations

import pytest

from hermes_cli import runtime_provider as rp
from hermes_cli.auth import AuthError
from hermes_cli.providers import SAFE_PROVIDERS


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this suite off real env credentials — only the flag under test
    and explicitly-set values should influence resolution."""
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


def _configure(
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


def _empty_pool():
    return type("_EmptyPool", (), {"has_credentials": lambda self: False})()


# ---------------------------------------------------------------------------
# (a) anthropic + flag + a live OAuth pool entry -> blocked
# ---------------------------------------------------------------------------


def test_anthropic_with_live_oauth_pool_entry_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical lock forbids anthropic_oauth_spoof_provider regardless
    of billing shape: a live, unexpired Claude Code OAuth credential in the
    pool must not be used to serve provider="anthropic" while the flag is
    set. select_anthropic_oauth_only() must never even be called."""
    _configure(
        monkeypatch,
        model_cfg={"provider": "anthropic", "default": "claude-sonnet-4"},
        disable_paid_api_fallback=True,
    )
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "anthropic")

    class _OAuthEntry:
        access_token = "fake-anthropic-oauth-token"
        base_url = "https://api.anthropic.com"
        source = "claude_code_oauth"

    class _Pool:
        def select_anthropic_oauth_only(self):
            raise AssertionError(
                "select_anthropic_oauth_only() must not be reached when "
                "auth.disable_paid_api_fallback=true"
            )

        def has_credentials(self) -> bool:
            raise AssertionError("pool must not be probed under the flag")

    monkeypatch.setattr(rp, "load_pool", lambda provider: _Pool())

    with pytest.raises(AuthError) as exc_info:
        rp.resolve_runtime_provider(requested="anthropic")

    assert exc_info.value.code == "paid_provider_blocked"
    assert exc_info.value.provider == "anthropic"


# ---------------------------------------------------------------------------
# (b) openrouter + flag + env key -> blocked
# ---------------------------------------------------------------------------


def test_openrouter_with_env_key_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(
        monkeypatch,
        model_cfg={"provider": "auto", "default": "some-model"},
        disable_paid_api_fallback=True,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-env-key")
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "openrouter")
    monkeypatch.setattr(rp, "load_pool", lambda provider: _empty_pool())

    with pytest.raises(AuthError) as exc_info:
        rp.resolve_runtime_provider(requested="auto")

    assert exc_info.value.code == "paid_provider_blocked"
    assert exc_info.value.provider == "openrouter"


# ---------------------------------------------------------------------------
# (c) each SAFE_PROVIDERS member + flag -> NOT blocked by this gate
#
# SAFE_PROVIDERS currently has exactly three members. This assertion pins
# that set so that adding a fourth forces whoever changes it to also add an
# integration test below — the whole point of this file is that the
# allow-list and its exercised behavior cannot silently drift apart.
# ---------------------------------------------------------------------------


def test_safe_providers_set_matches_the_integration_tests_below() -> None:
    assert SAFE_PROVIDERS == frozenset({"openai-codex", "xai-oauth", "claude-cli-subprocess"})


def test_openai_codex_is_not_blocked_by_the_safe_provider_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(
        monkeypatch,
        model_cfg={"provider": "openai-codex", "default": "gpt-5-codex"},
        disable_paid_api_fallback=True,
    )
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "openai-codex")
    monkeypatch.setattr(rp, "load_pool", lambda provider: _empty_pool())
    monkeypatch.setattr(
        rp,
        "resolve_codex_runtime_credentials",
        lambda: {
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "fake-codex-token",
            "source": "codex-auth-json",
        },
    )

    resolved = rp.resolve_runtime_provider(requested="openai-codex")

    assert resolved["provider"] == "openai-codex"
    assert resolved["api_key"] == "fake-codex-token"


def test_xai_oauth_is_not_blocked_by_the_safe_provider_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(
        monkeypatch,
        model_cfg={"provider": "xai-oauth", "default": "grok-4"},
        disable_paid_api_fallback=True,
    )
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "xai-oauth")
    monkeypatch.setattr(rp, "load_pool", lambda provider: _empty_pool())
    monkeypatch.setattr(
        rp,
        "resolve_xai_oauth_runtime_credentials",
        lambda: {
            "provider": "xai-oauth",
            "base_url": "https://api.x.ai/v1",
            "api_key": "fake-xai-oauth-token",
            "source": "hermes-auth-store",
        },
    )

    resolved = rp.resolve_runtime_provider(requested="xai-oauth")

    assert resolved["provider"] == "xai-oauth"
    assert resolved["api_key"] == "fake-xai-oauth-token"


def test_claude_cli_subprocess_is_not_blocked_by_the_safe_provider_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude-cli-subprocess short-circuits before resolve_provider() is
    even called, so it never reaches — and is never blocked by — the
    paid-provider gate. Pins that early-return ordering explicitly."""
    _configure(
        monkeypatch,
        model_cfg={"provider": "claude-cli-subprocess"},
        disable_paid_api_fallback=True,
    )

    def _resolve_provider_must_not_run(*a, **k):
        raise AssertionError(
            "claude-cli-subprocess must short-circuit before resolve_provider()"
        )

    monkeypatch.setattr(rp, "resolve_provider", _resolve_provider_must_not_run)

    resolved = rp.resolve_runtime_provider(requested="claude-cli-subprocess")

    assert resolved["provider"] == "claude-cli-subprocess"
    assert resolved["api_mode"] == "claude_cli_subprocess"


# ---------------------------------------------------------------------------
# F3: MoA reference/aggregator slots must not turn a blocked "anthropic"
# resolution into a live (uncredentialed) client construction attempt.
#
# agent.moa_loop._slot_runtime() swallows ALL resolve_runtime_provider()
# errors (including paid_provider_blocked) and returns a bare
# {"provider": ..., "model": ...} dict with no base_url/api_key/api_mode —
# "never abort the whole MoA turn for one misconfigured slot". That credential
# -less dict is then handed to call_llm(task="moa_reference", **runtime). This
# pins today's actual safety net: with no auxiliary.moa_reference.fallback_chain
# configured (the default), _call_llm_impl must fail closed (raise) rather
# than proceeding to build/return a client. If a future change ever gives
# moa_reference tasks a default fallback chain, this test must be revisited
# to confirm that chain can't quietly resolve back to a blocked provider.
# ---------------------------------------------------------------------------


def test_moa_anthropic_slot_under_flag_fails_closed_with_empty_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli.auth import AuthError
    import hermes_cli.runtime_provider as rp_mod
    from agent import moa_loop

    def _blocked(*args, **kwargs):
        raise AuthError(
            "Provider 'anthropic' is blocked because auth.disable_paid_api_fallback=true",
            provider="anthropic",
            code="paid_provider_blocked",
        )

    monkeypatch.setattr(rp_mod, "resolve_runtime_provider", _blocked)

    # _slot_runtime() swallows the AuthError and returns a bare, credential
    # -less slot — pin that this is still the current (pre-existing) shape.
    slot_runtime = moa_loop._slot_runtime({"provider": "anthropic", "model": "claude-opus-4-7"})
    assert slot_runtime == {"provider": "anthropic", "model": "claude-opus-4-7"}
    assert "api_key" not in slot_runtime
    assert "base_url" not in slot_runtime

    import agent.auxiliary_client as aux

    client_construction_attempted = []

    def _no_client(*args, **kwargs):
        client_construction_attempted.append(args)
        return None, None

    def _empty_fallback_chain(*args, **kwargs):
        # Mirrors auxiliary.moa_reference.fallback_chain being unset (the
        # default) — _try_configured_fallback_chain() returns (None, None, "").
        return None, None, ""

    monkeypatch.setattr(aux, "_get_cached_client", _no_client)
    monkeypatch.setattr(
        aux, "_try_configured_fallback_for_unavailable_client", _empty_fallback_chain
    )

    with pytest.raises(RuntimeError):
        aux.call_llm(
            task="moa_reference",
            messages=[{"role": "user", "content": "hi"}],
            **slot_runtime,
        )

    assert client_construction_attempted, (
        "the no-client codepath must actually be exercised by this test"
    )
