# Concurrency Collision Matrix — Hermes-Codex Parallel Workflow

Companion to `DESIGN.md` §7 and the per-module specs. For every shared resource: the risk, the design that handles it, the failure mode if the design is violated, and the file:line citation backing the claim.

Read alongside `audits/cluster-C-kanban-memory-isa.md` (CAS findings) and `audits/cluster-A-codex-sessions.md` (Codex session concurrency).

---

## 1. Headline summary

The system targets **8 concurrent Codex sessions** with **2 concurrent Opus reviews** and **1 serialized merge stream**. The concurrency story splits into five categories:

| Category | Resource class | How handled |
|---|---|---|
| **Already CAS-safe** | kanban claim, heartbeat extension | reuse existing primitives |
| **Isolated by design** | file edits, branches, worktrees, dev-server ports | one-per-session, no sharing |
| **Serialized by broker** | `fork/main` push, merge sequencing | global `flock` mutex |
| **Namespaced by convention** | MVMS writes | per-session project key |
| **Quota-capped** | Opus review concurrency, reviews/day per session | pool size + per-session counter |

---

## 2. Full matrix

| Resource | Risk | Design | Failure mode if violated | Citations |
|---|---|---|---|---|
| Same file edited by two sessions | overwrite, lost work | One git worktree per session, `~/.hermes/codex-wt/<sid>/`; sessions never see each other's files | — (structurally impossible) | `git_janitor.py:50-100` (worktree subprocess pattern), `module-specs/worktree-broker.md` §3 |
| Same branch force-pushed twice | history rewrite, lost commits | Branch name = `codex/<sid>/<isa-slug>`; `<sid>` is a UUID4 assigned once at session allocate; branches are never reused | — (structurally impossible) | `DESIGN.md` §5; ISA-SPEC §2 (id immutability) |
| Kanban claim double-claim | two workers pick up one task | `BEGIN IMMEDIATE` + `UPDATE … WHERE status='ready' AND claim_lock IS NULL`; `cur.rowcount != 1` → loser returns None | (cannot happen at SQLite WAL level) reaper extends live-PID claims | `hermes_cli/kanban_db.py:1922-1931`, `kanban_db.py:2006-2116` |
| Kanban heartbeat race | stale claim wrongly extended | `heartbeat_claim()` CAS on `claim_lock` identity guard | wrong session extends a stolen claim → audit fails | `kanban_db.py:1989-2003` |
| Kanban idempotency-key duplicate insert | two concurrent creates with same key | Source admits "race is acceptable" — both may insert | duplicate cards; manual dedup | `kanban_db.py:1329-1340` (cluster C §"What is NOT safe") |
| MVMS writes from N sessions | last-write-wins on overlapping keys | Each session uses project key `codex-session-<sid>` (no overlap by construction); provider chooses its own thread-safety story | If two sessions accidentally use the same project key, last write wins silently | `agent/memory_manager.py:317-326`; cluster C §"Memory layer" |
| MVMS write during memory_manager iteration | race inside `MemoryManager.sync_all` | `MemoryManager` provides no lock; protected only by the GIL between sequential calls. Provider must serialize internally if needed | provider-specific (MVMS appears to serialize per project key — verify in MVMS provider impl) | `agent/memory_manager.py:317-326`; cluster C §"Concurrent-write failure mode" |
| Dev server ports (`hermes` plugins booting on a port) | `EADDRINUSE` | Central port broker `~/.hermes/codex-ports.json` (flock), range `50000–50007`, one per session; returned on release | session blocks (sleeps) waiting for port; bot logs warning | `module-specs/worktree-broker.md` §4 |
| `node_modules` concurrent writes | npm install corruption | **Per-worktree install on-demand (LOCKED in objective §7 #6).** Worktree broker detects `package.json`, runs `pnpm install` (preferred per external-research RQ4) or `npm install` at first JS-touching turn; worktrees have separate directories so install collision is impossible | install collision impossible (separate dirs); cost is disk + first-build latency. Worst case 8 × ~2 GB = 16 GB; pnpm `enableGlobalVirtualStore` shrinks this to ~200-500 MB total | `module-specs/worktree-broker.md` §5; `audits/external-research.md` RQ4 |
| Push to `fork/main` race | two PRs push concurrently → second push rejected (`non-fast-forward`) | MergeBroker holds `flock ~/.hermes/codex-merge.lock` during the entire `fetch → rebase → push → pr-create → label` sequence | second merge waits; queue depth visible on `/codex-sessions` tab | `module-specs/merge-broker.md` §3 |
| `discord_threads.json` write race | corrupt JSON | **Unchanged from current production behavior** — single writer (`DiscordAdapter` instance) + atomic_replace already in place; new design does NOT touch this file | (already handled by existing adapter) | `tests/gateway/test_discord_thread_persistence.py:1-95`; cluster B §"discord_threads.json — actual content vs objective's claim" |
| `codex_sessions.json` write race | corrupt JSON | Single bot writer + `flock` + atomic-tempfile-rename (mirrors `telegram.py:1077-1133`'s `atomic_replace`) | If bot dies between flock-acquire and rename, state recovers from worktree + tmux ls + Discord thread enumeration on next start | `module-specs/discord-gateway.md` §5 |
| Opus review pane storm | 8 sessions hit `phase: verify` simultaneously, all want a reviewer | Pane pool size = 2 by default; reviews FIFO-queued by the orchestrator; per-session-per-day cap = 10 | extra reviews queue (latency, not failure); per-day cap enforces a hard upper bound | `module-specs/peer-review-orchestrator.md` §4 |
| Opus pane death mid-review | verdict never captured | Orchestrator marks pane DEAD on health-check fail (5-min hard timeout + idle-detection); respawns pane via `tmux new-session`; current review auto-ESCALATEs to operator | review escalates to operator (Discord ping); reviewer pool down to N-1 until respawn completes | `module-specs/peer-review-orchestrator.md` §6 |
| Codex subprocess death mid-turn | session loses progress | `codex_app_server_session.py:425-436` detects via `is_alive()` each loop iter; returns `TurnResult(should_retire=True)`; dispatcher transitions session to ORPHANED, surfaces "needs revive" in the Discord thread | session marked NEEDS_REVIVE; operator hits revive button (P5) or `/revive` slash command to spawn a fresh session and re-claim from last commit | `audits/cluster-A-codex-sessions.md` §"Subprocess lifecycle" + §"Edge cases" #1 |
| tmux session killed externally | bot rebinding finds no session | On bot start: `tmux ls` ∩ `codex_sessions.json` → live; difference → ORPHANED + NEEDS_REVIVE | session listed as needs-revive in `/codex-sessions` tab; operator triggers revive | `module-specs/discord-gateway.md` §6 |
| Bot process death | in-flight Discord acks dropped | Discord delivers events to long-poll/gateway; missed events on restart are recoverable by re-reading thread history (existing `MessageDeduplicator` in `gateway/platforms/helpers.py` handles double-delivery on a 5-min TTL) | A message during the bot-down window may be lost if it falls outside dedup window AND the bot doesn't replay; design assumes bot uptime SLO of >99% (single-host PM2/systemd) | `audits/external-research.md` RQ2; `gateway/platforms/helpers.py:27` |
| Concurrent ISA write by parent + child Codex (option A) | merge corruption | Parent owns master ISA; child owns `_ephemeral/<feature>.md` slice; parent calls `isa_reconcile.py` (which is ID-keyed, abort-on-drift) when child completes | If child invents an ISC ID, reconcile aborts and surfaces "DRIFT" to parent; no silent corruption | `ISA-SPEC.md:107-113`; `scripts/isa_reconcile.py:146-261` (cluster C) |
| Two reviews queued for the same session | duplicate work, conflicting verdicts | Orchestrator dedups by `session_id` in the queue (one in-flight + zero queued per sid at most) | (cannot happen — dedup in queue) | `module-specs/peer-review-orchestrator.md` §4 |
| Auto-merge race (PR labeled before required checks finish) | PR merged with red CI | Mergify rule requires `check-success = ci AND label = auto-merge`; if label is added before CI passes, Mergify waits | (cannot happen — Mergify gates label + checks) | `audits/external-research.md` RQ5; `module-specs/merge-broker.md` §5 |
| ISA reconcile race (two slices submitted for same master concurrently) | one slice's verification block overwrites the other | Reconcile is **per call** atomic — but two callers writing master simultaneously is not coordinated by the tool. MergeBroker holds the same `flock` for any reconcile-then-merge sequence | If reconcile is called outside the merge broker (e.g. operator hand-runs it during a merge), last write wins | `audits/cluster-C-kanban-memory-isa.md` §"Open questions" |
| Two dispatchers in one bot (e.g. plugin double-load) | duplicate session creation per event | Bot enforces single `CodexSessionDispatcher` instance via module-level singleton + `_acquire_platform_lock` (already used by `discord.py:632` for the bot-token lock) | duplicate sessions on the same thread → second allocate gets a different `<sid>` but both write to `codex_sessions.json`. Bot start-of-day check de-dupes by thread_id (last allocate wins; older session marked ORPHANED) | `gateway/platforms/discord.py:632` (`_acquire_platform_lock`) |
| `~/.hermes` disk exhaustion under 8 worktrees | session writes fail | Worktree broker reads `df -P ~/.hermes` before allocate; refuses new session if free < 4 GB; gc (P5) reclaims orphaned worktrees on a 5-min tick | dispatcher reports "ENOSPC" to the Discord thread; operator either triggers gc or frees disk | `module-specs/worktree-broker.md` §7 |
| Telegram + Discord double-handling of the same task during retirement window | duplicate work | Per `telegram-retirement-appendix.md` and P5 ISA: Telegram adapter is fully deleted in P5, not just disabled. Before deletion, set `TELEGRAM_BOT_TOKEN=""` in env so the adapter refuses to connect | (cannot happen post-P5; during P1-P4 transition, operator should already not be sending Tier-2 work via Telegram per current practice) | `telegram-retirement-appendix.md` §1 |

---

## 3. What's intentionally NOT serialized

The design accepts these races as the cheapest acceptable behavior:

| Resource | Race | Why we accept it |
|---|---|---|
| Multiple read-only `kanban_show` calls | none material | reads are SELECT-only; SQLite WAL allows concurrent readers |
| Pulse SSE event ordering | events may arrive out-of-order at the browser | UI is idempotent — `kind: codex-session` events carry `sid` + `last_updated`, browser reorders by timestamp |
| `~/.hermes/codex-session-status.json` read by dashboard while dispatcher writes | dashboard may read mid-write | Same pattern as `~/.hermes/.ruflo-status.json` (which the existing /hives tab handles fine — see `dashboard_health.py:471-478`); writer uses atomic_replace |
| Two Discord threads in different channels working on overlapping ISA scopes | conflicting work | Out of scope for this design — operator is responsible for scoping work to non-overlapping ISAs |

---

## 4. Quantification

The numbers backing the design (objective §12 "Quantify"):

| Quantity | Value | Source |
|---|---|---|
| Concurrent Codex sessions (target) | 8 | objective §1 |
| Concurrent Opus reviews (default cap) | 2 | objective §7 #3 |
| Reviews per session per day (cap) | 10 | objective §7 #3 |
| Peer-review iteration limit | 3 rounds | objective §7 #3 |
| Per-review timeout | 5 min | objective §7 #3 |
| Pane idle-detection threshold | 15 s after `VERDICT:` sentinel | `module-specs/peer-review-orchestrator.md` §3 |
| Review payload max | 20 KB raw diff (summarize above) | objective §7 #3 |
| Per-worktree disk (heavy JS) | ~2 GB worst case | `audits/external-research.md` RQ4 |
| Per-worktree disk (pnpm globalVirtualStore) | ~200-500 MB total across all worktrees | RQ4 |
| 8-session peak disk (npm path) | ~16 GB | RQ4 |
| 8-session peak disk (pnpm path) | ~0.5 GB | RQ4 |
| Free-disk floor before refusing allocate | 4 GB | this doc §2 |
| Port range | `50000–50007` (8 ports) | objective §7 #6 |
| Kanban claim TTL | 900 s (`DEFAULT_CLAIM_TTL_SECONDS`) | `kanban_db.py:101` |
| Pulse SSE heartbeat | 15 s | `web_server.py:4354-4356` |
| Pulse SSE health probe | 10 s | `web_server.py:4332-4340` |
| /codex-sessions snapshot cache | 15 s (mirror `_HIVES_TTL`) | `dashboard_health.py:55-56` |

---

*End of collision-matrix.md.*
