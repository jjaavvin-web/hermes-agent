# Telegram Retirement Appendix

**Drafted:** 2026-05-24 by `codex-parallel-design` hive
**Scope:** inventory only — actual edits are P5's ISA (`isas/P5-hardening.md`)
**Status when this doc was written:** Telegram adapter is still live; `discord-notify.sh` is already the active operator-launch notifier (cluster B §"Wired into operator launch templates — CONFIRMED"); Discord gateway adapter exists at ~5169 LOC

---

## 1. Retirement order of operations

| Phase | Timing | Action |
|-------|--------|--------|
| **Phase 1** | Any time before P5 | Set `TELEGRAM_BOT_TOKEN=""` in operator env. `telegram.py:1385-1397` short-circuits connection if config is incomplete — this is the safe "off" switch. Bot continues to run Discord-only. |
| **Phase 2** | P5 ISA | Delete code, scrub docs, drop SQLite tables (with backup-first migration). |
| **Phase 3** | Post-P5 | Purge any Telegram-specific cache key prefixes; remove `python-telegram-bot` from extras-require. |

---

## 2. Code surface to delete or edit

### 2a. Delete entirely

| File | Rationale |
|------|-----------|
| `gateway/platforms/telegram.py` | Entire adapter — 5140 lines, `class TelegramAdapter` at line 317 |
| `gateway/platforms/telegram_network.py` | Fallback IP transport; serves no purpose without the adapter |
| `tests/gateway/test_telegram_approval_buttons.py` | Adapter test |
| `tests/gateway/test_telegram_caption_merge.py` | Adapter test |
| `tests/gateway/test_telegram_clarify_buttons.py` | Adapter test |
| `tests/gateway/test_telegram_conflict.py` | Adapter test |
| `tests/gateway/test_telegram_documents.py` | Adapter test |
| `tests/gateway/test_telegram_format.py` | Adapter test |
| `tests/gateway/test_telegram_group_gating.py` | Adapter test |
| `tests/gateway/test_telegram_mention_boundaries.py` | Adapter test |
| `tests/gateway/test_telegram_model_picker.py` | Adapter test |
| `tests/gateway/test_telegram_network.py` | Adapter test |
| `tests/gateway/test_telegram_network_reconnect.py` | Adapter test |
| `tests/gateway/test_telegram_photo_interrupts.py` | Adapter test |
| `tests/gateway/test_telegram_project_intake_buttons.py` | Adapter test |
| `tests/gateway/test_telegram_reactions.py` | Adapter test |
| `tests/gateway/test_telegram_reply_mode.py` | Adapter test |
| `tests/gateway/test_telegram_reply_quote.py` | Adapter test |
| `tests/gateway/test_telegram_text_batch_perf.py` | Adapter test |
| `tests/gateway/test_telegram_text_batching.py` | Adapter test |
| `tests/gateway/test_telegram_thread_fallback.py` | Adapter test |
| `tests/gateway/test_telegram_topic_mode.py` | Adapter test |
| `tests/gateway/test_telegram_webhook_secret.py` | Adapter test |

### 2b. Edit (remove Telegram-specific sections)

