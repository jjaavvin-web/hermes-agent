# Codex sanitizer fuzz/invariant test report

## Fixture provenance

| file | provenance | notes |
| --- | --- | --- |
| `tests/agent/fixtures/codex_schemas/real_notion_retrieve_page.json` | real-from-session | Extracted from `/home/josep/.hermes/sessions/session_20260519_071305_5cf193.json`, tool `mcp_notion_API_retrieve_a_page`; contains `$defs`, `$ref`, `oneOf`/`anyOf`, `const`, and `format`. |
| `tests/agent/fixtures/codex_schemas/real_notion_query_data_source.json` | real-from-session | Extracted from `/home/josep/.hermes/sessions/session_20260519_071305_5cf193.json`, tool `mcp_notion_API_query_data_source`; larger Notion object tree with nested refs and advisory keywords. |
| `tests/agent/fixtures/codex_schemas/hand_notion_page_ref_defs.json` | hand-built | Small Notion-style page object fixture for `$ref` + `$defs`, nested `additionalProperties`, list-form `type`, and `format`. |
| `tests/agent/fixtures/codex_schemas/hand_mvms_union_target.json` | hand-built | MVMS-style target union for `oneOf`/`anyOf`, non-null variant selection, and description/title carry-over. |
| `tests/agent/fixtures/codex_schemas/hand_allof_merge_required.json` | hand-built | `allOf` property merge and sorted `required` union fixture. |
| `tests/agent/fixtures/codex_schemas/hand_const_list_type_advisory.json` | hand-built | `const` -> enum, list-form `type`, and advisory keyword drop fixture. |
| `tests/agent/fixtures/codex_schemas/regression_69tool.json` | hand-built | Historic failing-request model: 69 tool objects, each with `function.parameters` mixing `$ref`, `$defs`, `anyOf`, `format`, and `const`. |

## PROOF 1 — focused suite

Command:

```bash
venv/bin/pytest tests/agent/test_codex_schema_sanitizer.py -v
```

Collected count: `230 items`.

Summary line:

```text
============================= 230 passed in 5.70s ==============================
```

Rerun note: the exact command was run after the mutation check was restored and exited 0 again.

## PROOF 2 — mutation check

Temporary mutation applied in `agent/codex_responses_adapter.py`: replaced the advisory-drop condition `or k in _CODEX_DROP_SCHEMA_KEYWORDS` with a no-op mutation comment, then reran:

```bash
venv/bin/pytest tests/agent/test_codex_schema_sanitizer.py -q
```

Result: exited `1`; the suite went red (`108 failed, 122 passed in 6.93s`). First failing test:

```text
FAILED tests/agent/test_codex_schema_sanitizer.py::test_const_to_enum_and_drops_format_pattern
```

Representative assertion text:

```text
AssertionError: $comment leaked at $.$comment
```

The adapter was restored immediately with `git checkout -- agent/codex_responses_adapter.py`. Restoration proof:

```text
git diff --stat agent/
# empty output
```

## PROOF 3 — final source-scope check

Pre-commit status after restoring the mutation and before staging showed only test/fixture changes, with no source-file mutation:

```text
 M tests/agent/test_codex_schema_sanitizer.py
?? tests/agent/fixtures/
```

Adapter/source diff proof:

```text
git diff --stat agent/
# empty output
```

## Existing adapter-test baseline

Command:

```bash
venv/bin/pytest tests/agent/ -q -k codex
```

Current result:

```text
4 failed, 555 passed, 4241 deselected, 2 warnings in 59.54s
```

Base-ref comparison against `2229c825b` in `/tmp/hermes-codex-base-2229`:

```text
4 failed, 339 passed, 4241 deselected, 2 warnings in 52.52s
```

The same four failures were present at base (`tests/agent/test_auxiliary_client.py` expired-Codex/custom-endpoint fallback tests), so this branch introduced no new codex adapter-test failures.

## Sanitizer bugs / edge cases found but not patched

None in the contract-covered JSON-Schema shapes tested here. During generator tuning I intentionally kept generated arrays inside valid JSON-Schema positions (`items`) because top-level non-dict input is contractually returned unchanged.

## Reproduce commands

```bash
cd /home/josep/hermes-lane-wt/burn4-05-codex-schema-sanitizer-fuz
venv/bin/python -c "import json,glob; [print(f) for f in glob.glob('/home/josep/.hermes/sessions/*.json')[:5]]"
grep -l '"\$ref"\|"\$defs"\|"oneOf"\|"anyOf"\|"allOf"' /home/josep/.hermes/sessions/*.json | head
venv/bin/pytest tests/agent/test_codex_schema_sanitizer.py -v
venv/bin/ruff check tests/agent/test_codex_schema_sanitizer.py
venv/bin/pytest tests/agent/ -q -k codex
# Mutation proof, do not leave modified:
# temporarily comment out `or k in _CODEX_DROP_SCHEMA_KEYWORDS` in agent/codex_responses_adapter.py
# venv/bin/pytest tests/agent/test_codex_schema_sanitizer.py -q
# git checkout -- agent/codex_responses_adapter.py
```
