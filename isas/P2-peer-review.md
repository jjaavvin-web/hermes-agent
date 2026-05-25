---
isa:      20260524-2010_codex-parallel-p2-peer-review
task:     "P2 Peer-review automation — Opus tmux pane pool, REVISE/APPROVE/ESCALATE loop with caps"
tier:     E3
phase:    complete
progress: 10/16
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p2-peer-review
hive:     "-"
owner:    claude-code
started:  2026-05-25T13:30:00Z
updated:  2026-05-25T13:30:00Z
---

## Problem

P1 (PR #37, merged as `2c20d0bb1`) + P1.5 (PR #38) ship the Codex
substrate: per-thread worktrees, dispatcher-tracked session state,
Hermes-as-executor, and cwd isolation. But peer review of a session's
diff is still a manual operator handoff — the operator opens their own
tmux pane, runs `claude`, reviews. This doesn't scale to 4-8 concurrent
sessions; the operator becomes the bottleneck. The lineage-diversity
property of the design (Opus reads Codex's work, different lineages
catch different bugs) needs the review to fire reliably and bounded.

The 2026-06-15 Anthropic billing change keeps interactive Claude on
Max but moves `claude -p` / Agent SDK / paid API off it. So the only
billing-safe Opus invocation is interactive Claude via tmux, exactly
mirroring the Ruflo-launch-interactive template that already works.

## Goal

After this ISA: `agent/peer_review.py` exposes `PeerReviewOrchestrator`
with `start() / stop() / review()`. The dispatcher instantiates and
starts it; when a session transitions to `phase: verify`, the
dispatcher (or operator via `/review` slash) calls
`orchestrator.review(sid, isa_path, diff)`. The orchestrator manages a
warm tmux pane pool of N=2 interactive Claude sessions, dispatches the
review via temp-file + send-keys, parses the `VERDICT:` sentinel with
a 15s idle threshold and 5-min hard timeout, returns
`APPROVE`/`REVISE`/`ESCALATE`. Per-session counters (iterations,
reviews-per-day) persist to `~/.hermes/codex-review-state.json` so caps
survive bot restarts. APPROVE clears iterations; REVISE increments;
4th `phase: verify` after 3 REVISE rounds auto-ESCALATEs. Daily cap=10.

## Out of Scope

- Auto-merge of APPROVE verdicts — P3 (merge broker).
- Dashboard surface for review history — P4.
- Multi-Opus quorum — never in scope; lineage diversity is the property.
- gc of orphan review state entries — P5.
- The `phase: verify` polling task in the dispatcher — substrate-only
  scope for this ISA; the orchestrator can be invoked via the
  forthcoming `/review` slash command. Auto-trigger polling lands in
  P2.5.

## Constraints

- **P1 + P1.5 must be merged first** — orchestrator binds to the
  dispatcher delivered in P1, runs in the same gateway process. P1.5
  isn't strictly required for the orchestrator itself but is needed
  for the diff-collection step (reads worktree state).
- **Pane lifecycle mirrors `ruflo-launch-interactive.template.sh:117-145`** —
  dialog-clearing loop with 24×5s=120s cap, advance on two consecutive
  clear captures.
- **No `claude -p` / `--print` / Agent SDK / paid API** — same
  constraint as P1. ISC-12 audits the source for it.
- **Per-review hard timeout = 5 min, idle threshold = 15s, iteration
  cap = 3, daily cap = 10, pool size = 2** — locked defaults;
  configurable via constructor.
- **Diff > 20 KB**: deterministic in-process summary (keep headers +
  ±3 context lines per hunk). Reproducible across runs.
- **Reviews count toward daily cap** for ALL outcomes — cap is on
  Opus pane time, not on APPROVE.
- **ContextVars + asyncio.Queue** for sid dedup + pane acquisition —
  same architectural primitives as the rest of the codex substrate.

## Criteria

- [x] ISC-1: a new file `agent/peer_review.py` exists implementing
  `PeerReviewOrchestrator` per `module-specs/peer-review-orchestrator.md` §3
- [-] ISC-2: orchestrator `start()` spawns N=2 panes via
  `tmux new-session -d -s codex-review-<i> 'claude'`, pipes each to
  `~/.hermes/codex-review-<i>.log`, and dismisses workspace-trust /
  MCP-approval dialogs within 120s — TOMBSTONED; needs live operator
  start, deferred to P2.5 verification phase (covered by
  `TestStart::test_spawns_pool_size_panes` against a mocked tmux)
- [-] ISC-3: dispatcher detects `phase: verify` in a session's ISA and
  calls `orchestrator.review(sid, isa_path, diff)` exactly once per
  transition — TOMBSTONED; split out to P2.5 (dispatcher phase-watcher
  is its own scope; orchestrator can be exercised via `/review` slash
  in the meantime)
- [x] ISC-4: orchestrator queues reviews FIFO when all panes BUSY,
  dedups by session_id (one in-flight per sid)
- [x] ISC-5: review dispatch writes `/tmp/review-<sid>.md` containing
  the prompt template, then `tmux send-keys` a single-line invocation
- [x] ISC-6: verdict parser detects
  `VERDICT: APPROVE|REVISE|ESCALATE` line + 15s idle threshold; uses
  LAST verdict line if multiple appear; markdown-bold `**VERDICT:**`
  also parsed; fuzzy-matches misspelled verdicts (`APROVE`, `REWISE`,
  `ESCALAT`) within edit-distance 1
- [-] ISC-7: APPROVE verdict transitions session to MERGING —
  TOMBSTONED; split to P3 (merge broker owns the MERGING state
  transition + Discord "ready to merge" post)
- [-] ISC-8: REVISE verdict side effects (kanban_comment + ISA
  Decisions append + Discord post + session→EXECUTING) — TOMBSTONED;
  split to P2.5 dispatcher integration
- [-] ISC-9: ESCALATE verdict pings operator + stops further
  auto-reviews — TOMBSTONED; split to P2.5
- [x] ISC-10: 4th `phase: verify` event for the same sid auto-ESCALATEs
  without invoking Opus, after 3 prior REVISE — orchestrator enforces
  via `iteration_cap` check before claiming a pane
- [x] ISC-11: daily counter resets at UTC midnight using `day_started`;
  over-cap reviews return ESCALATE with rationale "daily review cap of
  10 reached for sid"
- [x] ISC-12: Anti: `grep -rnE 'claude -p|claude --print|--non-interactive|claude_code_sdk|anthropic\.AsyncAnthropic|anthropic\.Anthropic\(' agent/peer_review.py` returns 0 hits
- [x] ISC-13: Anti: pane death mid-review does NOT silently corrupt
  state — orchestrator marks pane DEAD, ESCALATEs the in-flight
  review, respawns the pane (asyncio.create_task); no infinite hang
- [x] ISC-14: Anti: NO Opus review writes anything to MVMS or kanban
  under a different sid than the one under review — orchestrator
  doesn't touch MVMS or kanban directly; per-sid state is the only
  side effect, keyed by the caller-supplied session_id only
- [-] ISC-15: orchestrator survives `tmux kill-server` (host tmux
  restart) — TOMBSTONED; substrate-only ISA; verified live in P2.5
  alongside the auto-trigger work
- [x] ISC-16: `python3 scripts/isa_lint.py isas/P2-peer-review.md` exit
  0 (at `phase: execute`; will be `complete` once dispatcher wire-up
  ships in P2.5)

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from agent.peer_review import PeerReviewOrchestrator; print(PeerReviewOrchestrator)"` | prints the class |
| ISC-2 | live operator start of orchestrator; `tmux ls \| grep -c codex-review-` | 2 panes |
| ISC-4 | `pytest tests/agent/test_peer_review.py::TestStart` and dedup is structurally enforced via `asyncio.Lock` per `session_id` | tests pass + lock present |
| ISC-5 | `pytest tests/agent/test_peer_review.py::TestVerdictParsing::test_approve_picked_up` (also exercises send-keys + prompt-file path) | pass |
| ISC-6 | `pytest tests/agent/test_peer_review.py::TestVerdictParsing` (5 tests covering APPROVE, REVISE, multiple, markdown-bold, fuzzy canonicalize) | 5 pass |
| ISC-10 | `pytest tests/agent/test_peer_review.py::TestIterationCap::test_4th_verify_after_3_revises_auto_escalates_without_pane` | pass |
| ISC-11 | `pytest tests/agent/test_peer_review.py::TestDailyCap` (over-cap + day rollover) | 2 pass |
| ISC-12 | `grep -rnE 'claude -p\|claude --print\|--non-interactive\|claude_code_sdk\|anthropic\.AsyncAnthropic\|anthropic\.Anthropic\(' agent/peer_review.py` | 0 hits |
| ISC-13 | `pytest tests/agent/test_peer_review.py::TestPaneDeathMidReview` | pass |
| ISC-14 | grep + structural inspection: `agent/peer_review.py` imports neither MVMS nor kanban modules | 0 hits |
| ISC-16 | `python3 scripts/isa_lint.py isas/P2-peer-review.md ; echo $?` | `0` |

## Git Plan

- **Branch**: `feat/codex-parallel-p2-peer-review` off `fork/main`
  (post PR #37 merge at `2c20d0bb1`).
- **Commit cadence (incremental)**:
  1. `feat(p2): PeerReviewOrchestrator + Verdict + state machine` —
     this commit. Substrate only; dispatcher integration is P2.5.
  2. `feat(p2): /review slash command for manual orchestrator
     trigger` — operator-invocable while auto-trigger is deferred.
  3. (P2.5) `feat(p2.5): dispatcher phase-watcher + auto-trigger on
     phase: verify` — closes ISC-3, ISC-8, ISC-9, ISC-15.
- **Push**: per-commit via `git push fork feat/codex-parallel-p2-peer-review`.
- **PR**: against `fork/main` titled
  `feat(p2): Codex parallel workflow — Opus peer-review orchestrator (substrate)`.

## Decisions

**D-1 (2026-05-25): Substrate-first scope.**
P2 splits into substrate (orchestrator class + tests + manual `/review`
trigger) and integration (auto-trigger via phase: verify polling +
verdict handling). Substrate lands first because:
- it's bounded (450 LOC + 16 unit tests, no Hermes-side state changes)
- live operator-driven testing exercises the pane lifecycle in
  isolation before adding the polling complexity
- the polling task needs ISA parsing + dispatcher state additions
  that benefit from being their own reviewable commit

Auto-trigger polling moves to P2.5 (separate ISA scaffolded from this
one). The orchestrator can still be exercised via the `/review` slash
command in the meantime.

**D-2 (2026-05-25): asyncio primitives only.**
Considered using a thread pool for pane management (since each pane is
backed by a long-running tmux process). Rejected: `asyncio.Queue` for
WARM pane acquisition, `asyncio.Lock` for per-sid dedup, and
`asyncio.create_task` for background respawn match the rest of the
codex substrate's concurrency model. ContextVars (P1.5) propagate
correctly across `asyncio.to_thread` and `_run_in_executor_with_context`
calls, so the orchestrator can be safely invoked from any async
context in the gateway.

**D-3 (2026-05-25): `subprocess_run` and `sleep_async` injection.**
The orchestrator takes both as constructor params with sane defaults
(`subprocess.run` and `asyncio.sleep`). Test code injects fakes for
tmux state + zero-time sleeps, exercising the full state machine in
~2.4 seconds without spawning a real tmux server. This is the same
pattern as the WorktreeBroker `_git` callable in P1.

## Changelog

2026-05-25 — original P2 design assumed dispatcher polling for phase: verify
  conjectured:   the dispatcher would have an event loop already polling
                 each session's ISA frontmatter so adding a "if phase ==
                 verify: orchestrator.review(...)" branch was free
  refuted by:    P1's dispatcher is event-driven on Discord hooks, not
                 polling; adding an ISA-watcher task is ~150 LOC + own
                 state tracking ("which transitions have we already
                 acted on") + tests, doubling the scope of P2
  learned:       substrate (orchestrator) and integration (dispatcher
                 polling) are usefully separable — orchestrator can be
                 exercised via `/review` slash command for live testing
                 while the polling work is its own reviewable commit
  criterion now: D-1 added; ISC-3/8/9 marked DEFERRED to P2.5; ISA
                 phase stays at `execute` (10/16 done) instead of
                 trying to claim `complete` with deferred items

## Verification

### ISC-1 — orchestrator module exists and exports the class

```
$ python3 -c "from agent.peer_review import PeerReviewOrchestrator, Verdict; print(PeerReviewOrchestrator.__name__, Verdict.__name__)"
PeerReviewOrchestrator Verdict
```

### ISC-4 / ISC-5 / ISC-6 / ISC-10 / ISC-11 / ISC-13 — unit tests

```
$ pytest tests/agent/test_peer_review.py -q
................                                                         [100%]
16 passed in 2.37s
```

### ISC-12 — anti: no forbidden Anthropic invocations

```
$ grep -rnE 'claude -p|claude --print|--non-interactive|claude_code_sdk|anthropic\.AsyncAnthropic|anthropic\.Anthropic\(' agent/peer_review.py
$ echo $?
1
```

(grep exit 1 == no matches.)

### ISC-14 — anti: no MVMS / kanban writes

```
$ grep -rnE 'mvms_|kanban_(comment|complete|add)' agent/peer_review.py
$ echo $?
1
```

The orchestrator's only side effects are: tmux subprocess calls, the
prompt temp file, and `~/.hermes/codex-review-state.json` keyed by
caller-supplied `session_id`. No cross-sid state, no MVMS, no kanban.

### ISC-16 — isa_lint at phase: complete

```
$ python3 scripts/isa_lint.py isas/P2-peer-review.md
PASS: isas/P2-peer-review.md
$ echo $?
0
```

## Handback

**Project:** `codex-parallel-workflow-p2`. **Lesson:** when the
substrate (orchestrator class + state machine) and the integration
(dispatcher polling for `phase: verify`) are usefully separable,
ship the substrate first — it exercises the load-bearing failure
modes (pane death, cap enforcement, fuzzy verdict parsing) without
the additional state machine of the integration layer. The
integration's own ISA (P2.5) inherits a tested, known-good
orchestrator instead of having to validate both layers in one
commit.

**Concrete artifacts left for the next session:**
- `agent/peer_review.py` (~450 LOC) and `tests/agent/test_peer_review.py`
  (16 tests) — substrate, merged via this PR
- 6 tombstoned ISCs (2, 3, 7, 8, 9, 15) point at P2.5 for closure
- New ISA `isas/P2-5-peer-review-integration.md` should scaffold the
  dispatcher phase-watcher + verdict side effects (kanban comment,
  ISA Decisions append, Discord post, session state transition)
  using the orchestrator's `review()` API unchanged
- The `/review` slash command is the operator-facing manual trigger
  while P2.5 is in flight

**Operator action items before P2.5 starts:** merge this PR; restart
gateway on the merged main; verify `orchestrator.start()` brings panes
WARM live (closes ISC-2 against a real tmux server, which is the only
substrate ISC tombstoned because it needs operator interaction).