| File | Lines | Action |
|------|-------|--------|
| `gateway/config.py` | 1195 | Remove `Platform.TELEGRAM: "TELEGRAM_BOT_TOKEN"` from token-map |
| `gateway/platforms/base.py` | 32–33 | Remove `_TELEGRAM_AUDIO_ATTACHMENT_EXTS`, `_TELEGRAM_VOICE_EXTS` constants |
| `gateway/platforms/base.py` | 57 | Remove `_platform_name == "telegram"` branch |
| `gateway/platforms/base.py` | 78 | Remove `platform == "telegram"` DM-topic branch |
| `gateway/run.py` | 69–83 | Remove `_telegramize_command_mentions` function |
| `gateway/run.py` | 1704–1768 | Remove `_telegram_topic_mode_enabled`, `_is_telegram_topic_root_lobby`, `_is_telegram_topic_lane`, `_should_send_telegram_lobby_reminder` methods |
| `gateway/run.py` | 5296–5316 | Remove `TelegramAdapter` import block and telegram-notifications wiring |
| `gateway/run.py` | 5630 | Remove `_warned_telegram_group_users_legacy` branch |
| `hermes_state.py` | 2387–2483+ | Remove `apply_telegram_topic_migration`, `enable_telegram_topic_mode`, `disable_telegram_topic_mode`, `is_telegram_topic_mode_enabled`, `get_telegram_topic_binding`, `bind_telegram_topic`, `is_telegram_session_linked_to_topic`, `list_unlinked_telegram_sessions_for_user`; replace with new `drop_telegram_dm_topic_tables` migration (see §6) |
| `toolsets.py` | 400 | Remove `"hermes-telegram"` toolset definition block |
| `toolsets.py` | 533 | Remove `"hermes-telegram"` from `hermes-all` includes list |
| `pyproject.toml` | 84 | Remove `python-telegram-bot[webhooks]==22.6` from `messaging` extra |
| `pyproject.toml` | 130 | Remove `python-telegram-bot[webhooks]==22.6` hard dependency line |
| `pyproject.toml` | 141 | Remove `telegram` from the lazy-install comment |
| `Dockerfile` | 98 | Remove lazy-install telegram comment / any pre-install step |
| `mcp_serve.py` | 483 | Remove `telegram` from platform filter param docs |
| `mcp_serve.py` | 745 | Remove `target="telegram:6308981865"` example |
| `mcp_serve.py` | 777 | Remove `telegram` from platform filter in second occurrence |
| `run_agent.py` | 1242 | Remove `telegram` from platform param docstring |
| `run_agent.py` | 1263 | Remove `self.platform = "telegram"` assignment branch |
| `tools/send_message_tool.py` | 532, 555, 779–780, 801–802 | Remove four `TelegramAdapter` import+use blocks |
| `tools/lazy_deps.py` | 118 | Remove `"platform.telegram": ("python-telegram-bot[webhooks]==22.6",)` entry |
| `tools/cronjob_tools.py` | 194–198, 593 | Remove `telegram` from deliver-target docstrings and schema description |
| `agent/redact.py` | 129, 356, 360 | Remove `_TELEGRAM_RE`, `_redact_telegram`, and their call site |
| `agent/prompt_builder.py` | 422 | Remove `"telegram"` entry from platform prompt map |
| `agent/skill_utils.py` | 125 | Remove `telegram` from platform doc example |
| `agent/memory_provider.py` | 70 | Remove `telegram` from platform enum in docstring |
| `hermes_cli/platforms.py` | 23 | Remove `("telegram", PlatformInfo(...))` tuple |
| `hermes_cli/setup.py` | 1967–2035, 2516–2517 | Remove `_setup_telegram()` function and its call site |
| `hermes_cli/commands.py` | 474–574, 600–607, 706–733, 746 | Remove `telegram_bot_commands`, `_sanitize_telegram_name`, `_clamp_telegram_names`, `telegram_menu_commands` and all related references |
| `hermes_cli/status.py` | 390 | Remove `"Telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL")` status check |
| `hermes_cli/config.py` | 102, 2319–2333, 4961, 5044 | Remove `TELEGRAM_HOME_CHANNEL*` from config listing; remove `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_PROXY` config-key entries; remove from config dump and export lists |
| `hermes_cli/dump.py` | 101 | Remove `"telegram": "TELEGRAM_BOT_TOKEN"` entry |
| `cli.py` | 7599, 7634 | Remove `Platform.TELEGRAM` entry in platform table; remove `TELEGRAM_BOT_TOKEN` printout |
| `gateway/display_config.py` | 84 | Remove `"telegram": {**_TIER_HIGH, "tool_progress": "new"}` display config entry |
| `gateway/channel_directory.py` | 355 | Remove `"telegram"` bare-platform-name example from docstring |
| `gateway/mirror.py` | 96 | Remove `telegram` from session-key origin comment |
| `gateway/stream_consumer.py` | 847 | Remove `python-telegram-bot` version reference from comment |
| `gateway/platforms/webhook.py` | 14, 173, 242, 765 | Remove `telegram` from deliver-target lists and route-response docstrings |
| `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` | 1343, 1390–1393 | Remove telegram allowlist path read and token migration block |
| `.env.example` | 339–346 | Remove all `TELEGRAM_*` commented-out env vars |
| `website/scripts/generate-llms-txt.py` | 92, 119 | Remove `user-guide/messaging/telegram` and `guides/team-telegram-assistant` from llms.txt generation |

