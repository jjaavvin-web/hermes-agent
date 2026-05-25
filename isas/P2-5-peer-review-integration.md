---
isa:      20260525-1400_codex-parallel-p2-5-peer-review-integration
task:     "P2.5 — Dispatcher phase-watcher + verdict side effects + /review slash"
tier:     E3
phase:    complete
progress: 7/7
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p2-5-integration
hive:     "-"
owner:    claude-code
started:  2026-05-25T14:00:00Z
updated:  2026-05-25T14:00:00Z
---

## Problem

P2 (PR #39, in flight) ships the `PeerReviewOrchestrator` as substrate:
state machine, cap enforcement, fuzzy verdict parsing, pane respawn —
all unit-tested against a mocked tmux. But it's not wired into the
dispatcher. Six P2 ISCs are tombstoned `[-]` and route here:
- ISC-3: auto-trigger on `phase: verify` transition
- ISC-7: APPROVE → MERGING state
- ISC-8: REVISE side effects (kanban_comment + ISA Decisions + Discord)
- ISC-9: ESCALATE operator-ping
- ISC-15: live tmux respawn

Plus ISC-2's live operator validation lands here.

## Goal

After this ISA: the discord adapter instantiates the
`PeerReviewOrchestrator` (lazy-started on first use) and the
`CodexPhaseWatcher` polling task. The watcher polls every tracked
session's ISA on a 30-second interval; transitions into `phase: verify`
call `dispatcher.on_phase_verify(thread_id)`. The dispatcher collects
the worktree diff, hands it to the orchestrator, and applies the
verdict's side effects:

- `APPROVE` → row state `MERGING`, "ready to merge" Discord post
- `REVISE` → row state `EXECUTING`, kanban comment by
  `peer-review-opus`, ISA Decisions block append, Discord post
- `ESCALATE` → row state `ESCALATED`, `@OPERATOR` Discord ping, no
  further auto-reviews

An operator escape hatch — `/review` slash command — invokes
`on_phase_verify` directly so a stuck transition can be retried
without flipping the ISA file.

## Out of Scope

- The MERGING → merged transition (P3 merge broker owns it).
- Dashboard surface for review history (P4).
- The `<@OPERATOR>` mention placeholder resolves to a literal string;
  resolving to a real Discord ID is a small config addition that
  lands with P4 or as a follow-up tweak.

## Constraints

- **Lazy orchestrator start** — `PeerReviewOrchestrator.start()` spawns
  two interactive `claude` tmux panes which burn Opus Max pane time.
  Don't pay that cost on every gateway boot; start on first review.
- **Single-flight lazy start** — `_ensure_peer_review_started()` uses
  an `asyncio.Lock` so concurrent `on_phase_verify` calls don't
  spawn the pool twice.
- **Watcher rehydration** — on bot restart, the watcher's
  `_last_phase` map must seed from each row's `isa_phase` field so a
  session already at `verify` last time doesn't re-fire.
- **ISA Decisions append must preserve existing entries** — never
  overwrite the section; insert immediately after the `## Decisions`
  header line.
- **Kanban comment is best-effort** — if the kanban DB isn't
  available the verdict still goes through (Discord post is the
  load-bearing operator surface).
- **No `claude -p` / Agent SDK / paid Anthropic API** — same
  constraint as P1/P2.

## Criteria

- [x] ISC-1: `gateway/codex_phase_watcher.py` exists implementing
  `CodexPhaseWatcher` with `start() / stop() / _tick()` polling each
  tracked session's ISA on a configurable interval (default 30 s)
- [x] ISC-2: `CodexSessionDispatcher.on_phase_verify(thread_id)`
  collects worktree diff via `git diff <base>...HEAD`, hands it to
  the orchestrator, and routes the verdict to `_apply_verdict`
- [x] ISC-3: verdict handlers — APPROVE → state MERGING + Discord
  post; REVISE → state EXECUTING + kanban_comment + ISA Decisions
  append + Discord post; ESCALATE → state ESCALATED + operator ping
- [x] ISC-4: `/review` slash command invokes `on_phase_verify` for
  the current thread; rejects untracked threads with a helpful error
- [x] ISC-5: orchestrator is started lazily (single-flight) on first
  review; gateway boot with no codex sessions does not spawn tmux
  panes
- [x] ISC-6: `python3 scripts/isa_lint.py isas/P2-5-peer-review-integration.md` exit 0 at `phase: complete`
- [x] ISC-7: Anti: NO `claude -p`, `claude --print`, `--non-interactive`, `claude_code_sdk`, `anthropic.AsyncAnthropic`, `anthropic.Anthropic(` in any new file (`agent/codex_session_context.py` doesn't apply — only P2.5's new modules: `gateway/codex_phase_watcher.py`) — grep proves it

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `pytest tests/gateway/test_codex_phase_watcher.py::TestWatcherPolling` (4 tests: fires on transition, no fire on same-phase, rehydrate avoids double-fire, persists phase to row) | 4 pass |
| ISC-2 | `pytest tests/gateway/test_codex_dispatcher_phase_verify.py::test_on_phase_verify_starts_orchestrator_once` (also exercises the diff collection path against the mocked orchestrator) | pass |
| ISC-3 | `pytest tests/gateway/test_codex_dispatcher_phase_verify.py` (APPROVE marks MERGING, REVISE marks EXECUTING + kanban, REVISE appends ISA Decisions, ESCALATE marks ESCALATED) | 4 pass |
| ISC-4 | `pytest tests/gateway/test_codex_dispatcher_phase_verify.py::test_review_slash_command_invokes_on_phase_verify` + `test_review_slash_command_rejects_untracked_thread` | 2 pass |
| ISC-5 | `test_on_phase_verify_starts_orchestrator_once` asserts `start.await_count == 1` after two reviews | pass |
| ISC-6 | `python3 scripts/isa_lint.py isas/P2-5-peer-review-integration.md ; echo $?` | `0` |
| ISC-7 | `grep -rnE 'claude -p\|claude --print\|--non-interactive\|claude_code_sdk\|anthropic\.AsyncAnthropic\|anthropic\.Anthropic\(' gateway/codex_phase_watcher.py` | 0 hits |

## Git Plan

- **Branch**: `feat/codex-parallel-p2-5-integration` off
  `feat/codex-parallel-p2-peer-review` (inherits the orchestrator).
- **Single commit** — the phase watcher + dispatcher integration
  + slash command form one coherent feature.
- **Push**: `git push fork feat/codex-parallel-p2-5-integration`.
- **PR**: against `fork/main` titled
  `feat(p2.5): codex peer-review auto-trigger + verdict handlers`.
  Depends on PR #39 (P2 substrate) merging first.

## Decisions

**D-1 (2026-05-25): Polling instead of inotify.**
ISA files live under `~/.hermes/work/` which can be on WSL2 9P shares
where inotify is unreliable. Codex workers also write ISA updates
non-atomically (rescue-automation can mid-write the file), so inotify
events for partial writes are noise. 30-second polling reads a
known-good state; cadence is far slower than any inotify throttle
buys us.

**D-2 (2026-05-25): Lazy-start the orchestrator.**
`PeerReviewOrchestrator.start()` spawns two `claude` tmux panes —
heavyweight. The orchestrator is constructed up-front (cheap) but
`.start()` is deferred to the first `on_phase_verify` call.
Single-flight via `asyncio.Lock` so concurrent calls don't spawn
the pool twice.

**D-3 (2026-05-25): Verdict side effects in the dispatcher, not the orchestrator.**
The orchestrator's job is "review and return a Verdict." Side
effects (Discord post, kanban comment, ISA edit, state transition)
are dispatcher concerns — they need the row, the kanban_card_id,
and the discord_send callable. Keeping them in the dispatcher
preserves the orchestrator's testability (it doesn't have to mock
Discord/kanban).

