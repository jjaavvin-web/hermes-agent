---
isa:      20260524-2040_codex-parallel-p5-hardening
task:     "P5 Hardening — WorktreeBroker.gc + 7-day reaper + /revive slash"
tier:     E3
phase:    complete
progress: 8/16
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p5-hardening
hive:     "-"
owner:    claude-code
started:  2026-05-25T15:30:00Z
updated:  2026-05-25T15:30:00Z
---

## Problem

After P1-P4 the codex parallel pipeline is functional but two
hardening classes remain:

1. **Worktree orphans.** `WorktreeBroker.allocate / release` ship but
   `gc` is still a `NotImplementedError`. Sessions killed externally
   or whose worktrees survived a bot crash leave directories under
   `~/.hermes/codex-wt/` with no upper bound.
2. **NEEDS_REVIVE half-wired.** P1 detects the state on bot restart
   and P2.5 marks rows ORPHANED when the worktree is missing — but
   there's no operator-visible recovery path. Operator has to `/kill`
   + open a new Discord thread, losing the conversation continuity.

Telegram retirement (originally folded into P5 per the design)
turns out to be its own blast-radius event — delete adapter +
SQLite migration + pyproject change + tests + docs. Splitting into
its own ISA (P5.5) so this PR stays bounded and reviewable.

## Goal

After this ISA:

- `WorktreeBroker.gc(tracked_sids, live_branches)` sweeps
  `~/.hermes/codex-wt/` for orphans (no row in `codex_sessions.json`,
  no open PR for the branch) and renames each into
  `~/.hermes/codex-wt/.deleted-<ts>/<sid>/` — never `rm -rf`.
- `WorktreeBroker.reap_deleted(max_age_days=7)` purges those
  `.deleted-<ts>` buckets after 7 days. The 7-day window is the
  recovery margin if the operator wants to retrieve work from a
  worktree gc'd too eagerly.
- `/revive` slash command rebuilds a session under the SAME Discord
  thread: archives previous ISA progress to `_ephemeral/orphaned-<ts>.md`
  per ISA-SPEC §7, releases the old worktree, allocates fresh.

## Out of Scope

- Telegram adapter deletion (`gateway/platforms/telegram.py` + tests).
- Telegram SQLite tables drop (`telegram_dm_topic_mode`,
  `telegram_dm_topic_bindings`) — requires a backup-first migration.
- `pyproject.toml` removal of `python-telegram-bot[webhooks]==22.6`.
- `WORKFLOW-LESSONS.md` §3 rule #7 edit (telegram-notify → discord-notify).
- `.env.example` scrub of `TELEGRAM_*` vars.
- Bot/tmux retry / respawn loops beyond what P2/P2.5 already ship.

## Constraints

- **No `rm -rf` or `git clean -fxd` anywhere** per WORKFLOW-LESSONS
  §3 rule 5. `reap_deleted` uses `shutil.rmtree` ONLY on paths
  starting with `.deleted-` — the namespace gc owns exclusively.
- **`/revive` archives, never deletes** prior ISA — the
  `_ephemeral/orphaned-<ts>.md` write must succeed before the row is
  dropped (best-effort archive; broker.release still proceeds if
  archive fails because the row is already orphaned).
- **gc only renames** — never deletes a worktree directly. The 7-day
  reaper is the deletion path; that gap is the recovery margin.

## Criteria

- [x] ISC-1: `WorktreeBroker.gc(*, tracked_sids, live_branches=None)`
  is implemented; replaces the P1 `NotImplementedError` stub
- [x] ISC-2: gc treats a directory as orphan iff its sid is NOT in
  `tracked_sids` AND no entry in `live_branches` references that sid
- [x] ISC-3: gc renames each orphan to
  `~/.hermes/codex-wt/.deleted-<ts>/<sid>/`; the `.deleted-<ts>`
  prefix is reserved (gc skips its own buckets on re-entry)
- [x] ISC-4: `WorktreeBroker.reap_deleted(max_age_days=7)` purges
  `.deleted-<ts>` directories whose timestamp is older than
  `max_age_days`; unparseable timestamps are skipped (defensive)
- [x] ISC-5: `/revive` slash command rebuilds a session under an
  existing Discord thread; archives prior ISA progress as
  `_ephemeral/orphaned-<ts>.md` per ISA-SPEC §7
- [x] ISC-6: `/revive` is rejected for state COMPLETE / MERGING /
  CLAIMED (and any other terminal state); guidance directs the
  operator to `/kill` first
- [x] ISC-7: Anti: NO `rm -rf` or `git clean -fxd` in
  `agent/worktree_broker.py` — grep proves it; the only delete path
  is `shutil.rmtree` scoped to `.deleted-*` entries in `reap_deleted`
- [-] ISC-8: Anti: gc on tracked sids leaves their worktrees alone —
  verified by unit test
