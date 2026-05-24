# Adversarial Critique — Codex Parallel Workflow Design

## Verdict
- **revise-before-ship** — Two critical correctness holes in P3 and a silent data-loss scenario on /revive must be closed before the design is executed as written. The remaining concerns are fixable during P2-P5 without blocking P1.

---

## Critical concerns (must fix before P1 lands)

### 1. Bot-restart "reattach" conflates tmux-session-alive with hermes-process-alive — silent message loss on real restarts

**Where:** `DESIGN.md §2 Decision C`, `architecture-diagram.md §6`, `audits/cluster-A-codex-sessions.md §"Resume semantics"`, `audits/external-research.md RQ3`

**Why it breaks:** The design's reattach story is: bot restarts → `tmux ls` finds `codex-sess-<sid>` → session is declared LIVE → subsequent Discord messages are routed via `tmux send-keys`. What is actually alive after the bot dies is the *tmux session*, which contains a *shell*, which previously contained a *hermes* process, which previously contained a *Codex app-server* subprocess. If the bot died because the OS killed it (OOM, SIGKILL), the hermes process and its Codex subprocess both died. The tmux pane is at a shell prompt. `tmux send-keys` will paste the Discord message into that shell prompt — not into a running Codex session. The Codex session, per `audits/cluster-A-codex-sessions.md §"Resume semantics"`, has no resume mechanism in the current Hermes transport. The operator's Discord message disappears into a shell that will do something unpredictable with it.

The external research (`audits/external-research.md RQ3`) confirms that `thread/resume` exists in the Codex JSON-RPC protocol and a new app-server process CAN continue an existing thread from the same storage path. The design explicitly defers this as a "later hardening" (`architecture-diagram.md §6`). That deferral is fine — but the current reattach path must be honest about what it reattaches to.

**Suggested fix:** In `module-specs/discord-gateway.md §6` and `architecture-diagram.md §6`, add explicit detection: after confirming `tmux has-session -t codex-sess-<sid>`, also check whether a hermes process is alive inside the pane via `tmux display-message -p -t codex-sess-<sid> '#{pane_pid}'` + `pgrep -P <pane-pid> hermes`. If hermes is running, the reattach is genuine. If hermes is dead (shell prompt), classify the session as NEEDS_REVIVE rather than LIVE, even though tmux is alive. This is a one-paragraph addition to the spec and prevents silent message loss.

---

### 2. `/revive` discards uncommitted work in the worktree silently — ISA progress is preserved but source code changes are not

**Where:** `isas/P5-hardening.md ISC-5`, `module-specs/discord-gateway.md §6 NEEDS_REVIVE banner`, `module-specs/worktree-broker.md §8 release semantics`

**Why it breaks:** The `/revive` handler allocates a NEW sid and NEW worktree. The spec says "previous ISA progress is preserved as `_ephemeral/orphaned-<ts>.md`." The ISA markdown file is committed and tracked — preserving it is correct. But Codex sessions work by modifying source files in the worktree. Any source edits, test changes, or config modifications that were NOT committed to git before the session died are NOT preserved by archiving the ISA file. They exist in the old worktree directory. The `/revive` spec (`isas/P5-hardening.md ISC-5`) does not say to commit or stash the old worktree's uncommitted changes before creating the new one. The NEEDS_REVIVE banner in `module-specs/discord-gateway.md §6` says "Worktree: `<path>` [exists / missing]" but does not prompt the operator to capture uncommitted work. `WorktreeBroker.release()` uses `git worktree remove --force`, which discards uncommitted changes (explicitly documented as intentional in `module-specs/worktree-broker.md §8`). The old worktree is only preserved for gc's 7-day window, which itself is not wired until P5.

