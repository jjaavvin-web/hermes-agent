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
        ("loopback_pkce", "xai-oauth", False),
        ("hermes_pkce", "nous", True),
        ("unknown-source", "anthropic", True),
        ("loopback_pkce", "XAI-OAUTH", False),
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