---

## 3. Config surface

### 3a. Operator env vars to remove from `~/.hermes/.env`

| Variable | Source citation |
|----------|----------------|
| `TELEGRAM_BOT_TOKEN` | `gateway/config.py:1195` |
| `TELEGRAM_WEBHOOK_URL` | `telegram.py:1228` |
| `TELEGRAM_WEBHOOK_PORT` | `telegram.py:1383` |
| `TELEGRAM_WEBHOOK_SECRET` | `telegram.py:1384` |
| `TELEGRAM_ALLOWED_USERS` | `telegram.py:557` |
| `TELEGRAM_ALLOW_ALL_USERS` | `tests/conftest.py:228` |
| `TELEGRAM_HOME_CHANNEL` | `hermes_cli/config.py:102` |
| `TELEGRAM_HOME_CHANNEL_THREAD_ID` | `hermes_cli/config.py:102` |
| `TELEGRAM_HOME_CHANNEL_NAME` | `hermes_cli/config.py:102` |
| `TELEGRAM_REQUIRE_MENTION` | `tests/conftest.py:285` |
| `TELEGRAM_PROXY` | `telegram.py:1295` |
| `HERMES_TELEGRAM_MEDIA_BATCH_DELAY_SECONDS` | `telegram.py:442` |
| `HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS` | `telegram.py:454` |
| `HERMES_TELEGRAM_TEXT_BATCH_SPLIT_DELAY_SECONDS` | `telegram.py:460` |
| `HERMES_TELEGRAM_HTTP_POOL_SIZE` | `telegram.py:1277` |
| `HERMES_TELEGRAM_HTTP_POOL_TIMEOUT` | `telegram.py:1278` |
| `HERMES_TELEGRAM_DISABLE_FALLBACK_IPS` | `telegram.py:1284` |

### 3b. Operator config files to scrub

| File | Action |
|------|--------|
| `~/.hermes/config.yaml` | Remove `platforms.telegram.*` keys (shape at `telegram.py:1077`): `dm_topics`, `extra.dm_topics`, any `telegram:` platform block |
| `cli-config.yaml.example:647` | Remove `hermes-telegram` default toolset reference |

---

## 4. Docs surface

