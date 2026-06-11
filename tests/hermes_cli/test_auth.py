import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.auth as auth


@pytest.fixture
def isolated_auth_paths(tmp_path, monkeypatch):
    """Keep auth.py away from the real ~/.hermes and ~/.qwen stores."""
    hermes_home = tmp_path / "hermes_home"
    qwen_file = tmp_path / "qwen" / "oauth_creds.json"
    hermes_home.mkdir()
    qwen_file.parent.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(auth, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(auth, "_qwen_cli_auth_path", lambda: qwen_file)
    return SimpleNamespace(hermes_home=hermes_home, qwen_file=qwen_file)


def _qwen_tokens(*, access_token="qwen-access", refresh_token="qwen-refresh", expires_in_seconds=3600):
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "resource_url": "portal.qwen.ai",
        "expiry_date": int((time.time() + expires_in_seconds) * 1000),
    }


def _write_qwen_tokens(path: Path, **overrides):
    tokens = _qwen_tokens(**overrides)
    path.write_text(json.dumps(tokens), encoding="utf-8")
    return tokens


def _jwt_with_exp(expires_in_seconds: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time() + expires_in_seconds)}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _iso_in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class _FakeHTTPResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


def test_auth_error_exposes_structured_fields():
    err = auth.AuthError(
        "reauth needed",
        provider="qwen-oauth",
        code="qwen_refresh_failed",
        relogin_required=True,
    )

    assert str(err) == "reauth needed"
    assert err.provider == "qwen-oauth"
    assert err.code == "qwen_refresh_failed"
    assert err.relogin_required is True


def test_qwen_runtime_credentials_skip_refresh_when_token_is_not_expiring(isolated_auth_paths, monkeypatch):
    tokens = _write_qwen_tokens(isolated_auth_paths.qwen_file, expires_in_seconds=600)
    def fail_refresh(tokens):
        pytest.fail("unexpected Qwen refresh for non-expiring token")

    monkeypatch.setattr(auth, "_refresh_qwen_cli_tokens", fail_refresh)

    creds = auth.resolve_qwen_runtime_credentials(refresh_skew_seconds=120)

    assert creds["provider"] == "qwen-oauth"
    assert creds["api_key"] == tokens["access_token"]
    assert creds["expires_at_ms"] == tokens["expiry_date"]
    assert creds["auth_file"] == str(isolated_auth_paths.qwen_file)


def test_qwen_runtime_credentials_refresh_within_skew_window(isolated_auth_paths, monkeypatch):
    _write_qwen_tokens(isolated_auth_paths.qwen_file, access_token="old", expires_in_seconds=30)
    refreshed = _qwen_tokens(access_token="new", refresh_token="rotated", expires_in_seconds=900)
    calls = []

    def fake_refresh(tokens):
        calls.append(tokens)
        return refreshed

    monkeypatch.setattr(auth, "_refresh_qwen_cli_tokens", fake_refresh)

    creds = auth.resolve_qwen_runtime_credentials(refresh_skew_seconds=120)

    assert len(calls) == 1
    assert calls[0]["access_token"] == "old"
    assert creds["api_key"] == "new"
    assert creds["expires_at_ms"] == refreshed["expiry_date"]


def test_qwen_runtime_credentials_force_refresh_always_refreshes(isolated_auth_paths, monkeypatch):
    _write_qwen_tokens(isolated_auth_paths.qwen_file, access_token="fresh", expires_in_seconds=3600)
    refreshed = _qwen_tokens(access_token="forced", expires_in_seconds=3600)
    calls = []

    def fake_refresh(tokens):
        calls.append(tokens)
        return refreshed

    monkeypatch.setattr(auth, "_refresh_qwen_cli_tokens", fake_refresh)

    creds = auth.resolve_qwen_runtime_credentials(force_refresh=True, refresh_skew_seconds=0)

    assert len(calls) == 1
    assert calls[0]["access_token"] == "fresh"
    assert creds["api_key"] == "forced"


def test_qwen_runtime_credentials_can_disable_refresh_if_expiring(isolated_auth_paths, monkeypatch):
    tokens = _write_qwen_tokens(isolated_auth_paths.qwen_file, access_token="expired", expires_in_seconds=-60)
    monkeypatch.setattr(auth, "_refresh_qwen_cli_tokens", pytest.fail)

    creds = auth.resolve_qwen_runtime_credentials(refresh_if_expiring=False)

    assert creds["api_key"] == "expired"
    assert creds["expires_at_ms"] == tokens["expiry_date"]


