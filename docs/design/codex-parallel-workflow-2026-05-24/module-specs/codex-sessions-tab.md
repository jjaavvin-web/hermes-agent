# Module Spec — /codex-sessions Dashboard Tab

**Implements:** DESIGN.md §6.5
**Phase:** P4 (lands after MergeBroker; prereqs P1–P3 complete)
**Diagram:** architecture-diagram.md §7 (SSE event flow)
**Template:** audits/cluster-D-worktree-dashboard.md §"Dashboard hives pattern"

---

## 1. Purpose & scope

This tab gives the operator a live view of every active Codex session: which thread is executing, where it is in its ISA, whether a reviewer is busy on it, and whether it needs intervention. It is a read-mostly observability surface with four destructive controls (pause / resume / kill / force-merge) guarded by confirmation tokens. It adds no new SSE endpoint and requires no dashboard service restart — both constraints are load-bearing (WORKFLOW-LESSONS §3 rule 2).

---

## 2. Files created / touched

| File | Action | Notes |
|---|---|---|
| `hermes_cli/dashboard_codex_sessions.py` | **Create** | New APIRouter; mirrors `dashboard_health.py` structure and cache pattern (`_HIVES_CACHE` / `_HIVES_TTL` at `dashboard_health.py:55–56`) |
| `hermes_cli/web_server.py` | **Touch (minimal)** | Register the new APIRouter (one `app.include_router(...)` line); add `kind: codex-session` emission path in `pulse_activity_iter` call site — 1–2 line additions, no restart-triggering changes |
| `hermes_cli/pulse_data.py` | **Touch (additive)** | Add one new async generator source to `pulse_activity_iter()` that yields `kind: codex-session` events from `CodexSessionDispatcher` emissions |
| Dashboard SPA (`/hives` tab) | **Out of scope** | The `/hives` tab is the frontend template; execution hive should clone its card-grid + detail-pane pattern. Point the SPA engineer at the existing `/hives` component. |

`dashboard_codex_sessions.py` must stay under 500 lines (CLAUDE.md rule). Split into `_snapshot.py` and `_actions.py` helpers if needed.

---

## 3. New API routes

All routes live under the `dashboard_router` APIRouter (prefix `/api/dashboard`, same as `dashboard_health.py:27`). All require `X-Hermes-Session-Token` header except the SSE path, which uses query-token auth already wired in `_QUERY_TOKEN_PATHS` (`web_server.py:132–134`).

| Method + Route | Handler | Purpose |
|---|---|---|
| `GET /api/dashboard/codex-sessions` | `get_codex_sessions_snapshot()` | List view with aggregate counters; 15 s cache |
| `GET /api/dashboard/codex-sessions/{sid}` | `get_codex_session_detail()` | Single-session: ISA verbatim, diff, review history, tmux tail |
| `GET /api/dashboard/codex-sessions/{sid}/log` | `get_codex_session_log()` | Log tail; mirrors `get_hive_log()` at `dashboard_health.py:2016–2029` |
| `POST /api/dashboard/codex-sessions/{sid}/pause` | `post_pause_session()` | Operator action; confirm token required |
| `POST /api/dashboard/codex-sessions/{sid}/resume` | `post_resume_session()` | Operator action; no confirm token (non-destructive) |
| `POST /api/dashboard/codex-sessions/{sid}/kill` | `post_kill_session()` | Destructive; confirm token `"KILL_CODEX_SESSION"` |
| `POST /api/dashboard/codex-sessions/{sid}/force-merge` | `post_force_merge()` | Destructive; confirm token `"FORCE_MERGE_CODEX_SESSION"` |

`{sid}` is the UUID4 session identifier written by `CodexSessionDispatcher` to `codex_sessions.json` at session creation (DESIGN.md §4 step 5).

---

## 4. JSON payload contracts

### GET /api/dashboard/codex-sessions