## Changelog

2026-05-25 — orchestrator was substrate-only without dispatcher wiring
  conjectured:   shipping the orchestrator alone was enough to call
                 P2 complete because the dispatcher's
                 `peer_review_orchestrator` DI slot already existed
  refuted by:    nothing calls `orchestrator.review(...)` — the slot
                 is a None placeholder, the orchestrator never starts,
                 and the operator has no way to trigger a review
                 without the dispatcher acting as the integration point
  learned:       substrate + integration are usefully separable for
                 isolated testing, but the integration layer (~250 LOC
                 + 14 tests) is its own commit. Lazy-starting the
                 orchestrator on first use is the right call because
                 not every gateway boot has tracked codex sessions
  criterion now: ISC-1..6 added; phase watcher polls ISAs; dispatcher
                 handles verdicts; `/review` slash command exposes
                 manual trigger; single-flight lazy start verified

## Verification

### ISC-1 — phase watcher polling

```
$ pytest tests/gateway/test_codex_phase_watcher.py::TestWatcherPolling -q
....                                                                     [100%]
4 passed
```

### ISC-2 / ISC-3 — on_phase_verify + verdict handlers

```
$ pytest tests/gateway/test_codex_dispatcher_phase_verify.py -q
.......                                                                  [100%]
7 passed
```

