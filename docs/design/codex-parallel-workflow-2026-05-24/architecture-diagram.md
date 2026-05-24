# Architecture Diagrams — Hermes-Codex Parallel Workflow

ASCII-only. Companion to `DESIGN.md`. Per-module diagrams + state machines + timing.

---

## 1. Full pipeline

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                            OPERATOR (Joseph)                                  ║
║                              via Discord                                      ║
╚════════════════════════════════════╤═════════════════════════════════════════╝
                                     │ thread create / message / archive
                                     ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ DiscordAdapter   gateway/platforms/discord.py:532  (existing, ~5169 LOC)     │
│   connect / disconnect / send / edit_message / get_chat_info                 │
│   ThreadParticipationTracker → ~/.hermes/discord_threads.json (joined tids)  │
└─────────────────────┬────────────────────────────────────────────────────────┘
                      │ on_thread_create | on_message | on_thread_update(archived)
                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ CodexSessionDispatcher   gateway/codex_session_dispatcher.py    (NEW — P1)   │
│   thread_id  ─┐                                                              │
│   session_id ─┤  ↔  ~/.hermes/codex_sessions.json   (flock + atomic_replace) │
│   kanban_id  ─┤                                                              │
│   tmux_sess  ─┤                                                              │
│   worktree   ─┤                                                              │
│   isa_id     ─┘                                                              │
│                                                                              │
│   Slash cmds: /spawn /pause /resume /kill /status /handoff-to-ruflo          │
└──┬─────────────────────────┬─────────────────────────┬───────────────────────┘
   │ allocate(sid)           │ run_turn(sid, msg)      │ phase==verify
   ▼                         ▼                         ▼
┌─────────────────────┐ ┌───────────────────────┐  ┌─────────────────────────┐
│ WorktreeBroker      │ │ tmux session          │  │ PeerReviewOrchestrator  │
│ (NEW — P1)          │ │ codex-sess-<sid>      │  │ (NEW — P2)              │
│ allocate / release  │ │ ┌───────────────────┐ │  │ pane pool:              │
│ gc (P5)             │ │ │ hermes …          │ │  │   codex-review-0 (warm) │
│ port broker         │ │ │ + HERMES_KANBAN_… │ │  │   codex-review-1 (warm) │
│                     │ │ │ + Codex transport │ │  │ send-keys → capture-pane│
│ git worktree add    │ │ │   codex_app_server│ │  │ verdict: APPROVE        │
│  /home/josep/.hermes│ │ │   _session.py:202 │ │  │          | REVISE       │
│  /codex-wt/<sid>/   │ │ └───────────────────┘ │  │          | ESCALATE     │
│                     │ └───────────────────────┘  │ iter cap = 3            │
└─────────────────────┘                            │ reviews/sess/day ≤ 10   │
                                                   └─────────────┬───────────┘
                                                                 │ APPROVE
                                                                 ▼
                                              ┌─────────────────────────────────┐
                                              │ MergeBroker    (NEW — P3)       │
                                              │ flock ~/.hermes/codex-merge.lock│
                                              │ git fetch && git rebase         │
                                              │ isa_lint <ISA> → must pass      │
                                              │ git push → fork                 │
                                              │ gh pr create --base fork/main   │
                                              │ if safe-class: add auto-merge   │
                                              │ else: queue for human review    │
                                              └─────────────┬───────────────────┘
                                                            │
                                                            ▼
                                       ┌────────────────────────────────────────┐
                                       │ GitHub (fork/main)                     │
                                       │ Mergify rule: label=auto-merge +       │
                                       │   checks pass + ≥1 approval (Opus)     │
                                       └────────────────────────────────────────┘

  ╔════════════ Observability sidecar ════════════╗
  ║ /codex-sessions dashboard tab  (NEW — P4)     ║
  ║   GET /api/dashboard/codex-sessions           ║
  ║   GET /api/dashboard/codex-sessions/{sid}     ║
  ║   SSE event: pulse.activity                   ║
  ║     data: {kind:"codex-session", sid, ...}    ║
  ║   reuses web_server.py:4308-4369 channel      ║
  ╚═══════════════════════════════════════════════╝
```

---

## 2. Discord gateway dispatcher — state machine

```
States: NEW  CLAIMED  EXECUTING  VERIFYING  REVIEWING  MERGING  COMPLETE
        │     │         │          │          │          │        │
        │     │         │          │          │          │        ▼
        │     │         │          │          │          │      ARCHIVED
        │     │         │          │          │          │
        └─────┴─────────┴──────────┴──────────┴──────────┴─→ ORPHANED (any state)
                                                              │
                                                              ▼
                                                          NEEDS_REVIVE


