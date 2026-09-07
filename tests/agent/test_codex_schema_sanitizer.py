"""Codex tool-schema sanitizer — flatten MCP/rich JSON-Schema to the subset the
ChatGPT backend-api/codex endpoint accepts.

Regression for the 2026-06-23 ``HTTP 400 {'detail':'Unsupported content type'}``
that killed gpt-5.5 loki lanes: MCP tool schemas carry ``$ref``/``$defs``/
``oneOf``/``anyOf``/``format``/``const`` which Codex rejects, taking down the
whole request (non-retryable, stuck in history).
"""
import copy
import json
import random
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from agent.codex_responses_adapter import _sanitize_tool_schema_for_codex
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


# ---------------------------------------------------------------------------
# Fixture corpus, invariants, regression request shape, and deterministic fuzz
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex_schemas"
FORBIDDEN_KEYS = {
    "$ref",
    "$defs",
    "definitions",
    "oneOf",
    "anyOf",
    "allOf",
    "const",
    "$schema",
    "$id",
    "$comment",
    "$anchor",
    "examples",
    "format",
    "pattern",
    "contentEncoding",
    "contentMediaType",
    "discriminator",
}


def _fixture_paths() -> list[Path]:
    return sorted(p for p in FIXTURE_DIR.glob("*.json") if p.name != "regression_69tool.json")


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _walk_keys(obj: Any, path: str = "$", *, in_properties: bool = False) -> Iterable[tuple[str, str]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            current = f"{path}.{key}"
            if not in_properties:
                yield key, current
            yield from _walk_keys(value, current, in_properties=key == "properties")
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            yield from _walk_keys(value, f"{path}[{idx}]", in_properties=False)


def _iter_type_values(obj: Any, path: str = "$") -> Iterable[tuple[Any, str]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            current = f"{path}.{key}"
            if key == "type":
                yield value, current
            yield from _iter_type_values(value, current)
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            yield from _iter_type_values(value, f"{path}[{idx}]")


def _assert_codex_clean(obj: Any) -> None:
    for key, path in _walk_keys(obj):
        assert key not in FORBIDDEN_KEYS, f"{key} leaked at {path}"
    for type_value, path in _iter_type_values(obj):
        assert not isinstance(type_value, list), f"list-form type survived at {path}: {type_value!r}"


def _sanitize_fixture(path: Path) -> Any:
    return _sanitize_tool_schema_for_codex(_load_json(path))


def _merge_required(existing: Iterable[str], new: Iterable[str]) -> list[str]:
    return sorted(set(existing) | set(new))


def _resolvable_schema(schema: Any, defs: dict[str, Any] | None = None, depth: int = 0) -> Any:
    """Small test-side resolver for semantic assertions, independent of sanitizer output."""
    if not isinstance(schema, dict) or depth > 16:
        return schema
    local_defs = dict(defs or {})
    for key in ("$defs", "definitions"):
        value = schema.get(key)
        if isinstance(value, dict):
            local_defs.update(value)

    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = local_defs.get(ref.split("/")[-1])
        return _resolvable_schema(target, local_defs, depth + 1) if isinstance(target, dict) else schema

    for union_key in ("oneOf", "anyOf"):
        variants = schema.get(union_key)
        if isinstance(variants, list) and variants:
            dict_variants = [variant for variant in variants if isinstance(variant, dict)]
            chosen = next(
                (variant for variant in dict_variants if variant.get("type") != "null"),
                dict_variants[0] if dict_variants else {},
            )
            resolved = _resolvable_schema(chosen, local_defs, depth + 1)
            if isinstance(resolved, dict):
                resolved = dict(resolved)
                for carry in ("description", "title"):
                    if carry in schema and carry not in resolved:
                        resolved[carry] = schema[carry]
            return resolved

    out: dict[str, Any] = {}
    if isinstance(schema.get("allOf"), list):
        for sub in schema["allOf"]:
            resolved = _resolvable_schema(sub, local_defs, depth + 1)
            if not isinstance(resolved, dict):
                continue
            if isinstance(resolved.get("properties"), dict):
                out.setdefault("properties", {}).update(resolved["properties"])
            if isinstance(resolved.get("required"), list):
                out["required"] = _merge_required(out.get("required", []), resolved["required"])
            for key, value in resolved.items():
                if key not in {"properties", "required"}:
                    out.setdefault(key, value)
        if "description" in schema:
            out.setdefault("description", schema["description"])
        return out

    for key, value in schema.items():
        if key in {"$defs", "definitions"}:
            continue
        if isinstance(value, dict):
            out[key] = _resolvable_schema(value, local_defs, depth + 1)
        elif isinstance(value, list):
            out[key] = [
                _resolvable_schema(item, local_defs, depth + 1) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            out[key] = value
    return out


def _property_names(schema: Any) -> set[str]:
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        return set(schema["properties"])
    return set()


def _required_names(schema: Any) -> set[str]:
    if isinstance(schema, dict) and isinstance(schema.get("required"), list):
        return {str(item) for item in schema["required"]}
    return set()


def _find_description(obj: Any, needle: str) -> bool:
    if isinstance(obj, dict):
        if obj.get("description") == needle:
            return True
        return any(_find_description(value, needle) for value in obj.values())
    if isinstance(obj, list):
        return any(_find_description(value, needle) for value in obj)
    return False


@pytest.mark.parametrize("fixture_path", _fixture_paths(), ids=lambda p: p.name)
def test_corpus_outputs_are_codex_clean(fixture_path: Path) -> None:
    _assert_codex_clean(_sanitize_fixture(fixture_path))


@pytest.mark.parametrize("fixture_path", _fixture_paths(), ids=lambda p: p.name)
def test_no_list_type_survives(fixture_path: Path) -> None:
    sanitized = _sanitize_fixture(fixture_path)
    for type_value, path in _iter_type_values(sanitized):
        assert not isinstance(type_value, list), f"list-form type survived at {path}: {type_value!r}"


@pytest.mark.parametrize("fixture_path", _fixture_paths(), ids=lambda p: p.name)
def test_semantic_preservation(fixture_path: Path) -> None:
    raw = _load_json(fixture_path)
    resolved = _resolvable_schema(raw)
    sanitized = _sanitize_tool_schema_for_codex(raw)

    assert _property_names(sanitized) >= _property_names(resolved)
    assert _required_names(sanitized) >= _required_names(resolved)

    expected_descriptions = {
        "real_notion_retrieve_page.json": "Identifier for a Notion page",
        "real_notion_query_data_source.json": "Identifier for a Notion data source (database)",
        "hand_notion_page_ref_defs.json": "Human readable title text",
        "hand_mvms_union_target.json": "MVMS link target union description",
        "hand_allof_merge_required.json": "Search query to execute",
        "hand_const_list_type_advisory.json": "Contact email with advisory validation keywords",
    }
    assert _find_description(sanitized, expected_descriptions[fixture_path.name])


def test_const_becomes_enum() -> None:
    sanitized = _sanitize_tool_schema_for_codex({"type": "object", "properties": {"mode": {"const": "x"}}})
    mode = sanitized["properties"]["mode"]
    assert mode["enum"] == ["x"]
    assert "const" not in mode


def test_regression_69_tool_shape() -> None:
    tools = _load_json(FIXTURE_DIR / "regression_69tool.json")
    assert len(tools) == 69
    sanitized_parameters = [
        _sanitize_tool_schema_for_codex(copy.deepcopy(tool["function"]["parameters"])) for tool in tools
    ]
    for idx, parameters in enumerate(sanitized_parameters):
        try:
            _assert_codex_clean(parameters)
        except AssertionError as exc:
            raise AssertionError(f"tool {idx} was not Codex-clean: {exc}") from exc


def _random_scalar_schema(rng: random.Random) -> dict[str, Any]:
    scalar_type = rng.choice(["string", "integer", "number", "boolean"])
    schema: dict[str, Any] = {"type": scalar_type, "description": f"generated {scalar_type}"}
    if scalar_type == "string" and rng.random() < 0.45:
        schema[rng.choice(["format", "pattern", "contentEncoding"])] = rng.choice(
            ["uuid", "date-time", "^[a-z]+$", "base64"]
        )
    if rng.random() < 0.2:
        schema["type"] = [scalar_type, "null"]
    if rng.random() < 0.1:
        schema["const"] = "fixed"
    return schema


def _generated_schema(rng: random.Random, depth: int) -> Any:
    if depth <= 0:
        return _random_scalar_schema(rng)

    choice = rng.choice(["object", "array", "array_tuple", "oneOf", "anyOf", "allOf", "ref", "scalar"])
    if choice == "scalar":
        return _random_scalar_schema(rng)
    if choice == "array":
        return {"type": "array", "items": _generated_schema(rng, depth - 1)}
    if choice == "array_tuple":
        return {"type": "array", "items": [_generated_schema(rng, depth - 1) for _ in range(rng.randint(1, 4))]}
    if choice == "oneOf":
        return {
            "oneOf": [{"type": "null"}, _generated_schema(rng, depth - 1)],
            "description": "generated oneOf description",
        }
    if choice == "anyOf":
        return {"anyOf": [{"type": "null"}, _generated_schema(rng, depth - 1)]}
    if choice == "allOf":
        return {
            "allOf": [
                {
                    "type": "object",
                    "properties": {"a": _generated_schema(rng, depth - 1)},
                    "required": ["a"],
                },
                {
                    "type": "object",
                    "properties": {"b": _generated_schema(rng, depth - 1)},
                    "required": ["b"],
                },
            ]
        }
    if choice == "ref":
        return {
            "$defs": {"Node": {"type": "object", "properties": {"value": _random_scalar_schema(rng)}}},
            "$ref": "#/$defs/Node",
        }

    width = rng.randint(1, 4)
    properties = {f"p{idx}": _generated_schema(rng, depth - 1) for idx in range(width)}
    schema = {"type": "object", "properties": properties, "required": sorted(properties)[: rng.randint(0, width)]}
    if rng.random() < 0.35:
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["examples"] = [{"sample": True}]
    return schema


@pytest.mark.parametrize("case_index", range(200))
def test_deterministic_fuzz_codex_clean_and_fast(case_index: int) -> None:
    rng = random.Random(20260623 + case_index)
    requested_depth = rng.randint(0, 40)
    schema = _generated_schema(rng, requested_depth)
    start = time.monotonic()
    try:
        sanitized = _sanitize_tool_schema_for_codex(schema)
    except Exception as exc:  # noqa: BLE001 - fuzz invariant should report any exception class.
        pytest.fail(f"sanitizer raised {type(exc).__name__}: {exc}")
    assert time.monotonic() - start < 0.25
    assert isinstance(sanitized, dict | str | int | float | bool | list | type(None))
    _assert_codex_clean(sanitized)


def test_cyclic_refs_terminate_at_depth_guard() -> None:
    self_ref = {
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/Node"}},
    }
    mutual_ref = {
        "$defs": {
            "A": {"type": "object", "properties": {"b": {"$ref": "#/$defs/B"}}},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}},
        },
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/A"}},
    }

    for schema in (self_ref, mutual_ref):
        try:
            sanitized = _sanitize_tool_schema_for_codex(schema)
        except RecursionError as exc:
            pytest.fail(f"sanitizer hit RecursionError instead of depth guard: {exc}")
        _assert_codex_clean(sanitized)
        assert _contains_depth_guard_string(sanitized), sanitized


def _contains_depth_guard_string(obj: Any) -> bool:
    if obj == {"type": "string"}:
        return True
    if isinstance(obj, dict):
        return any(_contains_depth_guard_string(value) for value in obj.values())
    if isinstance(obj, list):
        return any(_contains_depth_guard_string(value) for value in obj)
    return False


def test_depth_guard_direct_call_returns_string_schema() -> None:
    assert _sanitize_tool_schema_for_codex({"type": "object"}, _depth=17) == {"type": "string"}


def test_dangling_ref_becomes_permissive_object() -> None:
    assert _sanitize_tool_schema_for_codex({"$ref": "#/$defs/Missing"}) == {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }
