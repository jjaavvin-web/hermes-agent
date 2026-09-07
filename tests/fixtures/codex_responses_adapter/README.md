# codex_responses_adapter fixtures

Sanitized/minimized from real local Hermes Codex session JSONL rows under `~/.hermes/sessions/`.

- `assistant_reasoning_tool_call.*`: derived from `20260524_215626_b4dbab4f.jsonl`, first user/assistant/tool exchange. It preserves the real Codex-shaped `codex_reasoning_items`, `call_id`, `response_item_id`, and tool result linkage while trimming private tool output to a short success object.
- `assistant_parallel_mvms_calls.*`: derived from `20260512_172151_4e36e0.jsonl`, assistant turn with parallel MVMS Responses function calls. It preserves the real multi-call `call_id`/`response_item_id` shapes and query arguments.

The `.expected.json` files are the byte-for-byte canonical Responses API payload body fragment (`input` items) produced by `_chat_messages_to_responses_input` for those captured chat-history rows. They intentionally pin the transform boundary, not a live network request.
