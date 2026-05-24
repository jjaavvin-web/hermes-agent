# Cluster B — Gateway / Telegram / Discord tool

## Files audited (path:lines)

- `/home/josep/.local/share/hermes-agent/gateway/__init__.py` — 36 lines
- `/home/josep/.local/share/hermes-agent/gateway/platforms/base.py` — 3765 lines (abstract interface, helpers, cache utilities)
- `/home/josep/.local/share/hermes-agent/gateway/platforms/telegram.py` — 5140 lines (partial read; send, edit, connect, disconnect, retry reviewed)
- `/home/josep/.local/share/hermes-agent/gateway/platforms/discord.py` — 5169+ lines (key sections: connect, send, edit_message, get_chat_info, dedup)
- `/home/josep/.local/share/hermes-agent/gateway/platforms/helpers.py` — MessageDeduplicator, TextBatchAggregator
- `/home/josep/.local/share/hermes-agent/tools/discord_tool.py` — 960 lines (full read)
- `/home/josep/.hermes/scripts/discord-notify.sh` — 115 lines (full read)
- `/home/josep/.hermes/discord_threads.json` — 20-element JSON array

---

## Gateway platform contract

### Abstract interface (`base.py`)

Four `@abstractmethod` declarations — every platform adapter MUST implement:

| Method | Signature | Location |
|--------|-----------|----------|
| `connect` | `async def connect(self) -> bool` | `base.py:1538-1545` |
| `disconnect` | `async def disconnect(self) -> None` | `base.py:1547-1550` |
| `send` | `async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult` | `base.py:1552-1572` |
| `get_chat_info` | `async def get_chat_info(self, chat_id: str) -> Dict[str, Any]` | `base.py:3612-3621` |

Optional overrides with meaningful defaults:

- `edit_message(chat_id, message_id, content, *, finalize=False) -> SendResult` — default returns success=False (`base.py:1609-1616`)
- `send_draft(...)` — streaming draft preview; default raises `NotImplementedError` (`base.py:1355-1380`)
- `supports_draft_streaming(...) -> bool` — default False (`base.py:1336-1353`)
- `create_handoff_thread(parent_chat_id, name) -> Optional[str]` — default None (`base.py:1582-1606`)
- `format_message(content) -> str` — default identity (`base.py:3623-3632`)
- `REQUIRES_EDIT_FINALIZE: bool = False` — class constant, override for platforms needing lifecycle finalization (`base.py:1580`)

### Lifecycle: register → bind → run loop → shutdown

1. **Register**: `GatewayConfig` maps platform name to `PlatformConfig` via `load_gateway_config` (`gateway/config.py`). Token read from `DISCORD_BOT_TOKEN` env var → `PlatformConfig.token` (`gateway/config.py:1195`).
2. **Bind**: `set_message_handler(handler)` on base class, plus optional `set_session_store(store)` (`base.py:1526-1536`).
3. **Run loop**: `connect()` starts polling/webhook; Telegram uses `python-telegram-bot` Updater long-poll or webhook server; Discord uses `discord.py` client event loop. Both drive their own async loops internally.
4. **Shutdown**: `disconnect()` stops updater/client, cancels pending tasks, calls `_release_platform_lock()`, calls `_mark_disconnected()`.

### Dispatcher contract

The adapter receives a platform event, normalizes it into a `MessageEvent` dataclass (`base.py:916-999`), and delivers it to the handler registered via `set_message_handler`. The `MessageEvent` carries:
- `text`, `message_type` (TEXT, PHOTO, AUDIO, etc.)
- `source: SessionSource` — platform, chat_id, chat_type, user_id, thread_id
- `media_urls`, `message_id`, `reply_to_message_id`
- `auto_skill`, `channel_prompt`, `channel_context`

The gateway runner receives the normalized event and manages sessions.

### Persistence story: what does Telegram persist?

- **DM topic thread_ids** — written to `~/.hermes/config.yaml` via `_persist_dm_topic_thread_id()` using an atomic tempfile + `atomic_replace()` (`telegram.py:1077-1133`). Path: `platforms.telegram.extra.dm_topics[*].topics[*].thread_id`.
- **Runtime status** — written via `gateway.status.write_runtime_status()` on connect/disconnect/fatal (`base.py:1416-1466`).
- **Command sync state** — Discord writes `~/.hermes/gateway/discord_command_sync_state.json` (`discord.py:901-926`, constant at `discord.py:30`).
- **Image/audio/video/document caches** — `~/.hermes/cache/{images,audio,videos,documents}/` (`base.py:546-891`).
- Telegram does NOT write a message-ID seen list to disk. Dedup is in-process only via `MessageDeduplicator` in `helpers.py`.

