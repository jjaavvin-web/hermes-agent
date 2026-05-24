---
isa:      20260524-2010_codex-parallel-p2-peer-review
task:     "P2 Peer-review automation — Opus tmux pane pool, auto-trigger on phase: verify, REVISE/APPROVE/ESCALATE loop with caps"
tier:     E3
phase:    scaffold
progress: 0/17
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p2-peer-review
hive:     "-"
owner:    ruflo-hive
started:  2026-05-24T20:10:00Z
updated:  2026-05-24T20:10:00Z
---

## Problem

P1 ships a manual peer-review handoff — the operator decides when to open their own tmux pane and run an Opus review against a session's diff. This works for the first few sessions but does not scale: with 4-8 concurrent sessions, the operator becomes the bottleneck. The lineage-diversity property of the design (DESIGN §2 Decision A: Opus reviews Codex's work; different lineages catch different bugs) requires the review to fire reliably and bounded, not depend on operator availability.

The Anthropic 2026-06-15 billing change (PROVIDER-STACK.md §"2026-06-15 — Anthropic billing change") makes any `claude -p` / Agent SDK / paid API path unacceptable. The only billing-safe Opus invocation post-cutoff is interactive Claude Code, which forces a tmux pane lifecycle for orchestration.

## Goal

After this ISA: when a Codex session writes `phase: verify` to its ISA frontmatter, the dispatcher auto-triggers `PeerReviewOrchestrator.review(sid, isa_path, diff)`. The orchestrator runs a warm tmux pane pool of N=2 interactive Claude sessions (no `claude -p`), dispatches the review via temp-file + send-keys, parses the `VERDICT:` sentinel from `tmux capture-pane -p` with a 15s idle threshold and 5-min hard timeout, returns one of `APPROVE` / `REVISE` / `ESCALATE`, and persists per-session counters (iterations, reviews-per-day) to `~/.hermes/codex-review-state.json`. REVISE feedback lands as `kanban_comment` + ISA Decisions entry + Discord-thread post, then the session re-enters `phase: execute`. After 3 REVISE rounds, the 4th `phase: verify` auto-escalates without invoking Opus. Daily cap = 10 reviews/sid prevents runaway loops.

## Out of Scope

- Auto-merge of APPROVE verdicts — that's P3 (merge broker).
- Dashboard surface for review history — P4.
- Multi-Opus quorum (more than one reviewer) — never in scope; lineage diversity is the property, not numerosity.
- Reviewer swapping (e.g. Hermes-4 as alternate adversarial critic per PROVIDER-STACK.md:45) — future work, not P2.
- gc of orphan review state in `~/.hermes/codex-review-state.json` — minor cleanup, can fold into P5.

## Constraints

