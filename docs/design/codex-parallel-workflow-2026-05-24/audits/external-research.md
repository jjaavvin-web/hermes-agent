# External Research — Codex Parallel Design Audit

Generated: 2026-05-24

---

## RQ1: Git worktree for parallel AI agents — does anyone do it?

**Verdict: works**

### Findings

1. **Established practice, multiple tools.** Claude Squad, ComposioHQ/agent-orchestrator, MindStudio, Augment Code, and Aider all explicitly support N-agents-in-N-worktrees. The pattern is described as production-ready in multiple 2025-2026 writeups.
   - Source: [MindStudio — How to Run Parallel AI Coding Agents With Git Worktrees](https://www.mindstudio.ai/blog/parallel-ai-coding-agents-git-worktrees)
   - Source: [ComposioHQ/agent-orchestrator](https://github.com/ComposioHQ/agent-orchestrator) — "Each agent gets its own git worktree, its own branch, and its own PR."

2. **node_modules isolation is the conservative consensus.** MindStudio recommends running `npm install` separately per worktree. No source recommends sharing a bare `node_modules` directory across worktrees.
   - Source: [MindStudio blog](https://www.mindstudio.ai/blog/parallel-ai-coding-agents-git-worktrees) — "Each worktree shares the same `package.json` from the repo, but has its own `node_modules` directory."

3. **pnpm globalVirtualStore solves the disk cost problem.** pnpm's `enableGlobalVirtualStore: true` makes each worktree's `node_modules` contain only symlinks into a single content-addressable store. Near-zero per-worktree disk delta; subsequent installs are "nearly instant."
   - Source: [pnpm — Git Worktrees docs](https://pnpm.io/next/git-worktrees) — "each worktree's `node_modules` contains only symlinks into a single content-addressable store on disk."

4. **Claude Squad is the closest prior art to the Hermes design.** It manages multiple agents in parallel with isolated worktrees and supports Claude Code, Codex, Aider, and Gemini.
   - Source: [DEV.to — Claude Squad](https://dev.to/stevengonsalvez/claude-squad-run-multiple-ai-agents-in-parallel-without-the-mess-1hfl)

5. **Collision reports.** No source reports file-system collisions between worktrees themselves. Merge conflict risk is mentioned only for concurrent edits to the same file (git-layer, not fs-layer). npm concurrent installs within the same physical `node_modules` are the one cautioned failure mode — avoided by per-worktree isolation.

### Synthesis

Per-worktree isolation is the right call. It matches the entire industry pattern. If JS dependencies matter, use pnpm with `enableGlobalVirtualStore: true` to keep disk cost near zero across 8 worktrees. Do not share a single `node_modules` directory across worktrees.

---

## RQ2: discord.py / discord bot patterns for stateless gateway with long-lived worker subprocesses

**Verdict: works-with-caveat**

### Findings

1. **No direct prior art for `thread_id → tmux_session_name` mapping.** The closest real implementation found is [discord-tmux-mc-bot](https://github.com/laxerhd/discord-tmux-mc-bot), a Java Discord bot that sends commands to a named tmux session via `tmux send-keys`. It does not persist the mapping across restarts — sessions are manually pre-created.

2. **tmux sessions survive bot process death.** tmux is server-managed; the session stays alive as long as the tmux server is running. A restarted bot process can call `tmux ls` to enumerate live sessions and re-bind its in-memory map.
   - Source: [tmux.app session docs](https://tmux.app/sessions/) — "Unlike a standard terminal, a tmux session keeps running after you close your terminal — you can reattach to it from any terminal at any time."

3. **discord.py has no native process lifecycle hooks.** There is no built-in "on_restart" event. Re-binding state after restart requires: (a) persisting the map to SQLite/Redis before shutdown or periodically, and (b) reading it back in `on_ready`. Discord.py docs recommend asyncpg or asqlite for non-blocking persistence.
   - Source: [thegamecracks — Writing Persistent Views](https://thegamecracks.github.io/discord.py/persistent_views.html)

4. **Process manager layer (PM2 / systemd) is standard.** Bot restarts are treated as normal; the infrastructure is designed around `Restart=always`.
   - Source: [space-node.net — Preventing Silent Bot Deaths](https://space-node.net/blog/discord-bot-silent-death-24-7-supervision)

### Caveats

- The `thread_id → session_name` map **must be durable** (SQLite or Redis), not in-memory only. A bot crash without a flush loses all mappings.
- If the tmux server itself dies (host reboot), all sessions die. The bot re-binding will find no live sessions and must create fresh ones. Design must handle this gracefully.
- Discord thread IDs are stable across bot restarts (they live in Discord's API), so using them as the durable key is correct.

### Synthesis

The pattern is sound and has real (if simple) prior art. The key caveat is that the `thread_id → session_name` map must be persisted to disk on every write, not just in memory. The rest — tmux surviving bot restarts, re-binding on `on_ready` via `tmux ls` — is straightforward.

---

## RQ3: Codex CLI app-server session — durability and resume semantics

**Verdict: works-with-caveat**

### Findings

1. **`thread/resume` RPC exists and is documented.** The codex app-server JSON-RPC protocol explicitly defines `thread/resume`:
   - Source: [OpenAI Codex — App Server docs](https://developers.openai.com/codex/app-server) — "thread/resume — reopen an existing thread by id so later turn/start calls append to it."
   - Source: [openai/codex — app-server/README.md](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) — confirmed `thread/resume` with params `{ threadId, personality }` and same response shape as `thread/start`.

2. **Thread state is persisted to disk (JSONL rollout files + state DB).** A new server process can load existing threads if it accesses the same storage directory.
   - Source: [DeepWiki — App Server and JSON-RPC Protocol](https://deepwiki.com/openai/codex/4.4-app-server-and-json-rpc-protocol) — "threads are persisted in rollout files plus a state database, enabling recovery after process termination."
   - Source: [OpenAI Codex App Server docs](https://developers.openai.com/codex/app-server) — "To continue a stored session, call thread/resume with the thread.id you recorded earlier."

3. **Critical caveat — server process death vs. storage continuity.** Thread persistence holds only if the new app-server instance points at the same storage path as the old one. If the Hermes adapter launches a fresh codex app-server subprocess without pointing it at the previous storage directory, `thread/resume` will return an error ("thread not found").

4. **The design doc's note that "subprocess dies when its parent does" is accurate for the default subprocess launch mode.** This means the running codex app-server process is lost on Hermes restart. The thread data on disk survives, but a new app-server must be spawned and pointed at the same storage root to resume.

### Synthesis

`thread/resume` exists and works. The Hermes adapter's PID-reattach limitation is not a blocker for thread continuity — what matters is storage path continuity. The adapter must: (1) record the `thread_id` durably, (2) on restart, spawn a new app-server pointing at the same storage root, then call `thread/resume <thread_id>`. This is a design caveat, not a showstopper.

---

## RQ4: node_modules under N concurrent worktrees — pnpm vs yarn vs npm

**Verdict: works (pnpm is the clear winner)**

### Findings

1. **pnpm has a dedicated git-worktrees feature page.** `enableGlobalVirtualStore: true` in `pnpm-workspace.yaml` routes all worktrees through a single content-addressable store. Per-worktree node_modules contains only symlinks — "near-zero per-worktree overhead."
   - Source: [pnpm — Git Worktrees](https://pnpm.io/next/git-worktrees)

2. **pnpm concurrent install safety.** The store is content-addressable and write operations are atomic file-level. Concurrent installs in separate worktrees writing to the same global store are safe by design (same as concurrent pnpm installs in a monorepo). The store uses hard-links and symlinks; two processes writing the same package hash is idempotent.
   - Source: [pnpm FAQ](https://pnpm.io/faq) — "pnpm uses a content-addressable filesystem to store all files from all module directories on a disk."

3. **npm is unsafe for concurrent installs.** npm installs are not atomic at the `node_modules` level; concurrent installs into separate worktrees are fine (different directories), but npm provides no shared deduplication — 8 worktrees = 8 full `node_modules` copies. Disk cost for a medium JS project: ~200-500 MB × 8 = 1.6-4 GB vs. ~200-500 MB total with pnpm.

4. **yarn (PnP or classic) is a middle option.** Yarn's global cache deduplicates tarballs but does not provide the symlink-based near-zero overhead of pnpm's global virtual store for worktrees specifically.

### Recommendation

Use **pnpm** with `enableGlobalVirtualStore: true`. For 8 worktrees each running `pnpm install` on first JS-touching turn:
- Disk cost: near-zero incremental per worktree
- Concurrent install safety: yes (content-addressable, atomic writes)
- First install populates the store; all subsequent worktree installs are symlink-creation only

Do not use npm for this use case.

---

## RQ5: PR auto-merge — GitHub native vs Mergify vs Kodiak (May 2026)

**Verdict: works-with-caveat (Mergify recommended; GitHub native if label-trigger is not required)**

### Findings

1. **GitHub native auto-merge cannot be triggered by adding a label.** It has no label-gating concept at all. It merges when required checks pass and required reviews approve — no further conditions.
   - Source: [Mergify — GitHub Auto-Merge: When the Native Button Is Enough](https://mergify.com/blog/github-auto-merge-when-native-is-enough/) — "GitHub's native auto-merge has no concept of labels in its gating."

2. **GitHub native auto-merge CAN be enabled via `gh pr merge --auto`.** A GitHub Actions workflow triggered by `on: pull_request: types: [labeled]` can call `gh pr merge --auto --squash` when a specific label (e.g., `automerge`) is added. This is a workaround, not native label-gating.
   - Source: [DEV.to — Automate and Auto-Merge PRs using GitHub Actions and gh CLI](https://dev.to/nickytonline/automate-and-merge-pull-requests-using-github-actions-and-the-github-cli-4lo6)
   - Source: [GitHub Marketplace — Enable Pull Request Automerge action](https://github.com/marketplace/actions/enable-pull-request-automerge) — dedicated action for this pattern.

3. **Mergify supports label-gated auto-merge natively with YAML rules:**
   ```yaml
   merge_protections:
     - name: must have ready-to-merge label
       success_conditions:
         - label = ready-to-merge
         - "#approved-reviews-by >= 1"
         - check-success = ci
   ```
   - Source: [Mergify — GitHub Auto-Merge comparison](https://mergify.com/blog/github-auto-merge-when-native-is-enough/)

4. **Kodiak is no longer actively maintained.** Multiple 2026 sources confirm this.
   - Source: [Mergify — Compare Kodiak](https://mergify.com/alternative/kodiak)
   - Source: [codeant.ai — Best Tools 2026](https://www.codeant.ai/blogs/top-pull-request-automation-tools)

5. **GitHub native merge queue** (not auto-merge) requires GitHub Enterprise Cloud for private repos to use batching. For public repos it is free.

### Recommendation

| Requirement | Tool |
|---|---|
| Label triggers auto-merge natively | **Mergify** |
| No label requirement, simplest setup | **GitHub native** (`gh pr merge --auto` in Actions) |
| Label trigger via Actions workaround | GitHub Actions + `gh pr merge --auto` (acceptable, more fragile) |
| Kodiak | Do not use — unmaintained |

For a Hermes-Codex workflow where an agent adds a label to signal "ready to land," use **Mergify** with a `success_conditions` rule. If the label trigger is dropped from the design, GitHub native + `gh pr merge --auto` is sufficient and has zero operational overhead.

---

## Open Threads

- **Claude Squad architecture** is the closest existing system to Hermes-Codex. Worth reading its source for session lifecycle patterns: [DEV.to article](https://dev.to/stevengonsalvez/claude-squad-run-multiple-ai-agents-in-parallel-without-the-mess-1hfl)
- **codex `thread/fork`** RPC could be useful for branching a conversation if the design ever needs parallel sub-tasks within a single agent thread.
- **pnpm `enableGlobalVirtualStore`** is marked as `next` (pre-release) in the pnpm docs URL. Verify it is stable in the pnpm version being targeted before committing to it.
