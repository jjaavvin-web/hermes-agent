"""Regression test: set_runtime_main() must not leak a custom provider's
base_url/api_key into ``provider: auto`` auxiliary resolution.

Originally (https://github.com/NousResearch/hermes-agent/issues/34777)
upstream's ``_resolve_auto`` had a "Step 1: main provider" branch that used
whatever ``custom:<name>`` main provider was carried in the runtime globals.
The fork's fail-closed policy for ``provider: auto`` (see the docstring on
``agent.auxiliary_client._resolve_auto_route``) intentionally removed that
entire discovery chain: ``auto`` may ONLY resolve to the sanctioned
subscription route (openai-codex/gpt-5.5), never to a user's custom/paid
endpoint, even when one is fully configured and live in the runtime globals.
That policy already held at the fork's v0.20 base — these tests were stale
against the fork's own architecture even before the v0.20.1 upstream merge,
so they are rewritten here to guard the CURRENT contract: that a fully wired
custom provider (config.yaml entry *and* live set_runtime_main() globals)
never leaks through resolve_provider_client("auto", ...).
"""
import pytest
from unittest.mock import patch, MagicMock


def _get_globals(mod):
    """Read runtime globals without triggering redaction."""
    return {
        "provider": mod._RUNTIME_MAIN_PROVIDER,
        "model": mod._RUNTIME_MAIN_MODEL,
        "base_url": mod._RUNTIME_MAIN_BASE_URL,
        "cred": mod._RUNTIME_MAIN_API_KEY,  # renamed to avoid redaction
        "api_mode": mod._RUNTIME_MAIN_API_MODE,
    }


class TestSetRuntimeMainCustomProvider:
    """set_runtime_main must propagate base_url/api_key/api_mode for custom providers."""


    def test_clear_resets_all_globals(self):
        """clear_runtime_main resets all five globals to empty."""
        import agent.auxiliary_client as mod

        mod.set_runtime_main(
            "custom:x", "m",
            base_url="https://x.example.com",
            api_key="sk-abc",
            api_mode="chat_completions",
        )
        mod.clear_runtime_main()
        g = _get_globals(mod)
        for v in g.values():
            assert v == "", f"Expected empty, got {v!r}"

    def test_resolve_auto_ignores_custom_provider_globals(self):
        """_resolve_auto must resolve to the sanctioned provider/model even
        when set_runtime_main() has a custom provider live in the globals —
        the fail-closed policy for provider:auto overrides any main-runtime
        custom routing rather than reading base_url/api_key from it."""
        import agent.auxiliary_client as mod

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:test-router",
                "test-model",
                base_url="https://custom-endpoint.example.com/v1",
                api_key="sk-test-123",
            )

            with patch.object(mod, "resolve_provider_client") as mock_resolve:
                mock_resolve.return_value = (MagicMock(), "test-model")
                client, resolved = mod._resolve_auto(main_runtime=None)

                mock_resolve.assert_called_once()
                call_args = mock_resolve.call_args
                # Sanctioned route only — never the custom provider carried
                # in the runtime globals.
                assert call_args[0][0] == mod._SANCTIONED_AUTO_PROVIDER
                assert call_args[0][1] == mod._SANCTIONED_AUTO_MODEL
                assert call_args[0][0] != "custom"
                assert call_args[1]["api_mode"] == "codex_responses"
                # No explicit_base_url/explicit_api_key override — the
                # sanctioned route resolves through its own credentials,
                # not the custom endpoint's.
                assert call_args[1].get("explicit_base_url") is None
                assert call_args[1].get("explicit_api_key") is None
        finally:
            mod.clear_runtime_main()




