# Codex schema sanitizer fixtures

These JSON files exercise `_sanitize_tool_schema_for_codex` against real MCP tool schemas and hand-built edge cases. Unless noted otherwise, a fixture is the raw `parameters` JSON-Schema object passed for a tool.

| file | provenance | coverage |
| --- | --- | --- |
| `real_notion_retrieve_page.json` | real from `/home/josep/.hermes/sessions/session_20260519_071305_5cf193.json`, tool `mcp_notion_API_retrieve_a_page` | Notion MCP `$defs`/`$ref`, `oneOf`/`anyOf`, `const`, `format` |
| `real_notion_query_data_source.json` | real from `/home/josep/.hermes/sessions/session_20260519_071305_5cf193.json`, tool `mcp_notion_API_query_data_source` | Larger Notion MCP object tree with nested refs and advisory keywords |
| `hand_notion_page_ref_defs.json` | hand-built because captured sessions did not include a small nested page-create parameter object | Notion-style `$ref` + `$defs`, nested `additionalProperties`, list-form type, `format` |
| `hand_mvms_union_target.json` | hand-built because captured sessions available to this worktree did not include MVMS parameter schemas with rich unions | `oneOf`/`anyOf`, non-null variant selection, branch/container description preservation |
| `hand_allof_merge_required.json` | hand-built | `allOf` property merge and sorted required-union preservation |
| `hand_const_list_type_advisory.json` | hand-built | `const` to enum, list-form `type`, advisory keyword dropping (`format`, `pattern`, content keywords) |
| `regression_69tool.json` | hand-built request-shape regression modeled on the sanitizer docstring's “real 69-tool failing requests” note | 69 tool objects; each `function.parameters` mixes `$ref`, `$defs`, `anyOf`, `format`, and `const` |