```json
{
  "sessions": [
    {
      "sid": "<uuid4>",
      "thread_id": "<discord-snowflake>",
      "thread_name": "<channel#thread-name>",
      "tmux_session": "codex-sess-<sid>",
      "tmux_alive": true,
      "worktree_path": "/home/josep/.hermes/codex-wt/<sid>",
      "worktree_branch": "codex/<sid>/<isa-slug>",
      "kanban_card": "t_xxxxxxxx",
      "isa_id": "20260524-2000_codex-feature-xyz",
      "isa_phase": "execute",
      "isa_progress": "5/12",
      "review_iteration": 0,
      "last_review_verdict": null,
      "last_message_at": "<ISO-8601>",
      "token_spend_estimate": null,
      "port": 50001,
      "created_at": "<ISO-8601>",
      "state": "EXECUTING|VERIFYING|REVIEWING|MERGING|NEEDS_REVIVE"
    }
  ],
  "scanned_at": "<ISO-8601>",
  "active_count": 3,
  "needs_revive_count": 1,
  "review_pool": {
    "size": 2,
    "warm": 1,
    "busy": 1,
    "dead": 0
  }
}
```

`token_spend_estimate` is P4 best-effort (null is valid). `review_pool` is read from `~/.hermes/codex-review-state.json` (written by `PeerReviewOrchestrator`).

Pagination kicks in at 50 rows (see §11). Default sort: `state == NEEDS_REVIVE` first, then `created_at` descending.

### GET /api/dashboard/codex-sessions/{sid}

All snapshot row fields, plus:

```json
{
  "isa_verbatim": "<full ISA.md text>",
  "current_diff": "<git diff origin/main output, capped at 200 KB>",
  "diff_truncated": false,
  "review_history": [
    {
      "iteration": 0,
      "verdict": "REVISE",
      "rationale": "...",
      "captured_at": "<ISO-8601>"
    }
  ],
  "tmux_capture_tail": ["<line>", "..."]
}
```

`tmux_capture_tail` is the last 100 lines of `tmux capture-pane -p -t codex-sess-<sid>` (`subprocess.run`, `capture_output=True`, same pattern as `dashboard_health.py:447–458`). If the tmux session is dead, the field is `null`.

### GET /api/dashboard/codex-sessions/{sid}/log

```json
{
  "lines": ["..."],
  "path": "/home/josep/.hermes/codex-wt/<sid>/.hermes-session.log",
  "mtime": "<ISO-8601>",
  "truncated_to": 200
}
```

Mirrors `_get_hive_log_tail()` at `dashboard_health.py:696–738`. Default tail: 200 lines. Client may pass `?lines=N` (max 1000).

### POST destructive endpoints

Request body (all four destructive routes):

```json
{ "confirm": "<EXACT-TOKEN>" }
```

- `pause`: confirm token `"PAUSE_CODEX_SESSION"`
- `kill`: confirm token `"KILL_CODEX_SESSION"`
- `force-merge`: confirm token `"FORCE_MERGE_CODEX_SESSION"`
- `resume`: no confirm body required

On wrong or missing token: HTTP 422 with body:

```json
{
  "detail": "confirm token required; expected \"KILL_CODEX_SESSION\""
}
```

Pattern per WORKFLOW-LESSONS §4.6. The expected token string appears verbatim in the 422 body and in OpenAPI `examples`. This is self-documenting — an operator reading the error knows exactly what to send.

Success response for all POST actions:

```json
{ "ok": true, "sid": "<sid>", "action": "kill" }
```

---

## 5. State source (filesystem-only, no DB)

The handler builds each session row from four filesystem reads, identical in philosophy to `_build_hives_snapshot()` at `dashboard_health.py:624–678`:

```
~/.hermes/codex_sessions.json          ← primary session map (flock-read, fcntl.flock LOCK_SH)
~/.hermes/codex-review-state.json      ← reviewer pool status (flock-read)
~/.hermes/work/<isa-id>/ISA.md         ← frontmatter: phase, progress (read-only)
tmux ls -F '#{session_name}'           ← liveness check (subprocess, same as dashboard_health.py:447–458)
```

Per-session enrichment for the detail route:

```
git -C <worktree> diff origin/main     ← capped at 200 KB (subprocess.run, capture_output=True)
~/.hermes/codex-wt/<sid>/.review-history.json  ← written by PeerReviewOrchestrator
tmux capture-pane -p -t codex-sess-<sid>       ← last 100 lines
```

Cache: one module-level `_CODEX_SESSIONS_CACHE: dict | None = None` and `_CODEX_SESSIONS_TS: float = 0.0`, TTL 15 s (`_CODEX_SESSIONS_TTL = 15.0`). Cache applies only to the list route; the detail and log routes are always live (they are not called on every poll cycle).

All subprocess calls use `subprocess.run([...], capture_output=True, text=True, check=False)` — the established pattern at `git_janitor.py:50–55`. Non-zero return codes produce a warning log entry, not a 500.

---

## 6. SSE wiring

No new SSE endpoint. The existing `/api/pulse/stream` channel (`web_server.py:4308–4369`) carries `codex-session` events as a new discriminated variant of the existing `pulse.activity` event type.

**Event queue backpressure:** the `asyncio.Queue` used by `pulse_activity_iter()` for `kind: codex-session` events must be capped at a maximum depth of **100 events**. On overflow (queue full), drop the **oldest** event (not the newest) — the dashboard needs current state, not history. Add a `dropped_codex_events` counter to the SSE health probe so the operator can detect backpressure from a slow client. Rationale: with 8 concurrent Codex sessions each emitting events on every `run_turn` completion, phase transition, and review verdict, the queue can fill faster than the drain rate.

```
architecture-diagram.md §7 — full flow diagram

CodexSessionDispatcher
  │  emits on: allocate / run_turn complete / phase transition / verdict / merge
  ▼
pulse_data.pulse_activity_iter()  ← additive async generator source (pulse_data.py)
  │  yields: {"kind": "codex-session", "sid": "...", "sub_kind": "...", ...}
  ▼
web_server.py:4350  ← existing event: pulse.activity emission
  │  data: {kind, sid, sub_kind, phase, progress, tmux_alive, last_msg_at}
  ▼
browser EventSource('/api/pulse/stream?token=<tok>')
  │  filters: event.data.kind === "codex-session"
  ▼
/codex-sessions tab card update
```

`sub_kind` values:

| sub_kind | Trigger |
|---|---|
| `session_created` | Dispatcher allocates a new session |
| `phase_change` | ISA phase transition written |
| `review_started` | Orchestrator claims pane for this sid |
| `review_verdict` | APPROVE / REVISE / ESCALATE captured |
| `merge_complete` | MergeBroker.merge() succeeds |
| `needs_revive` | tmux session gone, worktree present |

The browser tab listens on the existing EventSource the SPA already opens for the `/hives` tab. The `kind` discriminator means the `/codex-sessions` tab can share the same connection.

Auth for EventSource: query-param `?token=<_SESSION_TOKEN>`. The token is in `window.__HERMES_SESSION_TOKEN__` (WORKFLOW-LESSONS §6.1). The `/api/pulse/stream` path is already in `_QUERY_TOKEN_PATHS` (`web_server.py:132–134`); no change needed.

---

## 7. Frontend hooks (out of detailed scope)

| Element | Detail |
|---|---|
| Tab id | `codex-sessions` |
| Card grid | One card per session row; `NEEDS_REVIVE` cards render with red border |
| Per-card controls | pause / resume / kill / force-merge buttons; kill and force-merge open a modal requiring the operator to type the confirm token |
| Detail pane | ISA-verbatim (markdown renderer), diff (syntax-highlighted), review history timeline, tmux-tail pre-formatted block |
| Live update | SSE `kind === "codex-session"` events patch the matching card in-place; no full list re-fetch |
| Template | Clone the `/hives` tab component; swap `hive_id` for `sid`, add the four action buttons, add the detail-pane ISA/diff/review sections |

---

## 8. Auth

`_SESSION_TOKEN` is generated at `web_server.py:92` (`secrets.token_urlsafe(32)`) on every process start and is not stored to disk. This design does not introduce a persistent token, does not add `/api/dashboard/codex-sessions` to the public-endpoint list, and does not modify any auth middleware.