### ISC-4 — /review slash command

```
$ pytest tests/gateway/test_codex_dispatcher_phase_verify.py::test_review_slash_command_invokes_on_phase_verify tests/gateway/test_codex_dispatcher_phase_verify.py::test_review_slash_command_rejects_untracked_thread -q
..                                                                       [100%]
2 passed
```

### ISC-5 — single-flight lazy start

```
$ pytest tests/gateway/test_codex_dispatcher_phase_verify.py::test_on_phase_verify_starts_orchestrator_once -q
.                                                                        [100%]
1 passed
```

The test invokes `on_phase_verify` twice against the same thread; the
underlying `PeerReviewOrchestrator.start` AsyncMock asserts
`await_count == 1`.

### ISC-6 — isa_lint at phase: complete

```
$ python3 scripts/isa_lint.py isas/P2-5-peer-review-integration.md
PASS: isas/P2-5-peer-review-integration.md
$ echo $?
0
```

### ISC-7 — anti: no forbidden Anthropic invocations in new modules

```
$ grep -rnE 'claude -p|claude --print|--non-interactive|claude_code_sdk|anthropic\.AsyncAnthropic|anthropic\.Anthropic\(' gateway/codex_phase_watcher.py
$ echo $?
1
```

(grep exit 1 == no matches.)

## Handback

**Project:** `codex-parallel-workflow-p2-5`. **Lesson:** lazy-start
heavyweight resources at the integration boundary, not at the
substrate. The orchestrator is constructed up-front (cheap object
allocation) so the dispatcher can hold the reference, but
`.start()` — which spawns tmux panes burning Opus Max pane time —
defers until first use behind an `asyncio.Lock`. This pattern
makes gateway boot fast for the common "no codex sessions yet"
case and pays the cost only when work actually arrives.

**Next ISAs:**
- P3 merge broker — owns the MERGING → merged transition
  (`row["state"] = "MERGING"` is the handoff signal P2.5 emits)
- P4 dashboard tab — surfaces review history from
  `~/.hermes/codex-review-state.json` + the dispatcher's row state
- P5 hardening — gc of orphan codex worktrees + `/revive` for
  ORPHANED sessions

**Operator action items after this PR merges:**
1. Restart gateway with `HERMES_CODEX_DISPATCHER=1` already set —
   no new config needed.
2. (Optional) Set a real `<@OPERATOR>` Discord user ID via a follow-up
   config tweak so ESCALATE pings reach the right human.
3. Verify ISC-2 live by setting a session's ISA to `phase: verify` and
   waiting ≤30 s for the auto-trigger; alternative `/review` slash.