| File | Line(s) | Current text | Replacement |
|------|---------|-------------|-------------|
| `~/.hermes/WORKFLOW-LESSONS.md` | 111 | "Always wire `telegram-notify.sh` into every hive watcher" | "Always wire `discord-notify.sh` into every hive watcher" (`discord-notify.sh` already active per cluster B §"Wired into operator launch templates — CONFIRMED") |
| `~/.hermes/WORKFLOW-LESSONS.md` | 243 | "Telegram-notify in every watcher." | "Discord-notify in every watcher." |
| `~/.hermes/OPERATOR-RUNBOOK.md` | 7 | "telegram watcher wired" in paragraph | Replace with "discord-notify watcher wired" |
| `~/.hermes/OPERATOR-RUNBOOK.md` | 157 | `[[ -x "$HOME/.hermes/scripts/telegram-notify.sh" ]]` preflight check | Replace with `discord-notify.sh` check |
| `~/.hermes/OPERATOR-RUNBOOK.md` | 565 | `telegram-notify.sh wired in watcher` checkbox | Replace with `discord-notify.sh wired in watcher` |
| `AGENTS.md` | 37 | `platforms/` dir note lists `telegram` | Remove `telegram` from adapter list |
| `AGENTS.md` | 159 | "Telegram — `telegram_bot_commands()` generates the BotCommand menu" section | Remove entirely |
| `AGENTS.md` | 938 | "See `gateway/platforms/telegram.py` for the canonical pattern." | Replace with "See `gateway/platforms/discord.py` for the canonical pattern." |
| `CONTRIBUTING.md` | 141 | `toolsets.py` comment mentions `hermes-telegram` | Remove `hermes-telegram` |
| `CONTRIBUTING.md` | 187 | `telegram.py, discord_adapter.py, slack.py` in layout | Remove `telegram.py` |
| `gateway/platforms/ADDING_A_PLATFORM.md` | (review) | May use Telegram-specific examples as canonical | Replace examples with Discord equivalents; do NOT delete the guide — it serves other platforms |
| `website/sidebars.ts` | 605, 656 | `user-guide/messaging/telegram`, `guides/team-telegram-assistant` sidebar entries | Remove both entries |
| `website/docs/developer-guide/gateway-internals.md` | 151 | `telegram.py` in platforms dir listing | Remove line |
| `website/docs/user-guide/configuration.md` | 1295 | `telegram:` key example in config block | Remove `telegram:` block |
| `website/docs/user-guide/features/cron.md` | 224–226, 244, 311, 338, 371 | All `telegram` deliver-target rows and examples | Remove rows; keep Discord equivalents |
| `website/docs/user-guide/sessions.md` | 72, 173, 189, 266, 298, 338, 360, 402 | Telegram session examples and source filter | Remove Telegram-specific rows and examples |
| `website/docs/guides/automation-templates.md` | 47, 130, 155, 184, 264, 301, 326, 489, 519, 568, 572 | `--deliver telegram` in all template examples | Replace with `--deliver discord`; remove `telegram:CHAT_ID` rows from table |
| `website/docs/developer-guide/tools-runtime.md` | 100 | `hermes-telegram` in platform presets list | Remove |
| `hermes-already-has-routines.md` | 21, 80, 101, 105, 139 | `--deliver telegram` examples | Remove; replace lead example at line 101 with `--deliver discord` |
| `website/docs/guides/migrate-from-openclaw.md` | 135–136, 205–211 | Telegram migration rows and config examples | Remove entire Telegram migration section |
| `website/docs/user-guide/skills/bundled/devops/devops-webhook-subscriptions.md` | 91, 132, 156, 188, 197 | `--deliver telegram` in webhook subscription examples | Replace with `--deliver discord` |
| `website/docs/user-guide/features/kanban-tutorial.md` | 309 | `--platform telegram` in notify-subscribe example | Replace with `--platform discord` |
| `RELEASE_v0.5.0.md` | 109 | DNS fallback for `api.telegram.org` | Leave as-is — release history |
| `RELEASE_v0.7.0.md` | (multiple) | Telegram feature release notes | Leave as-is — release history |
| `locales/en.yaml` | 291 | `not_telegram_dm: "The /topic command is only available in Telegram private chats."` | Remove key (or replace if `/topic` is repurposed for Discord threads) |
| `locales/uk.yaml`, `zh.yaml`, `fr.yaml`, `ja.yaml`, `ko.yaml`, `hu.yaml`, `es.yaml`, `ga.yaml`, `de.yaml`, `pt.yaml`, `it.yaml`, `ru.yaml`, `zh-hant.yaml`, `af.yaml`, `tr.yaml` | 276 each | `not_telegram_dm` locale string | Remove key in all 15 locale files |

---

## 5. Memory surface

| Location | Current text | Action |
|----------|-------------|--------|
| `~/.claude/projects/-home-Josep--local-share-hermes-agent/memory/MEMORY.md:41` | "wire telegram-notify.sh into every watcher.sh" in feedback-hive-completions-must-push entry | Supersede via `mcp__mvms-writer__mvms_supersede` — update lesson text to reference `discord-notify.sh` |
| `~/.claude/projects/-home-Josep--local-share-hermes-agent/memory/feedback-hive-completions-must-push-not-wait.md` | References `telegram-notify.sh` as the required notifier | Supersede with updated lesson pointing to `discord-notify.sh`; add `superseded_by` pointer |
| `~/.claude/projects/-home-Josep--local-share-hermes-agent/memory/hermes-workflow-lessons-doc.md` | May echo WORKFLOW-LESSONS §3 rule #7 | Check; supersede if it contains the `telegram-notify.sh` rule verbatim |
| Any MVMS lesson or completion record citing `telegram-notify.sh` as required | — | Supersede (never delete) — MVMS history is preserved; add `superseded_by` pointer to discord-notify lesson |

---

## 6. SQLite migration story

