---
isa:      20260524-2040_codex-parallel-p5-hardening
task:     "P5 Hardening — worktree gc, bot/tmux revive flow, Telegram retirement (delete adapter + scrub docs)"
tier:     E3
phase:    scaffold
progress: 0/19
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p5-hardening
hive:     "-"
owner:    ruflo-hive
started:  2026-05-24T20:40:00Z
updated:  2026-05-24T20:40:00Z
---

## Problem

After P1-P4 the pipeline is functional but unhardened. Three classes of debt accumulate:

1. **Worktree orphans.** P1 ships `WorktreeBroker.allocate` / `release` but not `gc`. Sessions killed externally, or whose worktrees survive a bot crash where the release path didn't run, leave directories under `~/.hermes/codex-wt/` that grow with no upper bound.
2. **`NEEDS_REVIVE` is half-wired.** P1 detects the state and posts a banner; P5 ships the actual `/revive` slash command that re-allocates a fresh session under the existing Discord thread, archives the previous ISA progress as `_ephemeral/orphaned-<ts>.md`, and resumes work.
3. **Telegram is still live.** Per the locked decision (objective §4), Telegram is being retired. The inventory of changes is in `telegram-retirement-appendix.md`; P5's job is to execute that inventory — delete the adapter, scrub the docs, drop the SQLite tables (with backup-first per WORKFLOW-LESSONS §3 rule 5), and update WORKFLOW-LESSONS §3 rule #7 from `telegram-notify.sh` to `discord-notify.sh`.

## Goal

After this ISA: `WorktreeBroker.gc()` runs every 5 min when free disk < 8 GB, sweeps `~/.hermes/codex-wt/` for orphans (no row in `codex_sessions.json`, no live tmux session, no open PR), and renames each orphan to `~/.hermes/codex-wt/.deleted-<ts>/<sid>/` for a 7-day reaper to purge. The `/revive` slash command rebuilds a session under an existing Discord thread, preserving thread history and isa_id (with prior progress archived). The Telegram adapter and its supporting code are deleted, its SQLite tables are dropped (after backup), its env vars are removed from `.env.example`, and `WORKFLOW-LESSONS.md` §3 rule #7 references `discord-notify.sh`. The whole pipeline is now production-grade.

## Out of Scope

- New features beyond gc + revive + Telegram retirement.
- pnpm globalVirtualStore migration — recommended by external research RQ4 but a project-level call; document in `## Decisions` if the operator opts in, otherwise leave per-worktree npm install.
- Codex `thread/resume` support in the Hermes transport — external research RQ3 confirms the RPC exists in codex's protocol but Hermes' adapter doesn't use it today. Implementing it would give host-reboot survival; defer as a follow-up ISA, not P5.
- Multi-channel Discord support (one channel per team) — operator may want it later, not now.
- Dashboard alert routing (email/SMS on NEEDS_REVIVE) — operator uses Discord post for now.

## Constraints

- **P4 must be landed** — gc and revive both emit pulse SSE events the dashboard renders.
- **No `rm -rf` anywhere** per WORKFLOW-LESSONS §3 rule 5; gc uses rename-to-deleted-<ts> pattern.
- **No `--no-verify`, no force-push** anywhere per WORKFLOW-LESSONS §3 rule 4.
- **Backup-before-drop for SQLite tables** per WORKFLOW-LESSONS §3 rule 6 and rule 3 (backups are the rollback).
- **No production data loss during Telegram retirement** — `telegram_dm_topic_mode` and `telegram_dm_topic_bindings` tables are dropped only after a SQL dump is stored at `~/.hermes/backups/telegram-dm-topics-<ts>.sql`.
- **MVMS lessons referring to Telegram are SUPERSEDED, not deleted** per WORKFLOW-LESSONS §7 procedure.
- **`telegram-retirement-appendix.md` is the canonical inventory** — every edit P5 makes should map to a row in that appendix; deviations need a Decisions entry.

## Criteria

