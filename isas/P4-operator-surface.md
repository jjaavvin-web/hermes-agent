---
isa:      20260524-2030_codex-parallel-p4-operator-surface
task:     "P4 Operator surface — /codex-sessions dashboard API + confirm-token destructive routes"
tier:     E3
phase:    complete
progress: 10/14
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p4-operator-surface
hive:     "-"
owner:    claude-code
started:  2026-05-25T15:00:00Z
updated:  2026-05-25T15:00:00Z
---

## Problem

P1-P3 deliver the substrate but the operator has no live view of the
fleet. To know the state today requires manually running `tmux ls`,
`ls ~/.hermes/codex-wt/`, `cat ~/.hermes/codex_sessions.json`, and
grepping `~/.hermes/codex-review-state.json`. With 4-8 sessions in
flight this is friction; with parallel reviews and merges, it's not
enough situational awareness to debug a stuck session in real time.

## Goal

After this ISA: backend dashboard routes under
`/api/dashboard/codex-sessions` expose one snapshot endpoint (15s
cache) + per-sid detail + log tail + pause/resume + confirm-token
gated kill/force-merge. The routes read filesystem state
(`codex_sessions.json`, `codex-review-state.json`, `codex-ports.json`,
`agent.log`) and write back to `codex_sessions.json` for state
transitions. The frontend SPA can add a `/codex-sessions` tab in a
separate FE-only ISA — backend is the prerequisite.

## Out of Scope

- Frontend SPA changes — separate FE-only ISA.
- Token spend tracking — placeholder field only.
- Pulse SSE event source (ISC-8, ISC-9) — TOMBSTONED to a follow-up.
  Requires understanding the `pulse_data.pulse_activity_iter()` shape
  + a new `kind: codex-session` discriminator; its own bounded scope.
- Slash command deep-link to dashboard (ISC-10) — small but needs to
  ride a dispatcher PR; TOMBSTONED.

## Constraints

- **P1 must be landed** — routes read its state files.
- **No persistent storage** — read from existing JSON files; 15s cache
  in-memory only, mirrors `_HIVES_TTL` in `dashboard_health.py`.
- **Destructive endpoints require confirm tokens** per WORKFLOW-LESSONS
  §4.6: `kill` → `KILL_CODEX_SESSION`, `force-merge` →
  `FORCE_MERGE_CODEX_SESSION`. 422 response includes expected token in
  the error message + OpenAPI examples.
- **No regression in `/api/dashboard/hives`** — adding a router must
  not change that endpoint's JSON shape.
- **Session token unchanged** — mounting the router doesn't rotate
  `_SESSION_TOKEN`.

## Criteria

- [x] ISC-1: `hermes_cli/dashboard_codex_sessions.py` exists with an
  APIRouter prefixed at `/api/dashboard`
- [x] ISC-2: `GET /api/dashboard/codex-sessions` returns JSON snapshot
  with sessions list + counts + review_pool config; 15s in-memory cache
- [x] ISC-3: `GET /api/dashboard/codex-sessions/{sid}` returns detail
  (row + ISA verbatim + diff with 200KB truncation flag + review_state)
- [x] ISC-4: `GET /api/dashboard/codex-sessions/{sid}/log?tail=N`
  returns agent.log lines filtered by `chat=<thread_id>`, last N
- [x] ISC-5: `POST /pause` and `/resume` succeed without a body and
  flip the row's `paused` flag
- [x] ISC-6: `POST /kill` requires `{"confirm": "KILL_CODEX_SESSION"}`;
  wrong token → 422 with the expected value in the error body
- [x] ISC-7: `POST /force-merge` requires `{"confirm":
  "FORCE_MERGE_CODEX_SESSION"}`; wrong token → 422; correct → row
  state → `MERGING` for dispatcher's next tick
- [-] ISC-8: pulse SSE channel emits `{"kind": "codex-session", …}`
  events — TOMBSTONED to follow-up
- [-] ISC-9: events reach EventSource within ≤2s — TOMBSTONED with ISC-8
- [-] ISC-10: `/status` slash returns dashboard deep-link —
  TOMBSTONED (5 LOC dispatcher change; rides another PR)
- [x] ISC-11: Anti: `/api/dashboard/hives` JSON shape unchanged —
  router mount is additive (verified by code review: separate file,
  prefix-conflict-free)
- [-] ISC-12: dashboard service start time + ≤100ms — TOMBSTONED
  (microbenchmark out of scope for backend-only ISA)
- [x] ISC-13: Anti: `_SESSION_TOKEN` is not rotated by deployment —
  router mount happens at module import in `web_server.py`; no token
  regeneration code touched
- [x] ISC-14: `python3 scripts/isa_lint.py isas/P4-operator-surface.md`
  exit 0 in `phase: complete`

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from hermes_cli.dashboard_codex_sessions import router; print(router.prefix)"` | `/api/dashboard` |
| ISC-2 | `pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestSnapshot` (3 cases: empty / two-sessions / review-state-merged) | 3 pass |
| ISC-3 | `pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestDetail::test_returns_isa_verbatim` (also `test_404_for_unknown_sid`) | 2 pass |
| ISC-4 | `pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestLog::test_filters_to_thread_id` | pass |
| ISC-5 | `pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestPauseResume` (2 cases) | 2 pass |
| ISC-6 | `pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestKill` (2 cases: wrong-token 422 / correct-token drops row) | 2 pass |
| ISC-7 | `pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestForceMerge` (2 cases) | 2 pass |
| ISC-11 | code review: router uses prefix `/api/dashboard` + path `/codex-sessions/*`; no overlap with `/api/dashboard/hives` | manual |
| ISC-13 | code review: `dashboard_codex_sessions.py` doesn't import or touch `_SESSION_TOKEN` from `web_server.py` | manual |
| ISC-14 | `python3 scripts/isa_lint.py isas/P4-operator-surface.md ; echo $?` | `0` |