class TestResolveAutoCustomEndToEnd:
    """End-to-end routing assertions — build a *real* client (no mock on
    resolve_provider_client) and verify the fail-closed auxiliary auto-detect
    chain never leaks to the user's custom endpoint, even when one is fully
    configured via both config.yaml and the live set_runtime_main() globals.
    These guard against #34777's fix regressing into a security hole: routing
    'auto' straight to whatever custom base_url happens to be configured,
    bypassing the sanctioned-only fail-closed policy entirely.
    """

    def test_config_less_custom_endpoint_does_not_leak_via_auto(self, tmp_path, monkeypatch):
        """custom:<name> with NO config entry, only a live base_url carried
        by set_runtime_main(): resolve_provider_client("auto", ...) must NOT
        build a client at that endpoint. With no Codex OAuth token in this
        hermetic test environment, the sanctioned route is unavailable, so
        the call must fail closed (None) — not fall through to the custom
        endpoint the way pre-fork upstream's Step 1 did."""
        import agent.auxiliary_client as mod

        # Hermetic: no aggregator creds, no stale OPENAI_BASE_URL.
        for var in ("OPENROUTER_API_KEY", "NOUS_API_KEY", "OPENAI_API_KEY",
                    "OPENAI_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: glm-5.1\n"
            "  provider: 'custom:ephemeral'\n"
            "  base_url: ''\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:ephemeral",
                "glm-5.1",
                base_url="https://ephemeral.live/v1",
                api_key="sk-live",
            )
            client, resolved = mod.resolve_provider_client("auto", None)
            assert client is None, (
                "provider:auto built a client despite no sanctioned Codex "
                "OAuth token — it must fail closed, not silently fall "
                "through to the custom endpoint carried in the runtime "
                "globals (the #34777 fix must not become a leak)"
            )
            assert resolved is None
        finally:
            mod.clear_runtime_main()

    def test_named_custom_with_config_entry_does_not_leak_via_auto(self, tmp_path, monkeypatch):
        """custom:<name> WITH a custom_providers config entry: even a fully
        configured named endpoint must not be reachable through
        resolve_provider_client("auto", ...) — only an explicit
        provider="custom:openclaw" request may route there."""
        import agent.auxiliary_client as mod

        for var in ("OPENROUTER_API_KEY", "NOUS_API_KEY", "OPENAI_API_KEY",
                    "OPENAI_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: glm-5.1\n"
            "  provider: 'custom:openclaw'\n"
            "  base_url: ''\n"
            "custom_providers:\n"
            "  - name: openclaw\n"
            "    base_url: 'https://withcfg.example/v1'\n"
            "    model: glm-5.1\n"
            "    api_key: cfg-key\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main("custom:openclaw", "glm-5.1")
            client, resolved = mod.resolve_provider_client("auto", None)
            assert client is None, (
                "provider:auto resolved to the named custom_providers entry "
                "instead of failing closed — auto must never reach a "
                "custom endpoint, config-declared or not"
            )
            assert resolved is None
        finally:
            mod.clear_runtime_main()

    def test_named_custom_anthropic_messages_does_not_leak_via_auto(
            self, tmp_path, monkeypatch):
        """PR #36043's named-custom-provider arm (api_mode: anthropic_messages,
        e.g. Palantir Foundry's Anthropic proxy) must also stay unreachable
        through provider:auto — the fail-closed policy applies uniformly
        regardless of which resolve_provider_client arm the named entry would
        otherwise land in."""
        import agent.auxiliary_client as mod

        for var in ("OPENROUTER_API_KEY", "NOUS_API_KEY", "OPENAI_API_KEY",
                    "OPENAI_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        proxy_base = "https://acme.palantirfoundry.com/api/v2/llm/proxy/anthropic"
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  default: claude-4-6-opus\n"
            "  provider: 'custom:palantir'\n"
            "  base_url: ''\n"
            "custom_providers:\n"
            "  - name: palantir\n"
            f"    base_url: '{proxy_base}'\n"
            "    model: claude-4-6-opus\n"
            "    api_key: foundry-token\n"
            "    api_mode: anthropic_messages\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        mod.clear_runtime_main()
        try:
            mod.set_runtime_main(
                "custom:palantir",
                "claude-4-6-opus",
                base_url=proxy_base,
                api_key="foundry-token",
                api_mode="anthropic_messages",
            )
            client, resolved = mod.resolve_provider_client("auto", None)
            assert client is None, (
                "provider:auto resolved to the anthropic_messages named "
                "custom provider instead of failing closed"
            )
            assert resolved is None

            # Wiring check: _resolve_auto must hand the SANCTIONED
            # provider/model to resolve_provider_client — never the
            # custom:<name> string carried in the runtime globals, even
            # though that string is fully configured end to end.
            with patch.object(mod, "resolve_provider_client") as mock_resolve:
                mock_resolve.return_value = (MagicMock(), "claude-4-6-opus")
                mod._resolve_auto(main_runtime=None)
            mock_resolve.assert_called_once()
            assert mock_resolve.call_args.args[0] == mod._SANCTIONED_AUTO_PROVIDER
            assert mock_resolve.call_args.args[0] != "custom:palantir"
        finally:
            mod.clear_runtime_main()
