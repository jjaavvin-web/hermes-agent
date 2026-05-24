# Recommendation — Hermes-Codex Parallel Workflow

**For:** Joseph
**From:** `codex-parallel-design` hive (2026-05-24)
**Status:** Design complete + adversarial critique landed (verdict REVISE — 3 critical findings the execution hive must address before P1 ships). No production code in this deliverable.
**Read in order:** this doc → `critique.md` → `DESIGN.md` → the per-phase ISAs. The critique's 3 criticals are summarised in §"Critique-driven amendments" below; the full text is in `critique.md`.

---

## TL;DR

Build the Codex-parallel-workflow in **five small phases** (P1-P5), gated on PR #34 merging first. Each phase ships one new module + an ISA-gated PR against `fork/main`. Total scope: ~5 small-to-medium modules, ~5 PRs, ~5 ISAs that already pass `isa_lint` clean. The design is grounded in what the Hermes codebase actually does today (every claim cites `file:line`) and re-uses 3 production-proven patterns:

1. The **interactive-Claude tmux launcher** (`ruflo-launch-interactive.template.sh:104-145`) for the Opus peer-review pane pool — already production-proven and survives the 2026-06-15 Anthropic billing change with no change.
2. The **`/api/dashboard/hives` snapshot pattern** (`dashboard_health.py:1997-2013` + `web_server.py:4308-4369`) for the `/codex-sessions` tab — filesystem-only state source + 15s cache + existing SSE channel.
3. The **kanban CAS dispatcher** (`kanban_db.py:1922-1931`) for task claim — used as-is; no new concurrency primitives.

The biggest design-time finding: the **`stateless bot reattaches by PID`** premise in the objective doesn't match reality — `codex_app_server_session.py` has no PID reattach. The design corrects it to **stateless bot reattaches by tmux session name** (`tmux ls ∩ codex_sessions.json`). This is strictly stronger because tmux survives bot restarts (RQ2). The host-reboot survival story is deferred to a future ISA that wires `thread/resume` (RQ3 — the codex protocol supports it; Hermes' adapter doesn't use it yet).

---

## Order of operations

```
[NOW]                                                         [SHIP P5]
  │                                                                │
  ▼                                                                ▼
[PR #34 merges to fork/main]
  │
  ▼
[P1 — MVP: Discord dispatcher + worktree broker + 1 codex session per thread]
  │  ISA: 20260524-2000_codex-parallel-p1-mvp (18 ISCs, E3)
  │
  ▼
[P2 — Peer-review automation: Opus tmux pane pool + REVISE/APPROVE/ESCALATE]
  │  ISA: 20260524-2010_codex-parallel-p2-peer-review (16 ISCs, E3)
  │
  ▼
[P3 — Merge broker: flock + isa_lint gate + Mergify-or-Actions auto-merge]
  │  ISA: 20260524-2020_codex-parallel-p3-merge-broker (15 ISCs, E3)
  │
  ▼
[P4 — Operator surface: /codex-sessions dashboard tab + pulse SSE wiring]
  │  ISA: 20260524-2030_codex-parallel-p4-operator-surface (14 ISCs, E3)
  │
  ▼
[P5 — Hardening: gc + /revive + Telegram retirement]
   ISA: 20260524-2040_codex-parallel-p5-hardening (19 ISCs, E3)
```

Each phase is single-hive-or-single-Hermes scope. Run each as a Ruflo hive when you want fan-out, or hand to a single Hermes session if you prefer interactive supervision. Either way, **brief the hive/session with the ISA path** — that's the contract.

---

## Prerequisite work (the load-bearing one)

**PR #34 (`feat/isa-enforcement-clean`) must merge to `fork/main` before P1's ISA scaffolds.**

Why it's load-bearing:

1. The merge broker (P3) calls `python3 scripts/isa_lint.py <ISA>` as its CheckCompleteness gate. Without `isa_lint` on the merge-target branch, the merge broker has no programmatic way to refuse incomplete ISAs.
2. The sub-agent option-A reconcile pattern (DESIGN §2 Decision B) calls `python3 scripts/isa_reconcile.py master.md _ephemeral/*.md`. Without it, parent/child Codex sessions can't merge slices mechanically.
3. **PR #34's cherry-pick skipped one commit**: `feat(isa): gate Kanban bridge task completion on the linked ISA`. The skip happened because `scripts/claude_kanban_bridge.py` was deleted on `fork/main` after the source branch was authored (objective §16). **Re-implementing that gate against current `fork/main` code is `P1-mvp.md` ISC-1.** This is the first thing P1's execution hive needs to do, and it's a non-trivial port because the surrounding kanban-bridge code shape has changed since the original commit was written.

If PR #34 doesn't merge: P1 BLOCKED, P3 BLOCKED. The whole pipeline is upstream of that PR.

---

## Critique-driven amendments (must apply before P1 ships)

