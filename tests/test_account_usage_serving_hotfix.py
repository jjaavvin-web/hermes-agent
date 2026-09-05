"""Offline regression contract for the exact serving Codex usage-card hotfix.

Only synthetic JWT claims and mock HTTP responses are used. Every test, including
negative paths, replaces singleton reads and pool reads before requesting usage.
"""
import base64
import json
import logging
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest

from agent import account_usage as usage
from hermes_cli import auth

BASE_URL = "https://usage.invalid"
USAGE_URL = BASE_URL + "/api/codex/usage"
PAYLOAD = {
    "plan_type": "pro",
    "rate_limit": {
        "primary_window": {"used_percent": 77, "reset_at": 1_900_000_000},
        "secondary_window": {"used_percent": 40},
    },
    "rate_limit_reset_credits": {"available_count": 2},
    "credits": {"has_credits": True, "balance": 12.5},
}


def _token(account_id):
    # Unsigned, synthetic fixtures; never credentials accepted by a provider.
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    return encode({"alg": "none"}) + "." + encode(claims) + ".test-signature"


PRIMARY = _token("primary-account")


def _entry(token, refresh="2026-09-01T00:00:00Z", **extra):
    return {"id": "synthetic-entry", "access_token": token, "last_refresh": refresh, **extra}


def _error(status):
    response = httpx.Response(status, request=httpx.Request("GET", USAGE_URL))
    return httpx.HTTPStatusError("synthetic HTTP failure", request=response.request, response=response)


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch):
    reader = Mock(return_value={"tokens": {"account_id": "stale-singleton-account"}})
    resolver = Mock(return_value={"api_key": PRIMARY, "base_url": BASE_URL})
    pool = Mock(return_value=[])
    monkeypatch.setattr(usage, "_read_codex_tokens", reader)
    monkeypatch.setattr(auth, "_read_codex_tokens", reader)
    monkeypatch.setattr(usage, "resolve_codex_runtime_credentials", resolver)
    monkeypatch.setattr(auth, "read_credential_pool", pool)
    monkeypatch.setattr(usage.httpx, "Client", Mock(side_effect=AssertionError("HTTP must be mocked")))
    return reader, resolver, pool


def _http(monkeypatch, *outcomes):
    pending = iter(outcomes)
    calls = []
    timeouts = []

    class Client:
        def __init__(self, *, timeout):
            timeouts.append(timeout)
            assert timeout == 15.0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            assert url == USAGE_URL
            calls.append(dict(headers))
            outcome = next(pending)  # Extra attempts fail instead of contacting a socket.
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(200, json=outcome, request=httpx.Request("GET", url))

    monkeypatch.setattr(usage.httpx, "Client", Client)
    return calls, timeouts


def _assert_snapshot(snapshot):
    assert snapshot.provider == "openai-codex"
    assert snapshot.source == "usage_api"
    assert snapshot.plan == "Pro"
    assert snapshot.windows[0].used_percent == 77.0
    assert snapshot.windows[0].reset_at == datetime.fromtimestamp(1_900_000_000, timezone.utc)
    assert snapshot.windows[1].used_percent == 40.0
    assert "You have 2 resets banked - use /usage reset to activate" in snapshot.details
    assert "Credits balance: $12.50" in snapshot.details
    assert "Session: 23% remaining (77% used)" in usage.render_account_usage_lines(snapshot)[2]


@pytest.mark.parametrize("status", [401, 403])
def test_retries_are_bounded_newest_first_and_preserve_original_error(monkeypatch, offline_credentials, status):
    original = _error(status)
    _, _, pool = offline_credentials
    newest, second, forbidden_third = _token("newest"), _token("second"), _token("third")
    pool.return_value = [
        _entry(forbidden_third, "2026-09-01T00:00:00Z"),
        _entry(second, "2026-09-02T00:00:00Z"),
        _entry(newest, "2026-09-03T00:00:00Z"),
    ]
    calls, timeouts = _http(monkeypatch, original, _error(403), _error(500), PAYLOAD)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        usage._fetch_codex_account_usage()
    assert caught.value is original
    assert usage._CODEX_USAGE_POOL_RETRY_LIMIT == 2
    assert [h["Authorization"] for h in calls] == [f"Bearer {t}" for t in (PRIMARY, newest, second)]
    assert timeouts == [15.0, 15.0, 15.0]
    pool.assert_called_once_with("openai-codex")


@pytest.mark.parametrize("status", [401, 403])
def test_explicit_key_never_falls_back_to_pool(monkeypatch, offline_credentials, status):
    reader, resolver, pool = offline_credentials
    original = _error(status)
    calls, _ = _http(monkeypatch, original)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        usage._fetch_codex_account_usage(base_url=BASE_URL, api_key="  explicit-test-key  ")
    assert caught.value is original
    assert calls[0]["Authorization"] == "Bearer explicit-test-key"
    assert "ChatGPT-Account-Id" not in calls[0]
    assert len(calls) == 1
    reader.assert_not_called()
    resolver.assert_not_called()
    pool.assert_not_called()