NEW ──── on_thread_create ────────────────────────► CLAIMED
                                                       │
                                                       │ WorktreeBroker.allocate
                                                       │ tmux new-session
                                                       │ codex_sessions.json write
                                                       ▼
CLAIMED ─ first message routed to run_turn(sid) ──► EXECUTING
                                                       │
                                                       │ N message turns
                                                       │ ISA progress N/M climbs
                                                       ▼
EXECUTING ─ ISA phase: verify written by session ──► VERIFYING
                                                       │
                                                       │ P1: operator triggers /review
                                                       │ P2+: dispatcher auto-triggers
                                                       ▼
VERIFYING ── PeerReviewOrchestrator.review(sid) ───► REVIEWING
                                                       │
                                       ┌───────────────┼──────────────┐
                                       │APPROVE        │REVISE        │ESCALATE
                                       ▼               ▼              ▼
                                    MERGING       EXECUTING       (paused;
                                       │         (iter < 3)        operator
                                       │              │            takes over)
                                       │              │
                                       │   3rd REVISE │ → auto-ESCALATE
                                       │              │
                                       ▼              │
MERGING ─ MergeBroker.merge succeeds ──────────────► COMPLETE
                                                       │
                                                       │ release worktree
                                                       │ codex_sessions.json delete
                                                       │ Discord thread archive
                                                       ▼
                                                    ARCHIVED


Failure transitions:
- ANY state + tmux session dead         → ORPHANED → NEEDS_REVIVE
- ANY state + Discord thread archived   → COMPLETE (clean shutdown)
- ANY state + worktree removed externally → ORPHANED + NEEDS_REVIVE
- ANY state + bot restart               → reattach via tmux ls + codex_sessions.json
```

---

## 3. Worktree broker — lifecycle

```
                    ┌────────────────────┐
                    │ allocate(sid, isa) │
                    └─────────┬──────────┘
                              │
                              ▼
              ┌─────────────────────────────────────┐
              │ git -C <hermes-repo> worktree add   │
              │   /home/josep/.hermes/codex-wt/<sid>│
              │   -b codex/<sid>/<isa-slug>         │
              │   origin/main                       │
              │ (subprocess.run, check=False)       │
              └─────────────┬───────────────────────┘
                            │ ok
                            ▼
                ┌────────────────────────────┐
                │ allocate port via          │
                │ ports.json (range          │
                │ 50000-50007, flock)        │
                └─────────────┬──────────────┘
                              │
                              ▼
                ┌────────────────────────────┐
                │ if has package.json:       │
                │  enqueue first-touch hook  │
                │  → pnpm install --dir <wt> │
                │  (deferred; runs on first  │
                │   JS-touching turn)        │
                └─────────────┬──────────────┘
                              │
                              ▼
                ┌────────────────────────────┐
                │ return WorktreePath        │
                └────────────────────────────┘

                  ┌──────────────────────┐
                  │ release(sid)         │
                  └──────────┬───────────┘
                             │
                             ▼
                ┌──────────────────────────────┐
                │ tmux kill-session            │
                │ git -C <repo> worktree       │
                │   remove --force <wt>        │
                │ free port                    │
                │ codex_sessions.json delete   │
                └──────────────────────────────┘

                  ┌──────────────────────┐
                  │ gc()  (P5 only)      │
                  └──────────┬───────────┘
                             │
                             ▼
            ┌───────────────────────────────────────────┐
            │ git worktree list --porcelain             │
            │   ↑ already used by git_janitor.py:68     │
            │ for each wt:                              │
            │   if no row in codex_sessions.json AND    │
            │      no live tmux session AND             │
            │      no open PR for its branch:           │
            │        worktree remove --force            │
            │        rename to .deleted.<ts>/ first     │
            │        (per WORKFLOW-LESSONS §3 rule 3)   │
            └───────────────────────────────────────────┘