def test_qwen_runtime_credentials_expired_refresh_failure_keeps_documented_error_shape(
    isolated_auth_paths, monkeypatch
):
    _write_qwen_tokens(isolated_auth_paths.qwen_file, expires_in_seconds=-10)

    def fail_refresh(tokens):
        raise auth.AuthError(
            "Qwen OAuth refresh failed: boom",
            provider="qwen-oauth",
            code="qwen_refresh_failed",
        )

    monkeypatch.setattr(auth, "_refresh_qwen_cli_tokens", fail_refresh)

    with pytest.raises(auth.AuthError) as exc_info:
        auth.resolve_qwen_runtime_credentials(refresh_skew_seconds=0)

    err = exc_info.value
    assert str(err) == "Qwen OAuth refresh failed: boom"
    assert err.provider == "qwen-oauth"
    assert err.code == "qwen_refresh_failed"
    assert err.relogin_required is False


def test_qwen_refresh_posts_to_token_endpoint_and_persists_new_tokens(isolated_auth_paths, monkeypatch):
    tokens = _write_qwen_tokens(isolated_auth_paths.qwen_file, access_token="old", expires_in_seconds=-1)
    post_calls = []

    def fake_post(url, headers, data, timeout):
        post_calls.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        return _FakeHTTPResponse(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "resource_url": "portal.qwen.ai",
                "expires_in": 600,
            }
        )

    monkeypatch.setattr(auth.httpx, "post", fake_post)

    refreshed = auth._refresh_qwen_cli_tokens(tokens, timeout_seconds=3.5)

    assert len(post_calls) == 1
    assert post_calls[0]["url"] == auth.QWEN_OAUTH_TOKEN_URL
    assert post_calls[0]["data"]["grant_type"] == "refresh_token"
    assert post_calls[0]["data"]["refresh_token"] == "qwen-refresh"
    assert post_calls[0]["timeout"] == 3.5
    assert refreshed["access_token"] == "new-access"
    assert refreshed["refresh_token"] == "new-refresh"
    assert json.loads(isolated_auth_paths.qwen_file.read_text())["access_token"] == "new-access"


def test_qwen_auth_status_logged_out_includes_error_and_auth_file(isolated_auth_paths):
    status = auth.get_qwen_auth_status()

    assert status == {
        "logged_in": False,
        "auth_file": str(isolated_auth_paths.qwen_file),
        "error": "Qwen CLI credentials not found. Run 'qwen auth qwen-oauth' first.",
    }


def test_qwen_auth_status_logged_in_fresh_does_not_refresh(isolated_auth_paths, monkeypatch):
    tokens = _write_qwen_tokens(isolated_auth_paths.qwen_file, access_token="fresh", expires_in_seconds=600)
    monkeypatch.setattr(auth, "_refresh_qwen_cli_tokens", pytest.fail)

    status = auth.get_qwen_auth_status()

    assert status["logged_in"] is True
    assert status["auth_file"] == str(isolated_auth_paths.qwen_file)
    assert status["source"] == "qwen-cli"
    assert status["api_key"] == "fresh"
    assert status["expires_at_ms"] == tokens["expiry_date"]


def test_qwen_auth_status_logged_in_expiring_refreshes(isolated_auth_paths, monkeypatch):
    _write_qwen_tokens(isolated_auth_paths.qwen_file, access_token="old", expires_in_seconds=30)
    refreshed = _qwen_tokens(access_token="new", expires_in_seconds=600)
    monkeypatch.setattr(auth, "_refresh_qwen_cli_tokens", lambda tokens: refreshed)

    status = auth.get_qwen_auth_status()

    assert status["logged_in"] is True
    assert status["api_key"] == "new"
    assert status["expires_at_ms"] == refreshed["expiry_date"]


def test_qwen_auth_status_corrupt_auth_file_reports_logged_out(isolated_auth_paths):
    isolated_auth_paths.qwen_file.write_text("{not-json", encoding="utf-8")

    status = auth.get_qwen_auth_status()

    assert status["logged_in"] is False
    assert status["auth_file"] == str(isolated_auth_paths.qwen_file)
    assert "Failed to read Qwen CLI credentials" in status["error"]
    assert str(isolated_auth_paths.qwen_file) in status["error"]