- [-] ISC-9: `gateway/platforms/telegram.py` deleted — TOMBSTONED to P5.5
- [-] ISC-10: `gateway/platforms/telegram_network.py` deleted — TOMBSTONED to P5.5
- [-] ISC-11: every `tests/gateway/test_telegram_*.py` deleted — TOMBSTONED to P5.5
- [-] ISC-12: SQLite migration drops `telegram_dm_*` tables (with backup) — TOMBSTONED to P5.5
- [-] ISC-13: `pyproject.toml` `python-telegram-bot` removed — TOMBSTONED to P5.5
- [-] ISC-14: `WORKFLOW-LESSONS.md` §3 rule #7 edited — TOMBSTONED to P5.5
- [-] ISC-15: `.env.example` `TELEGRAM_*` scrubbed — TOMBSTONED to P5.5
- [x] ISC-16: `python3 scripts/isa_lint.py isas/P5-hardening.md`
  exit 0 in `phase: complete`

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from agent.worktree_broker import WorktreeBroker; print(WorktreeBroker.gc)"` | callable, not NotImplementedError |
| ISC-2 | `pytest tests/agent/test_worktree_broker_gc.py::TestGc::test_tracked_sids_are_left_alone` + `test_untracked_sid_is_renamed_to_deleted_bucket` + `test_open_pr_branch_keeps_worktree` | 3 pass |
| ISC-3 | `pytest tests/agent/test_worktree_broker_gc.py::TestGc::test_untracked_sid_is_renamed_to_deleted_bucket` — asserts new path under `.deleted-<ts>/` | pass |
| ISC-4 | `pytest tests/agent/test_worktree_broker_gc.py::TestReapDeleted` (3 cases: old purged / recent kept / unparseable skipped) | 3 pass |
| ISC-5 | `pytest tests/gateway/test_codex_dispatcher_revive.py::test_revive_orphaned_archives_isa_and_reallocates` — asserts archive file + new sid allocated | pass |
| ISC-6 | `pytest tests/gateway/test_codex_dispatcher_revive.py::test_revive_rejects_completed_session` | pass |
| ISC-7 | `grep -rnE "rm -rf\|git clean -fxd" agent/worktree_broker.py` | 0 hits |
| ISC-16 | `python3 scripts/isa_lint.py isas/P5-hardening.md ; echo $?` | `0` |

## Git Plan

- **Branch**: `feat/codex-parallel-p5-hardening` off `fork/main`.
- **Single commit** for gc + reap + /revive + tests + ISA.
- **PR**: `feat(p5): codex worktree gc + 7-day reaper + /revive slash`.

## Decisions

**D-1 (2026-05-25): Telegram retirement split to P5.5.**
The original design folded Telegram retirement into P5 (ISC-7..16 of
the design ISA). On execution, the diff for that work alone is ~12
files including a SQLite migration — high blast radius. Splitting
into a separate ISA (P5.5) lets each PR stay reviewable and lets the
Telegram migration get the careful backup-first surgical treatment
it deserves. Marked the Telegram ISCs as tombstoned `[-]` per
ISA-SPEC §11.

**D-2 (2026-05-25): rename-to-deleted-<ts> instead of rm.**
WORKFLOW-LESSONS §3 rule 5 mandates no `rm -rf` for any cleanup. gc
renames orphans into `~/.hermes/codex-wt/.deleted-<ts>/`; the 7-day
reaper is the deletion path. That window is the operator's recovery
margin if gc is too eager — they can `mv` a worktree back out of the
bucket and rebuild state.

**D-3 (2026-05-25): /revive archives, doesn't delete.**
The previous ISA's content is copied to `_ephemeral/orphaned-<ts>.md`
before the row is dropped. Even if the operator's first revive turns
out wrong, the old work is preserved. The new session inherits no
stale ISA state by design (different sid + fresh worktree).

## Changelog

2026-05-25 — original P5 conflated gc + revive + Telegram retirement
  conjectured:   shipping all three together is simplest because the
                 design treated them as one phase
  refuted by:    Telegram retirement alone touches 12 files including
                 a SQLite migration, pyproject.toml, and many test
                 files; mixing it with gc + revive (the actually load-
                 bearing pieces for the parallel workflow) doubles the
                 diff and the review surface
  learned:       gc + revive are the load-bearing P5 pieces — they
                 keep the codex workflow operational under accumulated
                 orphans + recover from NEEDS_REVIVE. Telegram retirement
                 is a separable migration with its own backup-first
                 surgical requirements
  criterion now: D-1 added; ISC-9..15 tombstoned to P5.5 (separate
                 ISA); ISA-SPEC §11 split convention applied

## Verification

### ISC-1 — gc is callable, not NotImplementedError

```
$ python -c "from agent.worktree_broker import WorktreeBroker; print(WorktreeBroker.gc.__doc__.splitlines()[0])"
Sweep orphan worktrees out of ``~/.hermes/codex-wt/``.
```

### ISC-2 / ISC-3 — orphan detection + rename

```
$ pytest tests/agent/test_worktree_broker_gc.py::TestGc -q
.....                                                                    [100%]
5 passed
```

### ISC-4 — reaper

```
$ pytest tests/agent/test_worktree_broker_gc.py::TestReapDeleted -q
...                                                                      [100%]
3 passed
```

### ISC-5 / ISC-6 — /revive slash command

```
$ pytest tests/gateway/test_codex_dispatcher_revive.py -q
...                                                                      [100%]
3 passed
```

### ISC-7 — anti: no rm -rf or git clean -fxd

```
$ grep -rnE 'rm -rf|git clean -fxd' agent/worktree_broker.py
$ echo $?
1
```

### ISC-16 — isa_lint at phase: complete

```
$ python3 scripts/isa_lint.py isas/P5-hardening.md
PASS: isas/P5-hardening.md
```

## Handback

**Project:** `codex-parallel-workflow-p5`. **Lesson:** if a phase's
scope mixes load-bearing hardening with a destructive migration,
split them — the migration needs its own backup-first surgical pass.

**Tombstoned for P5.5:**
- Telegram adapter deletion (gateway/platforms/telegram*.py)
- Telegram test deletion (tests/gateway/test_telegram_*.py)
- SQLite migration dropping `telegram_dm_*` tables (backup → drop)
- pyproject.toml + .env.example cleanup
- WORKFLOW-LESSONS.md §3 rule #7 edit

Each is small individually; together they justified their own ISA.
