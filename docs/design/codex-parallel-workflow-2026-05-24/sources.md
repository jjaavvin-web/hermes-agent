# Sources — Hermes-Codex Parallel Workflow Design

Every Hermes file:line examined and every external URL fetched during the design hive.

---

## 1. Hermes codebase — read end-to-end

| File | Lines audited | Purpose | Cluster |
|------|--------------|---------|---------|
| `agent/transports/codex_app_server_session.py` | 1–811 | Codex session adapter — `ensure_started`, `run_turn`, `close`; subprocess lifecycle; no PID-reattach; not thread-safe (single caller) | A |
| `agent/transports/codex_app_server.py` | 1–369 | JSON-RPC 2.0 client; reader threads; `_pending` lock; subprocess spawn at lines 86-93 | A |
| `agent/transports/codex_event_projector.py` | 1–50+ | item/* → OpenAI-shaped message projection | A |
| `agent/transports/__init__.py` | 1–69 | Transport registry (codex_app_server_session NOT registered there) | A |
| `run_agent.py` | 16050–16168+ | `AIAgent._run_codex_app_server_turn` — only production consumer of CodexAppServerSession; lazy session per AIAgent | A |
| `gateway/__init__.py` | 1–36 | Gateway module init | B |
| `gateway/platforms/base.py` | 916–999, 1336–1466, 1526–1606, 1538–1572, 3612–3632, 32–33, 57, 78 | Abstract platform interface (connect/disconnect/send/get_chat_info); MessageEvent dataclass; runtime status writer | B |
| `gateway/platforms/telegram.py` | 22–60, 163–188, 264–314, 317, 333, 341, 427–429, 443–470, 472–489, 802, 882, 926, 985–1052, 1077–1133, 1135, 1228–1397, 1847, 2013, 2032, 5140 (full file extent) | Telegram adapter — full deprecation surface inventory (delete in P5) | B |
| `gateway/platforms/discord.py` | 30, 49, 532, 587, 632, 853, 901–926, 932, 1370, 2749, 5169+ | Discord adapter — EXISTS (~5169 LOC); `class DiscordAdapter` at line 532; ThreadParticipationTracker integration | B |
| `gateway/platforms/helpers.py` | 27 | `MessageDeduplicator`, `ThreadParticipationTracker` — in-process TTL 300s + disk-backed thread participation | B |
| `gateway/config.py` | 1195, 1275 | Platform→env-var mapping (TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN) | B |
| `tools/discord_tool.py` | 40, 53–55, 138, 351–391, 427–454, 744, 748, 818, 898–901, 908, 913 | Outbound Discord REST tool (~960 LOC); `_get_bot_token` reads `DISCORD_BOT_TOKEN`; `_create_thread` supports both standalone + message-attached threads | B |
| `tools/kanban_tools.py` | 115–144, 229, 300, 360, 438, 473, 521, 554, 630, 655, 1060–1139 | Kanban agent-facing tool API; `_enforce_worker_task_ownership` at 115; `kanban_comment` author locked to HERMES_PROFILE at 521 | C |
| `hermes_cli/kanban_db.py` | 64–66, 97–101, 753–882, 927–928, 980–986, 1101–1153, 1329–1340, 1876–2003, 2006–2116, 4044–4139 | Kanban SQLite + CAS; `claim_task` BEGIN IMMEDIATE + `UPDATE WHERE status='ready' AND claim_lock IS NULL` at 1922-1931; heartbeat CAS at 1989-2003; subprocess launch at 4131-4139; env injection at 4044-4089 | C |
| `agent/memory_manager.py` | 200–226, 231–242, 285–326, 483–511, 538–555 | Memory plugin layer — single external provider; sequential per-provider sync; NO internal lock | C |
| `agent/transports/hermes_tools_mcp_server.py` | 41 | Comment-only reference to CodexAppServerSession lifecycle | A |
| `tests/agent/transports/test_codex_app_server_session.py` | 17, 107–108 | Unit tests with FakeClient injection | A |
| `tests/run_agent/test_codex_app_server_integration.py` | 20, 46, 48, 168, 170, 312, 314, 335, 337, 369–372, 406–409 | Integration test surface for codex session paths | A |
| `tests/gateway/test_discord_thread_persistence.py` | 1–95 (full) | Pre-existing tests proving `~/.hermes/discord_threads.json` is production-written by `ThreadParticipationTracker` (joined-thread cap-list, not session state) | B |
| `hermes_cli/web_server.py` | 92, 132–170, 1378, 1405, 3284, 4308–4369 | Pulse SSE — `_SESSION_TOKEN` ephemeral; `_QUERY_TOKEN_PATHS` allowlist for EventSource; `api_pulse_stream` at 4308; auth middleware at 132-168 | D |
| `hermes_cli/dashboard_health.py` | 27–56, 30, 444–738, 447–458, 471–478, 486–500, 512–520, 523–546, 550–560, 624–678, 696–738, 1997–2030, 2016–2029 | `/api/dashboard/hives*` template; filesystem-only state source; 15s cache; tmux subprocess pattern | D |
| `hermes_cli/git_janitor.py` | 50–55, 63–100, 266 | Canonical subprocess-git pattern: `subprocess.run(["git","-C",repo,*args], capture_output=True, text=True, check=False)`; `inventory_worktrees` via `git worktree list --porcelain` at 68 | D |
| `hermes_cli/pulse_data.py` | 341 | Existing pulse_activity_iter consumer point | D |
| `hermes_cli/main.py` | 1224–1236, 7524, 7861 | `hermes -w` worktree mode (`_setup_worktree`/`_cleanup_worktree`) | D |
| `hermes_cli/cli.py` | 816–980 | `--worktree`/`-w` flag handler with `git worktree add` | D |
| `hermes_state.py` | 14, 2387–2416 | Telegram DM topic tables (DROP in P5 — `telegram_dm_topic_mode`, `telegram_dm_topic_bindings`); session source tagging doc | B |
| `toolsets.py` | 261, 267, 400, 406, 533 | `discord`, `discord_admin`, `hermes-discord`, `hermes-telegram` toolset definitions | B |
| `pyproject.toml` | 84, 130 | `discord.py[voice]==2.7.1`, `python-telegram-bot[webhooks]==22.6` deps | B |
| `uv.lock` | 1136 | `discord-py` package lock | B |
| `Dockerfile` | 98 | Lazy-install telegram-at-boot comment (scrub in P5) | B |
| `AGENTS.md` | 37, 706, 938 | Platforms dir notes; tool listing | B |
| `CONTRIBUTING.md` | 141, 187 | Layout references | B |
| `cli-config.yaml.example` | 642, 647, 659, 687, 723 | Discord + Telegram defaults | B |
| `.env.example` | 345 | TELEGRAM_WEBHOOK_URL example | B |
| `mcp_serve.py` | 483, 745 | Filter param docs + examples | B |
| `RELEASE_v0.7.0.md` | 105 | Discord `reactions` config | B |
| `RELEASE_v0.3.0.md` | 115 | Defer-discord-adapter annotation | B |
| `hermes-already-has-routines.md` | 101, 102 | `--deliver telegram` / `--deliver discord` examples | B |
| `website/sidebars.ts` | 606 | Discord docs sidebar | B |
| `website/src/components/UserStoriesCollage/index.tsx` | 128 | UI Discord mention | B |
| `README.zh-CN.md` | 9 | Discord community badge | B |
| `scripts/isa_lint.py` (branch `feat/isa-enforcement-clean`, PR #34) | 39–180, 44, 64, 69, 75, 83, 91, 99, 103, 111, 117, 126, 141, 156, 164 | 13 lint checks; pass condition `len(failures) == 0`; CLI at the bottom | C |
| `scripts/isa_reconcile.py` (PR #34) | 146–261, 177, 185–192 | ID-keyed slice merge; DRIFT detection; "last slice wins" on state conflict | C |
| `scripts/isa_common.py` (PR #34) | 50–53, 56–67, 70, 78, 395–414 | Required frontmatter fields; tier-section mapping; ISC regex; `find_isa_for_card` | C |
| `~/.hermes/discord_threads.json` | full (20 entries) | Production-active: `ThreadParticipationTracker` cap-list of joined thread IDs (not session metadata) | B |
| `~/.hermes/scripts/discord-notify.sh` | 1–115 (full) | Outbound Discord REST notify; env vars DISCORD_BOT_TOKEN + DISCORD_NOTIFY_CHANNEL_ID/DISCORD_HOME_CHANNEL; truncates at 1850 chars | B |
| `~/.hermes/scripts/templates/ruflo-launch-interactive.template.sh` | 1–167 (full) | Interactive Claude tmux launch template — basis for Opus pane pool (lines 104–145 dialog-clearing) | D |
| `~/.hermes/ISA-SPEC.md` | 1–307 (full) | ISA-SPEC §2 canonical home; §3 frontmatter; §4 sections; §5 tiers; §6 ISCs; §7 reconcile; §8 changelog; §9 CheckCompleteness; §10 stack integration; §13 enforcement layer | C |
| `~/.hermes/WORKFLOW-LESSONS.md` | 1–250 (full) | §1 lessons; §3 hard rules (incl. rule #7 telegram-notify → discord-notify); §4 patterns (4.3 master prompt, 4.6 confirmation tokens); §6 environment notes (6.1 dashboard auth, 6.3 ruflo binary) | D |
| `~/.hermes/PROVIDER-STACK.md` | 1–97 (full) | §"2026-06-15 — Anthropic billing change" clause (lines 7-16); role assignments (lines 36-47); h2reviewer paid-API misroute (lines 76-79) | D |

---

## 2. Codebase greps

- `grep -rn "telegram"` across hermes-agent: 4778 hits (cluster B §"Inventory of telegram references")
- `grep -rn "discord"` across hermes-agent: 3974 hits (cluster B §"Inventory of discord references")
- `grep -rn "HERMES_KANBAN_TASK"` — env-var consumers across kanban + agent code (cluster C audit)
- `grep -rn "memory_manager"` — call sites in `agent/` (cluster C audit)
- `grep -rn "git worktree"` — existing usages: `git_janitor.py:63-100`, `main.py:1224-1236`, `cli.py:816-980` (cluster D)

---

## 3. External URLs fetched (via WebFetch / WebSearch)

Full snippets and verdicts in `audits/external-research.md`. URLs:

| URL | Topic | Verdict |
|-----|-------|---------|
| https://www.mindstudio.ai/blog/parallel-ai-coding-agents-git-worktrees | Git worktree per AI agent — production patterns | works |
| https://github.com/ComposioHQ/agent-orchestrator | Agent orchestrator with per-agent worktrees + PRs | works |
| https://pnpm.io/next/git-worktrees | pnpm globalVirtualStore for worktrees | works |
| https://pnpm.io/faq | pnpm content-addressable store concurrency | works |
| https://dev.to/stevengonsalvez/claude-squad-run-multiple-ai-agents-in-parallel-without-the-mess-1hfl | Claude Squad — closest prior art for the design | works (reference) |
| https://tmux.app/sessions/ | tmux session durability across terminal close | works |
| https://thegamecracks.github.io/discord.py/persistent_views.html | discord.py persistence patterns for stateless bot | works-with-caveat (durable map required) |
| https://github.com/laxerhd/discord-tmux-mc-bot | discord.py + tmux send-keys prior art | works (simple) |
| https://developers.openai.com/codex/app-server | OpenAI Codex App Server — `thread/resume` RPC documented | RPC exists; Hermes adapter doesn't use it today |
| https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md | App server README — `thread/resume` params + response shape | confirmed |
| https://deepwiki.com/openai/codex/4.4-app-server-and-json-rpc-protocol | App server protocol — thread persistence via rollout files + state DB | confirmed; storage-path continuity is the real constraint |
| https://mergify.com/blog/github-auto-merge-when-native-is-enough/ | GitHub native auto-merge vs Mergify | works-with-caveat (native has no label-gating) |
| https://github.com/marketplace/actions/enable-pull-request-automerge | GitHub Action for `gh pr merge --auto` triggered by label | acceptable alternative |
| https://mergify.com/alternative/kodiak | Kodiak status (May 2026) | unmaintained — do not use |
| https://space-node.net/blog/discord-bot-silent-death-24-7-supervision | Bot process supervision (PM2/systemd) | works (industry standard) |
| https://dev.to/nickytonline/automate-and-merge-pull-requests-using-github-actions-and-the-github-cli-4lo6 | Actions + `gh pr merge --auto` pattern | works |
| https://www.codeant.ai/blogs/top-pull-request-automation-tools | 2026 tooling survey | confirms Kodiak unmaintained |

---

## 4. Audit reports produced by this hive (siblings to this doc)

- `audits/cluster-A-codex-sessions.md` — Codex session + transports
- `audits/cluster-B-gateway-discord.md` — gateway / Telegram / Discord tool
- `audits/cluster-C-kanban-memory-isa.md` — Kanban + Memory + ISA
- `audits/cluster-D-worktree-dashboard.md` — Worktree / Dashboard SSE / Interactive-Claude template
- `audits/external-research.md` — RQ1-RQ5 (worktree, discord.py, codex thread/resume, pnpm, auto-merge)

---

*Every architectural claim in `DESIGN.md`, the per-module specs, and the P1-P5 ISAs traces to one of the rows above. If a reviewer finds an unsupported claim, the citation is missing — open an issue.*