def test_spotify_runtime_credentials_refresh_semantics(isolated_auth_paths, monkeypatch):
    auth_store = {
        "version": auth.AUTH_STORE_VERSION,
        "providers": {
            "spotify": {
                "access_token": "old",
                "refresh_token": "refresh",
                "expires_at": _iso_in(30),
                "client_id": "client",
                "redirect_uri": "http://127.0.0.1:53682/callback",
                "api_base_url": "https://api.spotify.test/v1",
            }
        },
    }
    (isolated_auth_paths.hermes_home / "auth.json").write_text(json.dumps(auth_store), encoding="utf-8")
    calls = []

    def fake_refresh(state):
        calls.append(state)
        updated = dict(state)
        updated["access_token"] = "new"
        updated["expires_at"] = _iso_in(600)
        return updated

    monkeypatch.setattr(auth, "_refresh_spotify_oauth_state", fake_refresh)

    refreshed = auth.resolve_spotify_runtime_credentials(refresh_skew_seconds=120)
    assert len(calls) == 1
    assert refreshed["api_key"] == "new"

    calls.clear()
    no_refresh = auth.resolve_spotify_runtime_credentials(refresh_if_expiring=False)
    assert calls == []
    assert no_refresh["api_key"] == "new"

    forced = auth.resolve_spotify_runtime_credentials(force_refresh=True, refresh_skew_seconds=0)
    assert len(calls) == 1
    assert forced["api_key"] == "new"


def test_spotify_status_surfaces_logged_out_and_logged_in(isolated_auth_paths):
    assert auth.get_spotify_auth_status() == {"logged_in": False}

    auth_store = {
        "version": auth.AUTH_STORE_VERSION,
        "providers": {
            "spotify": {
                "auth_type": "oauth_pkce",
                "client_id": "client",
                "redirect_uri": "http://127.0.0.1:53682/callback",
                "scope": "user-read-email",
                "granted_scope": "user-read-email playlist-read-private",
                "expires_at": _iso_in(600),
                "api_base_url": "https://api.spotify.test/v1",
                "refresh_token": "refresh",
            }
        },
    }
    (isolated_auth_paths.hermes_home / "auth.json").write_text(json.dumps(auth_store), encoding="utf-8")

    status = auth.get_spotify_auth_status()

    assert status == {
        "logged_in": True,
        "auth_type": "oauth_pkce",
        "client_id": "client",
        "redirect_uri": "http://127.0.0.1:53682/callback",
        "scope": "user-read-email playlist-read-private",
        "expires_at": auth_store["providers"]["spotify"]["expires_at"],
        "api_base_url": "https://api.spotify.test/v1",
        "has_refresh_token": True,
    }


def test_codex_runtime_credentials_refresh_semantics(isolated_auth_paths, monkeypatch):
    token = _jwt_with_exp(30)
    refreshed_token = _jwt_with_exp(600)
    auth_store = {
        "version": auth.AUTH_STORE_VERSION,
        "providers": {
            "openai-codex": {
                "tokens": {"access_token": token, "refresh_token": "refresh"},
                "last_refresh": "old",
            }
        },
    }
    (isolated_auth_paths.hermes_home / "auth.json").write_text(json.dumps(auth_store), encoding="utf-8")
    calls = []

    def fake_refresh(tokens, timeout_seconds):
        calls.append((tokens, timeout_seconds))
        updated = dict(tokens)
        updated["access_token"] = refreshed_token
        auth._save_codex_tokens(updated)
        return updated

    monkeypatch.setattr(auth, "_refresh_codex_auth_tokens", fake_refresh)

    creds = auth.resolve_codex_runtime_credentials(refresh_skew_seconds=120)
    assert len(calls) == 1
    assert creds["api_key"] == refreshed_token

    calls.clear()
    creds = auth.resolve_codex_runtime_credentials(refresh_if_expiring=False)
    assert calls == []
    assert creds["api_key"] == refreshed_token

    creds = auth.resolve_codex_runtime_credentials(force_refresh=True, refresh_skew_seconds=0)
    assert len(calls) == 1
    assert creds["api_key"] == refreshed_token