New routes inherit the existing middleware check at `web_server.py:150–160`: `X-Hermes-Session-Token` header required for all HTTP routes. The SSE path (`/api/pulse/stream`) already accepts query-token via `_QUERY_TOKEN_PATHS` and is unmodified.

The 1–2 line addition to `web_server.py` (registering the new APIRouter) does not trigger a service restart by itself. WORKFLOW-LESSONS §3 rule 2 is satisfied: the implementation hive must deploy these additions through the existing reload path or a cold start, not by restarting the dashboard mid-session without checkpointing the token.

---

## 9. Performance & limits

| Concern | Limit | Source |
|---|---|---|
| List-route cache TTL | 15 s | Mirrors `_HIVES_TTL` at `dashboard_health.py:55–56` |
| Detail-route diff cap | 200 KB raw; `diff_truncated: true` flag if over | subprocess stdout cap |
| Log tail default | 200 lines; max 1000 via `?lines=N` | Mirrors `dashboard_health.py:2016–2029` |
| tmux capture-pane tail | 100 lines | Hard-coded; not configurable |
| Pagination threshold | 50 rows before cursor pagination | `needs_revive` rows always included before cutoff |
| Concurrency | Dashboard reads are filesystem-only; no shared write state; concurrent requests safe | |
| Destructive POST serialization | Each POST goes through `CodexSessionDispatcher.dispatch_action(sid, action)` which serialises per-session via an `asyncio.Lock` keyed on `sid` | |

---

## 10. Error modes

| Condition | HTTP | Response |
|---|---|---|
| `codex_sessions.json` missing | 200 | Empty `sessions: []`, `active_count: 0` |
| `codex_sessions.json` parse error | 500 | `{"detail": "codex_sessions.json parse error: ..."}` |
| `{sid}` not in sessions map | 404 | `{"detail": "session not found"}` |
| ISA.md missing for a session | 200 (list/detail) | `isa_phase: null`, `isa_progress: null`; no error thrown |
| tmux subprocess fails | 200 | `tmux_alive: false`; error logged at WARNING |
| git diff subprocess fails | 200 (detail) | `current_diff: null`, `diff_truncated: false`; WARNING log |
| Wrong confirm token on POST | 422 | `{"detail": "confirm token required; expected \"TOKEN\""}` |
| Session in NEEDS_REVIVE; pause/resume attempted | 409 | `{"detail": "session is in NEEDS_REVIVE state; use /revive"}` |
| `post_force_merge` while reviewer is BUSY | 409 | `{"detail": "review in progress; wait for verdict or use kill first"}` |
| `codex-review-state.json` missing | 200 | `review_pool` block omitted from response |

---

## 11. Edge cases

**NEEDS_REVIVE sessions.** When `tmux ls` returns no session matching `codex-sess-<sid>` but the row still exists in `codex_sessions.json`, the snapshot sets `state: NEEDS_REVIVE` and `tmux_alive: false`. The pulse SSE emits `sub_kind: needs_revive`. The tab renders these cards with a red border. The operator uses `/revive` (P5 slash command) or the dashboard kill-and-restart flow.

**No ISA yet.** Between `CodexSessionDispatcher.allocate()` writing the `codex_sessions.json` row and the ISA-required gate completing (DESIGN.md §11 — P1 first ISC), a session may have `isa_id: null`. The list handler renders `isa_phase: null` and `isa_progress: "0/?"`; the detail handler returns `isa_verbatim: null`. Neither case raises an error.