---

## Telegram-specific plumbing (deprecation surface)

| Construct | File:Line | Nature |
|-----------|-----------|--------|
| `TelegramAdapter` class | `telegram.py:317` | Entire adapter class |
| `python-telegram-bot` import block | `telegram.py:22-60` | Library dependency |
| `TELEGRAM_AVAILABLE` flag | `telegram.py:38` | Lazy-install guard |
| `_MDV2_ESCAPE_RE`, `_escape_mdv2`, `_strip_mdv2` | `telegram.py:163-188` | MarkdownV2 rendering |
| `_wrap_markdown_tables` | `telegram.py:264-314` | Table→bullet reformat |
| `utf16_len` used as `message_len_fn` | `telegram.py:427-429` | Telegram-specific length measurement |
| `MEDIA_GROUP_WAIT_SECONDS`, `_pending_photo_batches` | `telegram.py:333,443-445` | Album batching |
| `_text_batch_delay_seconds`, `_pending_text_batches` | `telegram.py:454-467` | Text burst batching |
| `_polling_error_task`, `_polling_conflict_count` | `telegram.py:468-470` | Polling conflict/reconnect |
| `_dm_topics`, `_dm_topics_config` | `telegram.py:472-475` | DM Topics (Bot API 9.4) |
| `_model_picker_state`, `_approval_state` | `telegram.py:476-479` | Inline keyboard state |
| `_project_intake_state`, `_clarify_state` | `telegram.py:484-489` | Multi-step button flows |
| `REQUIRES_EDIT_FINALIZE = True` | `telegram.py:341` | Telegram-specific finalize requirement |
| `create_handoff_thread` / `_create_dm_topic` | `telegram.py:985-1052` | Thread creation |
| `_setup_dm_topics` | `telegram.py:1135` | Startup topic init |
| `_persist_dm_topic_thread_id` | `telegram.py:1077` | config.yaml write |
| `_handle_polling_conflict` / `_handle_polling_network_error` | `telegram.py:926, 802` | Reconnect ladder |
| `_verify_polling_after_reconnect` | `telegram.py:882` | Heartbeat probe |
| `send_draft` / `supports_draft_streaming` | `telegram.py:2013, 2032` | Telegram Bot API 9.5 streaming draft |
| `_edit_overflow_split` | `telegram.py:1847` | 4096-char overflow split |

**Public env vars / config keys (Telegram-specific):**

| Env var | Purpose | Source |
|---------|---------|--------|
| `TELEGRAM_BOT_TOKEN` | Bot token (via `config.token`) | `gateway/config.py:1195` |
| `TELEGRAM_WEBHOOK_URL` | Webhook mode URL | `telegram.py:1228-1369` |
| `TELEGRAM_WEBHOOK_PORT` | Webhook listen port (default 8443) | `telegram.py:1383` |
| `TELEGRAM_WEBHOOK_SECRET` | REQUIRED for webhook mode | `telegram.py:1384-1397` |
| `TELEGRAM_ALLOWED_USERS` | CSV of allowed user IDs | `telegram.py:557-561` |
| `HERMES_TELEGRAM_MEDIA_BATCH_DELAY_SECONDS` | Album wait (default 0.8s) | `telegram.py:442` |
| `HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS` | Text burst delay (default 0.3s) | `telegram.py:454` |
| `HERMES_TELEGRAM_TEXT_BATCH_SPLIT_DELAY_SECONDS` | Long-split detection (default 1.0s) | `telegram.py:460` |
| `HERMES_TELEGRAM_HTTP_POOL_SIZE` | httpx pool size (default 512) | `telegram.py:1277` |
| `HERMES_TELEGRAM_HTTP_POOL_TIMEOUT` | Pool timeout (default 8.0s) | `telegram.py:1278` |
| `HERMES_TELEGRAM_DISABLE_FALLBACK_IPS` | Disable IP fallback transport | `telegram.py:1284` |
| `TELEGRAM_PROXY` | Proxy URL for Telegram API | `telegram.py:1295` |

---

## Discord tool API (outbound)

This is `tools/discord_tool.py` — the agent-facing outbound REST tool (no gateway dependency).

### Public functions

