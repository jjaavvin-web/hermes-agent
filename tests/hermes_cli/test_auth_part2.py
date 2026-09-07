"""Part 2 coverage for hermes_cli.auth provider getters and login-state helpers.

This file intentionally stays isolated from real auth state: every auth path is
redirected into tmp_path and refresh/network/subprocess boundaries are mocked.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import auth
from hermes_cli.auth import AuthError


def _jwt_with_exp(exp_epoch: int) -> str:
    payload = {"exp": exp_epoch}
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
        .rstrip(b"=")
        .decode("utf-8")
    )
    return f"h.{encoded}.s"


@pytest.fixture()
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(auth, "_nous_auth_status_cache", None, raising=False)
    return home


def _write_auth_store(home: Path, store: dict) -> Path:
    path = home / "auth.json"
    path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    return path


def _provider_store(provider_id: str, state: dict, *, active: str | None = None) -> dict:
    return {
        "version": auth.AUTH_STORE_VERSION,
        "active_provider": active if active is not None else provider_id,
        "providers": {provider_id: state},
    }


# ---------------------------------------------------------------------------
# Registry / normalization helpers
# ---------------------------------------------------------------------------


def test_provider_registry_helpers_normalize_known_display_names():
    assert auth.is_known_auth_provider("  NOUS  ") is True
    assert auth.is_known_auth_provider("spotify") is True
    assert auth.is_known_auth_provider("definitely-not-real") is False

    assert auth.get_auth_provider_display_name(" OPENAI-CODEX ") == "OpenAI Codex"
    assert auth.get_auth_provider_display_name("spotify") == "Spotify"
    assert auth.get_auth_provider_display_name("CustomThing") == "CustomThing"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("claude", "anthropic"),
        # "gemini-cli" -> "google-gemini-cli" removed in the v2026.7.1 merge —
        # upstream deleted the Google-OAuth/Gemini-CloudCode lane and its
        # alias; fork runtime does not use it.
        ("github-copilot-acp", "copilot-acp"),
        ("lm-studio", "lmstudio"),
        ("tokenhub", "tencent-tokenhub"),
    ],
)
def test_resolve_provider_normalizes_aliases(raw: str, expected: str):
    assert auth.resolve_provider(raw) == expected


# ---------------------------------------------------------------------------
# Auth-store persistence helpers
# ---------------------------------------------------------------------------


def test_auth_store_load_missing_and_malformed_json_are_empty_and_preserved(hermes_home: Path):
    missing = auth._load_auth_store()
    assert missing == {"version": auth.AUTH_STORE_VERSION, "providers": {}}

    auth_file = hermes_home / "auth.json"
    auth_file.write_text("not-json{{", encoding="utf-8")

    loaded = auth._load_auth_store()

    assert loaded == {"version": auth.AUTH_STORE_VERSION, "providers": {}}
    assert (hermes_home / "auth.json.corrupt").exists()


def test_save_provider_state_roundtrip_uses_tmp_auth_store_only(hermes_home: Path):
    store = auth._load_auth_store()
    auth._save_provider_state(store, "spotify", {"access_token": "tok"})
    saved_path = auth._save_auth_store(store)

    assert saved_path == hermes_home / "auth.json"
    reloaded = auth._load_auth_store()
    assert reloaded["providers"]["spotify"]["access_token"] == "tok"
    assert reloaded["active_provider"] == "spotify"


def test_credential_pool_write_read_and_suppression_roundtrip(hermes_home: Path):
    saved_path = auth.write_credential_pool(
        "openai-codex",
        [{"access_token": "pool-token", "source": "manual:device_code"}],
    )
    assert saved_path == hermes_home / "auth.json"
    assert auth.read_credential_pool("openai-codex") == [
        {"access_token": "pool-token", "source": "manual:device_code"}
    ]

    auth.suppress_credential_source("openai-codex", "manual:device_code")
    assert auth.is_source_suppressed("openai-codex", "manual:device_code") is True
    assert auth.unsuppress_credential_source("openai-codex", "manual:device_code") is True
    assert auth.is_source_suppressed("openai-codex", "manual:device_code") is False


# ---------------------------------------------------------------------------
# Codex provider getter
# ---------------------------------------------------------------------------


def test_codex_runtime_credentials_happy_path_and_shape(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    _write_auth_store(
        hermes_home,
        _provider_store(
            "openai-codex",
            {
                "tokens": {"access_token": "codex-access", "refresh_token": "codex-refresh"},
                "last_refresh": "2026-06-01T00:00:00Z",
                "auth_mode": "chatgpt",
            },
        ),
    )
    monkeypatch.setenv("HERMES_CODEX_BASE_URL", "https://codex.example/v1/")

    creds = auth.resolve_codex_runtime_credentials(refresh_if_expiring=False)

    assert creds == {
        "provider": "openai-codex",
        "base_url": "https://codex.example/v1",
        "api_key": "codex-access",
        "source": "hermes-auth-store",
        "last_refresh": "2026-06-01T00:00:00Z",
        "auth_mode": "chatgpt",
    }


def test_codex_runtime_credentials_missing_auth_file(hermes_home: Path):
    with pytest.raises(AuthError) as exc:
        auth.resolve_codex_runtime_credentials()

    assert exc.value.provider == "openai-codex"
    assert exc.value.code == "codex_auth_missing"
    assert exc.value.relogin_required is True


def test_codex_runtime_credentials_malformed_auth_file(hermes_home: Path):
    (hermes_home / "auth.json").write_text("not-json{{", encoding="utf-8")

    with pytest.raises(AuthError) as exc:
        auth.resolve_codex_runtime_credentials()

    assert exc.value.provider == "openai-codex"
    assert exc.value.code == "codex_auth_missing"
    assert exc.value.relogin_required is True
    assert (hermes_home / "auth.json.corrupt").exists()


def test_codex_expired_token_terminal_refresh_signals_relogin_required(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    expired = _jwt_with_exp(int(time.time()) - 30)
    _write_auth_store(
        hermes_home,
        _provider_store(
            "openai-codex",
            {"tokens": {"access_token": expired, "refresh_token": "refresh"}},
        ),
    )

    def _terminal_refresh(_tokens: dict, _timeout_seconds: float) -> dict:
        raise AuthError(
            "refresh token invalid",
            provider="openai-codex",
            code="codex_refresh_invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "_refresh_codex_auth_tokens", _terminal_refresh)

    with pytest.raises(AuthError) as exc:
        auth.resolve_codex_runtime_credentials(refresh_if_expiring=True)

    assert exc.value.code == "codex_refresh_invalid_grant"
    assert exc.value.relogin_required is True


# ---------------------------------------------------------------------------
# xAI OAuth provider getter
# ---------------------------------------------------------------------------


def test_xai_oauth_runtime_credentials_happy_path_and_shape(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    _write_auth_store(
        hermes_home,
        _provider_store(
            "xai-oauth",
            {
                "tokens": {"access_token": "xai-access", "refresh_token": "xai-refresh"},
                "last_refresh": "2026-06-02T00:00:00Z",
                "auth_mode": "oauth_pkce",
                "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"},
                "redirect_uri": "http://127.0.0.1:56121/callback",
            },
        ),
    )
    monkeypatch.setenv("HERMES_XAI_BASE_URL", "https://api.x.ai/v1/")

    creds = auth.resolve_xai_oauth_runtime_credentials(refresh_if_expiring=False)

    assert creds == {
        "provider": "xai-oauth",
        "base_url": "https://api.x.ai/v1",
        "api_key": "xai-access",
        "source": "hermes-auth-store",
        "last_refresh": "2026-06-02T00:00:00Z",
        # Device-code is the only supported xAI OAuth flow; resolve_*
        # reports it unconditionally even when the auth store still
        # carries a legacy "oauth_pkce" label (display/telemetry only).
        "auth_mode": "oauth_device_code",
    }


def test_xai_oauth_runtime_credentials_missing_auth_file(hermes_home: Path):
    with pytest.raises(AuthError) as exc:
        auth.resolve_xai_oauth_runtime_credentials()

    assert exc.value.provider == "xai-oauth"
    assert exc.value.code == "xai_auth_missing"
    assert exc.value.relogin_required is True


def test_xai_oauth_runtime_credentials_malformed_auth_file(hermes_home: Path):
    (hermes_home / "auth.json").write_text("not-json{{", encoding="utf-8")

    with pytest.raises(AuthError) as exc:
        auth.resolve_xai_oauth_runtime_credentials()

    assert exc.value.provider == "xai-oauth"
    assert exc.value.code == "xai_auth_missing"
    assert exc.value.relogin_required is True
    assert (hermes_home / "auth.json.corrupt").exists()


def test_xai_expired_token_terminal_refresh_quarantines_and_signals_relogin_required(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    expired = _jwt_with_exp(int(time.time()) - 30)
    _write_auth_store(
        hermes_home,
        _provider_store(
            "xai-oauth",
            {
                "tokens": {"access_token": expired, "refresh_token": "refresh"},
                "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"},
                "redirect_uri": "http://127.0.0.1:56121/callback",
            },
        ),
    )

    def _terminal_refresh(*_args, **_kwargs) -> dict:
        raise AuthError(
            "invalid grant",
            provider="xai-oauth",
            code="xai_refresh_invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "_refresh_xai_oauth_tokens", _terminal_refresh)
    monkeypatch.setattr(auth, "_is_terminal_xai_oauth_refresh_error", lambda exc: True)

    with pytest.raises(AuthError) as exc:
        auth.resolve_xai_oauth_runtime_credentials(refresh_if_expiring=True)

    assert exc.value.code == "xai_refresh_invalid_grant"
    assert exc.value.relogin_required is True
    state = auth.get_provider_auth_state("xai-oauth")
    assert state is not None
    assert state["tokens"] == {}
    assert state["last_auth_error"]["relogin_required"] is True


# ---------------------------------------------------------------------------
# Google Gemini OAuth provider getter
#
# test_gemini_oauth_runtime_credentials_happy_path_and_shape,
# test_gemini_oauth_runtime_credentials_missing_auth_file,
# test_gemini_oauth_runtime_credentials_malformed_auth_file, and
# test_mark_google_gemini_cli_active_persists_minimal_provider_state removed
# in the v2026.7.1 merge — upstream deleted the Google-OAuth/Gemini-CloudCode
# lane (agent/google_oauth.py); fork runtime does not use it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Spotify provider getter
# ---------------------------------------------------------------------------


def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_spotify_runtime_credentials_happy_path_and_shape(hermes_home: Path):
    expires_at = _future_iso()
    _write_auth_store(
        hermes_home,
        _provider_store(
            "spotify",
            {
                "access_token": "spotify-access",
                "refresh_token": "spotify-refresh",
                "token_type": "Bearer",
                "api_base_url": "https://api.spotify.test/v1/",
                "scope": "user-read-playback-state",
                "granted_scope": "user-read-playback-state playlist-read-private",
                "client_id": "spotify-client",
                "redirect_uri": "http://127.0.0.1/callback",
                "expires_at": expires_at,
                "auth_type": "oauth_pkce",
            },
        ),
    )

    creds = auth.resolve_spotify_runtime_credentials(refresh_if_expiring=False)

    assert creds == {
        "provider": "spotify",
        "access_token": "spotify-access",
        "api_key": "spotify-access",
        "token_type": "Bearer",
        "base_url": "https://api.spotify.test/v1",
        "scope": "user-read-playback-state playlist-read-private",
        "client_id": "spotify-client",
        "redirect_uri": "http://127.0.0.1/callback",
        "expires_at": expires_at,
        "refresh_token": "spotify-refresh",
    }


def test_spotify_runtime_credentials_missing_auth_file(hermes_home: Path):
    with pytest.raises(AuthError) as exc:
        auth.resolve_spotify_runtime_credentials()

    assert exc.value.provider == "spotify"
    assert exc.value.code == "spotify_auth_missing"
    assert exc.value.relogin_required is True


def test_spotify_runtime_credentials_malformed_auth_file(hermes_home: Path):
    (hermes_home / "auth.json").write_text("not-json{{", encoding="utf-8")

    with pytest.raises(AuthError) as exc:
        auth.resolve_spotify_runtime_credentials()

    assert exc.value.provider == "spotify"
    assert exc.value.code == "spotify_auth_missing"
    assert exc.value.relogin_required is True
    assert (hermes_home / "auth.json.corrupt").exists()


# ---------------------------------------------------------------------------
# Nous provider getter
# ---------------------------------------------------------------------------


def test_nous_runtime_credentials_happy_path_uses_invoke_jwt_shape(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    access_token = _jwt_with_exp(int(time.time()) + 3600)
    _write_auth_store(
        hermes_home,
        _provider_store(
            "nous",
            {
                "portal_base_url": "https://portal.example.test",
                # Must be on the Nous inference host allowlist (see
                # _ALLOWED_NOUS_INFERENCE_HOSTS) — persisted values are
                # re-validated against it on every load as defense-in-depth
                # against a poisoned auth store redirecting the bearer
                # token, so an off-allowlist test host would silently heal
                # to DEFAULT_NOUS_INFERENCE_URL instead of exercising the
                # plumbing this test means to cover.
                "inference_base_url": "https://inference-api.nousresearch.com/v2",
                "client_id": "hermes-cli",
                "scope": auth.NOUS_INFERENCE_INVOKE_SCOPE,
                "access_token": access_token,
                "refresh_token": "nous-refresh",
                "expires_at": _future_iso(),
            },
        ),
    )
    monkeypatch.setattr(auth, "_sync_nous_pool_from_auth_store", lambda: None)
    monkeypatch.setattr(auth, "_write_shared_nous_state", lambda _state: None)

    creds = auth.resolve_nous_runtime_credentials()

    assert creds["provider"] == "nous"
    assert creds["base_url"] == "https://inference-api.nousresearch.com/v2"
    assert creds["api_key"] == access_token
    assert creds["source"] == auth.NOUS_AUTH_PATH_INVOKE_JWT
    assert creds["auth_path"] == auth.NOUS_AUTH_PATH_INVOKE_JWT
    assert isinstance(creds["expires_in"], int)


def test_nous_runtime_credentials_missing_auth_file(hermes_home: Path):
    with pytest.raises(AuthError) as exc:
        auth.resolve_nous_runtime_credentials()

    assert exc.value.provider == "nous"
    assert exc.value.relogin_required is True


def test_nous_runtime_credentials_malformed_auth_file(hermes_home: Path):
    (hermes_home / "auth.json").write_text("not-json{{", encoding="utf-8")

    with pytest.raises(AuthError) as exc:
        auth.resolve_nous_runtime_credentials()

    assert exc.value.provider == "nous"
    assert exc.value.relogin_required is True
    assert (hermes_home / "auth.json.corrupt").exists()


# ---------------------------------------------------------------------------
# MiniMax OAuth provider getter
# ---------------------------------------------------------------------------


def test_minimax_oauth_runtime_credentials_happy_path_and_shape(hermes_home: Path):
    _write_auth_store(
        hermes_home,
        _provider_store(
            "minimax-oauth",
            {
                "access_token": "minimax-access",
                "refresh_token": "minimax-refresh",
                "portal_base_url": auth.MINIMAX_OAUTH_GLOBAL_BASE,
                "inference_base_url": "https://api.minimax.test/anthropic/",
                "client_id": auth.MINIMAX_OAUTH_CLIENT_ID,
                "expires_at": _future_iso(),
                "region": "global",
            },
        ),
    )

    creds = auth.resolve_minimax_oauth_runtime_credentials()

    assert creds == {
        "provider": "minimax-oauth",
        "api_key": "minimax-access",
        "base_url": "https://api.minimax.test/anthropic",
        "source": "oauth",
    }


def test_minimax_oauth_runtime_credentials_missing_auth_file(hermes_home: Path):
    with pytest.raises(AuthError) as exc:
        auth.resolve_minimax_oauth_runtime_credentials()

    assert exc.value.provider == "minimax-oauth"
    assert exc.value.code == "not_logged_in"
    assert exc.value.relogin_required is True


def test_minimax_oauth_runtime_credentials_malformed_auth_file(hermes_home: Path):
    (hermes_home / "auth.json").write_text("not-json{{", encoding="utf-8")

    with pytest.raises(AuthError) as exc:
        auth.resolve_minimax_oauth_runtime_credentials()

    assert exc.value.provider == "minimax-oauth"
    assert exc.value.code == "not_logged_in"
    assert exc.value.relogin_required is True
    assert (hermes_home / "auth.json.corrupt").exists()


def test_minimax_expired_token_terminal_refresh_quarantines_and_signals_relogin_required(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state = {
        "access_token": "expired-minimax-access",
        "refresh_token": "minimax-refresh",
        "portal_base_url": auth.MINIMAX_OAUTH_GLOBAL_BASE,
        "inference_base_url": auth.MINIMAX_OAUTH_GLOBAL_INFERENCE,
        "client_id": auth.MINIMAX_OAUTH_CLIENT_ID,
        "expires_at": _future_iso(-60),
    }
    _write_auth_store(hermes_home, _provider_store("minimax-oauth", state))

    def _terminal_refresh(_state: dict) -> dict:
        raise AuthError(
            "invalid refresh token",
            provider="minimax-oauth",
            code="refresh_failed",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "_refresh_minimax_oauth_state", _terminal_refresh)

    with pytest.raises(AuthError) as exc:
        auth.resolve_minimax_oauth_runtime_credentials()

    assert exc.value.code == "refresh_failed"
    assert exc.value.relogin_required is True
    quarantined = auth.get_provider_auth_state("minimax-oauth")
    assert quarantined is not None
    assert "access_token" not in quarantined
    assert "refresh_token" not in quarantined
    assert quarantined["last_auth_error"]["relogin_required"] is True


# ---------------------------------------------------------------------------
# API-key and external-process provider getters
# ---------------------------------------------------------------------------


def test_api_key_provider_credentials_happy_path_and_shape(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "_resolve_api_key_provider_secret", lambda _pid, _cfg: ("glm-key", "GLM_API_KEY"))
    monkeypatch.setattr(auth, "_resolve_zai_base_url", lambda _key, default, env: env or default)
    monkeypatch.setenv("GLM_BASE_URL", "https://zai.example/v4/")

    creds = auth.resolve_api_key_provider_credentials("zai")

    assert creds == {
        "provider": "zai",
        "api_key": "glm-key",
        "base_url": "https://zai.example/v4",
        "source": "GLM_API_KEY",
    }


def test_api_key_provider_credentials_missing_secret_returns_default_shape(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "_resolve_api_key_provider_secret", lambda _pid, _cfg: ("", ""))
    monkeypatch.setattr(auth, "_resolve_zai_base_url", lambda _key, default, env: env or default)

    creds = auth.resolve_api_key_provider_credentials("zai")

    assert creds["provider"] == "zai"
    assert creds["api_key"] == ""
    assert creds["base_url"] == auth.PROVIDER_REGISTRY["zai"].inference_base_url
    assert creds["source"] == "default"


def test_api_key_provider_credentials_invalid_provider():
    with pytest.raises(AuthError) as exc:
        auth.resolve_api_key_provider_credentials("openai-codex")

    assert exc.value.code == "invalid_provider"


def test_external_process_provider_credentials_happy_path_and_shape(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth.shutil, "which", lambda cmd: f"/tmp/bin/{cmd}")
    monkeypatch.setenv("HERMES_COPILOT_ACP_COMMAND", "copilot-test")
    monkeypatch.setenv("HERMES_COPILOT_ACP_ARGS", "--acp --stdio --debug")
    monkeypatch.setenv("COPILOT_ACP_BASE_URL", "acp+tcp://127.0.0.1:4321/")

    creds = auth.resolve_external_process_provider_credentials("copilot-acp")

    assert creds == {
        "provider": "copilot-acp",
        "api_key": "copilot-acp",
        "base_url": "acp+tcp://127.0.0.1:4321",
        "command": "/tmp/bin/copilot-test",
        "args": ["--acp", "--stdio", "--debug"],
        "source": "process",
    }


def test_external_process_provider_credentials_missing_command(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth.shutil, "which", lambda _cmd: None)
    monkeypatch.delenv("COPILOT_ACP_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_COPILOT_ACP_COMMAND", raising=False)

    with pytest.raises(AuthError) as exc:
        auth.resolve_external_process_provider_credentials("copilot-acp")

    assert exc.value.provider == "copilot-acp"
    assert exc.value.code == "missing_copilot_cli"


def test_external_process_provider_credentials_invalid_provider():
    with pytest.raises(AuthError) as exc:
        auth.resolve_external_process_provider_credentials("openai-codex")

    assert exc.value.code == "invalid_provider"


# ---------------------------------------------------------------------------
# Google credential read/write helper (provider sibling auth-file case)
#
# test_google_oauth_credential_file_roundtrip_and_malformed_json removed in
# the v2026.7.1 merge — upstream deleted the Google-OAuth/Gemini-CloudCode
# lane (agent/google_oauth.py); fork runtime does not use it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Generic status dispatcher
# ---------------------------------------------------------------------------


def test_get_auth_status_dispatches_known_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "get_spotify_auth_status", lambda: {"logged_in": True, "provider": "spotify"})

    assert auth.get_auth_status("spotify") == {"logged_in": True, "provider": "spotify"}


def test_get_auth_status_without_active_provider_returns_false(hermes_home: Path):
    assert auth.get_auth_status() == {"logged_in": False}
