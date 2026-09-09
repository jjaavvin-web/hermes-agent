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


# Same synthetic account claim as PRIMARY, different login session. That is the
# real on-disk shape: every local Codex credential shares chatgpt_account_id and
# differs only in the session / jti claims - and each login session reports its
# OWN meter. Built off _token so the account claim is provably identical; still
# unsigned and synthetic.
OTHER_SESSION = _token("primary-account").rsplit(".", 1)[0] + ".other-session-signature"

# What the other login session was answering 200 with while the real account sat
# near a quarter used: a fully-spent meter that would render as "0% left".
OTHER_SESSION_PAYLOAD = {
    "plan_type": "pro",
    "rate_limit": {
        "primary_window": {"used_percent": 100.0, "reset_at": 1_789_225_391},
        "secondary_window": {"used_percent": 100.0},
    },
}


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_signin_never_serves_another_login_sessions_meter(
    monkeypatch, offline_credentials, status
):
    # The pool entry carries the SAME chatgpt_account_id as PRIMARY, because
    # that is what is actually on disk. An account-id comparison would let it
    # through and render its 100%-used meter as this account's. The only safe
    # rule is that no substitute credential is contacted at all.
    _, _, pool = offline_credentials
    pool.return_value = [_entry(OTHER_SESSION, "2026-09-09T00:00:00Z")]
    assert usage._codex_account_id_from_token(OTHER_SESSION) == usage._codex_account_id_from_token(PRIMARY)
    calls, _ = _http(monkeypatch, _error(status), OTHER_SESSION_PAYLOAD)

    snapshot = usage._fetch_codex_account_usage()

    # Exactly one outbound call: the pool entry is never contacted, so no quota
    # is burned on it and it cannot be marked exhausted by this read.
    assert len(calls) == 1
    assert calls[0]["Authorization"] == f"Bearer {PRIMARY}"
    assert snapshot.provider == "openai-codex"
    assert snapshot.source == "usage_api"
    assert snapshot.windows == ()
    assert snapshot.details == ()
    assert snapshot.plan is None
    assert snapshot.unavailable_reason is usage._CODEX_SIGNIN_REJECTED_REASON
    assert snapshot.available is False


def test_public_fetch_returns_unavailable_snapshot_instead_of_none(
    monkeypatch, offline_credentials, caplog
):
    # Our half of a handshake with an out-of-git consumer this repo's CI cannot
    # see: the usage-tracker plugin short-circuits on a non-None snapshot BEFORE
    # running its rollout-file fallback. Returning None here is the door into
    # that fallback, and the rollout records carry no account or session id at
    # all, so it cannot tell one login session's meter from another's.
    _http(monkeypatch, _error(401))
    with caplog.at_level(logging.WARNING, logger="agent.account_usage"):
        snapshot = usage.fetch_account_usage("openai-codex")
    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.unavailable_reason is usage._CODEX_SIGNIN_REJECTED_REASON
    rejections = [r for r in caplog.records if "sign-in rejected" in r.getMessage()]
    assert len(rejections) == 1
    assert "401" in rejections[0].getMessage()
    assert not [r for r in caplog.records if "account usage fetch failed" in r.getMessage()]


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_signin_never_reads_the_credential_pool(monkeypatch, offline_credentials, status):
    # Structural guarantee, independent of any identity rule: if anyone re-adds
    # a cross-credential retry loop, this fails whatever it guards on.
    _, _, pool = offline_credentials
    pool.side_effect = AssertionError("the credential pool must never be read on a rejected sign-in")
    calls, _ = _http(monkeypatch, _error(status))

    snapshot = usage._fetch_codex_account_usage()

    assert pool.call_count == 0
    assert len(calls) == 1
    assert snapshot.unavailable_reason is usage._CODEX_SIGNIN_REJECTED_REASON


def test_unavailable_reason_is_one_constant_both_renderers_can_show(monkeypatch, offline_credentials):
    _http(monkeypatch, _error(401))
    snapshot = usage._fetch_codex_account_usage()
    reason = usage._CODEX_SIGNIN_REJECTED_REASON
    assert f"Unavailable: {reason}" in usage.render_account_usage_lines(snapshot)
    assert "hermes auth" in reason
    for banned in ("401", "token", "oauth", "credential pool", "expired", "revoked"):
        assert banned not in reason.lower()


@pytest.mark.parametrize("pool_value", [[], None, [{"access_token": "", "last_status": "dead"}]])
def test_rejected_signin_fails_closed_whatever_the_pool_holds(
    monkeypatch, offline_credentials, pool_value
):
    # Kept parametrized on purpose: the outcome no longer branches on pool
    # contents at all, because the pool is never read. Returning the original
    # 401 to the caller is what used to hand control to the rollout fallback.
    offline_credentials[2].return_value = pool_value
    calls, _ = _http(monkeypatch, _error(401))

    snapshot = usage._fetch_codex_account_usage()

    assert len(calls) == 1
    assert snapshot.windows == ()
    assert snapshot.details == ()
    assert snapshot.unavailable_reason is usage._CODEX_SIGNIN_REJECTED_REASON
    offline_credentials[2].assert_not_called()