| Function | File:Line | Behavior |
|----------|-----------|----------|
| `discord_core(action, **kwargs) -> str` | `discord_tool.py:908` | Executes core actions: `fetch_messages`, `search_members`, `create_thread` |
| `discord_admin_handler(action, **kwargs) -> str` | `discord_tool.py:913` | Executes admin actions: list/info/pin/role/delete |
| `check_discord_tool_requirements() -> bool` | `discord_tool.py:818` | Returns True iff `DISCORD_BOT_TOKEN` is set |
| `get_dynamic_schema_core() -> Optional[Dict]` | `discord_tool.py:744` | Dynamic schema filtered by intents + config allowlist |
| `get_dynamic_schema_admin() -> Optional[Dict]` | `discord_tool.py:748` | Admin schema variant |
| `_detect_capabilities(token) -> Dict` | `discord_tool.py:138` | Hits `GET /applications/@me`; caches `has_members_intent`, `has_message_content` |

### Auth: how the token is obtained

> `def _get_bot_token() -> Optional[str]: return os.getenv("DISCORD_BOT_TOKEN", "").strip() or None`
> (`discord_tool.py:53-55`)

Token lives in `DISCORD_BOT_TOKEN` env var. No fallback to config.yaml — purely env. The gateway adapter also reads the same var via `gateway/config.py:1195`.

### Thread API

| Operation | Supported | API path | Source |
|-----------|-----------|----------|--------|
| CREATE thread (from message) | YES | `POST /channels/{id}/messages/{msg_id}/threads` | `discord_tool.py:436-438` |
| CREATE standalone thread | YES | `POST /channels/{id}/threads` (type=11, PUBLIC_THREAD) | `discord_tool.py:442-448` |
| POST to thread (send message) | YES — via gateway adapter `send()` | `discord.py:1370` | gateway only, not tool |
| FETCH messages from thread/channel | YES | `GET /channels/{id}/messages` with `before`/`after` pagination | `discord_tool.py:351-391` |

`_create_thread` at `discord_tool.py:427-454`:
> "If `message_id` is supplied, creates a thread from an existing message; otherwise creates a standalone PUBLIC_THREAD (type 11). Returns `{success, thread_id, name}`."

### Rate-limit handling

The tool uses `urllib.request` (synchronous, no rate-limit retry). On `DiscordAPIError` with status 403, it calls `_enrich_403(action, body)` for user-readable guidance (`discord_tool.py:898-901`). No explicit retry-after / 429 handling in the tool — 429 responses raise `DiscordAPIError` and return the error JSON to the model. Rate limiting is left to the model to handle via retry. The gateway adapter (`discord.py`) has its own rate-limit handling via `discord.py` library's built-in rate limiter.

---

## discord-notify.sh contract

**File:** `/home/josep/.hermes/scripts/discord-notify.sh`

### Env vars required

| Var | Required | Purpose |
|-----|----------|---------|
| `DISCORD_BOT_TOKEN` | YES (fatal exit 2) | Bot auth header |
| `DISCORD_NOTIFY_CHANNEL_ID` | One of these two (fatal exit 3) | Target channel |
| `DISCORD_HOME_CHANNEL` | Fallback if above absent | Target channel |
| `DISCORD_NOTIFY_DRYRUN` | No | Dry-run validation without send |
| `HERMES_ENV_FILE` | No (defaults `~/.hermes/.env`) | dotenv file path |
| `HERMES_NOTIFY_PYTHON` | No (defaults `python3`) | Python interpreter |

### POST shape

```
POST https://discord.com/api/v10/channels/{channel}/messages
Authorization: Bot {token}
Content-Type: application/json

{"content": "<message>", "allowed_mentions": {"parse": []}}
```

Content is truncated to 1850 chars if over 1900 (`discord-notify.sh:83-84`). `allowed_mentions: {"parse": []}` suppresses all ping parsing — safe by default.

### Wired into operator launch templates — CONFIRMED

Per cutover audit report (`/home/josep/.hermes/ruflo-work/system-health-discord-cutover-readiness-20260523T115054Z`):
> "Templates already migrated — `ruflo-launch.template.sh`, `ruflo-watcher.template.sh`, `gateway-watchdog.sh`, `reap-stale-runs.sh` all reference `discord-notify` only, none reference `telegram-notify`"

Confirmed also by `watcher.sh` files at:
- `/home/josep/.hermes/ruflo-work/infra-deepdive-audit-20260523/watcher.sh:21`
- `/home/josep/.hermes/ruflo-work/agent-profile-optimization-20260522T171702Z/watcher.sh:22`

Objective §17 claim is **confirmed** — `discord-notify.sh` is wired into active operator launch templates.

---

## discord_threads.json — actual content vs objective's claim

**Actual content** (`~/.hermes/discord_threads.json`):
```json
["1506858959716225065", "1506877524246925353", "1507081158821675132", ... 20 entries total]
```

