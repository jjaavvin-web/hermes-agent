# Cluster D — Worktree / Dashboard SSE / Interactive-Claude template

## Files audited (path:lines)

- `/home/josep/.local/share/hermes-agent/hermes_cli/web_server.py` — 4679 lines (read lines 1–2043, 4260–4369, auth/middleware sections)
- `/home/josep/.local/share/hermes-agent/hermes_cli/dashboard_health.py` — 2030 lines (read full)
- `/home/josep/.local/share/hermes-agent/hermes_cli/git_janitor.py` — lines 1–150 (git plumbing section)
- `/home/josep/.local/share/hermes-agent/hermes_cli/pulse_data.py` — grep only
- `/home/josep/.local/share/hermes-agent/hermes_cli/main.py` — grep only (worktree section)
- `/home/josep/.hermes/scripts/templates/ruflo-launch-interactive.template.sh` — 167 lines (full)
- `/home/josep/.hermes/WORKFLOW-LESSONS.md` — 250 lines (full)
- `/home/josep/.hermes/PROVIDER-STACK.md` — 97 lines (full)

---

## Pulse SSE (web_server.py)

### api_pulse_stream: file:line range

`web_server.py:4308–4369` — `@app.get("/api/pulse/stream")` / `async def api_pulse_stream()`

### SSE wire format

Three event shapes, all standard SSE:

```
id: {event_id}\nevent: health\ndata: {json}\n\n        # health probe per chip
id: {event_id}\nevent: pulse.activity\ndata: {json}\n\n # hive log delta
: heartbeat\n\n                                          # keepalive comment line
```

Source: `web_server.py:4336` (health), `web_server.py:4350` (pulse.activity), `web_server.py:4355` (heartbeat).

### Event-enqueue path

- **health** events: `_probe_all()` is called via `loop.run_in_executor(None, _probe_all)` every 10 s (`web_server.py:4332–4340`). Probe functions are defined in `dashboard_health.py:84–248` (process/TCP probes). Events are emitted directly from the generator — no queue, no thread-safe buffer.
- **pulse.activity** events: sourced from `pulse_activity_iter()` (imported from `hermes_cli.pulse_data`), an async generator polled via `asyncio.wait({activity_task}, timeout=1.0)` (`web_server.py:4345`). Events drain one-at-a-time per loop tick.
- Loop cadence: health every 10 s, heartbeat every 15 s, activity drained on every tick with 1 s timeout.

### Auth: dashboard token file + how it's checked

The session token is **ephemeral** — generated on every server start at `web_server.py:92`:

```python
_SESSION_TOKEN = secrets.token_urlsafe(32)
```

It is NOT loaded from `~/.hermes/.dashboard-token`. There is no persistent token file. The token is injected into the served SPA HTML at startup.

The `/api/pulse/stream` endpoint is in `_QUERY_TOKEN_PATHS` (`web_server.py:132–134`), meaning the browser's `EventSource` (which cannot set custom headers) sends it as `?token=...`. The auth check at `web_server.py:162–168` accepts it:

```python
if request.url.path in _QUERY_TOKEN_PATHS:
    query_token = request.query_params.get("token", "")
    if query_token and hmac.compare_digest(query_token.encode(), _SESSION_TOKEN.encode()):
        return True
```

All other `/api/` paths require either `X-Hermes-Session-Token` header or `Authorization: Bearer <token>` (`web_server.py:150–160`).

WORKFLOW-LESSONS §1.7 and §6.1 note:

> "The dashboard service generates `_SESSION_TOKEN = secrets.token_urlsafe(32)` on every process start. ... When H6 rebuilt the bundle and triggered a restart, the existing session token I'd grabbed went stale → next API call returned 401 → I had to re-grab."

> "HTTP SSE endpoints DO NOT [accept `?token=` query-param] — see CRITICAL-002 from the Codex audit. Fix pending in the fixer hive." (`WORKFLOW-LESSONS.md:182`)

The Codex audit finding (CRITICAL-002) noted the query-token SSE path was broken; the fix appears to have landed in the current code (the `_QUERY_TOKEN_PATHS` frozenset now includes `/api/pulse/stream`). Verify the fixer hive was applied.

### Reconnection: keepalive interval

Heartbeat comment line emitted every **15 s** (`web_server.py:4354–4356`):

```python
if time.monotonic() - last_heartbeat >= 15:
    yield ": heartbeat\n\n"
```

No explicit SSE `retry:` field is set. Browser `EventSource` reconnects on drop using its default backoff (typically 3 s). `Cache-Control: no-cache` and `X-Accel-Buffering: no` are set on the StreamingResponse headers (`web_server.py:4368`).