- [ ] ISC-1: `WorktreeBroker.gc()` is implemented per `module-specs/worktree-broker.md` §9
- [ ] ISC-2: a background tick (every 5 min) runs `gc()` when `df -P ~/.hermes` reports < 8 GB free; otherwise skip
- [ ] ISC-3: gc detects orphan worktrees (worktree exists, NO row in `codex_sessions.json`, NO live tmux session, NO open PR for its branch) and renames them to `~/.hermes/codex-wt/.deleted-<ts>/<sid>/`
- [ ] ISC-4: a 7-day reaper background task purges `~/.hermes/codex-wt/.deleted-*` directories older than 7 days, using `git worktree prune` first to release any git-side references
- [ ] ISC-5: `/revive` slash command rebuilds a session under an existing Discord thread: BEFORE allocating a new worktree, the handler runs `git -C <old-worktree> diff --stat` and posts the output to the Discord thread so the operator can see what uncommitted source changes (if any) are at risk of being lost. The message must appear in the thread BEFORE the new worktree is allocated. After the diff-stat post, allocates a NEW sid + NEW tmux session + NEW worktree (off the same branch if it exists on remote, else off `origin/main`); previous ISA progress is preserved as `_ephemeral/orphaned-<ts>.md` per ISA-SPEC §7
- [ ] ISC-6: `/revive` posts the new sid + tmux session name back to the Discord thread for operator confirmation. The confirmation post must appear AFTER the diff-stat warning from ISC-5, so the operator sees the diff before the revive completes.
- [ ] ISC-7: `gateway/platforms/telegram.py` is deleted (file removed entirely)
- [ ] ISC-8: `gateway/platforms/telegram_network.py` is deleted
- [ ] ISC-9: every `tests/gateway/test_telegram_*.py` file is deleted; remaining test suite passes (`pytest tests/gateway/`)
- [ ] ISC-10: `gateway/config.py:1195` `Platform.TELEGRAM: "TELEGRAM_BOT_TOKEN"` mapping is removed
- [ ] ISC-11: `gateway/platforms/base.py:32-33, 57, 78` telegram-specific branches are removed
- [ ] ISC-12: `toolsets.py:400, 533` `hermes-telegram` toolset is removed and dropped from `hermes-all`
- [ ] ISC-13: `pyproject.toml:84, 130` `python-telegram-bot[webhooks]==22.6` is removed from messaging extra + hard dep
- [ ] ISC-14: a new SQLite migration drops `telegram_dm_topic_mode` and `telegram_dm_topic_bindings` (cited at `hermes_state.py:2403, 2416`); the migration script first dumps each table to `~/.hermes/backups/telegram-dm-topics-<ts>.sql` per WORKFLOW-LESSONS §3 rule 6
- [ ] ISC-15: `WORKFLOW-LESSONS.md` §3 rule #7 is edited from "Always wire `telegram-notify.sh`" to "Always wire `discord-notify.sh`" (`telegram-notify.sh` was already deprecated in launch templates per `audits/cluster-B-gateway-discord.md`)
- [ ] ISC-16: `.env.example` has all `TELEGRAM_*` and `HERMES_TELEGRAM_*` env vars removed
- [ ] ISC-17: Anti: NO `rm -rf` or `git clean -fxd` in any P5 script or migration — grep proves it
- [ ] ISC-18: Anti: `pytest tests/ -k "not telegram"` passes; `pytest tests/gateway/` passes; `python -c "from gateway.platforms.telegram import TelegramAdapter"` raises ImportError; `grep -rn "telegram" /home/josep/.local/share/hermes-agent --include="*.py" | grep -v "RELEASE_" | grep -v "__pycache__"` returns 0 Python source hits
- [ ] ISC-19: `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2040_codex-parallel-p5-hardening/ISA.md` exit 0 in `phase: complete`

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from agent.worktree_broker import WorktreeBroker; print(WorktreeBroker.gc)"` | callable |
| ISC-2 | mock `df` to report 5 GB free; advance time by 5 min; check gc was called | called once |
| ISC-3 | seed an orphan worktree (no codex_sessions.json row, no tmux session, no PR); run gc; check rename | worktree moved to `.deleted-<ts>/<sid>/` |
| ISC-4 | seed `.deleted-<ts>/` dirs at 6 and 8 days old; run reaper; check disk | 8-day dir gone, 6-day dir intact |
| ISC-5 | put a session in NEEDS_REVIVE with uncommitted files in old worktree; run `/revive`; check (a) Discord thread first message contains `git diff --stat` output before new worktree is allocated, (b) new sid + new tmux session allocated, (c) `_ephemeral/orphaned-<ts>.md` exists in ISA dir | all three checks pass; diff-stat message precedes confirmation message |
| ISC-6 | run `/revive`; check Discord thread message order: diff-stat warning appears first, then confirmation with new sid + tmux session name | correct message order |
| ISC-7 | `test -e gateway/platforms/telegram.py ; echo $?` | non-zero |
| ISC-8 | `test -e gateway/platforms/telegram_network.py ; echo $?` | non-zero |
| ISC-9 | `ls tests/gateway/test_telegram_*.py 2>&1` and `pytest tests/gateway/ -q` | no telegram tests + suite green |
| ISC-10 | `grep -n 'Platform.TELEGRAM' gateway/config.py` | 0 hits |
| ISC-11 | `grep -nE '_TELEGRAM_AUDIO_\|platform == "telegram"' gateway/platforms/base.py` | 0 hits |
| ISC-12 | `grep -n 'hermes-telegram' toolsets.py` | 0 hits |
| ISC-13 | `grep -n 'python-telegram-bot' pyproject.toml` | 0 hits |
| ISC-14 | run migration; `ls ~/.hermes/backups/telegram-dm-topics-*.sql` + `sqlite3 <db> '.tables' \| grep -c telegram` | backup exists; 0 telegram tables |
| ISC-15 | `grep -n 'telegram-notify\|discord-notify' /home/josep/.hermes/WORKFLOW-LESSONS.md` | only `discord-notify.sh` in rule #7 |
| ISC-16 | `grep -nE 'TELEGRAM_\|HERMES_TELEGRAM_' .env.example` | 0 hits |
| ISC-17 | `grep -rnE 'rm -rf\|git clean -fxd' <P5 scripts>` | 0 hits |
| ISC-18 | run the 4 probe commands listed in the ISC | all pass |
| ISC-19 | `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2040_codex-parallel-p5-hardening/ISA.md ; echo $?` | `0` |