A flat JSON array of 20 Discord snowflake message-ID strings. All 20 are message IDs (18-digit integers as strings), not thread IDs or session records.

**Objective's claim** (objective.md:31, objective.md:67):
> "Session state lives in `~/.hermes/discord_threads.json` (already exists)"
> "`discord_threads.json` schema for `thread_id ↔ session_id ↔ kanban_card_id ↔ worktree_path ↔ codex_pid ↔ isa_id`"

**Reality:** The file is a seen-message-IDs dedup list, not session state. It is written by the existing Discord gateway adapter's `MessageDeduplicator` or a predecessor seen-IDs mechanism (no live reference to this specific file path found in `gateway/platforms/discord.py`; the file was likely written by an older version or a test harness — the test file `tests/gateway/test_discord_thread_persistence.py` references `discord_threads.json` as a persistence target for a session-state schema that does not yet exist in production).

**Implication for design:** The session-state schema (`thread_id ↔ session_id ↔ codex_pid ↔ worktree_path ↔ isa_id`) is **greenfield** — not an extension of the existing file. The file format must be redesigned from a flat array to a keyed map/object. Atomic writes (flock + tempfile rename) are needed given the single-writer constraint called out in objective.md:129.

---

## Inventory of "telegram" references

Total count: **4778** (includes test files, docs, release notes, and cache)

Top 30 (hermes-agent source only, excluding __pycache__):

| File:Line | Content |
|-----------|---------|
| `toolsets.py:400` | `"hermes-telegram"` toolset definition |
| `toolsets.py:533` | includes list: `hermes-telegram` in `hermes-all` |
| `Dockerfile:98` | comment: lazy-install telegram at boot |
| `AGENTS.md:37` | platforms dir note |
| `AGENTS.md:938` | `telegram.py` as canonical pattern |
| `.env.example:345` | `TELEGRAM_WEBHOOK_URL` example |
| `mcp_serve.py:483` | filter param docs |
| `mcp_serve.py:745` | example target `telegram:...` |
| `pyproject.toml:84` | `python-telegram-bot[webhooks]==22.6` in messaging extra |
| `pyproject.toml:130` | hard dependency line |
| `hermes_state.py:2387` | `apply_telegram_topic_migration()` |
| `hermes_state.py:2403` | `CREATE TABLE telegram_dm_topic_mode` |
| `hermes_state.py:2416` | `CREATE TABLE telegram_dm_topic_bindings` |
| `run_agent.py:1242` | platform param doc |
| `run_agent.py:1263` | `self.platform` assignment |
| `gateway/config.py:1195` | `Platform.TELEGRAM: "TELEGRAM_BOT_TOKEN"` mapping |
| `gateway/platforms/base.py:32-33` | `_TELEGRAM_AUDIO_ATTACHMENT_EXTS`, `_TELEGRAM_VOICE_EXTS` |
| `gateway/platforms/base.py:57` | `_platform_name == "telegram"` branch |
| `gateway/platforms/base.py:78` | `platform == "telegram"` DM topic branch |
| `gateway/platforms/telegram.py:317` | `class TelegramAdapter` |
| `gateway/platforms/telegram_network.py` | Fallback IP transport module |
| `CONTRIBUTING.md:187` | `telegram.py, discord_adapter.py` in layout |
| `cli-config.yaml.example:647` | `hermes-telegram` default toolset |
| `hermes-already-has-routines.md:101` | `--deliver telegram` example |
| `hermes_state.py:14` | session source tagging doc |
| `RELEASE_v0.7.0.md` (multiple) | Telegram release notes |
| `website/sidebars.ts` | docs sidebar |
| `gateway/platforms/ADDING_A_PLATFORM.md` | onboarding doc |
| `gateway/platforms/helpers.py` | indirect via `MessageDeduplicator` pattern origin |
| `gateway/platforms/discord.py:932` | `_desired_command_sync_fingerprint` (no Telegram refs) |

---

## Inventory of "discord" references

Total count: **3974** (includes test files, docs, release notes, and website)

Top 30 (hermes-agent source only, excluding __pycache__):