---

## Dashboard hives pattern (template for /codex-sessions)

### Route paths

All routes are prefixed `/api/dashboard` (APIRouter at `dashboard_health.py:27`):

| Route | Handler | Line |
|---|---|---|
| `GET /api/dashboard/hives` | `get_hives_snapshot()` | `dashboard_health.py:1997–2013` |
| `GET /api/dashboard/hives/{hive_id}/log` | `get_hive_log()` | `dashboard_health.py:2016–2029` |

No `/api/dashboard/hives/{hive_id}` single-item route exists yet — only list + log-tail. The `/codex-sessions` design will need to add it.

### JSON payload contract

`GET /api/dashboard/hives` response (from `_build_hives_snapshot()` at `dashboard_health.py:624–678`):

```json
{
  "hives": [
    {
      "id": "<workdir-name>",
      "workdir": "/abs/path",
      "session": "<tmux-session-name-or-null>",
      "status": "running|completed|blocked|stale",
      "tracking_card": "<kanban-card-id-or-null>",
      "started_at": "<ISO-8601-or-null>",
      "updated_at": "<ISO-8601-or-null>",
      "elapsed_seconds": 0,
      "final_report_status": "COMPLETE|BLOCKED|null",
      "final_report_path": "/abs/path-or-null",
      "log_path": "/abs/path-or-null",
      "log_size_bytes": 0,
      "log_mtime": "<ISO-8601-or-null>",
      "tmux_alive": true,
      "track_title": "<TRACK_TITLE from LAUNCH.sh or null>",
      "objective_summary": "<first 200 chars of objective.md or null>"
    }
  ],
  "scanned_at": "<ISO-8601>",
  "active_count": 0,
  "completed_count": 0,
  "stale_count": 0
}
```

`GET /api/dashboard/hives/{hive_id}/log` response (from `_get_hive_log_tail()` at `dashboard_health.py:696–738`):

```json
{
  "lines": ["..."],
  "path": "/abs/path",
  "mtime": "<ISO-8601>",
  "truncated_to": 200
}
```

### State source: file? in-memory?

**Filesystem-only, no database.** `_build_hives_snapshot()` at `dashboard_health.py:624` scans `~/.hermes/ruflo-work/` directory entries. Per hive, it reads (read-only):

- `<workdir>/.ruflo-status.json` — session name, tracking card, updated_at (`dashboard_health.py:471–478`)
- `<workdir>/LAUNCH.sh` — mtime as started_at, grep for `TRACK_TITLE=` (`dashboard_health.py:486–500`)
- `<workdir>/objective.md` — first 200 chars (`dashboard_health.py:512–520`)
- `<workdir>/FINAL-REPORT.md` — first 500 chars for status line (`dashboard_health.py:523–546`)
- `<workdir>/hive-mind.log` — stat only for log_path/size/mtime (`dashboard_health.py:550–560`)

tmux liveness is checked via `tmux ls -F #{session_name}` subprocess (`dashboard_health.py:447–458`).

Cache TTL: **15 s** in-memory (`_HIVES_CACHE`, `_HIVES_TTL = 15.0` at `dashboard_health.py:55–56`).

**Template implication for /codex-sessions**: mirror the same pattern — scan a `~/.hermes/codex-sessions/` (or `ruflo-work/codex-*/`) directory, write a `.codex-session-status.json` per run, expose `GET /api/dashboard/codex-sessions` + `GET /api/dashboard/codex-sessions/{id}/log` with a 15 s cache.

---

## Existing git-shell-out patterns

### Uses of `git` in hermes_cli — file:line list

| File | Line(s) | Command | Context |
|---|---|---|---|
| `git_janitor.py` | 53 | `["git", "-C", str(repo), *args]` | Central `_git()` helper; all read-only `worktree list`, `log`, `merge-base` calls go through it |
| `git_janitor.py` | 68 | `git worktree list --porcelain` | Inventory worktrees via `_git()` |
| `git_janitor.py` | 266 | `"git worktree remove"` | String only (label), not a subprocess call |
| `pulse_data.py` | 341 | `["git", "-C", repo, "log", "fork/main", "--since=...", ...]` | PR merge counting for KPIs |
| `banner.py` | 141, 158, 167, 288, 316, 352 | `git ls-remote`, `git fetch`, `git rev-list`, `git rev-parse`, `git describe` | Version/upstream checks |
| `profile_distribution.py` | 379 | `["git", "clone", "--depth", "1", url, dest]` | Plugin install from git URL |
| `dump.py` | 25 | `["git", "rev-parse", "--short=8", "HEAD"]` | Build-info dump |
| `main.py` | 7524, 7861 | `["git"]`, `"git"` | Git init / worktree commands for `hermes -w` |

Subprocess pattern used throughout: `subprocess.run([...], capture_output=True, text=True, check=False)` (matches `git_janitor.py:50–55`).

### Uses of `git worktree` — confirmed locations

- `git_janitor.py:63–100` — `inventory_worktrees()` calls `git worktree list --porcelain` via `_git()`. Read-only.
- `git_janitor.py:266` — `"git worktree remove"` is a status label string, not a call.
- `main.py:1224–1236` — `_setup_worktree()` and `_cleanup_worktree()` for `hermes -w` mode.
- `cli.py:816–980` — `--worktree` / `-w` flag handler with `git worktree add` / `git worktree remove --force`.

None of these are in the dashboard or hives layer. The worktree broker for codex-parallel is **greenfield** — no existing module in the dashboard stack creates or manages worktrees. Must establish the subprocess-git pattern mirroring `git_janitor.py:50–55`:

```python
subprocess.run(["git", "-C", str(repo), "worktree", "add", ...],
               capture_output=True, text=True, check=False)
```

---

## ruflo-launch-interactive.template.sh — the Opus pane template

**File:** `/home/josep/.hermes/scripts/templates/ruflo-launch-interactive.template.sh`

### Full breakdown

| Section | Lines | Detail |
|---|---|---|
| Shebang + mode | 1, 34 | `#!/usr/bin/env bash` / `set -uo pipefail` (no `-e` — intentional; individual steps checked inline) |
| Required substitutions | 37–41 | `WORKDIR`, `SESSION`, `TRACK_TITLE`, `TRACK_BODY` must be replaced; `ISA=""` optional |
| Binary paths | 44–50 | `RUFLO=/home/josep/.hermes/node/bin/ruflo`, `HERMES=/home/josep/.local/bin/hermes` — hardcoded absolute paths |
| Support files | 47–51 | `MCP_TEMPLATE`, `SETTINGS_TEMPLATE`, `NOTIFY_SCRIPT`, `HELPERS` — all absolute paths |
| Helpers source | 53 | `. "$HELPERS"` (ruflo-launcher-helpers.sh) provides `kanban_breadcrumb_launch`, `isa_brief_objective` |
| Preflight checks | 57–61 | `objective.md`, `ruflo` binary, `watcher.sh` must exist |
| Ruflo init | 83 | `ruflo init --force --minimal --skip-claude --no-global` — clobbers `.mcp.json` + `.claude/settings.json` |
| Template restore | 85–88 | Immediately restores both files from `MCP_TEMPLATE` and `SETTINGS_TEMPLATE` |
| Hive-mind init | 91 | `ruflo hive-mind init -m 15 -t hierarchical-mesh` |
| Kanban card creation | 94–99 | `hermes kanban create` + card ID written to `.tracking-card` |
| tmux launch | 104–105 | `tmux new-session -d -s "$SESSION"` — detached, runs `ruflo hive-mind spawn --claude --objective "$(cat ...)" -n 4 --mcp-config .mcp.json ; exec sleep infinity` |
| Log capture | 108 | `tmux pipe-pane -o` redirects pane output to `hive-mind.log` |
| Dialog clearing | 117–145 | Bounded loop (24 × 5 s = 120 s max); sends `Enter` to accept workspace-trust + MCP-approval dialogs interactively |
| Watcher fork | 149–151 | `nohup bash watcher.sh >> watcher.log 2>&1 &`; PID written to `.watcher.pid` |
| Kanban breadcrumb | 155–158 | `kanban_breadcrumb_launch` — blocks card as EXTERNAL-RUNNING |
| Notify | 160 | `discord-notify.sh` (note: template calls it `discord-notify.sh`, not `telegram-notify.sh`) |

### Key env vars that must propagate

- `HERMES` — set explicitly at line 46 and exported: `export HERMES`
- `RUFLO` — referenced as absolute path; not exported as env var
- `HOME` — implicit; all `~/.hermes`, `~/.claude` paths resolve from it
- No explicit `ANTHROPIC_TOKEN` or OAuth env var exports — auth is resolved at runtime by claude binary reading `~/.claude/.credentials.json`

### Where Max OAuth state lives

`~/.claude/.credentials.json` — cited in `web_server.py:1378` (`"source_label": "Claude Code (~/.claude/.credentials.json)"`) and `dashboard_health.py:30` (`CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"`). The `read_claude_code_credentials()` function (`web_server.py:1405`) reads it.

The interactive template does NOT set or export OAuth env vars. It relies on the `claude` binary inheriting the user's shell environment and reading `~/.claude/.credentials.json` directly.

### tmux usage

tmux is central:
- `tmux new-session -d -s "$SESSION"` spawns the hive in a detached pane (line 104)
- `tmux pipe-pane` captures pane output to log (line 108)
- `tmux capture-pane -p` polls for dialog text (line 134)
- `tmux send-keys` sends Enter to dismiss dialogs (line 136)
- `tmux has-session` used for liveness checks (lines 69, 110, 131)

For an Opus peer-review pane, the design must add a second `tmux new-window` or `tmux split-window` call within the same session, passing a separate prompt file and potentially a separate `--mcp-config`. The `exec sleep infinity` tail (line 105) keeps the pane alive after `ruflo` exits, which is the correct pattern for a peer-review pane that needs to stay attached.

---

## WORKFLOW-LESSONS.md quotes (verbatim)

### §3 Hard rules — numbered list

> 1. **Never mutate `ms` project** (run_id `rg_91b80749ac82`). Backend pytest blocks; harness classifier blocks. Don't bypass either.
> 2. **Never restart the dashboard service or gateway** without checkpointing the session token + warning the user (restart rotates the token and breaks any active sessions).
> 3. **Never delete the backup files** at `*.bak.<ts>/` paths. They are the rollback. If you absolutely must, rename to `.deleted.<ts>/` instead (the H6-introduced safe-delete pattern).
> 4. **Never skip hooks** (`--no-verify` on git, `--no-gpg-sign`, etc.) unless the user explicitly requests it AND there's a documented reason.
> 5. **Never use destructive shell** (`rm -rf`, force-truncate via `>`, `git clean -fxd`) as a cleanup shortcut. Use rename-to-deleted pattern.
> 6. **Always backup** before mutating shared state — `projects.json`, plugin trees, config files.
> 7. **Always wire `telegram-notify.sh`** into every hive watcher (launch + on done/failed/blocked/timeout). Without it, completions silently disappear.
> 8. **Always recursive-glob for FINAL-REPORT.md** in watchers (queens may organize output into subdirs; root-only check fails silently).
> 9. **Always include `kanban_breadcrumb_*` helpers** in launch + completion paths (otherwise the kanban board drifts from live tmux state).
> 10. **Never claim a feature is live** without a curl + JSON + DOM end-to-end assertion. See preference 2.2.

(`WORKFLOW-LESSONS.md:105–114`)

### §4.3 — "git-explicit rule" (master prompt pattern, not git-explicit)

Section 4.3 is labeled "Master prompt + memory entry for re-engagement" — NOT a git-explicit rule. Verbatim:

> ### 4.3 Master prompt + memory entry for re-engagement
> After a long-running build, write a "MASTER-PROMPT.md" in the workdir + a memory entry pointing to it. Future sessions can re-engage by reading the master prompt + chain.log.

(`WORKFLOW-LESSONS.md:127–129`)

There is no §4.3 "git-explicit rule for hives" in the current document. This may refer to a planned or misidentified section.

### §4.6 — Confirmation-token API pattern

> ### 4.6 Confirmation-token API pattern
> For destructive endpoints (stop, delete, mutate): `{"confirm": "EXACT_STRING"}` with the expected string in the error message + OpenAPI examples + description. Cheap, self-documenting, accident-resistant.

(`WORKFLOW-LESSONS.md:135–137`)

This is the "force-merge confirmation token" equivalent. For `/codex-sessions` destructive ops (stop, reap, force-merge), apply this pattern.

### §6 Environment notes — relevant excerpts

> **§6.1 Dashboard auth**
> - Token at `window.__HERMES_SESSION_TOKEN__` in the served HTML; ephemeral per process start
> - Grab via `curl -s :9119/ | grep -oE '__HERMES_SESSION_TOKEN__="[^"]*"' | sed ...`
> - Header: `X-Hermes-Session-Token: $TOKEN`
> - Public endpoints (no token needed): `/api/status`, `/api/config/defaults`, `/api/config/schema`, `/api/model/info`, `/api/dashboard/themes`, `/api/dashboard/plugins`, `/api/dashboard/plugins/rescan`, `/api/welcome/first-run-status`
> - WebSocket endpoints accept `?token=...` query-param (line 3284+ in web_server.py)
> - HTTP SSE endpoints DO NOT — see CRITICAL-002 from the Codex audit. Fix pending in the fixer hive.

(`WORKFLOW-LESSONS.md:177–183`)

> **§6.3 Ruflo binary**
> - At `/home/josep/.hermes/node/bin/ruflo` (not on PATH; see memory `ruflo-cli-location`)

(`WORKFLOW-LESSONS.md:189`)

---

## PROVIDER-STACK.md quotes (verbatim)

### 2026-06-15 billing-split clause

> ## ⚠️ 2026-06-15 — Anthropic billing change
>
> From 2026-06-15 Anthropic moves programmatic usage — `claude -p` / `claude --print`, the Agent SDK, Claude Code GitHub Actions, third-party agents — off the Max subscription onto a separate, API-priced "Agent SDK credit" pool ($100 Max-5x / $200 Max-20x, no rollover). Only **interactive** Claude Code stays on the Max subscription, so **`claude -p` is no longer billing-safe**. To keep Ruflo hives on Max, launch them interactively — `~/.hermes/scripts/templates/ruflo-launch-interactive.template.sh` (built + validated 2026-05-22). Full detail: memory `anthropic-jun15-billing-split`.

(`PROVIDER-STACK.md:7–16`)

### Role assignments table

> | Role | Model | Auth | Why |
> |------|-------|------|-----|
> | Orchestrator | claude-opus-4.7 | Max OAuth (`claude -p`) | Strongest reasoning + tool fidelity |
> | Executor | claude-sonnet-4-6 | Max OAuth | Default agentic quality; free on Max |
> | Routing / triage | claude-haiku-4-5 | Max OAuth | Fast classification, tool-safe, free |
> | Grunt (text) | claude-haiku-4-5 | Max OAuth | Reverses earlier "Gemini Flash" plan — Haiku is free under Max and tool-safer |
> | Grunt (vision) | gemini-3-flash | Google AI Studio key | Haiku has no vision |
> | Grunt (>200K context) | gemini-3-flash | Google AI Studio key | Haiku's 200K limit |
> | Burst overflow | gemini-3-flash | Google AI Studio key | When Max quota nears limit |
> | Architecture / config review | nous/hermes-4 | Nous Portal OAuth | Hermes-aligned reasoning |
> | Adversarial-diversity critic | gpt-5.5 | ChatGPT OAuth (codex CLI) | Different lineage from Anthropic catches blind spots |
> | Reviewer (Codex path) | gpt-5.5 via codex CLI | ChatGPT OAuth | h2reviewer alternative + dream-reflect |

(`PROVIDER-STACK.md:36–47`)

### tmux / interactive-pane pattern

No dedicated tmux/interactive-pane section exists in PROVIDER-STACK.md. The interactive-pane design is documented exclusively in `ruflo-launch-interactive.template.sh` (see above) and the billing-change note in PROVIDER-STACK.md:7–16. The pattern is: use `tmux new-session -d` + `ruflo hive-mind spawn --claude` (no `--non-interactive`) + `exec sleep infinity` tail to keep the PTY alive.

---

## Open questions for the queen

- WORKFLOW-LESSONS §6.1 says SSE query-token auth "CRITICAL-002 fix pending in the fixer hive" — is the current `_QUERY_TOKEN_PATHS` frozenset (web_server.py:132) the shipped fix, or is it still broken? Need to verify with a live curl against `/api/pulse/stream?token=...`.
- The template uses `discord-notify.sh` (line 160) but §3 rule #7 says `telegram-notify.sh`. Which is the live notify script for new hives? The watcher template may differ from the launcher.
- `~/.hermes/.dashboard-token` is mentioned in the task brief as the token source, but the code shows no such file is used — the token is purely ephemeral in-memory. Is a persistent token file planned, or is the brief spec diverging from current reality?
- The `/api/dashboard/hives/{hive_id}` single-item GET does not exist (only `/hives` list + `/hives/{id}/log`). Codex-sessions design needs to decide: add the single-item route, or have the client filter from the list?
- The worktree broker is fully greenfield in the dashboard layer. Must decide: (a) add it as a new `dashboard_health.py` section, (b) new file `dashboard_worktrees.py`, or (c) a plugin. The `git_janitor.py:50–55` subprocess pattern is the canonical model.
- `claude-opus-4.7` is listed as Orchestrator in PROVIDER-STACK.md but as `claude -p` (which moves off Max on 2026-06-15). For the peer-review Opus pane: must it be interactive (no `-p`) to stay billing-safe? The interactive template is the answer — but Opus interactive mode means the dialog-clearing loop (lines 117–145) must be tested with Opus specifically.
- h2reviewer misroute (`t_b1719e96`) is still unresolved per PROVIDER-STACK.md:76–78. Does any codex-sessions review path route through h2reviewer? If so, the paid-API bleed is live.