The `hermes_state.py` migration block at lines 2387–2483 creates and manages:
- `telegram_dm_topic_mode` (with `idx_telegram_dm_topic_bindings_session` index)
- `telegram_dm_topic_bindings` (with `idx_telegram_dm_topic_bindings_user` index)
- Schema version key: `telegram_dm_topic_schema_version` in `_hermes_kv`

New migration to add in P5 ISA (`drop_telegram_dm_topic_tables`):

```python
# Backup first — store as ~/.hermes/backups/telegram-dm-topics-<ts>.sql via sqlite3 .dump
cursor.executescript("""
    DROP TABLE IF EXISTS telegram_dm_topic_bindings;
    DROP TABLE IF EXISTS telegram_dm_topic_mode;
    DELETE FROM _hermes_kv WHERE key = 'telegram_dm_topic_schema_version';
""")
```

Backup approach: per WORKFLOW-LESSONS §3 rule 5 ("rename-to-deleted" for reversibility), dump both tables to `~/.hermes/backups/telegram-dm-topics-<ts>.sql` before the DROP. SQLite DROP is irreversible in-place; the dump is the rollback path. The migration function replaces `apply_telegram_topic_migration` at the same call location so a fresh DB does not error.

---

## 7. Cache cleanup

`~/.hermes/cache/{images,audio,videos,documents}/` is shared — Discord uses the same paths (`base.py:546-891`). Do not purge wholesale. Telegram media files age out via the existing TTL. No Telegram-specific cache key prefix was found in the audit; no targeted purge is required. If a forced purge is needed post-P5, filter by origin metadata before deleting.

---

## 8. PR composition (suggested for P5)

| Commit | Scope |
|--------|-------|
| 1 | Delete `gateway/platforms/telegram.py`, `telegram_network.py`, all `tests/gateway/test_telegram_*.py` (23 files) |
| 2 | Code edits — all EDIT rows in §2b |
| 3 | Config and env scrub — `.env.example`, `cli-config.yaml.example`, operator `~/.hermes/.env` and `config.yaml` |
| 4 | Docs and locale updates — all rows in §4 |
| 5 | SQLite migration — new `drop_telegram_dm_topic_tables` migration + backup script |
| 6 | MVMS supersede — run `mcp__mvms-writer__mvms_supersede` for lessons in §5 (document in PR description, not a git commit) |

---

## 9. Verification (after P5 lands)

| Check | Expected result |
|-------|----------------|
| `grep -rn "telegram" $HOME/.local/share/hermes-agent/ --include="*.py"` | Zero hits outside `RELEASE_v*` and `optional-skills/migration/` historical prose |
| `grep -rn "TelegramAdapter" $HOME/.local/share/hermes-agent/ --include="*.py"` | Zero hits |
| `python -c "from gateway.platforms.telegram import TelegramAdapter"` | `ModuleNotFoundError` |
| `python -c "import python_telegram_bot"` | `ModuleNotFoundError` (package uninstalled) |
| `grep -rn "telegram" $HOME/.local/share/hermes-agent/ --include="*.md"` | Hits in `RELEASE_v*` only |
| `grep -rn "TELEGRAM_" $HOME/.local/share/hermes-agent/ --include="*.py"` | Zero hits |
| Bot startup with `TELEGRAM_BOT_TOKEN` set in env | Does not crash — no code reads the var anymore |
| `hermes doctor` / `hermes status` | No Telegram row in platform status output |
| `locales/en.yaml` | No `not_telegram_dm` key |

---

## 10. Additional files found by grep (not in cluster B top-30)