- **P1 must be landed and live first** — orchestrator binds to the dispatcher delivered in P1.
- **Pane lifecycle must mirror `~/.hermes/scripts/templates/ruflo-launch-interactive.template.sh:104-145` exactly** (dialog-clearing, pipe-pane, capture-pane). That template is the load-bearing pattern.
- **No `claude -p` / `--print` / Agent SDK / paid API** — same constraint as P1. The orchestrator is the most-likely-to-violate module; ISC-12 (Anti) audits this.
- **Per-review hard timeout = 5 min, idle threshold = 15s, iteration cap = 3, daily cap = 10, pool size = 2** — locked per objective §7 #3. Configurable via constructor for tests but defaults must be these.
- **Diff > 20 KB**: summarize in-process before pasting (objective §7 #3). Summary must be deterministic so reviews are reproducible.
- **REVISE must not loop forever**: 3rd REVISE → 4th `phase: verify` auto-ESCALATEs.
- **Reviews count toward daily cap** even if the verdict is REVISE or ESCALATE — the cap is on Opus pane time, not on APPROVE outcomes.

## Criteria

- [ ] ISC-1: a new file `agent/peer_review.py` exists implementing `PeerReviewOrchestrator` per `module-specs/peer-review-orchestrator.md` §3
- [ ] ISC-2: orchestrator `start()` spawns N=2 panes via `tmux new-session -d -s codex-review-<i> 'claude'`, pipes each to `~/.hermes/codex-review-<i>.log`, and dismisses workspace-trust / MCP-approval dialogs within 120s (mirror `ruflo-launch-interactive.template.sh:117-145`)
- [ ] ISC-3: dispatcher (from P1) detects `phase: verify` in a session's ISA and calls `orchestrator.review(sid, isa_path, diff)` exactly once per `phase: verify` transition
- [ ] ISC-4: orchestrator queues reviews FIFO when all panes are BUSY, dedups by session_id (one in-flight per sid)
- [ ] ISC-5: review dispatch writes `/tmp/review-<sid>.md` containing the prompt template from `module-specs/peer-review-orchestrator.md` §6, then `tmux send-keys` a single-line invocation
- [ ] ISC-6: verdict parser detects `VERDICT: APPROVE|REVISE|ESCALATE` line + 15s idle threshold after that line; uses LAST verdict line if multiple appear; fuzzy-matches misspelled verdicts within edit-distance 1
- [ ] ISC-7: APPROVE verdict transitions session to MERGING (handoff to P3's merge broker; P2 just emits the handoff event — for P2 the handoff is a no-op that posts "ready to merge" to Discord)
- [ ] ISC-8: REVISE verdict (a) posts the rationale via `kanban_comment` (`kanban_tools.py:521`) with author=peer-review-opus, (b) appends a Decisions entry to the ISA, (c) posts the rationale to the Discord thread, (d) transitions session back to EXECUTING
- [ ] ISC-9: ESCALATE verdict pings the operator in the Discord thread and stops the session at `phase: verify` (no further reviews until operator intervenes)
- [ ] ISC-10: 4th `phase: verify` event for the same sid auto-ESCALATEs without invoking Opus, after 3 prior REVISE
- [ ] ISC-11: daily counter resets at UTC midnight using the `day_started` field in `~/.hermes/codex-review-state.json`; over-cap reviews return ESCALATE with rationale "daily review cap of 10 reached for sid"
- [ ] ISC-12: Anti: `grep -rnE 'claude -p|claude --print|--non-interactive|claude_code_sdk|anthropic\.AsyncAnthropic|anthropic\.Anthropic' agent/peer_review.py` returns 0 hits
- [ ] ISC-13: Anti: pane death mid-review does NOT silently corrupt state — orchestrator marks pane DEAD, respawns it, and ESCALATEs the in-flight review (no infinite hang)
- [ ] ISC-14: Anti: NO Opus review writes anything to MVMS or kanban under a different sid than the one under review (each review is sid-scoped only) — verified by mock and grep
- [ ] ISC-15: orchestrator survives `tmux kill-server` (host tmux restart) by treating all panes as DEAD on next dispatch and respawning serially
- [ ] ISC-16: `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2010_codex-parallel-p2-peer-review/ISA.md` exit 0 in `phase: complete`
- [ ] ISC-17: verdict parser uses regex `r'[*#\s]*VERDICT:\s+(APPROVE|REVISE|ESCALATE)\b'` tolerating bold/heading markdown wrapping — proven by test 8 in `module-specs/peer-review-orchestrator.md §13`: `**VERDICT: APPROVE**` and `## VERDICT: REVISE` both parse correctly without timing out

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from agent.peer_review import PeerReviewOrchestrator; print(PeerReviewOrchestrator)"` | prints the class |
| ISC-2 | start orchestrator; `tmux ls \| grep -c codex-review-`; check both panes have `claude` running | 2 panes, both healthy within 120s |
| ISC-3 | drive a test session through `phase: verify`; check orchestrator.review was called exactly once | mock asserts 1 call |
| ISC-4 | submit 3 reviews for sid-A and 1 for sid-B with pool size=2; trace the queue | sid-A second review queues until first completes; sid-B starts immediately on the free pane |
| ISC-5 | trigger a review; `cat /tmp/review-<sid>.md; tmux show-buffer` | file contents match spec §6 template; send-keys log shows the one-liner |
| ISC-6 | mock claude output with `... VERDICT: REVISE ...\nrationale...`; orchestrator verdict | `Verdict(kind="REVISE", rationale=...)` |
| ISC-7 | mock APPROVE; check dispatcher state | session in MERGING state; Discord post "ready to merge" |
| ISC-8 | mock REVISE; check kanban_comment was called with author=peer-review-opus; check ISA has new Decisions entry; check Discord post; check session in EXECUTING | all four side effects observed |
| ISC-9 | mock ESCALATE; check Discord ping + session stops further auto-reviews | yes |
| ISC-10 | drive 3 REVISE rounds; trigger 4th `phase: verify`; check NO pane was acquired and ESCALATE emitted directly | yes |
| ISC-11 | set day_started to yesterday in state file; submit review; check counter resets, review proceeds | counter = 1 after review |
| ISC-12 | `grep -rnE 'claude -p\|claude --print\|--non-interactive\|claude_code_sdk\|anthropic\.AsyncAnthropic\|anthropic\.Anthropic' agent/peer_review.py` | 0 hits |
| ISC-13 | mid-review, `tmux kill-session -t codex-review-0`; check orchestrator response | pane re-spawned within 60s; in-flight review ESCALATED |
| ISC-14 | run sid-A review; grep MVMS+kanban for sid-B mentions during the run | 0 hits referencing sid-B |
| ISC-15 | `tmux kill-server`; dispatch a new review; check orchestrator behavior | first dispatch logs "pool dead, respawning"; subsequent dispatches succeed |
| ISC-16 | `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2010_codex-parallel-p2-peer-review/ISA.md ; echo $?` | `0` |
| ISC-17 | mock pane output `**VERDICT: APPROVE**\nLooks good.` → `Verdict(kind="APPROVE")`; mock pane output `## VERDICT: REVISE\nISC-3 missing.` → `Verdict(kind="REVISE")`; both with regex `r'[*#\s]*VERDICT:\s+(APPROVE|REVISE|ESCALATE)\b'` | both sub-cases pass |

## Git Plan

- **Branch**: `feat/codex-parallel-p2-peer-review` off `fork/main` (after P1 lands).
- **Prerequisite**: P1's CodexSessionDispatcher must be merged and live; orchestrator binds to it.
- **Commit cadence (early + incremental)**:
  1. `chore(isa): scaffold P2 ISA + work dir`
  2. `feat(peer-review): PeerReviewOrchestrator base class + pane pool init (ISC-1, ISC-2)`
  3. `feat(peer-review): review dispatch protocol — temp file + send-keys + capture-pane parse (ISC-5, ISC-6)`
  4. `feat(peer-review): verdict handling — APPROVE / REVISE / ESCALATE branches (ISC-7, ISC-8, ISC-9)`
  5. `feat(peer-review): iteration + daily caps + auto-ESCALATE (ISC-10, ISC-11)`
  6. `feat(dispatcher): wire phase: verify auto-trigger (ISC-3, ISC-4)`
  7. `feat(peer-review): pane health check + respawn (ISC-13, ISC-15)`
  8. `test(p2): end-to-end peer-review loop integration test (ISC-7, ISC-8, ISC-9, ISC-10)`
  9. `docs(p2): operator notes — manual review still available via /handoff-to-ruflo if needed`
- **Push**: `git push fork feat/codex-parallel-p2-peer-review` after each commit.
- **PR**: against `fork/main` titled `feat(p2): Codex parallel workflow — Opus peer-review automation`.
- **Do NOT merge** until `phase: complete` per ISC-16.
- **No auto-merge label** — new module under `agent/` is sensitive per `module-specs/merge-broker.md` §5.

## Decisions

_(filled during execute)_

## Changelog

_(filled on each correction — 4-tuple format per ISA-SPEC §8)_

## Verification

_(filled during verify — probe output pasted verbatim, one block per [x] ISC)_

## Handback

- On complete: `mvms_record_completion` under project `codex-parallel-workflow` linking branch + PR + ISA path.
- For each Changelog entry: `mvms_record_lesson` under project `codex-parallel-workflow`.
- Discord notification via `~/.hermes/scripts/discord-notify.sh`.
- Kanban: `kanban_complete <card>`.