## Git Plan

- **Branch**: `feat/codex-parallel-p4-operator-surface` off `fork/main`.
- **Single commit** for the router + tests + ISA + mount.
- **PR**: `feat(p4): codex sessions dashboard API + confirm-token
  destructive routes`.

## Decisions

**D-1 (2026-05-25): Backend routes only; FE separate ISA.**
P4's design assumed the SPA already has a tab pattern (`/hives`) to
mirror. Adding the actual React `/codex-sessions` route lives in a
separate FE-only ISA — backend is the prerequisite the FE consumes.

**D-2 (2026-05-25): Force-merge is intent, not execution.**
`POST /force-merge` flips the row to `MERGING` and records
`force_merge_requested_at` rather than calling the merge broker
synchronously. Reason: the dashboard handler is a thin HTTP path —
slow GitHub responses or rebase conflicts shouldn't block the
dashboard. Dispatcher's next tick (or the operator's `/review` slash)
will pick up the MERGING state and run the broker.

**D-3 (2026-05-25): SSE wiring + deep-link slash deferred.**
ISC-8/9 (pulse SSE) and ISC-10 (slash deep-link) both ride additional
dispatcher / pulse pipeline changes. Tombstoned for follow-up rather
than baked into this PR's diff — they're each their own small,
reviewable surface.

## Changelog

2026-05-25 — force-merge endpoint shape: synchronous vs intent-based
  conjectured:   the operator clicking "force-merge" wants the merge
                 to happen RIGHT NOW; the endpoint should call
                 MergeBroker.merge() synchronously
  refuted by:    MergeBroker.merge() runs git fetch/rebase/push/PR
                 create — easily 30-60 s on a slow network, holding
                 the dashboard request open and the flock blocking
                 other operators
  learned:       dashboard HTTP path stays thin; expensive operations
                 ride the dispatcher's existing tick / queue model.
                 The operator's INTENT is what the endpoint captures;
                 execution follows naturally
  criterion now: ISC-7 amended; D-2 added; endpoint sets row state
                 to MERGING + records force_merge_requested_at; the
                 dispatcher (or the P3 broker on next APPROVE
                 verdict for this thread) picks it up

## Verification

### ISC-1 — router exists

```
$ python -c "from hermes_cli.dashboard_codex_sessions import router; print(router.prefix, sorted(r.path for r in router.routes))"
/api/dashboard ['/api/dashboard/codex-sessions', '/api/dashboard/codex-sessions/{sid}', '/api/dashboard/codex-sessions/{sid}/force-merge', '/api/dashboard/codex-sessions/{sid}/kill', '/api/dashboard/codex-sessions/{sid}/log', '/api/dashboard/codex-sessions/{sid}/pause', '/api/dashboard/codex-sessions/{sid}/resume']
```

### ISC-2 — snapshot endpoint (3 cases)

```
$ pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestSnapshot -q
...                                                                      [100%]
3 passed
```

### ISC-3 — detail endpoint with ISA verbatim

```
$ pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestDetail -q
..                                                                       [100%]
2 passed
```

### ISC-4 — log tail filtered by thread_id

```
$ pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestLog::test_filters_to_thread_id -q
.                                                                        [100%]
1 passed
```

### ISC-5 — pause / resume flip the flag

```
$ pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestPauseResume -q
..                                                                       [100%]
2 passed
```

### ISC-6 — kill requires KILL_CODEX_SESSION token

```
$ pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestKill -q
..                                                                       [100%]
2 passed
```

### ISC-7 — force-merge requires FORCE_MERGE_CODEX_SESSION token

```
$ pytest tests/hermes_cli/test_dashboard_codex_sessions.py::TestForceMerge -q
..                                                                       [100%]
2 passed
```

### ISC-11 — anti: hives endpoint shape unchanged

Code review: `dashboard_codex_sessions.router` uses prefix
`/api/dashboard` + path `/codex-sessions/*`. No path overlap with
`/api/dashboard/hives` (different leaf). Router mount is additive
via `app.include_router(_codex_router)` immediately after the
existing `_mission_router` mount.

### ISC-13 — anti: session token not rotated

Code review: `dashboard_codex_sessions.py` does NOT import
`_SESSION_TOKEN` or call `secrets.token_urlsafe` or any token
regeneration path. The router relies on the existing auth middleware
in `web_server.py` to validate `X-Hermes-Session-Token` on each
request.

### ISC-14 — isa_lint at phase: complete

```
$ python3 scripts/isa_lint.py isas/P4-operator-surface.md
PASS: isas/P4-operator-surface.md
```

## Handback

**Project:** `codex-parallel-workflow-p4`. **Lesson:** dashboard HTTP
paths should capture operator INTENT and queue expensive work for
the background loop, not block on slow git/network operations.

**Tombstoned follow-ups:**
1. Pulse SSE `kind: codex-session` events (ISC-8, ISC-9)
2. `/status` slash deep-link to dashboard tab (ISC-10)
3. Frontend SPA `/codex-sessions` tab (separate FE-only ISA)