The adversarial reviewer (`critique.md`) flagged three structural defects in the design that an execution hive will hit immediately if not addressed first. The defects don't invalidate the design's spine — they're patchable inside the existing module-spec boundaries — but each is a load-bearing fix and earns one extra ISC in the relevant ISA.

| # | Defect | Fix | Lands in |
|---|---|---|---|
| **C1** | **"tmux session alive" ≠ "hermes process alive."** Dispatcher routes Discord messages via `tmux send-keys` after probing `tmux has-session`. If hermes died (OOM/SIGKILL) but the tmux pane is sitting at a shell prompt, the next message gets pasted into the shell — silent message loss / arbitrary shell execution. | After `tmux has-session` returns 0, probe `tmux display-message -p -t <session> '#{pane_pid}'` + `pgrep -P <pane-pid> hermes`. Hermes dead → classify NEEDS_REVIVE. Add an ISC to `isas/P1-mvp.md` (extend ISC-11 with the pid-probe check). | **P1** (extend ISC-11) |
| **C2** | **`/revive` loses uncommitted source edits.** P5 archives the ISA markdown as `_ephemeral/orphaned-<ts>.md` but the source-file edits live in the (now-discarded) worktree. `WorktreeBroker.release` uses `--force` and drops them. | NEEDS_REVIVE banner + `/revive` handler must run `git -C <old-worktree> diff --stat` and post the output to the Discord thread before allocating a new worktree. Operator can `git stash` or cherry-pick before reviving. Add an ISC to `isas/P5-hardening.md` (extend ISC-5/ISC-6). | **P5** (extend ISC-5 + ISC-6); also note in `module-specs/discord-gateway.md §6` |
| **C3** | **Merge broker holds `flock` across `gh pr create` + Discord post** — 30-min starvation window for the whole session fleet if GitHub or Discord is slow. The mutex only needs to cover `fetch → rebase → push`; PR creation and labeling are idempotent against `fork/main`. | Release the flock after step 5 (push) in `module-specs/merge-broker.md §4`. Steps 6-9 (`gh pr create`, label, Discord post) run unlocked. Add ISC to `isas/P3-merge-broker.md` (new ISC enforcing the unlocked-after-push invariant). | **P3** (add new ISC) |

The five major findings (`critique.md` §Major) are equally real but less load-bearing — they're easy to incorporate during execution without changing the design's spine:

- **M4** Verdict regex `^VERDICT:` misses `**VERDICT: APPROVE**` (Opus markdown). Fix: `r'[*#\s]*VERDICT:\s+(APPROVE|REVISE|ESCALATE)\b'`. Add as P2 ISC.
- **M5** Mergify rule `#approved-reviews-by >= 1` is never satisfied because nothing calls `gh pr review --approve`. Fix: either P2's APPROVE path calls `gh pr review --approve --pr-number <N>` OR drop the condition from `.mergify.yml`. P3 ISA's Decisions section picks one.
- **M6** Post-merge poll `gh pr list --label auto-merge --state merged` matches operator-labeled PRs from any branch. Fix: add `--head 'codex/*'`. P3 ISC-12 probe needs the same flag.
- **M7** `WorktreeBroker._registry` empty on bot restart enables double-allocation. Fix: populate from `codex_sessions.json` at `__init__`. P1 implementation detail.
- **M8** SSE backpressure unspecified — cap event queue at 100, drop-oldest on overflow. P4 implementation detail.

The four minors (Mergify private-repo cost, pnpm `next/` docs URL, corruption-recovery silent failure, last_message_id dedup race) are noted in `critique.md` §Minor and are not blocking.