def test_codex_auth_status_surfaces_store_states(isolated_auth_paths, monkeypatch):
    monkeypatch.setattr(auth, "_pool_codex_access_token", lambda: "")

    status = auth.get_codex_auth_status()

    assert status["logged_in"] is False
    assert status["auth_store"] == str(isolated_auth_paths.hermes_home / "auth.json")
    assert status["error"] == "No Codex credentials stored. Run `hermes auth` to authenticate."

    auth_store = {
        "version": auth.AUTH_STORE_VERSION,
        "providers": {
            "openai-codex": {
                "tokens": {"access_token": _jwt_with_exp(600), "refresh_token": "refresh"},
                "last_refresh": "now",
                "auth_mode": "chatgpt",
            }
        },
    }
    (isolated_auth_paths.hermes_home / "auth.json").write_text(json.dumps(auth_store), encoding="utf-8")

    status = auth.get_codex_auth_status()

    assert status["logged_in"] is True
    assert status["auth_store"] == str(isolated_auth_paths.hermes_home / "auth.json")
    assert status["last_refresh"] == "now"
    assert status["auth_mode"] == "chatgpt"


def test_xai_oauth_runtime_credentials_refresh_semantics(isolated_auth_paths, monkeypatch):
    token = _jwt_with_exp(30)
    refreshed_token = _jwt_with_exp(600)
    auth_store = {
        "version": auth.AUTH_STORE_VERSION,
        "providers": {
            "xai-oauth": {
                "tokens": {"access_token": token, "refresh_token": "refresh"},
                "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"},
                "redirect_uri": "http://127.0.0.1:56223/callback",
                "last_refresh": "old",
            }
        },
    }
    (isolated_auth_paths.hermes_home / "auth.json").write_text(json.dumps(auth_store), encoding="utf-8")
    calls = []

    def fake_refresh(tokens, *, token_endpoint, redirect_uri, timeout_seconds):
        calls.append((tokens, token_endpoint, redirect_uri, timeout_seconds))
        updated = dict(tokens)
        updated["access_token"] = refreshed_token
        auth._save_xai_oauth_tokens(
            updated,
            discovery={"token_endpoint": token_endpoint},
            redirect_uri=redirect_uri,
        )
        return updated

    monkeypatch.setattr(auth, "_refresh_xai_oauth_tokens", fake_refresh)

    creds = auth.resolve_xai_oauth_runtime_credentials(refresh_skew_seconds=120)
    assert len(calls) == 1
    assert creds["api_key"] == refreshed_token

    calls.clear()
    creds = auth.resolve_xai_oauth_runtime_credentials(refresh_if_expiring=False)
    assert calls == []
    assert creds["api_key"] == refreshed_token

    creds = auth.resolve_xai_oauth_runtime_credentials(force_refresh=True, refresh_skew_seconds=0)
    assert len(calls) == 1
    assert creds["api_key"] == refreshed_token


def test_generic_status_dispatch_for_oauth_status_surfaces(isolated_auth_paths, monkeypatch):
    qwen_status = {"logged_in": True, "provider": "qwen-oauth"}
    gemini_status = {"logged_in": False, "provider": "google-gemini-cli"}
    spotify_status = {"logged_in": True, "provider": "spotify"}

    monkeypatch.setattr(auth, "get_qwen_auth_status", lambda: qwen_status)
    monkeypatch.setattr(auth, "get_gemini_oauth_auth_status", lambda: gemini_status)
    monkeypatch.setattr(auth, "get_spotify_auth_status", lambda: spotify_status)

    assert auth.get_auth_status("qwen-oauth") is qwen_status
    assert auth.get_auth_status("google-gemini-cli") is gemini_status
    assert auth.get_auth_status("spotify") is spotify_status
    assert auth.get_auth_status("unknown-provider") == {"logged_in": False}


def test_corrupt_hermes_auth_store_is_ignored_and_preserved(isolated_auth_paths):
    auth_file = isolated_auth_paths.hermes_home / "auth.json"
    auth_file.write_text("{not-json", encoding="utf-8")

    status = auth.get_spotify_auth_status()

    assert status == {"logged_in": False}
    assert auth_file.with_suffix(".json.corrupt").read_text(encoding="utf-8") == "{not-json"
