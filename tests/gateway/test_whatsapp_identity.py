"""Tests for WhatsApp identity normalization, alias expansion, and canonicalization."""

import json

import pytest

from gateway.whatsapp_identity import (
    canonical_whatsapp_identifier,
    expand_whatsapp_aliases,
    normalize_whatsapp_identifier,
)


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path / "whatsapp" / "session"


def _write_mapping(session_dir, ident, target_jid, reverse=False):
    session_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_reverse" if reverse else ""
    path = session_dir / f"lid-mapping-{ident}{suffix}.json"
    path.write_text(json.dumps(target_jid), encoding="utf-8")
    return path


def test_normalize_whatsapp_identifier_strips_known_jid_shapes():
    assert normalize_whatsapp_identifier("60123456789@s.whatsapp.net") == "60123456789"
    assert normalize_whatsapp_identifier("60123456789:47@s.whatsapp.net") == "60123456789"
    assert normalize_whatsapp_identifier("999@lid") == "999"
    assert normalize_whatsapp_identifier("+15551234567") == "15551234567"
    assert normalize_whatsapp_identifier("") == ""
    assert normalize_whatsapp_identifier(None) == ""  # type: ignore[arg-type]


def test_expand_whatsapp_aliases_without_mapping_returns_normalized_input(session_dir):
    assert not session_dir.exists()

    assert expand_whatsapp_aliases("+15551234567@s.whatsapp.net") == {"15551234567"}


def test_expand_whatsapp_aliases_empty_or_whitespace_returns_empty_set(session_dir):
    assert expand_whatsapp_aliases("") == set()
    assert expand_whatsapp_aliases("   ") == set()


def test_expand_whatsapp_aliases_includes_forward_mapping_target(session_dir):
    _write_mapping(session_dir, "60123456789", "999@lid")

    assert expand_whatsapp_aliases("60123456789@s.whatsapp.net") == {"60123456789", "999"}


def test_expand_whatsapp_aliases_walks_transitive_cycle_safely(session_dir):
    _write_mapping(session_dir, "11111111111", "222@lid")
    _write_mapping(session_dir, "222", "11111111111@s.whatsapp.net")

    assert expand_whatsapp_aliases("11111111111") == {"11111111111", "222"}


def test_expand_whatsapp_aliases_rejects_path_traversal_identifiers(session_dir):
    _write_mapping(session_dir, "a", "safe@lid")

    aliases = expand_whatsapp_aliases("a/b")

    assert aliases == set()
    assert "a/b" not in aliases


def test_canonical_whatsapp_identifier_empty_and_no_mapping_cases(session_dir):
    assert canonical_whatsapp_identifier("") == ""
    assert canonical_whatsapp_identifier("   ") == ""
    assert canonical_whatsapp_identifier("+15551234567@s.whatsapp.net") == "15551234567"


def test_canonical_whatsapp_identifier_chooses_shortest_alias(session_dir):
    _write_mapping(session_dir, "60123456789", "999@lid")

    assert canonical_whatsapp_identifier("60123456789@s.whatsapp.net") == "999"