| File | Lines | Nature |
|------|-------|--------|
| `agent/redact.py` | 129, 356, 360 | `_TELEGRAM_RE` + `_redact_telegram` — redacts bot tokens from logs |
| `gateway/run.py` | 69–83 | `_telegramize_command_mentions` — Telegram-specific command mention formatter |
| `gateway/run.py` | 1704–1768 | Four DM-topic-mode helper methods |
| `gateway/run.py` | 5296–5630 | Adapter instantiation + group-users legacy warning |
| `gateway/display_config.py` | 84 | Telegram display tier config |
| `gateway/channel_directory.py` | 355 | Bare-platform-name docstring example |
| `gateway/mirror.py` | 96 | Session-key comment |
| `gateway/stream_consumer.py` | 847 | Library version comment |
| `gateway/platforms/webhook.py` | 14, 173, 242, 765 | Four deliver-target references |
| `tools/send_message_tool.py` | 532, 555, 779–802 | Four `TelegramAdapter` import+use sites |
| `tools/lazy_deps.py` | 118 | Lazy-install dependency entry |
| `tools/cronjob_tools.py` | 194–198, 593 | Deliver-target docstrings |
| `hermes_cli/commands.py` | 474–746 | Full Telegram command menu machinery |
| `hermes_cli/config.py` | 102, 2319–2333, 4961, 5044 | Config key definitions and export |
| `hermes_cli/dump.py` | 101 | Platform dump mapping |
| `hermes_cli/setup.py` | 1967–2035, 2516 | Interactive setup wizard |
| `hermes_cli/status.py` | 390 | Status health check |
| `cli.py` | 7599, 7634 | CLI platform table |
| `agent/prompt_builder.py` | 422 | Platform prompt map |
| `agent/skill_utils.py` | 125 | Skill util docstring |
| `agent/memory_provider.py` | 70 | Memory provider docstring |
| `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` | 1343–1393 | Migration helper reads Telegram config from OpenClaw |
| `locales/*.yaml:276` (15 files) | 276 each | `not_telegram_dm` locale string across all language packs |
| `website/src/data/userStories.json` | 780, 1216, 2221, 2577 | User story quotes mentioning Telegram — leave as-is (user-generated historical content) |
| `tests/honcho_plugin/test_client.py` | 690–741 | Uses `telegram` session key strings as fixture data — replace with `discord` fixture |
| `~/.hermes/OPERATOR-RUNBOOK.md` | 7, 157, 565 | Three `telegram-notify.sh` references in runbook |

---

## 11. What this doc does NOT do

- Does not write the actual ISA — that is `isas/P5-hardening.md`
- Does not perform any edits — read-only inventory
- Does not delete MVMS memory entries — supersede only; history is preserved
- Does not touch `website/src/data/userStories.json` user quotes — historical user-generated content

---

## 12. Sources

| Source | Citation |
|--------|----------|
| `audits/cluster-B-gateway-discord.md` | §"Telegram-specific plumbing (deprecation surface)", §"Inventory of telegram references" (4778 total hits) |
| `DESIGN.md` | §10 Telegram retirement |
| `~/.hermes/WORKFLOW-LESSONS.md:111,243` | Rule #7 `telegram-notify.sh` |
| `~/.hermes/OPERATOR-RUNBOOK.md:7,157,565` | `telegram-notify.sh` references in runbook |
| `gateway/platforms/telegram.py:317,1077,1228–1397` | Class declaration, config persistence, env var consumers |
| `hermes_state.py:2387–2483` | SQLite migration machinery |
| `pyproject.toml:84,130,141` | Dependency declarations |
| `Dockerfile:98` | Lazy-install comment |
| `grep -rn "telegram" $HOME/.local/share/hermes-agent/ --include="*.py" \| head -80` | Run 2026-05-24; sampled hit: `agent/redact.py:356 text = _TELEGRAM_RE.sub(_redact_telegram, text)` |
| `grep -rn "telegram" $HOME/.local/share/hermes-agent/ --include="*.md" \| head -50` | Run 2026-05-24; sampled hit: `hermes-already-has-routines.md:101 --deliver telegram` |
| `grep -rn "telegram" $HOME/.local/share/hermes-agent/ --include="*.yaml" --include="*.toml" --include="*.json" \| head -30` | Run 2026-05-24; sampled hit: `locales/en.yaml:291 not_telegram_dm` string in 15 locale files |
| `grep -rn "TelegramAdapter" $HOME/.local/share/hermes-agent/ --include="*.py"` | Run 2026-05-24; 4 import sites in `send_message_tool.py`; instantiation in `gateway/run.py:5296` |
| `grep -rn "TELEGRAM_" $HOME/.local/share/hermes-agent/ --include="*.py"` | Run 2026-05-24; 17 distinct env vars across `tests/conftest.py`, `hermes_cli/`, `cli.py` |