**Recommendation**: Joseph (or the P1 execution hive's lead) reads `critique.md` end-to-end before writing the first line of P1. The fixes are small but each is the kind of thing that's expensive to retrofit once shipping starts. Updating the P1 ISA to add ISC-19 for C1, the P3 ISA to add ISC-16 for C3, and the P5 ISA to extend ISC-5/6 for C2 is the lightweight path — total < 1 hour of ISA editing.

## What this design intentionally does NOT do

1. **No Tier 1 redesign.** Ruflo + Opus hive chains stay as the major-work path. Tier 2 starts where a bounded ≤5-step task arrives via Discord. The boundary is load-bearing.
2. **No Tier 3 replacement.** Surgical final-polish work stays interactive on a single Hermes session.
3. **No second-Codex peer review.** Lineage diversity (Opus reviews Codex) is the property; doubling Codex would defeat it.
4. **No `claude -p` / paid API anywhere.** Post 2026-06-15, only interactive Claude stays on Max — the Opus pane pool is the only Anthropic-billed path and it's interactive by construction.
5. **No new memory layer.** MVMS via the existing `memory_manager.py` plugin abstraction, with per-session project keys (`codex-session-<sid>`).
6. **No production code in this PR.** Everything is design, specs, and ISAs. Execution is later.

---

## Risks the operator should know about

In rough order of how-much-this-might-bite-you:

1. **`MemoryManager` has no internal write lock.** Cluster C audit confirms `memory_manager.py:317-326` is sequential-per-session only. If two sessions accidentally use the same MVMS project key, last-write-wins silently. The design mitigates this by convention (each session uses `codex-session-<sid>`), but operator-typo'd keys won't be caught at write time. Watch the MVMS supersede log if you see lessons being overwritten.
2. **No Codex `thread/resume`.** RQ3 confirmed the codex protocol supports `thread/resume`, but the Hermes adapter doesn't use it. On host reboot, every in-flight codex thread is abandoned. P5 ships `/revive` (operator-triggered fresh session); a future ISA could wire `thread/resume` for true survival.
3. **Mergify install cost on private repos.** RQ5 covers GitHub Actions as the no-third-party-tools fallback. Pick before P3 lands — both `.mergify.yml` and `.github/workflows/auto-merge.yml.disabled` are committed by P3; operator activates one.
4. **Opus pane-pool size = 2 is a soft cap.** If 8 sessions all hit `phase: verify` in a 5-min window, 6 reviews queue. Latency, not failure — but operator may want to bump the pool size to 3 once usage patterns are visible. Adjustable via orchestrator constructor.
5. **Disk pressure under 8 npm-heavy worktrees.** 8 × 2 GB = 16 GB worst case (per worktree node_modules). The 4 GB free floor in `WorktreeBroker.allocate` is the safety. If your WSL2 host gets tight, P5 should add `pnpm enableGlobalVirtualStore: true` (RQ4 — near-zero per-worktree disk delta). The P5 ISA explicitly defers this as a project choice.
6. **`MessageDeduplicator` is in-process TTL (300s).** Bot restart loses dedup state; messages within the 5-min window may be processed twice. This is the cost of statelessness; the cost is bounded.
7. **Per-worktree port range 50000-50007.** If you run other services on those ports, the broker fails to allocate. Reconfigure in `module-specs/worktree-broker.md` §4.

The adversarial critique in `critique.md` enumerates more scenarios — read it before P1 starts.

---

## What to do if you don't like a decision

The design encodes the four locked decisions (objective §3) faithfully. The two decisions that the audits *adjusted*:

- **Stateless bot reattach: PID → tmux session name.** Forced by the Codex adapter's actual capability (cluster A §"Resume semantics"). If you want true PID reattach, the path is to wire `thread/resume` in `agent/transports/codex_app_server_session.py` — that's a separate ISA, not Tier 2 scope.
- **Discord gateway adapter: extend, don't create.** Forced by the fact that `gateway/platforms/discord.py` already exists at ~5169 LOC (cluster B). If you want a fresh adapter, you'd be replacing working production code — strongly not recommended.

Everything else in §3 was buildable as locked.

---

## What I'd ship if I had budget for ONE more thing

Wire pnpm `enableGlobalVirtualStore` into `WorktreeBroker.allocate` for any project with a `pnpm-workspace.yaml`. It turns the 16 GB worst-case JS disk story into ~500 MB total (RQ4). Cheapest improvement-per-LOC in the whole design. If you do it, fold it into P1's ISC-4 (broker allocate) rather than waiting for P5.

---

## How to read the rest of this PR

1. **`DESIGN.md`** — start here. The narrative + cited claims.
2. **`architecture-diagram.md`** — when you want to see the pipeline.
3. **`collision-matrix.md`** — when you wonder "but what happens if X races Y?"
4. **`module-specs/`** — when you want to build a specific module. Each spec is ready-to-execute.
5. **`isas/P1-mvp.md`** — the FIRST thing the next hive does. All 5 ISAs pass `isa_lint` clean (verified during this hive — see `FINAL-REPORT.md` for the runs).
6. **`telegram-retirement-appendix.md`** — execute during P5.
7. **`critique.md`** — adversarial review; read before P1 starts.
8. **`audits/`** — raw research; cite these in any follow-up design work.
9. **`sources.md`** — every file:line + URL referenced.

---

## Order of execution (concrete next steps)

1. Operator merges PR #34.
2. Operator (or a fresh Ruflo hive) briefs an execution hive with `isas/P1-mvp.md`. Copy the ISA into `~/.hermes/work/20260524-2000_codex-parallel-p1-mvp/ISA.md` as the canonical home; the run-dir copy is for PR review only.
3. P1 hive ships per the ISA's Git Plan; lands the PR; ISA reaches `phase: complete` per `isa_lint`.
4. Repeat for P2, P3, P4, P5 — each waits on the previous.
5. After P5: full pipeline live, Telegram retired, gc + revive shipped.

Total estimate: ~3-5 days of hive time across the 5 phases if run sequentially. Faster with parallel hives if the operator wants — but the dependency order (P1 → P2 → P3 → P4 → P5) is real and matters; don't try to fan out P1+P2 simultaneously.

---

*If you want a different recommendation, the design's modular enough to swap parts — the merge broker can be pulled if you'd rather hand-merge; the dashboard tab can be deferred indefinitely; the Telegram retirement can be a separate hive. The only NON-removable piece is P1: without the dispatcher + broker, there's no Tier 2.*