| File:Line | Content |
|-----------|---------|
| `toolsets.py:261,267` | `"discord"` and `"discord_admin"` toolset defs |
| `toolsets.py:406` | `"hermes-discord"` preset |
| `toolsets.py:533` | `hermes-discord` in `hermes-all` |
| `tools/discord_tool.py:40` | `DISCORD_API_BASE` |
| `tools/discord_tool.py:53` | `_get_bot_token()` reads `DISCORD_BOT_TOKEN` |
| `gateway/platforms/discord.py:532` | `class DiscordAdapter` |
| `gateway/config.py:1195` | `Platform.DISCORD: "DISCORD_BOT_TOKEN"` |
| `gateway/config.py:1275` | `discord_token = os.getenv("DISCORD_BOT_TOKEN")` |
| `cli-config.yaml.example:642,647,659,687,723` | Discord config examples |
| `AGENTS.md:706` | `discord`, `discord_admin` in tool listing |
| `AGENTS.md:37` | `discord_adapter.py` in platforms dir |
| `pyproject.toml:84` | `discord.py[voice]==2.7.1` in messaging extra |
| `uv.lock:1136` | `discord-py` package lock |
| `hermes_state.py:14` | session source tagging |
| `RELEASE_v0.3.0.md:115` | defer discord adapter annotations |
| `RELEASE_v0.7.0.md:105` | `discord.reactions` config option |
| `hermes-already-has-routines.md:102` | `--deliver discord` example |
| `website/sidebars.ts:606` | `user-guide/messaging/discord` |
| `mcp_serve.py:483` | platform filter param doc |
| `run_agent.py:1242` | platform param doc |
| `gateway/platforms/discord.py:30` | `_DISCORD_COMMAND_SYNC_STATE_FILENAME` |
| `gateway/platforms/discord.py:587` | `self._dedup = MessageDeduplicator()` |
| `gateway/platforms/discord.py:632` | `_acquire_platform_lock('discord-bot-token', ...)` |
| `gateway/platforms/discord.py:853` | `self._client.start(self.config.token)` |
| `gateway/platforms/base.py:362` | `proxy_kwargs_for_bot` docstring mentions `discord.Client()` |
| `CONTRIBUTING.md:141,187` | layout references |
| `tests/gateway/test_discord_thread_persistence.py:4` | test file for session state schema |
| `gateway/platforms/helpers.py:27` | `MessageDeduplicator` — used by Discord adapter |
| `README.zh-CN.md:9` | Discord community badge |
| `website/src/components/UserStoriesCollage/index.tsx:128` | UI component |

---

## Open questions for the queen

- **discord_threads.json writer**: No live code path in `gateway/platforms/discord.py` writes to `~/.hermes/discord_threads.json`. The test file `tests/gateway/test_discord_thread_persistence.py` exercises a schema that doesn't exist in production. Who wrote the current 20-entry file, and is it safe to overwrite with the new session-state schema?
- **Session-state atomicity**: The objective requires flock + tempfile rename. `utils.atomic_json_write` is already imported in `discord.py` (`discord.py:49`). Should the new Discord gateway writer use this or the existing `atomic_replace` (used by Telegram)?
- **discord_threads.json path authority**: `hermes_constants.get_hermes_home()` returns `~/.hermes/`. Is the filename `discord_threads.json` locked in, or should it be renamed to `discord_session_state.json` to avoid confusion with the existing seen-IDs file?
- **discord_tool.py rate-limit gap**: The tool has no 429/retry-after handling. Under sustained agent usage (e.g., fetch_messages in a tight loop), it will surface errors to the model rather than backing off. Should a thin retry wrapper be added before the gateway build?
- **Telegram deprecation scope**: `hermes_state.py` has `telegram_dm_topic_mode` and `telegram_dm_topic_bindings` SQLite tables (`hermes_state.py:2403,2416`). These are not purged by the Discord-only cutover. Are they in scope for cleanup?
- **TELEGRAM_WEBHOOK_SECRET security**: The adapter refuses to start if `TELEGRAM_WEBHOOK_URL` is set without `TELEGRAM_WEBHOOK_SECRET` (`telegram.py:1385-1397`), citing GHSA-3vpc-7q5r-276h. This is the right behavior but the check is inside `connect()`, not at config validation time. If the operator is running Discord-only, this code is dormant — but not dead. Does the cutover plan include disabling the Telegram platform entirely in config, or just leaving it unconfigured?
- **Gateway adapter dedup vs. disk**: `MessageDeduplicator` in `helpers.py` is in-process TTL (max_size=2000, ttl=300s). On bot restart, all dedup state is lost. For the new Codex-parallel session bot, message-ID dedup across restarts requires the disk-backed file. Confirm the new `discord_threads.json` schema should include a `seen_message_ids` section, or whether dedup is handled separately.
- **`discord.py` adapter `get_chat_info`**: Found at `discord.py:2749`. Not read in full — confirm it returns `chat_type` correctly for threads vs. channels (needed for `SessionSource` construction in the new gateway).