## Git Plan

- **Branch**: `feat/codex-parallel-p5-hardening` off `fork/main` (after P4 lands).
- **Commit cadence (early + incremental; one commit per appendix-§ category to keep the diff reviewable)**:
  1. `chore(isa): scaffold P5 ISA + work dir`
  2. `feat(worktree): WorktreeBroker.gc + 5-min tick + disk threshold (ISC-1, ISC-2, ISC-3)`
  3. `feat(worktree): 7-day deleted-dir reaper + git worktree prune (ISC-4)`
  4. `feat(dispatcher): /revive slash command + ephemeral ISA archive (ISC-5, ISC-6)`
  5. `chore(telegram-retire): delete gateway/platforms/telegram*.py + tests/gateway/test_telegram_*.py (ISC-7, ISC-8, ISC-9)`
  6. `chore(telegram-retire): scrub code references in gateway/, toolsets, pyproject (ISC-10, ISC-11, ISC-12, ISC-13)`
  7. `chore(telegram-retire): drop SQLite tables with backup-first migration (ISC-14)`
  8. `docs(telegram-retire): scrub docs + WORKFLOW-LESSONS rule #7 + .env.example (ISC-15, ISC-16)`
  9. `test(p5): gc + revive integration + telegram-removed smoke (ISC-18)`
- **Push**: `git push fork feat/codex-parallel-p5-hardening` after each commit.
- **PR**: against `fork/main` titled `feat(p5): Codex parallel workflow — gc, revive, Telegram retirement`.
- **Do NOT merge** until `phase: complete` per ISC-19.
- **Auto-merge label**: NO — Telegram retirement touches `migrations/`, `pyproject.toml`, `gateway/`, all sensitive per `module-specs/merge-broker.md` §5.
- **Post-merge operator action**: rotate any MVMS lessons referencing Telegram via `mvms_supersede`; check `~/.hermes/config.yaml` for any residual `platforms.telegram.*` keys and remove by hand if present (config files are operator-owned, not in this PR).

## Decisions

_(filled during execute — including the pnpm globalVirtualStore opt-in decision)_

## Changelog

_(filled on each correction — 4-tuple format per ISA-SPEC §8)_

## Verification

_(filled during verify — probe output pasted verbatim, one block per [x] ISC)_

## Handback

- On complete: `mvms_record_completion` under project `codex-parallel-workflow` linking branch + PR + ISA path; note that Telegram is fully retired in the completion summary.
- For each Changelog entry: `mvms_record_lesson` under project `codex-parallel-workflow`.
- Supersede any prior MVMS lesson mentioning `telegram-notify.sh` via `mvms_supersede`.
- Discord notification via `~/.hermes/scripts/discord-notify.sh`.
- Kanban: `kanban_complete <card>`.