**Suggested fix:** The NEEDS_REVIVE banner must include a line: "Warning: the old worktree at `<path>` may contain uncommitted source changes. Run `git -C <path> diff` before reviving to capture any unsaved work." Add this to `module-specs/discord-gateway.md §6`. The `/revive` handler should also run `git -C <old-worktree> diff --stat` and include the output in its Discord confirmation post so the operator can see what is at risk. This is a two-line addition to the handler spec.

---

### 3. Merge broker holds `flock` across synchronous network calls (`gh pr create`, Discord post) — worst-case queue starvation is 30 minutes

**Where:** `module-specs/merge-broker.md §4 steps 6-9`, `collision-matrix.md §2 "Push to fork/main race"`

**Why it breaks:** The global merge mutex covers steps 1 through 9: flock acquire through flock release. Steps 6 (`gh pr create`) and 8 (`discord_tool.post`) are synchronous subprocess + network calls inside the critical section. `gh pr create` on a large diff can take 10-30 seconds on a slow network. If GitHub is experiencing an incident, it can hang indefinitely (the broker has no per-step timeout; the 30-min timeout is on the *waiting* side, not the *holding* side). With 8 sessions queued behind a stuck merge, every session waits for the full 30 minutes before timing out and escalating. The specification says "second merge waits; queue depth visible on /codex-sessions tab" as if this is acceptable — but a 30-minute starvation window for work that is already Opus-approved is a significant UX failure.

**Suggested fix:** Release the flock after `git push` (step 5) and before `gh pr create` (step 6). The race condition that requires serialization is the `fork/main` non-fast-forward push — once the branch is pushed, PR creation and labeling are idempotent and commutative. This reduces the critical section to `fetch → rebase → push` and eliminates network-call latency from the lock window. Update `module-specs/merge-broker.md §4` to show the flock released after step 5, with steps 6-9 running unlocked. The sequence diagram in `architecture-diagram.md §5` must be updated to match.

---

## Major concerns (should fix during P2-P5)

### 4. Verdict parser regex `^VERDICT:` will miss Opus markdown-formatted output — silent ESCALATE on every bold conclusion

**Where:** `module-specs/peer-review-orchestrator.md §5 step 11`, `architecture-diagram.md §4 poll loop`

**Why it breaks:** The poll loop scans for regex `^VERDICT:\s+(APPROVE|REVISE|ESCALATE)`. Claude in interactive mode frequently responds with `**VERDICT: APPROVE**` (bold markdown). The `^` anchor will not match a line starting with `**VERDICT:`. The fuzzy-match mitigation in `§7 "Verdict word misspelled"` applies only to the keyword after the colon (`APRROVE`), not to markdown wrapping of the whole sentinel. An Opus pane that responds `**VERDICT: APPROVE** — the diff correctly implements all ISCs` will produce zero regex matches, hit the hard-timeout path (5 minutes), mark the pane DEAD, and return ESCALATE. This will happen at non-trivial frequency with Opus 4.7.

**Suggested fix:** Change the regex to `r'[*#\s]*VERDICT:\s+(APPROVE|REVISE|ESCALATE)'` to tolerate leading markdown formatting characters, or strip markdown from the captured lines before matching. Add a test case to `module-specs/peer-review-orchestrator.md §13` that explicitly covers `**VERDICT: APPROVE**` format (test 7 currently only checks multiple `VERDICT:` lines, not formatted lines).

---

### 5. Mergify's `#approved-reviews-by >= 1` condition is unmet — no code path calls `gh pr review --approve`, so auto-merge never fires

**Where:** `module-specs/merge-broker.md §6 Option (a)`, `module-specs/peer-review-orchestrator.md §8`

**Why it breaks:** The `.mergify.yml` rule requires `#approved-reviews-by >= 1` alongside `label = auto-merge` and `check-success = ci`. The spec says in a parenthetical: "the peer-review orchestrator must call `gh pr review --approve` as part of the APPROVE path — coordinate with `module-specs/peer-review-orchestrator.md`." That coordination never happens. The peer-review orchestrator spec (`§8 REVISE feedback loop`) describes the REVISE path in detail but has no equivalent step for the APPROVE path. There is no ISC in P2 or P3 that requires `gh pr review --approve` to be called. As written, every PR labeled `auto-merge` will sit forever because Mergify's approval condition is never satisfied.

