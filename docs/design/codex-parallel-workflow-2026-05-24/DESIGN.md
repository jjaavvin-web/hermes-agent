# Hermes-Codex Parallel Workflow — Design

**Hive ID:** `codex-parallel-design-20260524T193752Z`
**Authored:** 2026-05-24
**Status:** Design (no production code in this PR)
**Tier:** E3 — substantial single-hive design + planning
**Sibling deliverables:** `architecture-diagram.md`, `collision-matrix.md`, `module-specs/*`, `isas/P{1..5}-*.md`, `telegram-retirement-appendix.md`, `sources.md`, `RECOMMENDATION.md`

> Every architectural claim cites a `file:line` in the Hermes codebase or a sibling deliverable. Numbers without citations are bugs in this doc.

---

## 0. Executive summary

This document defines how Tier-2 work (the parallel, middle-sized slice of Joseph's three-tier execution model) runs in Hermes-Codex: 4–8 long-lived Codex sessions, one per Discord thread, driven through bounded ISA-scoped tasks, peer-reviewed by an Opus pane pool over tmux, and merged through a serialising broker that opens auto-merge-labelled PRs against `fork/main`.

The shape is decided. The audits this hive ran (`audits/cluster-A..D-*.md` + `audits/external-research.md`) settle six questions that change how the locked decisions are built:

1. **Codex subprocess reattach by PID is not supported.** `agent/transports/codex_app_server_session.py:202-272` exposes `ensure_started → run_turn → close`; the `_thread_id` lives only in Python object memory and `close()` nulls it (line 272). The stateless-bot reattach story therefore reattaches to a *tmux session* hosting the codex subprocess, not to a free-running PID. Cluster A §"Resume semantics".
2. **`gateway/platforms/discord.py` already exists** at 5169+ lines with a working `DiscordAdapter` (`discord.py:532` for the class; connect, send, edit_message, get_chat_info all implemented). The work is *extension*, not greenfield — a Codex-session dispatcher layer that sits on top of the adapter. Cluster B §"Discord tool API".
3. **`~/.hermes/discord_threads.json` is production-active** but holds the `ThreadParticipationTracker` joined-thread cap-list (`tests/gateway/test_discord_thread_persistence.py:1-95`, `gateway/platforms/helpers.py:27`), not session metadata. Session mapping needs a new file (`~/.hermes/codex_sessions.json`) — overloading the existing one would break the participation tracker.
4. **Kanban CAS is exactly as advertised** — `BEGIN IMMEDIATE` + `UPDATE … WHERE status='ready' AND claim_lock IS NULL` at `hermes_cli/kanban_db.py:1922-1931`. Heartbeat extension is CAS-guarded at `kanban_db.py:1989-2003`. Tier-2 inherits this without touching it.
5. **MemoryManager has no internal write lock** — `memory_manager.py:317-326` iterates providers in a sequential for-loop, with no thread-safety guarantee across concurrent agent sessions. The session-namespacing burden therefore falls on the provider (MVMS) and on disciplined per-session project keys, not on the manager.
6. **The interactive-Claude tmux pattern is already production-proven** — `~/.hermes/scripts/templates/ruflo-launch-interactive.template.sh:104-145` uses `tmux new-session -d` + `tmux pipe-pane -o` + `tmux capture-pane -p` + `tmux send-keys` + bounded dialog-clearing loop. The Opus peer-review orchestrator is a direct port of this pattern into a warm pane pool. No new tmux primitives required.

These six findings carry the design. The five phased ISAs in §8 land them in order with the smallest possible blast radius per phase.

---

## 1. Position in the three-tier model

Joseph's execution is tiered (objective §2). This design covers **only Tier 2**:

| Tier | Tool | Share | When |
|---|---|---|---|
| 1 — major | Claude Ruflo (multi-hive chains) | ~70% | Research, gameplan, primary execution on large multi-file/multi-module work |
| **2 — parallel** | **Hermes-Codex via Discord, 4–8 sessions, Opus-reviewed** | **~20–25%** | Bounded parallelizable middle slice: features, fixes, polish passes scoped to a few files each |
| 3 — surgical | Single Hermes agent (interactive) | ~5–10% | Final surgical polish, last tweaks, human-loop close-out |

Boundary rules (not relitigated here):
- Anything that needs a >5-step gameplan stays on Ruflo (Tier 1).
- Anything that needs surgical human eyes stays on a single Hermes session (Tier 3).
- Tier 2 is the queue of well-scoped middle-sized work that fans out cleanly. A single Tier-2 task occupies one Discord thread + one Codex session + one worktree + at most one Opus reviewer pane at a time.

Concurrency target (objective §1, refined by audits):

- **8 concurrent Codex sessions** maximum, soft-capped by disk + the operator's mental headroom for parallel review.
- **2 concurrent Opus reviewer panes** in the warm pool by default (single global cap; further reviews queue).
- **Realistic peak**: 8 sessions × ~2 GB worktree each ≈ 16 GB disk worst case (Node-heavy); typical (mostly Python) ≈ 4 GB. Within disk budget on the dev WSL2 host.

---

## 2. Locked decisions and what the audits did to them

Objective §3 + §5 list the locked decisions. The audit recalibration is below.

### Decision A — Peer reviewer = Claude Opus 4.7 (not a second Codex)
**Buildable as locked.** PROVIDER-STACK.md:38 names `claude-opus-4.7` as Orchestrator; the audit verified Max OAuth at `~/.claude/.credentials.json` (cluster D, `web_server.py:1378`, `dashboard_health.py:30`). The lineage-diversity argument stands: Opus reading a Codex diff catches what Codex missed during write.

### Decision B — Sub-agent semantics = option A (shared worktree, ISA reconcile)
**Buildable as locked.** Reconcile pattern is fully specified at `ISA-SPEC.md:107-113` and the `isa_reconcile.py:146-261` tool (cluster C) implements it ID-keyed with abort-on-drift. A parent Codex spawning a child Codex in the same worktree is straightforward — both sessions reuse the same `CodexAppServerSession` factory, with the child driving an ephemeral `_ephemeral/<feature>.md` slice while the parent owns the master ISA.

### Decision C — Discord bot is stateless
**Reframed.** The literal "reattach to existing subprocess by PID" mechanism does not work — cluster A confirmed Codex sessions have no PID file, no resume RPC, and `close()` nulls `_thread_id` (`codex_app_server_session.py:262-272`). The corrected design:

- Each Codex session runs inside a *named tmux session* (e.g. `codex-sess-<thread_id_short>`), exactly as `ruflo-launch-interactive.template.sh:104` already does for Ruflo hives.
- The Discord bot's only persistent state is `~/.hermes/codex_sessions.json` (a new file — see §6.3 for why not `discord_threads.json`).
- On bot restart, the bot runs `tmux ls -F '#{session_name}'` (cf. `dashboard_health.py:447-458`), matches against `codex_sessions.json`, and reattaches by **tmux session name** (not by PID).
- If the tmux session is gone but the Discord thread is still active, the bot posts a "needs revive" banner with a `Revive` button — operator hits it to fork a fresh session, archive the old ISA progress as `_ephemeral/orphaned-<ts>.md`, and resume with a new tmux session.

This preserves the spirit of decision C (state lives on disk + tmux + worktree, not in the bot process) while honouring the codex transport's actual capability.

### Decision D — Merge target = PR to `fork/main` with auto-merge label
**Buildable as locked.** Git remote setup matches: `origin` = `NousResearch/hermes-agent`, `fork` = `jjaavvin-web/hermes-agent`. Auto-merge label policy is defined in `module-specs/merge-broker.md`.

### Decision E (constraint, objective §5) — Opus invocation via interactive tmux (Max OAuth only)
**Buildable as locked, with one caveat.** Cluster D §"ruflo-launch-interactive.template.sh" confirms every tmux primitive needed: detached session, pipe-pane logging, capture-pane verdict-read, send-keys prompt injection, bounded dialog-clearing loop. The caveat: PROVIDER-STACK.md:38 says Opus is invoked via `claude -p` *today* — after 2026-06-15 that path leaves Max. The peer-review orchestrator therefore launches the Opus pane the same way Ruflo does (`tmux new-session -d -s codex-review-<N> 'claude'`, no `-p`), so it crosses the 2026-06-15 cutoff without change.

---

## 3. Top-level architecture

```
                                  ┌──────────────────────┐
                                  │   Operator (Joseph)  │
                                  └──────────┬───────────┘
                                             │  Discord
                                             ▼
                            ┌────────────────────────────────┐
                            │  DiscordAdapter                │   gateway/platforms/discord.py:532
                            │  (existing, ~5k LOC)           │   connect/send/edit/get_chat_info
                            └──────────┬─────────────────────┘
                              thread events │ (new wiring)
                                            ▼
                            ┌────────────────────────────────┐
                            │  CodexSessionDispatcher        │   NEW — discord-gateway spec §3
                            │  ─ thread→session map          │
                            │  ─ codex_sessions.json (flock) │
                            └──┬─────┬─────┬─────────────────┘
                  thread_create │ msg │ archive
                                ▼     ▼     ▼
        ┌──────────────────┐  per-thread: spawn / route turn / close
        │ WorktreeBroker   │◀──────────────────────┐
        │ allocate/release │                       │
        │ (git worktree)   │                       │
        └────────┬─────────┘                       │
                 ▼                                 │
        ┌────────────────────────┐                 │
        │ tmux session           │                 │
        │ codex-sess-<sid>       │                 │
        │ ┌────────────────────┐ │                 │
        │ │ CodexAppServerSes  │ │ codex_app_server_session.py:202-272
        │ │ ensure_started     │ │                 │
        │ │ run_turn (turn loop)│ │                 │
        │ │ close              │ │                 │
        │ └────────────────────┘ │                 │
        └───────────┬────────────┘                 │
       phase: verify│ trigger                      │
                    ▼                              │
        ┌────────────────────────┐                 │
        │ PeerReviewOrchestrator │                 │
        │ tmux pane pool (N=2)   │ NEW — peer-review-orchestrator spec
        │ ┌────────────────────┐ │                 │
        │ │ codex-review-0     │ │ interactive `claude` (Opus 4.7, Max OAuth)
        │ │ codex-review-1     │ │ send-keys prompt / capture-pane verdict
        │ └────────────────────┘ │                 │
        └───────────┬────────────┘                 │
   VERDICT: APPROVE │ REVISE / ESCALATE            │
                    ▼                              │
        ┌────────────────────────┐                 │
        │ MergeBroker            │ NEW — merge-broker spec
        │ serialized push        │                 │
        │ rebase + auto-label PR │                 │
        └───────────┬────────────┘                 │
              merged│ → release worktree ───────────┘
                    ▼
        ┌────────────────────────┐
        │  fork/main             │
        │  github auto-merge     │
        └────────────────────────┘

  Observability sidecar:
  ┌──────────────────────┐
  │ /codex-sessions tab  │  NEW — codex-sessions-tab spec; mirrors /hives at dashboard_health.py:1997-2013
  │  via /api/dashboard  │  pulse SSE channel at web_server.py:4308-4369 (existing)
  └──────────────────────┘
```

The full ASCII diagrams are in `architecture-diagram.md`. State machines per module live with each module spec.

---

## 4. Data flow for the canonical happy path

A new Tier-2 task arrives. The flow:

1. **Operator → Discord**: operator creates a thread in a configured Discord channel and posts the task description (one bounded ask, ≤ 1 short paragraph + optional ISA path).
2. **DiscordAdapter intake**: `discord.py:532` receives `thread_create` and forwards it as a `MessageEvent` to the gateway runner; the runner dispatches to `CodexSessionDispatcher` (new — see `module-specs/discord-gateway.md`).
3. **Worktree allocate**: dispatcher asks `WorktreeBroker.allocate(session_id)` (new — see `module-specs/worktree-broker.md`). The broker runs `git worktree add ~/.hermes/codex-wt/<sid>/ codex/<sid>/<isa-slug> origin/main` via the `git_janitor.py:50-55` subprocess pattern.
4. **tmux launch**: dispatcher runs `tmux new-session -d -s codex-sess-<sid> -c <worktree>` and within that pane launches `hermes` with `HERMES_KANBAN_TASK=<kanban_card_id>` and the codex transport (`run_agent.py:16050-16168` calls `CodexAppServerSession.ensure_started` → spawns the `codex app-server` subprocess inside the worktree).
5. **Session-state persist**: dispatcher writes the new row `{thread_id, session_id, kanban_card_id, worktree_path, tmux_session, isa_id, created_at}` to `~/.hermes/codex_sessions.json` under `flock` (per objective §7 #6).
6. **Turn 0**: dispatcher posts the operator's task back into the tmux session via `tmux send-keys` (mirroring the dialog-clearing pattern at `ruflo-launch-interactive.template.sh:134-138`). The codex transport's `run_turn` executes, emitting events back through the codex projector (`codex_event_projector.py`).
7. **Inbound chat**: each subsequent Discord message in the thread becomes another `run_turn` on that session. Codex output is routed back to the thread via the existing outbound `tools/discord_tool.py` (`discord_tool.py:908`).
8. **Phase transition**: when the ISA reaches `phase: verify` (per `ISA-SPEC.md:131-140`), the session-side ISA driver writes a verify-marker file in the worktree and emits a `peer-review-requested` event to the dispatcher.
9. **Peer review dispatch**: `PeerReviewOrchestrator` claims a free pane from the warm pool (`codex-review-0` / `codex-review-1`), writes `/tmp/review-<sid>.md` containing `{ISA path, diff blob ≤20KB or summary, prompt}`, sends `tmux send-keys` with a one-liner, then polls `tmux capture-pane -p` for the `VERDICT:` sentinel + idle-N-seconds (see `module-specs/peer-review-orchestrator.md`).
10. **Verdict handling**:
    - `APPROVE` → handoff to `MergeBroker`.
    - `REVISE` → orchestrator posts review comments via `kanban_comment` (`kanban_tools.py:521`), appends new ISCs to the ISA, re-enters `phase: execute`. Iteration limit: 3 rounds; round 4 auto-escalates to operator.
    - `ESCALATE` → orchestrator pings the operator in the Discord thread and stops the session at `phase: verify`.
11. **Merge**: `MergeBroker.merge(session_id)` (new — see `module-specs/merge-broker.md`) acquires the global merge mutex, runs `git fetch origin && git rebase origin/main` inside the worktree, runs `isa_lint` against the master ISA (cluster C §"isa_lint.py — rules enforced"), pushes the branch, opens a PR against `fork/main` via `gh pr create`, applies the auto-merge label conditional on the change classification (see merge-broker spec §5).
12. **Cleanup**: post-merge, the broker posts a completion summary to the Discord thread (via `discord_tool.py`), invokes `WorktreeBroker.release(session_id)`, marks the kanban card complete (`kanban_complete` at `kanban_tools.py:360`), removes the row from `codex_sessions.json`, and archives the Discord thread.

The unhappy paths (subprocess death, OAuth expiry, conflict, escalation, bot restart) are enumerated in `collision-matrix.md` and the per-module specs.

---

## 5. The Codex session contract (what each session is)

A "Codex session" in this design is the 5-tuple:

| Element | Source of truth | Citation |
|---|---|---|
| `thread_id` | Discord | `gateway/platforms/discord.py:532` |
| `session_id` | dispatcher-generated UUID4 | new |
| `tmux_session` | OS-side tmux | `dashboard_health.py:447-458` |
| `worktree_path` | git | `git_janitor.py:50-100` |
| `kanban_card_id` | kanban SQLite | `kanban_db.py:753-882` |
| `isa_id` | ISA-SPEC §2 | `ISA-SPEC.md:22-23` |

The session lives as long as the Discord thread is non-archived AND the tmux session is alive AND the worktree exists. Any one missing → session is in a degenerate state and the dispatcher's gc moves it to either "needs revive" (worktree gone) or "orphaned" (tmux dead but worktree present, recoverable).

There is **no in-bot session object** holding state beyond what's on disk. The bot is a router; the truth is on the filesystem.

---

## 6. Why these five modules and not others

Objective §7 enumerates the gaps. Mapped to modules:

### 6.1 `gateway/platforms/discord.py` (extension, not new file)

The audit's biggest finding: this file already exists. The design therefore *extends* it with:
- A `CodexSessionDispatcher` class (new file, `gateway/codex_session_dispatcher.py`) that the `DiscordAdapter` calls into on `thread_create`/`thread_message`/`thread_archive`.
- Slash command handlers (`/spawn`, `/pause`, `/resume`, `/kill`, `/status`, `/handoff-to-ruflo`) registered against the existing adapter's command-sync state (`discord.py:901-926`).
- No invasive surgery on the adapter's existing send/edit/receive paths.

Spec: `module-specs/discord-gateway.md`.

### 6.2 `agent/worktree_broker.py` (new)

Greenfield in the dashboard/agent layer per cluster D §"Existing git-shell-out patterns". Public API: `allocate(session_id) → WorktreePath`, `release(session_id)`, `gc()`. Mirrors `git_janitor.py:50-55` subprocess pattern; layered on top of the existing `git_janitor` inventory machinery (which is already worktree-aware via `inventory_worktrees` at `git_janitor.py:68`).

Spec: `module-specs/worktree-broker.md`.

### 6.3 `agent/peer_review.py` (new)

Greenfield. The warm tmux pane pool is the direct port of the interactive launcher template (`ruflo-launch-interactive.template.sh:104-145`) into a Python orchestrator that:
- Spawns N panes at startup (`tmux new-session -d -s codex-review-<N> 'claude'`).
- Drives them via `tmux send-keys` (prompt injection via temp file to avoid quoting hell on multiline diffs).
- Polls `tmux capture-pane -p` for the `VERDICT:` sentinel with idle-N-second detection.
- Handles pane death + warm-pool rebalance.

Spec: `module-specs/peer-review-orchestrator.md`.

### 6.4 `agent/merge_broker.py` (new)

Greenfield. A serialised merge gate that:
- Holds a global mutex (`flock` on `~/.hermes/codex-merge.lock`).
- Rebases inside the worktree.
- Pushes + opens PR via `gh pr create`.
- Labels with `auto-merge` conditional on the change classification.
- Releases the worktree post-merge.

Spec: `module-specs/merge-broker.md`.

### 6.5 `/codex-sessions` dashboard tab (additive)

Mirrors the existing `/hives` pattern (`dashboard_health.py:1997-2013` for the snapshot, `web_server.py:4308-4369` for the SSE channel). The tab adds two routes:
- `GET /api/dashboard/codex-sessions` — list snapshot, JSON shape mirroring `_build_hives_snapshot`.
- `GET /api/dashboard/codex-sessions/{sid}` — single-session detail (ISA verbatim, diff, review history).
- Live updates piggyback on the existing pulse SSE event channel; the tab consumes `event: pulse.activity` with a `kind: codex-session` discriminator.

Spec: `module-specs/codex-sessions-tab.md`.

---

## 7. Concurrency — what's safe and what isn't

Full table in `collision-matrix.md`. The headline:

- **CAS-safe today** (no design work needed): kanban claim acquisition (`kanban_db.py:1922-1931`), heartbeat extension (`kanban_db.py:1989-2003`), per-board SQLite isolation.
- **Design-handled by isolation**: same-file two-session write (one worktree per session); same-branch force-push (branch names use the immutable `session_id`); merge race on `fork/main` (merge broker serialises with `flock`).
- **Design-handled by namespacing**: MVMS writes — MemoryManager itself has no lock (`memory_manager.py:317-326`), so each session uses a namespaced project key `codex-session-<sid>` to avoid collision; conflicts within the same project key fall back to last-write-wins, which is acceptable because sessions don't share project keys.
- **Design-handled by quota**: Opus review pane concurrency (default 2). Extra reviews queue rather than spawning more panes; Max OAuth has its own rate cap and exceeding it costs response latency, not money.
- **Design-handled by central broker**: dev-server port allocation (range `50000-50007`, one port per session, returned on release).
- **Per-worktree on-demand install**: each worktree gets its own `node_modules` (objective §7 #6 — locked). Disk worst case 16 GB; typical 2–4 GB; acceptable on the dev WSL2 host.

---

## 8. Phased implementation plan

Each phase has its own ISA in `isas/`. Summary in order of execution:

| Phase | ISA | Scope | Prereqs |
|---|---|---|---|
| **P1 — MVP** | `isas/P1-mvp.md` | Discord gateway adapter extension (single-thread happy path); WorktreeBroker (allocate/release only, no gc); one Codex session per thread; manual peer-review handoff (operator decides); no dashboard tab | PR #34 merged (isa-lint on fork/main) — see §11 |
| **P2 — Peer review automation** | `isas/P2-peer-review.md` | Auto-trigger Opus on `phase: verify`; iteration loop with cap=3; pane-pool warm/busy/dead lifecycle; reviews-per-session/day cap | P1 landed |
| **P3 — Merge automation** | `isas/P3-merge-broker.md` | MergeBroker module; PR auto-merge label policy; rebase + conflict escalation | P2 landed (review must approve before auto-merge) |
| **P4 — Operator surface** | `isas/P4-operator-surface.md` | `/codex-sessions` dashboard tab; pulse SSE wiring with `kind: codex-session` discriminator; slash-command surface | P3 landed |
| **P5 — Hardening** | `isas/P5-hardening.md` | WorktreeBroker.gc(); bot restart / tmux-rebind PID revive; Telegram retirement (delete `gateway/platforms/telegram.py`, scrub docs per `telegram-retirement-appendix.md`) | P4 landed |

Each ISA includes a full Git Plan section (per `ISA-SPEC.md:58` and the "feedback-hives-must-use-git-explicitly" guidance the operator has codified for hives).

---

## 9. The Anthropic 2026-06-15 billing constraint

`PROVIDER-STACK.md:7-16` is unambiguous: from 2026-06-15, `claude -p` / `--print` / Agent SDK / Claude Code GitHub Actions all leave Max for a paid pool. Only interactive Claude Code stays on Max.

The design honours this:

| Component | Invocation | Max-safe post-2026-06-15? |
|---|---|---|
| Codex sessions | `codex app-server` subprocess (not Claude — separate ChatGPT OAuth path per PROVIDER-STACK.md:46) | N/A — this is OpenAI's billing, not Anthropic's |
| **Peer reviewer Opus pane** | `tmux new-session -d 'claude'` (interactive, no `-p`) | ✅ stays on Max |
| Anything inside an agent turn that calls Claude | already routed via the agent's existing transport — no `claude -p` paths introduced by this design | ✅ unchanged |
| h2reviewer (`PROVIDER-STACK.md:76-79`) | misroutes to paid API today | ⚠️ pre-existing defect; not in scope; do not extend this design through h2reviewer |

The peer-review orchestrator's invocation pattern is the load-bearing one. Spec: `module-specs/peer-review-orchestrator.md` §"Pane lifecycle". Any implementation that uses `claude -p`, `claude --print`, or the Agent SDK from this codepath is a defect to fail review on.

---

## 10. Telegram retirement

Telegram is being retired (objective §4). The audit (cluster B §"Telegram-specific plumbing") inventoried the deprecation surface:

- Live code: `gateway/platforms/telegram.py` (5140 lines, `class TelegramAdapter` at line 317), `gateway/platforms/telegram_network.py`, `hermes_state.py:2387-2416` (`telegram_dm_topic_mode`, `telegram_dm_topic_bindings` tables).
- Config keys: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_URL`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_ALLOWED_USERS`, 6 `HERMES_TELEGRAM_*` knobs, `TELEGRAM_PROXY`.
- Toolset registrations: `toolsets.py:400,533`.
- Operator docs: `WORKFLOW-LESSONS.md:111` rule #7 ("Always wire `telegram-notify.sh`"); `AGENTS.md:37,938`; `CONTRIBUTING.md:187`; `cli-config.yaml.example:647`; release notes.
- Discord cutover already advanced: `discord-notify.sh` is the active operator-launch notifier (cluster B §"discord-notify.sh contract — wired into operator launch templates — CONFIRMED").

Full inventory in `telegram-retirement-appendix.md`. The actual deletes/edits are P5's ISA (`isas/P5-hardening.md`); this design doc only enumerates.

---

## 11. Prerequisite work

Before P1 can land, **PR #34 (`feat/isa-enforcement-clean`) must merge to `fork/main`**. Reasons (objective §16):

1. The Tier-2 pipeline depends on `isa-lint` to gate ISA `phase: complete` before merge — without it the merge broker has no way to assert the ISA passed CheckCompleteness (`ISA-SPEC.md:131-140`).
2. The Tier-2 pipeline depends on `isa-reconcile` for sub-agent option-A merges (`ISA-SPEC.md:107-113`) — without it parent/child Codex sessions cannot merge ephemeral slices back into the master ISA mechanically.
3. **The kanban-bridge ISA gate** (commit `feat(isa): gate Kanban bridge task completion on the linked ISA`) was skipped during the PR #34 cherry-pick because `scripts/claude_kanban_bridge.py` was deleted on `fork/main`. **Re-implementing that gate against current fork/main is the first ISC of P1's ISA** (per objective §16). Without it, the merge broker can complete a kanban card whose ISA still has open `[ ]` ISCs — defeating the gate's whole purpose.

The recommendation (`RECOMMENDATION.md`) opens with this prerequisite. The watcher posts the same caveat on completion.

---

## 12. What this design intentionally does NOT do

Per objective §13 anti-patterns and the Tier boundaries:

- **No replacement for Ruflo (Tier 1).** This design's surface starts where a well-scoped Tier-2 task arrives; gameplan and large-scale work continues to flow through Ruflo hives unchanged.
- **No replacement for the surgical single-Hermes (Tier 3) close-out.** Discord-driven sessions are for fan-out; final-polish work the operator wants to drive interactively stays interactive.
- **No second-Codex peer review.** Decided against in objective §3 #1; lineage diversity (Opus) is the point.
- **No kanban fan-out for in-feature decomposition.** Sub-agent semantics are option A only (shared worktree, ISA reconcile). Kanban-fanout is reserved for genuinely independent work and is out of scope for Tier 2.
- **No pooled worker reuse for sub-agents.** A child Codex is a fresh subprocess in the same worktree, not a long-lived worker.
- **No new memory layer.** MVMS via the existing `memory_manager.py` plugin abstraction with per-session namespacing.
- **No production code in this PR.** The output is ISAs, specs, and diagrams. Execution comes after.

---

## 13. Open questions deferred to the operator

These are choices the design surfaced but did not resolve, because they're operator judgment:

1. **Per-session port range.** §7 picks `50000-50007` (8 ports). If the operator runs other services on those ports, pick another contiguous range.
2. **Discord channel for new threads.** This design assumes one configured channel where operator creates threads. Multi-channel support (e.g., one per team) is straightforward but not in scope.
3. **Auto-merge label policy edge cases.** `merge-broker` spec §5 proposes a default rule (label if no files in `agent/`, `gateway/`, `auth/`, `migrations/`, `pyproject.toml`, `package*.json`). Operator may want to broaden/narrow the deny-list.
4. **Reviews-per-session-per-day cap.** Default 10 (objective §7 #3). Operator may want a lower bound during initial rollout.
5. **Bot restart "needs revive" UX.** Design spec proposes a Discord button; operator may prefer a `/revive` slash command.
6. **Should `~/.hermes/codex_sessions.json` be a new file or a sub-key of `discord_threads.json`?** Design picks new file to avoid breaking the existing `ThreadParticipationTracker`. Operator can override.

All are revisable post-P1.

---

## 14. Sources

`sources.md` lists every Hermes file:line examined and every external URL fetched. Every claim in this doc references one of those.

---

*End of DESIGN.md. Reviewers: cross-check every `file:line` citation against the cluster audit reports in `audits/`. If any citation is wrong, the design is wrong; fix it.*
