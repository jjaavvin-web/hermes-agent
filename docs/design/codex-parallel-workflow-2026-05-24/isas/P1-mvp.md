---
isa:      20260524-2000_codex-parallel-p1-mvp
task:     "P1 MVP — Discord gateway dispatcher + Worktree broker + one Codex session per thread (manual peer-review handoff)"
tier:     E3
phase:    scaffold
progress: 0/18
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p1-mvp
hive:     "-"
owner:    ruflo-hive
started:  2026-05-24T20:00:00Z
updated:  2026-05-24T20:00:00Z
---

## Problem

Tier-2 work (the bounded, fan-out-able middle slice of Joseph's three-tier execution model — `~/.hermes/ruflo-work/codex-parallel-design-20260524T193752Z/DESIGN.md` §1) has no execution substrate today. The Discord gateway adapter (`gateway/platforms/discord.py:532`) exists for chat but does not route messages to per-thread Codex sessions. The Codex session adapter (`agent/transports/codex_app_server_session.py:202-272`) exists but is invoked only from `AIAgent._run_codex_app_server_turn()` (`run_agent.py:16050-16168`) in a single-session-per-AIAgent model. There is no worktree-per-session isolation, no Discord-thread-to-session mapping, and no operator surface for the parallel fleet.

This ISA delivers the minimum substrate so that an operator can open a Discord thread, get a dedicated Codex session running in an isolated git worktree, hold a multi-turn conversation, and manually trigger a peer review when ready to land. Auto-review, auto-merge, dashboard, and gc are out of scope for P1 — those land in P2-P5.

## Goal

After this ISA: an operator opens a thread in the configured Discord channel, the bot allocates a fresh git worktree at `~/.hermes/codex-wt/<sid>/`, spawns a Codex session inside a tmux session `codex-sess-<sid>`, persists the mapping to `~/.hermes/codex_sessions.json` (single bot writer + flock + atomic-tempfile-rename), and routes every subsequent thread message to that session's `run_turn`. The operator can run `/spawn`, `/status`, `/pause`, `/resume`, `/kill`, and `/handoff-to-ruflo` slash commands. When the operator decides the work is ready for review, they manually trigger an Opus review (out-of-band — operator opens a tmux pane and runs `claude` themselves — automation lands in P2). Bot restart re-attaches to existing tmux sessions by reading `codex_sessions.json` and intersecting with `tmux ls`.

## Out of Scope

- Auto-trigger of Opus peer review on `phase: verify` — that's P2.
- Pane-pool warm/busy/dead lifecycle for review — P2.
- Auto-merge / merge broker / PR labelling — P3.
- `/codex-sessions` dashboard tab + SSE wiring — P4.
- Worktree gc (background reaper) — P5.
- Telegram deletion + doc scrubs — P5 (see `telegram-retirement-appendix.md`).
- Sub-agent (parent/child Codex) decomposition — design supports it (DESIGN §2 Decision B) but P1 ships single-session-per-thread only.

## Constraints

- **PR #34 must merge to `fork/main` first.** Tier 2 depends on `scripts/isa_lint.py`, `scripts/isa_reconcile.py`, `scripts/isa_common.py` being available on the merge-target branch so the merge broker (P3) can gate on CheckCompleteness. Without it the gate is unenforceable. (DESIGN §11.)
- **First ISC re-implements the kanban-bridge ISA gate** that was skipped during PR #34 cherry-pick — see ISC-1 below.
- **No `claude -p`, no Agent SDK, no paid Anthropic API anywhere in this codepath** — interactive Claude via tmux only. Manual peer-review handoff (P1) uses the operator's own tmux pane; the orchestrator (P2) extends this. PROVIDER-STACK.md §"2026-06-15 — Anthropic billing change".
- **`~/.hermes/discord_threads.json` must remain unchanged** — it is production-active for `ThreadParticipationTracker` (`tests/gateway/test_discord_thread_persistence.py:1-95`). Session state goes in NEW file `~/.hermes/codex_sessions.json`.
- **Branch name format is immutable**: `codex/<sid>/<isa-slug>` where `<sid>` is a UUID4 assigned once. Never reused.
- **No destructive shell** (`rm -rf`, `git clean -fxd`) per WORKFLOW-LESSONS §3 rule 5. Use rename-to-deleted-<ts> patterns where deletion is needed.
- **Outbound channel reuse**: agent→Discord messages must go through `tools/discord_tool.py` (`discord_tool.py:908`); do not duplicate the REST client.
- **Files under 500 lines per `CLAUDE.md` rule** — split modules if needed.
- **Discord bot token comes from `DISCORD_BOT_TOKEN`** env var (`gateway/config.py:1195`); same as outbound tool (`discord_tool.py:53-55`).

## Criteria

- [ ] ISC-1: `scripts/claude_kanban_bridge.py` is re-implemented against current `fork/main` and gates `kanban_complete` on the linked ISA reaching `phase: complete` per `isa_lint.py` (re-introduces the cherry-pick that was skipped during PR #34 per objective §16)
- [ ] ISC-2: a new file `gateway/codex_session_dispatcher.py` exists implementing `CodexSessionDispatcher` per `module-specs/discord-gateway.md` §3
- [ ] ISC-3: `gateway/platforms/discord.py` has hooks at thread_create, message, thread_update(archived), and on_ready wired to the dispatcher per `module-specs/discord-gateway.md` §7 (no more than the 4 hooks listed)
- [ ] ISC-4: a new file `agent/worktree_broker.py` exists implementing `WorktreeBroker.allocate / release / status / free_port` (NO `gc` — that's P5) per `module-specs/worktree-broker.md` §3
- [ ] ISC-5: `~/.hermes/codex_sessions.json` is written with the schema in `module-specs/discord-gateway.md` §5 (flock + atomic-tempfile-rename, mirroring `telegram.py:1077-1133` `atomic_replace`)
- [ ] ISC-6: `~/.hermes/codex-ports.json` port broker is implemented (range 50000-50007, flock, returns null entries as recovery on init when sid is absent from codex_sessions.json) per `module-specs/worktree-broker.md` §4
- [ ] ISC-7: opening a Discord thread in the configured channel allocates a worktree, starts a tmux session `codex-sess-<sid>`, spawns a hermes process inside with `HERMES_KANBAN_TASK=<kid>` env, and writes the session row to `codex_sessions.json` — verified end-to-end with a live Discord thread
- [ ] ISC-8: posting a follow-up message in that thread triggers `run_turn` on the existing session (not a fresh session) — verified by capturing the tmux pane and checking the message lands on the session prompt
- [ ] ISC-9: thread archive on Discord triggers `WorktreeBroker.release(sid)`, kills the tmux session, removes the worktree, removes the row from `codex_sessions.json`, and returns the port to the broker
- [ ] ISC-10: slash commands `/spawn`, `/status`, `/pause`, `/resume`, `/kill`, `/handoff-to-ruflo` are registered with Discord and respond per `module-specs/discord-gateway.md` §4
- [ ] ISC-11: bot restart with a live `codex-sess-<sid>` tmux session and a row in `codex_sessions.json` re-binds the dispatcher to that session — verified by killing the bot, restarting it, posting a message in the thread, and confirming the message lands on the same codex thread (codex shows session continuity)
- [ ] ISC-12: bot restart with a `codex_sessions.json` row whose tmux session is GONE marks the session as NEEDS_REVIVE and posts a banner in the Discord thread with `/revive` instructions (the `/revive` handler itself is P5; P1 only posts the banner)
- [ ] ISC-13: `python3 scripts/isa_lint.py ~/.hermes/work/<isa-id>/ISA.md` exit 0 against this ISA in `phase: complete`
- [ ] ISC-14: at least 4 concurrent threads can run simultaneously without file/branch/port collision — verified by spawning 4 threads, observing 4 distinct worktrees + tmux sessions + ports, and confirming `git -C <repo> branch --list "codex/*"` lists 4 distinct branches
- [ ] ISC-15: Anti: NO `claude -p`, `claude --print`, `--non-interactive`, or Agent SDK invocation appears in any new file — grep proves it
- [ ] ISC-16: Anti: NO new write to `~/.hermes/discord_threads.json` from the dispatcher or any new module — grep proves the file appears only in pre-existing code (`ThreadParticipationTracker` + its test)
- [ ] ISC-17: Anti: NO `rm -rf`, `git clean -fxd`, or force-truncate via `>` in any new module — grep proves it
- [ ] ISC-18: Anti: existing `/api/dashboard/hives` routes still return correct JSON shape after P1 lands (no dashboard-side regression) — verified by `curl -s -H "X-Hermes-Session-Token: $TOK" :9119/api/dashboard/hives | jq '.hives | length'` returns a non-error number

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `pytest tests/scripts/test_claude_kanban_bridge.py -k 'isa_gate'` | all pass |
| ISC-2 | `python -c "from gateway.codex_session_dispatcher import CodexSessionDispatcher; print(CodexSessionDispatcher)"` | prints the class |
| ISC-3 | `grep -nE 'codex_session_dispatcher\|on_thread_create\|on_thread_update' gateway/platforms/discord.py \| wc -l` | ≥ 4 hits |
| ISC-4 | `python -c "from agent.worktree_broker import WorktreeBroker; b = WorktreeBroker(repo_root=..., hermes_home=...); [getattr(b,m) for m in ('allocate','release','status','free_port')]"` | no AttributeError |
| ISC-5 | start dispatcher; allocate; cat `~/.hermes/codex_sessions.json` | valid JSON; schema matches spec |
| ISC-6 | allocate 2 sessions; cat `~/.hermes/codex-ports.json` | 2 ports claimed, 6 null |
| ISC-7 | open a Discord thread in configured channel, wait 5s, run `tmux ls \| grep codex-sess-` and `ls ~/.hermes/codex-wt/` | 1 new session + 1 new worktree |
| ISC-8 | post second message in the thread, `tmux capture-pane -p -t codex-sess-<sid> \| tail -20` | message text appears on session prompt |
| ISC-9 | archive the Discord thread, wait 5s, run `tmux ls` and `ls ~/.hermes/codex-wt/` | session gone, worktree gone |
| ISC-10 | invoke each slash command; check response | each returns expected behavior per spec §4 |
| ISC-11 | kill the bot process, restart, post message in existing thread, `tmux capture-pane -p -t codex-sess-<sid>` | message lands on same codex thread |
| ISC-12 | with a live session row + dead tmux: restart bot; check Discord thread | "needs revive" banner posted |
| ISC-13 | `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2000_codex-parallel-p1-mvp/ISA.md ; echo $?` | `0` |
| ISC-14 | open 4 Discord threads back-to-back; after 30s: `tmux ls \| grep -c codex-sess-`, `ls ~/.hermes/codex-wt/ \| wc -l`, `git -C <repo> branch --list 'codex/*' \| wc -l`, `cat ~/.hermes/codex-ports.json \| jq '[.[] \| select(. != null)] \| length'` | all 4 |
| ISC-15 | `grep -rnE 'claude -p\|claude --print\|--non-interactive\|claude_code_sdk\|anthropic\.AsyncAnthropic' gateway/codex_session_dispatcher.py agent/worktree_broker.py scripts/claude_kanban_bridge.py` | 0 hits |
| ISC-16 | `grep -rn 'discord_threads.json' gateway/codex_session_dispatcher.py agent/worktree_broker.py scripts/claude_kanban_bridge.py` | 0 hits |
| ISC-17 | `grep -rnE 'rm -rf\|git clean -fxd\|>\\s*[^>=&|]\|truncate' gateway/codex_session_dispatcher.py agent/worktree_broker.py scripts/claude_kanban_bridge.py` | 0 hits |
| ISC-18 | `curl -s -H "X-Hermes-Session-Token: $TOK" :9119/api/dashboard/hives \| jq '.hives \| length'` | non-error number |

## Git Plan

- **Branch**: `feat/codex-parallel-p1-mvp` off `fork/main` (after PR #34 merges).
- **Prerequisite branch state**: `fork/main` must contain `scripts/isa_lint.py`, `scripts/isa_reconcile.py`, `scripts/isa_common.py` (i.e. PR #34 merged). If not, this ISA is BLOCKED — surface in `## Decisions` and stop.
- **Commit cadence (per WORKFLOW-LESSONS §1.10 + ISA-SPEC §10 — commit early and incrementally; never let an ISA's work accumulate as one uncommitted blob)**:
  1. `chore(isa): scaffold P1 ISA + work dir` — create `~/.hermes/work/20260524-2000_codex-parallel-p1-mvp/ISA.md` (also lives at `isas/P1-mvp.md` in this PR for review)
  2. `feat(kanban): re-implement claude_kanban_bridge ISA gate (ISC-1)`
  3. `feat(worktree): WorktreeBroker allocate/release + port broker (ISC-4, ISC-5, ISC-6)`
  4. `feat(dispatcher): CodexSessionDispatcher base class + state persistence (ISC-2)`
  5. `feat(gateway): wire 4 DiscordAdapter hooks to dispatcher (ISC-3)`
  6. `feat(dispatcher): slash command surface (ISC-10)`
  7. `feat(dispatcher): bot-restart reattach via tmux ls + codex_sessions.json (ISC-11, ISC-12)`
  8. `test(p1): end-to-end Discord-thread → codex-session integration test (ISC-7, ISC-8, ISC-9, ISC-14)`
  9. `docs(p1): operator notes + ENV vars in .env.example` (do NOT add Telegram-related notes; P5 will scrub them)
- **Push**: `git push fork feat/codex-parallel-p1-mvp` after each commit (per the early-and-incremental rule).
- **PR**: open against `fork/main` titled `feat(p1): Codex parallel workflow MVP — Discord dispatcher + worktree broker`. Description includes ISA path (`~/.hermes/work/20260524-2000_codex-parallel-p1-mvp/ISA.md`) + a check-list mirroring the ISC list.
- **Do NOT merge** until `phase: complete` per the ISA gate (ISC-13).
- **Mergify / auto-merge label** does NOT apply to this PR — P1 changes are sensitive (new modules under `gateway/`, `agent/`, `scripts/`) per `module-specs/merge-broker.md` §5 classification.

## Decisions

_(filled during execute)_

## Changelog

_(filled on each correction — 4-tuple format per ISA-SPEC §8)_

## Verification

_(filled during verify — probe output pasted verbatim, one block per [x] ISC)_

## Handback

- On complete: `mvms_record_completion` with project `codex-parallel-workflow` linking branch + PR + this ISA path.
- For each Changelog entry: `mvms_record_lesson` under project `codex-parallel-workflow`.
- Discord notification via `~/.hermes/scripts/discord-notify.sh` (NOT `telegram-notify.sh` — already deprecated in launch templates per `audits/cluster-B-gateway-discord.md` §"discord-notify.sh contract").
- Kanban: `kanban_complete <card>` via `tools/kanban_tools.py:360`. The ISA gate from ISC-1 should make this a no-op if the ISA isn't actually complete.