**Orphan accumulation.** If `codex_sessions.json` has more than 50 rows (COMPLETE/ARCHIVED rows not yet gc'd), the list route returns the first 50 sorted by `state == NEEDS_REVIVE` → `created_at` descending, plus a `"truncated": true` flag and `"total_count": N` in the response envelope. The operator is expected to run gc (P5 `WorktreeBroker.gc()`). The spec does not auto-gc from the dashboard route — that would be a side effect on a read path.

**Concurrent detail requests.** The detail route runs `git diff` and `tmux capture-pane` as subprocesses. If two requests arrive for the same sid, both run their subprocesses concurrently. This is safe: reads only, no write contention. The detail route intentionally bypasses the list cache.

**flock contention.** `codex_sessions.json` flock is a shared read lock (`fcntl.LOCK_SH | fcntl.LOCK_NB`). If the dispatcher holds an exclusive write lock at the moment a dashboard read arrives, the read call falls back immediately (LOCK_NB) and returns stale cache data if within TTL, or an empty response with a `Retry-After: 1` header if no cache is available.

---

## 12. Test strategy (high-level)

| Test | What it checks |
|---|---|
| Smoke: GET /api/dashboard/codex-sessions | 200, `Content-Type: application/json`, `sessions` key present |
| Smoke: GET /codex-sessions/{sid} | 200 with known sid from fixture; `isa_verbatim` key present |
| Smoke: GET /codex-sessions/{sid}/log | 200, `lines` is a list |
| Confirm-token: kill with no body | 422, body contains `"KILL_CODEX_SESSION"` |
| Confirm-token: kill with wrong token | 422, body contains `"KILL_CODEX_SESSION"` |
| Confirm-token: kill with correct token | 200, `ok: true` |
| Confirm-token: force-merge wrong token | 422, body contains `"FORCE_MERGE_CODEX_SESSION"` |
| Cache hit: two GETs within 15 s | Second call does not invoke `tmux ls` subprocess (mock assert call count == 1) |
| Cache miss: GET after 15 s | `tmux ls` invoked again |
| SSE: codex-session event flows | Mock dispatcher emits `phase_change`; assert SSE stream yields `kind: codex-session` event within 2 s |
| NEEDS_REVIVE: tmux session absent | List route sets `state: NEEDS_REVIVE`, `tmux_alive: false` |
| No ISA: `isa_id` null in sessions.json | List route returns `isa_phase: null` without 500 |
| Pagination: 51 rows in sessions.json | Response has `truncated: true`, `sessions` length == 50 |

---

## 13. Citations

| Claim | Citation |
|---|---|
| `/hives` list handler pattern | `dashboard_health.py:1997–2013` (`get_hives_snapshot`) |
| Hives snapshot builder | `dashboard_health.py:624–678` (`_build_hives_snapshot`) |
| Log tail handler | `dashboard_health.py:2016–2029` (`get_hive_log`) |
| Cache TTL 15 s | `dashboard_health.py:55–56` (`_HIVES_CACHE`, `_HIVES_TTL = 15.0`) |
| tmux liveness check subprocess pattern | `dashboard_health.py:447–458` |
| Pulse SSE channel | `web_server.py:4308–4369` (`api_pulse_stream`) |
| SSE event shapes (health / pulse.activity / heartbeat) | `web_server.py:4336, 4350, 4355` |
| `_SESSION_TOKEN` ephemeral generation | `web_server.py:92` |
| `_QUERY_TOKEN_PATHS` (SSE query-token auth) | `web_server.py:132–134` |
| Auth middleware header check | `web_server.py:150–160` |
| git subprocess pattern | `git_janitor.py:50–55` |
| Confirmation-token pattern | `WORKFLOW-LESSONS.md:135–137` (§4.6) |
| Never restart dashboard without checkpointing | `WORKFLOW-LESSONS.md:105` (§3 rule 2) |
| Session 5-tuple definition | `DESIGN.md §5` |
| `codex_sessions.json` new file (not discord_threads.json) | `DESIGN.md §6.3` |
| Bot restart reattach via `tmux ls` | `DESIGN.md §3` (Decision C); `architecture-diagram.md §6` |
| P4 phase context | `DESIGN.md §8` (phased plan table) |
| APIRouter prefix `/api/dashboard` | `dashboard_health.py:27` |
| `pulse_activity_iter` async generator | `pulse_data.py` (grep; full signature out of scope for this tab spec) |
| SSE event flow diagram | `architecture-diagram.md §7` |
| `codex-review-state.json` source | `architecture-diagram.md §4` (pane pool state machine) |
| Max OAuth / billing constraint (no `claude -p`) | `PROVIDER-STACK.md:7–16`; `DESIGN.md §9` |
