"""Codex tool-schema sanitizer — flatten MCP/rich JSON-Schema to the subset the
ChatGPT backend-api/codex endpoint accepts.

Regression for the 2026-06-23 ``HTTP 400 {'detail':'Unsupported content type'}``
that killed gpt-5.5 loki lanes: MCP tool schemas carry ``$ref``/``$defs``/
``oneOf``/``anyOf``/``format``/``const`` which Codex rejects, taking down the
whole request (non-retryable, stuck in history).
"""
import json

from agent.codex_responses_adapter import _sanitize_tool_schema_for_codex as san

_FORBIDDEN = ("$ref", "$defs", "definitions", "oneOf", "anyOf", "allOf", "const")


def _has_keyword(blob: str, kw: str) -> bool:
    return f'"{kw}"' in blob


def test_inlines_ref_and_defs():
    schema = {
        "type": "object",
        "$defs": {"Color": {"type": "string", "enum": ["r", "g", "b"]}},
        "properties": {"c": {"$ref": "#/$defs/Color"}},
        "required": ["c"],
    }
    out = san(schema)
    blob = json.dumps(out)
    assert not _has_keyword(blob, "$ref") and not _has_keyword(blob, "$defs")
    assert out["properties"]["c"]["type"] == "string"
    assert out["properties"]["c"]["enum"] == ["r", "g", "b"]
    assert out["required"] == ["c"]  # required preserved


def test_collapses_oneof_anyof_picking_non_null():
    schema = {
        "type": "object",
        "properties": {
            "x": {"anyOf": [{"type": "null"}, {"type": "integer", "description": "count"}]},
            "y": {"oneOf": [{"type": "string"}, {"type": "number"}]},
        },
    }
    out = san(schema)
    blob = json.dumps(out)
    assert not _has_keyword(blob, "oneOf") and not _has_keyword(blob, "anyOf")
    assert out["properties"]["x"]["type"] == "integer"
    assert out["properties"]["y"]["type"] in ("string", "number")


def test_const_to_enum_and_drops_format_pattern():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"const": "fast"},
            "email": {"type": "string", "format": "email", "pattern": "^.+@.+$"},
        },
    }
    out = san(schema)
    blob = json.dumps(out)
    assert not _has_keyword(blob, "const")
    assert out["properties"]["mode"]["enum"] == ["fast"]
    # advisory keyword 'format' dropped; property name survives
    assert "format" not in out["properties"]["email"]
    assert "pattern" not in out["properties"]["email"]
    assert out["properties"]["email"]["type"] == "string"


def test_normalizes_list_type():
    out = san({"type": ["string", "null"], "description": "d"})
    assert out["type"] == "string"
    assert out["description"] == "d"


def test_property_named_pattern_is_preserved():
    # a tool arg literally named 'pattern' (grep/search) must survive
    schema = {"type": "object",
              "properties": {"pattern": {"type": "string", "description": "Regex pattern"}},
              "required": ["pattern"]}
    out = san(schema)
    assert "pattern" in out["properties"]
    assert out["required"] == ["pattern"]


def test_nested_recursion_and_arrays():
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"oneOf": [{"$ref": "#/$defs/I"}, {"type": "null"}]}},
        },
        "$defs": {"I": {"type": "object", "properties": {"k": {"const": 1}}}},
    }
    out = san(schema)
    blob = json.dumps(out)
    for kw in _FORBIDDEN:
        assert not _has_keyword(blob, kw), f"{kw} leaked"


def test_non_dict_passthrough():
    assert san("x") == "x"
    assert san(5) == 5
    assert san(None) is None
