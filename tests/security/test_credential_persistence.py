"""Credential persistence sanitization invariants.

Pins the fail-closed disk boundary that keeps borrowed runtime secrets out of
``auth.json`` while preserving owned credentials and safe status metadata.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from agent.credential_persistence import (
    is_borrowed_credential_source,
    sanitize_borrowed_credential_payload,
)


@pytest.mark.parametrize(
    ("source", "provider_id", "expected"),
    (
        (None, None, False),
        ("", None, False),
        ("   ", "anthropic", False),
        ("manual", None, False),
        ("manual:foo", "unknown", False),
        ("hermes_pkce", "anthropic", False),
        ("oauth", "minimax-oauth", False),
        ("device_code", "nous", False),
        ("device_code", "openai-codex", False),
        # xAI OAuth is device-code-only since 5ef0b8acb0 ("make xAI Grok
        # OAuth device-code-only, drop loopback login"): the persistable
        # pair is (xai-oauth, device_code); retired loopback_pkce state is
        # borrowed/reference-only and must fail closed at the disk boundary.
        ("device_code", "xai-oauth", False),
        ("device_code", "XAI-OAUTH", False),
        ("loopback_pkce", "xai-oauth", True),
        ("loopback_pkce", "XAI-OAUTH", True),
        ("loopback_pkce", None, True),
        ("oauth", "xai-oauth", True),
        ("hermes_pkce", "nous", True),
        ("device_code", "anthropic", True),
        ("unknown-source", "anthropic", True),
    ),
)
def test_is_borrowed_credential_source_truth_table(
    source: Any,
    provider_id: Any,
    expected: bool,
) -> None:
    assert is_borrowed_credential_source(source, provider_id) is expected


def test_sanitize_borrowed_credential_payload_keeps_owned_manual_secret() -> None:
    payload = {"source": "manual", "access_token": "raw-abc"}

    result = sanitize_borrowed_credential_payload(payload)

    assert result["access_token"] == "raw-abc"
    assert result == payload


def test_sanitize_strips_retired_xai_loopback_pkce_tokens() -> None:
    """Retired (xai-oauth, loopback_pkce) state may not persist raw secrets."""
    payload = {
        "source": "loopback_pkce",
        "access_token": "raw-loopback-token",
        "refresh_token": "raw-loopback-refresh",
        "token_type": "Bearer",
    }

    result = sanitize_borrowed_credential_payload(payload, provider_id="xai-oauth")

    assert "access_token" not in result
    assert "refresh_token" not in result
    assert result["token_type"] == "Bearer"
    assert re.fullmatch(r"sha256:[0-9a-f]{16}", result["secret_fingerprint"])
    assert "raw-loopback-token" not in str(result)


def test_sanitize_keeps_owned_xai_device_code_tokens_on_disk() -> None:
    """(xai-oauth, device_code) is the owned pair and passes through intact."""
    payload = {
        "source": "device_code",
        "access_token": "raw-device-token",
        "refresh_token": "raw-device-refresh",
        "token_type": "Bearer",
    }

    result = sanitize_borrowed_credential_payload(payload, provider_id="xai-oauth")

    assert result == payload


def test_sanitize_borrowed_credential_payload_strips_borrowed_secrets_and_keeps_metadata() -> None:
    payload = {
        "source": "borrowed-x",
        "provider": "p",
        "access_token": "raw-abc",
        "refresh_token": "r",
        "token_type": "Bearer",
        "scope": "all",
    }

    result = sanitize_borrowed_credential_payload(payload, provider_id="p")

    assert "access_token" not in result
    assert "refresh_token" not in result
    assert result["token_type"] == "Bearer"
    assert result["scope"] == "all"
    assert re.fullmatch(r"sha256:[0-9a-f]{16}", result["secret_fingerprint"])
    assert "raw-abc" not in result["secret_fingerprint"]