**Suggested fix:** Either (a) add an explicit ISC to P3 requiring `gh pr review --approve --pr-number <N> --body "VERDICT: APPROVE — <rationale>"` to be called from the APPROVE branch of `module-specs/peer-review-orchestrator.md §8`, or (b) remove `#approved-reviews-by >= 1` from `.mergify.yml` and rely on `label = auto-merge` + `check-success = ci` alone. Option (b) is simpler and does not require the orchestrator to act as a GitHub reviewer. Whichever is chosen must be reflected in both the Mergify config and the ISC checklist.

---

### 6. 60-second post-merge poll is not scoped to codex branches — spurious matches on operator-labeled PRs will crash cleanup

**Where:** `module-specs/merge-broker.md §10 post-merge cleanup`

**Why it breaks:** The dispatcher polls `gh pr list --label auto-merge --state merged --json number,headRefName` every 60 seconds. This returns ALL merged PRs with the `auto-merge` label across the entire repo, not just codex-session branches. If the operator manually labels a non-codex PR `auto-merge` and it merges, the cleanup loop will try to extract a session_id from `headRefName` (expecting format `codex/<sid>/<slug>`), fail to find a matching row in `codex_sessions.json`, and either raise an exception that stops the cleanup loop or silently no-op while logging a spurious WARNING. The accumulation of merged PRs also means the poll list grows unboundedly over time, making every poll tick more expensive.

**Suggested fix:** Add `--head 'codex/*'` to the `gh pr list` invocation in `module-specs/merge-broker.md §10`. Update the P3 ISC-12 test probe to include the flag. This is a one-word fix.

---

### 7. `WorktreeBroker._registry` is in-memory — broker restart loses all allocations, enabling double-allocation on reconnect

**Where:** `module-specs/worktree-broker.md §3 allocate() step 2`, `module-specs/discord-gateway.md §6`

**Why it breaks:** The idempotency check at `allocate()` step 2 consults `self._registry`, which is in-process memory. After a bot restart, `self._registry` is empty. If Discord replays a `thread_create` event during reconnect (discord.py replays missed events in a short window), the dispatcher's `on_thread_create` idempotency check reads `codex_sessions.json` under `flock` — but only if that check runs before the second replay event arrives. If both replays arrive before the first write completes (plausible under fast replay), both calls see no row and both call `allocate()`, creating two worktrees for the same thread_id.

**Suggested fix:** Populate `self._registry` from `codex_sessions.json` at `WorktreeBroker.__init__` time. The dispatcher reads this file anyway; pass the existing `{session_id: worktree_path}` mapping to the broker constructor. This costs one file read at startup and makes `allocate()` step 2 robust to restarts.

---

### 8. SSE backpressure under 8 active sessions is unspecified — slow browser client can cause unbounded memory growth

**Where:** `module-specs/codex-sessions-tab.md §6 SSE wiring`, `audits/cluster-D-worktree-dashboard.md §"Pulse SSE"`

**Why it breaks:** The existing pulse SSE channel drains events "one-at-a-time per loop tick" via `asyncio.wait` with a 1-second timeout (`audits/cluster-D-worktree-dashboard.md §"Event-enqueue path"`). With 8 Codex sessions each emitting events on every `run_turn` completion, phase transition, and review verdict, the event queue can fill faster than the 1-second drain rate. The existing `pulse_activity_iter()` design does not specify a maximum queue depth. A slow browser client (mobile, slow connection) holding an SSE connection open without consuming events will cause the `asyncio.Queue` to grow without bound. This is a pre-existing issue in the dashboard, but adding `kind: codex-session` events significantly increases the event rate.