```

---

## 4. Peer-review pane pool — state machine

```
Per-pane states:    WARM ──┬──► BUSY ──┬──► (verdict captured) ──► WARM
                           │          │
                           │          └──► (idle-N timeout) ──► WARM (REVISE/ESCALATE)
                           │
                           └──► (health check fail) ──► DEAD ──► (re-spawn) ──► WARM

  ┌─────────────────────────────────────────────────────────────┐
  │ Pool init at bot startup                                    │
  │   for i in 0..N-1:                                          │
  │     tmux new-session -d -s codex-review-<i> 'claude'        │
  │     tmux pipe-pane -o -t codex-review-<i> \                 │
  │       'cat >> ~/.hermes/codex-review-<i>.log'               │
  │     wait for prompt-ready sentinel in capture-pane          │
  │     mark WARM                                               │
  └─────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────┐
  │ review(sid, isa_path, diff)                                 │
  │                                                             │
  │   wait for free WARM pane (queue if all BUSY)               │
  │   pane_id ← claim a WARM pane → mark BUSY                   │
  │                                                             │
  │   if len(diff) > 20_000:                                    │
  │     diff_summary = summarize_diff(diff)  # in-process       │
  │     payload = (isa_path, diff_summary)                      │
  │   else:                                                     │
  │     payload = (isa_path, diff_raw)                          │
  │                                                             │
  │   write /tmp/review-<sid>.md  (prompt + ISA + diff/summary) │
  │                                                             │
  │   tmux send-keys -t codex-review-<pane_id> \                │
  │     "Review the diff and ISA at /tmp/review-<sid>.md and    │
  │      reply with VERDICT: APPROVE | REVISE | ESCALATE        │
  │      followed by the rationale." Enter                      │
  │                                                             │
  │   poll capture-pane -p every 5s, looking for:               │
  │     - "VERDICT:" sentinel line                              │
  │     - idle ≥ 15s after sentinel (output stable)             │
  │     - OR 5-min hard timeout                                 │
  │                                                             │
  │   parse last r'[*#\s]*VERDICT:\s+(APPROVE|REVISE|ESCALATE)\b'│
  │   (tolerates **VERDICT: APPROVE** and ## VERDICT: REVISE)    │
  │   on success → mark pane WARM, return verdict + rationale   │
  │   on hard timeout → mark pane DEAD, ESCALATE                │
  │   on health check fail (pane gone) → re-spawn, ESCALATE     │
  └─────────────────────────────────────────────────────────────┘
```

---

## 5. Merge broker — sequence

```
caller (session ready to land)                   broker            github
        │                                          │                 │
        │── merge(sid) ──────────────────────────►│                 │
        │                                          │ flock ~/.hermes/codex-merge.lock
        │                                          │
        │                                          │ cd <worktree>
        │                                          │── git fetch origin ──► (fetch)
        │                                          │── git rebase origin/main ──►
        │                                          │    │
        │                                          │    ├── ok ──┐
        │                                          │    │        │
        │                                          │    └── conflict ──► raise ConflictEscalation
        │                                          │             │
        │                                          │             ▼
        │                                          │ python3 scripts/isa_lint.py <ISA>
        │                                          │   exit != 0 → raise IsaLintFailed
        │                                          │
        │                                          │── git push fork <branch> ─────► (push)
        │                                          │
        │                                          │ flock release  ← RELEASED HERE
        │                                          │   (log: flock_released = True)
        │                                          │   Critical section ends. Steps below
        │                                          │   run unlocked — idempotent vs fork/main.
        │                                          │
        │                                          │── gh pr create --base fork/main \
        │                                          │     --head <branch> \
        │                                          │     --title <title> \
        │                                          │     --body  <body>           ─► PR opened
        │                                          │
        │                                          │ classify_change(sid):
        │                                          │   safe = no touched paths in
        │                                          │     {agent/, gateway/, auth/,
        │                                          │      migrations/, pyproject.toml,
        │                                          │      package*.json, .github/}
        │                                          │
        │                                          │ if safe:
        │                                          │   gh pr edit <pr#> --add-label auto-merge
        │                                          │   (GitHub Actions fires on label)
        │                                          │ else:
        │                                          │   gh pr edit <pr#> --add-label needs-human
        │                                          │   (queues for operator)
        │                                          │
        │◄── PR # + classification ────────────────│
        │                                          │
        │  post to Discord thread:                 │
        │   "PR #N opened (auto-merge|human-queue)"
```

---

## 6. Codex session subprocess + tmux containment

```
   tmux server (host-level, survives bot restarts, NOT host reboots)
   │
   ├── session: codex-sess-<sid>
   │   └── window 0  (PTY)
   │       └── shell (zsh)
   │           └── exec hermes \
   │                   -p <profile> \
   │                   --skills kanban-worker \
   │                   chat -q "work kanban task <kid>"
   │                   env:
   │                     HERMES_KANBAN_TASK=<kid>           # kanban_db.py:4066
   │                     HERMES_KANBAN_WORKSPACE=<wt>       # kanban_db.py:4067
   │                     HERMES_KANBAN_CLAIM_LOCK=<lock>    # kanban_db.py:4071
   │                     HERMES_KANBAN_DB=<db>              # kanban_db.py:4078
   │                     HERMES_PROFILE=<profile>           # kanban_db.py:4089
   │               │
   │               └── (lazy) Codex app-server child
   │                   = subprocess.Popen([codex, "app-server"])
   │                   = codex_app_server.py:86-93
   │                   = spawned by CodexAppServerSession.ensure_started
   │                     codex_app_server_session.py:202-260
   │
   ├── session: codex-sess-<other_sid>
   │   └── ... (same shape, completely isolated)
   │
   └── session: codex-review-0   (warm pane pool, P2)
       └── window 0
           └── claude  (interactive; Max OAuth via ~/.claude/.credentials.json)
                       NO `claude -p`  (objective §5 — 2026-06-15 billing constraint)
```

If the bot dies and is restarted: `tmux ls` enumerates `codex-sess-*` and `codex-review-*`, the bot reads `~/.hermes/codex_sessions.json` to recover the `thread_id ↔ tmux_session` mapping, and re-binds. The reattach classification requires a two-step check — tmux session alive is necessary but not sufficient:

```
Step A: tmux has-session -t codex-sess-<sid>
          → non-zero: session GONE → NEEDS_REVIVE (no further check)

Step B (only if Step A passes): pane_pid + pgrep probe
          pane_pid=$(tmux display-message -p -t codex-sess-<sid> '#{pane_pid}')
          pgrep -P "$pane_pid" hermes
          → hermes running (returncode 0): session LIVE → re-bind dispatcher
          → hermes NOT running (shell at prompt): session NEEDS_REVIVE
            (OOM/SIGKILL killed hermes but left tmux alive — tmux send-keys
             would paste the next Discord message into the shell, not into
             a Codex session; this must be caught here)
```

The codex app-server subprocesses keep running inside their tmux sessions throughout the bot's downtime (as long as hermes itself survives).

If the *host* reboots: tmux is gone, codex subprocesses gone. The bot's `codex_sessions.json` rows become NEEDS_REVIVE; operator either uses `/revive` to launch fresh sessions (P5 ISA), or in a later hardening the Hermes transport gains `thread/resume` support and reattaches by storage path + thread_id (external research RQ3 — capability exists in the codex protocol, not yet wired in Hermes).

---

## 7. SSE event flow for /codex-sessions

```
  /codex-sessions tab (browser)
     │
     │  EventSource('/api/pulse/stream?token=<tok>')
     │                              ↑
     │                              │ token from window.__HERMES_SESSION_TOKEN__
     │                              │ WORKFLOW-LESSONS §6.1
     ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ web_server.py:4308-4369   api_pulse_stream                    │
  │   loop:                                                       │
  │     _probe_all() every 10s  → event: health, data: {...}      │
  │     pulse_activity_iter()   → event: pulse.activity, data:{…} │
  │     idle 15s                → : heartbeat                     │
  └──────────────────────────────────────────────────────────────┘
                              ▲
                              │ enqueue
  ┌──────────────────────────────────────────────────────────────┐
  │ pulse_data.pulse_activity_iter()                              │
  │   (NEW additive in P4):                                       │
  │   for each codex session change (allocate/turn/review/merge): │
  │     yield {                                                   │
  │       "kind": "codex-session",                                │
  │       "sid":  "...",                                          │
  │       "phase": "execute",                                     │
  │       "progress": "5/12",                                     │
  │       "tmux_alive": true,                                     │
  │       "last_msg_at": "ISO-8601",                              │
  │     }                                                         │
  └──────────────────────────────────────────────────────────────┘
                              ▲
                              │ writes
  ┌──────────────────────────────────────────────────────────────┐
  │ CodexSessionDispatcher  emits pulse events on:                │
  │   - session allocate     (sid created)                        │
  │   - run_turn complete    (progress changed)                   │
  │   - phase transition                                          │
  │   - review verdict                                            │
  │   - merge complete       (sid removed)                        │
  └──────────────────────────────────────────────────────────────┘
```

---

## 8. Data placement summary

```
/home/josep/.hermes/
├── codex_sessions.json            ◄── NEW (P1) — thread_id ↔ session map (flock)
├── codex-merge.lock               ◄── NEW (P3) — merge-broker mutex (flock)
├── codex-review-0.log             ◄── NEW (P2) — pane 0 pipe-pane log
├── codex-review-1.log             ◄── NEW (P2) — pane 1 pipe-pane log
├── codex-wt/                      ◄── NEW (P1) — worktree root
│   ├── <sid-1>/                       (full git worktree, branch codex/<sid>/<slug>)
│   ├── <sid-2>/
│   └── …
├── discord_threads.json           ◄── UNCHANGED — ThreadParticipationTracker
│                                       (existing, gateway/platforms/discord.py)
├── codex-ports.json               ◄── NEW (P1) — port broker state (flock)
└── work/<isa-id>/                 ◄── UNCHANGED — ISA-SPEC §2 canonical home
    ├── ISA.md
    └── _ephemeral/                    (sub-agent option A slices, when used)
```

---

*End of architecture-diagram.md.*