@pytest.mark.parametrize("status", [400, 429, 500])
def test_non_auth_status_never_enters_diagnostic_pool(monkeypatch, offline_credentials, status):
    original = _error(status)
    calls, _ = _http(monkeypatch, original)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        usage._fetch_codex_account_usage()
    assert caught.value is original
    assert len(calls) == 1
    offline_credentials[2].assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_retry_success_uses_each_tokens_own_account_and_preserves_snapshot(monkeypatch, offline_credentials, status):
    retry1, retry2 = _token("retry-one-account"), _token("retry-two-account")
    offline_credentials[2].return_value = [
        _entry(retry1, "2026-09-03T00:00:00Z"),
        _entry(retry2, "2026-09-02T00:00:00Z"),
        _entry(_token("unused-account")),
    ]
    calls, _ = _http(monkeypatch, _error(status), _error(401), PAYLOAD)
    snapshot = usage._fetch_codex_account_usage()
    _assert_snapshot(snapshot)
    assert [h["ChatGPT-Account-Id"] for h in calls] == [
        "primary-account", "retry-one-account", "retry-two-account",
    ]
    assert [h["Authorization"] for h in calls] == [f"Bearer {t}" for t in (PRIMARY, retry1, retry2)]
    assert all(h["Accept"] == "application/json" and h["User-Agent"] == "codex-cli" for h in calls)


def test_success_stops_after_first_usable_retry(monkeypatch, offline_credentials):
    offline_credentials[2].return_value = [_entry(_token("retry")), _entry(_token("unused"))]
    calls, _ = _http(monkeypatch, _error(401), PAYLOAD)
    _assert_snapshot(usage._fetch_codex_account_usage())
    assert len(calls) == 2


@pytest.mark.parametrize("retry", ["opaque-synthetic-token", _token(None)])
def test_retry_without_account_claim_never_borrows_singleton_id(monkeypatch, offline_credentials, retry):
    offline_credentials[2].return_value = [_entry(retry)]
    calls, _ = _http(monkeypatch, _error(401), PAYLOAD)
    _assert_snapshot(usage._fetch_codex_account_usage())
    assert calls[0]["ChatGPT-Account-Id"] == "primary-account"
    assert "ChatGPT-Account-Id" not in calls[1]


def test_primary_success_prefers_token_local_id_without_pool(monkeypatch, offline_credentials):
    calls, _ = _http(monkeypatch, PAYLOAD)
    _assert_snapshot(usage._fetch_codex_account_usage())
    assert calls[0]["ChatGPT-Account-Id"] == "primary-account"
    offline_credentials[1].assert_called_once_with(refresh_if_expiring=True)
    offline_credentials[0].assert_called_once_with()
    offline_credentials[2].assert_not_called()


def test_primary_opaque_token_retains_resolver_account_fallback(monkeypatch, offline_credentials):
    offline_credentials[1].return_value = {"api_key": "opaque-primary", "base_url": BASE_URL}
    calls, _ = _http(monkeypatch, PAYLOAD)
    _assert_snapshot(usage._fetch_codex_account_usage())
    assert calls[0]["ChatGPT-Account-Id"] == "stale-singleton-account"
    offline_credentials[2].assert_not_called()


def test_unusable_entries_do_not_consume_retry_slots(monkeypatch, offline_credentials):
    good = _token("usable-account")
    offline_credentials[2].return_value = [
        None, "not-an-entry", {}, {"access_token": None}, {"access_token": 42},
        {"access_token": ""}, {"access_token": "   "}, _entry(PRIMARY),
        _entry(_token("dead"), last_status="DeAd"),
        _entry(_token("exhausted"), last_status="EXHAUSTED"),
        _entry(good, refresh="not-a-date"),
    ]
    calls, _ = _http(monkeypatch, _error(403), PAYLOAD)
    _assert_snapshot(usage._fetch_codex_account_usage())
    assert [h["Authorization"] for h in calls] == [f"Bearer {PRIMARY}", f"Bearer {good}"]
    assert calls[1]["ChatGPT-Account-Id"] == "usable-account"


@pytest.mark.parametrize("pool_value", [[], None, [{"access_token": "", "last_status": "dead"}]])
def test_empty_or_unusable_pool_preserves_original_error(monkeypatch, offline_credentials, pool_value):
    offline_credentials[2].return_value = pool_value
    original = _error(401)
    calls, _ = _http(monkeypatch, original)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        usage._fetch_codex_account_usage()
    assert caught.value is original
    assert len(calls) == 1
    offline_credentials[2].assert_called_once_with("openai-codex")


def test_pool_read_failure_does_not_replace_original_auth_error(monkeypatch, offline_credentials):
    offline_credentials[2].side_effect = OSError("synthetic pool unavailable")
    original = _error(403)
    calls, _ = _http(monkeypatch, original)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        usage._fetch_codex_account_usage()
    assert caught.value is original
    assert len(calls) == 1
    offline_credentials[2].assert_called_once_with("openai-codex")


def test_public_fail_open_logs_original_error(monkeypatch, offline_credentials, caplog):
    original = _error(401)
    _http(monkeypatch, original)
    with caplog.at_level(logging.WARNING, logger="agent.account_usage"):
        assert usage.fetch_account_usage("openai-codex") is None
    failures = [r for r in caplog.records if "account usage fetch failed for openai-codex" in r.message]
    assert len(failures) == 1
    assert failures[0].exc_info[1] is original
    offline_credentials[2].assert_called_once_with("openai-codex")