**Suggested fix:** Cap the `asyncio.Queue` depth for the codex-session event source at 100 events (one line in `pulse_data.py`). On overflow, drop the oldest event (not the newest — the dashboard needs current state, not history). Add a `dropped_events` counter to the SSE health probe so the operator can see when this is happening.

---

## Minor concerns (nice to fix, not blocking)

### 9. Mergify cost on private repos is not surfaced — operator may be surprised by a billing event

**Where:** `module-specs/merge-broker.md §6 Option (a)`, `DESIGN.md §13 open questions`

Mergify charges for private repos above a team-member threshold. The design recommends Mergify as the primary auto-merge option without mentioning cost. `jjaavvin-web/hermes-agent` is a personal fork, not a public repo. Add one sentence to `merge-broker.md §6`: "Mergify charges for private repos above N members — verify current pricing before choosing this option."

---

### 10. `pnpm enableGlobalVirtualStore` is under the `pnpm.io/next/` URL — may be pre-release

**Where:** `audits/external-research.md RQ4`, `module-specs/worktree-broker.md §5`

The external research notes this concern but it is not addressed in the worktree broker spec. Add a P5 ISC (or P1 gate) that verifies `pnpm --version` supports global virtual store before the advisory is acted on. The fallback (per-worktree npm install) is already documented — just gate the recommendation on a version check.

---

### 11. `codex_sessions.json` corrupt-recovery path posts no operator-visible signal — silent data loss at 3am

**Where:** `module-specs/discord-gateway.md §5 "Recovery on missing / corrupt file"`

On `JSONDecodeError`, the bot starts empty, `on_bot_restart` finds N tmux sessions, has no `thread_id` to post NEEDS_REVIVE banners to, and logs a WARNING at bot-log level only. The operator who is asleep will not see the WARNING. The 8 Codex sessions continue running but are no longer routed to Discord threads. Add: when `codex_sessions.json` is corrupt, post a message to `DISCORD_HOME_CHANNEL` via `discord-notify.sh` with "CRITICAL: codex_sessions.json corrupt on restart — all session routing lost. Inspect bot logs."

---

### 12. `on_thread_message` idempotency based on `last_message_id` has a crash-window gap

**Where:** `module-specs/discord-gateway.md §3 on_thread_message`

The row is updated *after* the turn runs. A crash between turn completion and row write means the message is re-delivered on the next restart, the dedup check passes (old `last_message_id`), and the turn runs twice. The design acknowledges duplicate delivery risk in `collision-matrix.md §2` ("may be lost if it falls outside dedup window"). The existing `MessageDeduplicator` in `gateway/platforms/helpers.py:27` has a 5-minute TTL that would cover this gap. Consider routing the `on_thread_message` check through `MessageDeduplicator` rather than only against `last_message_id` in the session row.

---

## Findings that are NOT concerns (design handled correctly)

The decision to use a new `codex_sessions.json` file rather than overloading `discord_threads.json` is directly validated by the audit (`audits/cluster-B-gateway-discord.md §"discord_threads.json — actual content"`): the existing file contains 20 message-ID snowflakes, not session records. The kanban CAS claim path is correctly inherited rather than re-implemented — `kanban_db.py:1922-1931` is production-proven and adding nothing on top of it avoids drift. The hard rule against `claude -p` / Agent SDK in the Opus reviewer path (ISC-15 in P1, ISC-12 in P2) is enforced by grep tests in the ISAs, which is the right anti-regression pattern for a billing-critical constraint. The phasing is sound: shipping P1 with manual review handoff before automating it (P2) front-loads learning without front-loading risk. The `flock + atomic-tempfile-rename` write protocol for all JSON state files correctly mirrors the production pattern from `telegram.py:1077-1133`. The branch naming scheme (`codex/<uuid4>/<isa-slug>`) makes push collisions structurally impossible without coordination, which is the right design given the concurrent-push scenario. PR #34 is correctly identified as a hard prerequisite rather than a soft dependency.
