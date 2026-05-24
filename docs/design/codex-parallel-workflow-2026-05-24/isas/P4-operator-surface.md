---
isa:      20260524-2030_codex-parallel-p4-operator-surface
task:     "P4 Operator surface — /codex-sessions dashboard tab + pulse SSE wiring + slash command polish"
tier:     E3
phase:    scaffold
progress: 0/15
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p4-operator-surface
hive:     "-"
owner:    ruflo-hive
started:  2026-05-24T20:30:00Z
updated:  2026-05-24T20:30:00Z
---

## Problem

P1-P3 deliver the substrate: Codex sessions running in worktrees, peer-reviewed by Opus, merged through the broker. But the operator has no live view of the fleet. To know the state of all running sessions today (after P1-P3 land) requires manually running `tmux ls`, `ls ~/.hermes/codex-wt/`, `cat ~/.hermes/codex_sessions.json`, and grepping `~/.hermes/codex-review-state.json`. With 4-8 sessions in flight this is friction; with parallel reviews and merges, it's not enough situational awareness to debug a stuck session in real time.

The dashboard at `:9119` already has the `/hives` tab pattern (`dashboard_health.py:1997-2013`) and the pulse SSE channel (`web_server.py:4308-4369`). Mirroring that pattern gives operator situational awareness without inventing new infrastructure.

## Goal

After this ISA: a new `/codex-sessions` tab in the dashboard shows one card per active Codex session. Each card shows: Discord thread name, ISA progress (N/M), current phase, worktree branch, peer-review iteration count, last-message timestamp, tmux liveness, and state (EXECUTING/VERIFYING/REVIEWING/MERGING/NEEDS_REVIVE). A detail pane shows the ISA verbatim, the current `git diff origin/main`, the review history, and the tail of `tmux capture-pane`. Operator controls (pause/resume/kill/force-merge) live on each card; destructive actions require a confirmation token per WORKFLOW-LESSONS §4.6. Live updates flow through the existing pulse SSE channel with a new `kind: codex-session` discriminator.

## Out of Scope

- Frontend SPA changes — the existing dashboard SPA gains a tab by mirroring `/hives`; the actual JSX/TSX is out of scope for this ISA (separate front-end ISA when needed).
- Token spend tracking — best-effort estimate is a placeholder field in the JSON, populated when available; precise tracking is future work.
- Cost attribution — Max OAuth has no per-review billing, so this is N/A for Opus; Codex (OpenAI) billing tracking is a different concern entirely.
- Custom alerting (e.g. email/SMS on NEEDS_REVIVE) — operator uses `discord-notify.sh` for now.
- Slash commands beyond the P1 set — P1 already shipped `/spawn`, `/status`, `/pause`, `/resume`, `/kill`, `/handoff-to-ruflo`; P4 only polishes their responses to include dashboard URLs.

## Constraints

- **P3 must be landed** — merge broker emits events the dashboard renders.
- **Must NOT trigger dashboard service restart** during deployment per WORKFLOW-LESSONS §3 rule 2 (token rotation breaks active sessions). New routes go in a fresh APIRouter that's mounted at server start; if the server is already running, operator must `systemctl --user restart hermes-dashboard.service` deliberately (and re-grab tokens as noted in rule 2).
- **Auth model unchanged** — `_SESSION_TOKEN` (ephemeral per `web_server.py:92`) governs new routes; SSE inherits the existing query-token allowlist (`_QUERY_TOKEN_PATHS` at `web_server.py:132-134`).
- **No new persistent storage** — the tab reads `~/.hermes/codex_sessions.json` + `~/.hermes/codex-review-state.json` + worktree state. Cache TTL = 15s (mirror `_HIVES_TTL` at `dashboard_health.py:55-56`).
- **Destructive endpoints require confirmation tokens** per WORKFLOW-LESSONS §4.6: `kill` → `KILL_CODEX_SESSION`, `force-merge` → `FORCE_MERGE_CODEX_SESSION`. The 422 response includes the expected token in the error message + OpenAPI examples.
- **No regression in /hives** — adding routes must not change `/api/dashboard/hives` JSON shape.

## Criteria

- [ ] ISC-1: a new file `hermes_cli/dashboard_codex_sessions.py` exists with the APIRouter and handlers per `module-specs/codex-sessions-tab.md` §3
- [ ] ISC-2: `GET /api/dashboard/codex-sessions` returns the JSON snapshot per `module-specs/codex-sessions-tab.md` §4, with 15s in-memory cache
- [ ] ISC-3: `GET /api/dashboard/codex-sessions/{sid}` returns the detail payload (ISA verbatim, diff ≤200 KB with `truncated` flag if larger, review history, tmux capture-pane tail of last 100 lines)
- [ ] ISC-4: `GET /api/dashboard/codex-sessions/{sid}/log` returns log tail mirroring `/api/dashboard/hives/{hive_id}/log` shape from `dashboard_health.py:2016-2029`
- [ ] ISC-5: `POST /api/dashboard/codex-sessions/{sid}/pause` and `/resume` (non-destructive) succeed with `{"confirm": "—"}` or no body
- [ ] ISC-6: `POST /api/dashboard/codex-sessions/{sid}/kill` requires `{"confirm": "KILL_CODEX_SESSION"}`; wrong token returns 422 with the expected value in the error message and the OpenAPI example
- [ ] ISC-7: `POST /api/dashboard/codex-sessions/{sid}/force-merge` requires `{"confirm": "FORCE_MERGE_CODEX_SESSION"}`; wrong token returns 422 with the expected value
- [ ] ISC-8: pulse SSE channel emits `{"kind": "codex-session", "sub_kind": "phase_change|review_verdict|merge_complete|needs_revive", "sid": "...", ...}` events drained per tick from `pulse_data.pulse_activity_iter()`
- [ ] ISC-9: events reach a browser EventSource connected via `?token=$TOK` within ≤2s of the underlying state change (e.g. dispatcher writes new phase → SSE event arrives in ≤2s)
- [ ] ISC-10: slash command `/status` (already shipped in P1) responds with the dashboard URL `https://<host>:9119/?tab=codex-sessions&sid=<sid>` (deep link)
- [ ] ISC-11: Anti: `GET /api/dashboard/hives` returns the same JSON shape pre- and post-P4 — no regression in keys, types, or counts
- [ ] ISC-12: Anti: dashboard service start time does not increase by > 100ms after P4 (mounting the new router is cheap)
- [ ] ISC-13: Anti: `_SESSION_TOKEN` is NOT changed by deployment — verified by capturing the token, deploying P4 without restart, and re-using the token successfully on the new routes
- [ ] ISC-14: `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2030_codex-parallel-p4-operator-surface/ISA.md` exit 0 in `phase: complete`
- [ ] ISC-15: the `asyncio.Queue` for `kind: codex-session` pulse events is capped at 100 events; on overflow the oldest event is dropped (not the newest); a `dropped_codex_events` counter is present in the SSE health probe — verified by mocking a slow consumer, enqueuing 110 events, and asserting (a) queue depth never exceeds 100, (b) the 10 oldest events are dropped, (c) `dropped_codex_events == 10` in the health probe

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from hermes_cli.dashboard_codex_sessions import router; print(router.prefix)"` | prints `/api/dashboard` |
| ISC-2 | `curl -s -H "X-Hermes-Session-Token: $TOK" :9119/api/dashboard/codex-sessions \| jq '.sessions, .scanned_at, .review_pool'` | three non-null fields present |
| ISC-3 | spawn a session; `curl … /codex-sessions/<sid> \| jq '.isa_verbatim, .current_diff, .review_history, .tmux_capture_tail \| length'` | all four present |
| ISC-4 | `curl … /codex-sessions/<sid>/log \| jq '.lines \| length'` | ≤ 200 |
| ISC-5 | `curl -X POST -H "X-Hermes-Session-Token: $TOK" … /pause`; check dispatcher state | session in PAUSED state |
| ISC-6 | `curl -X POST -H "X-Hermes-Session-Token: $TOK" -d '{"confirm":"WRONG"}' … /kill -w '%{http_code}'` and `… -d '{"confirm":"KILL_CODEX_SESSION"}' …` | first → 422 + expected token in body; second → 200 |
| ISC-7 | similar to ISC-6 with FORCE_MERGE_CODEX_SESSION | first 422, second 200 |
| ISC-8 | dispatch a phase change; `curl -N "…/api/pulse/stream?token=$TOK" \| head -50` | event line with `kind:codex-session` and `sub_kind:phase_change` |
| ISC-9 | open EventSource, dispatch state change, measure ms to first matching event | ≤ 2000 ms |
| ISC-10 | discord slash `/status` in a thread; check response | response contains `9119/?tab=codex-sessions&sid=` |
| ISC-11 | `curl … /api/dashboard/hives \| jq 'keys'` before vs after P4 | identical keys |
| ISC-12 | `time curl :9119/api/status` before vs after deploy | within 100ms |
| ISC-13 | save token; deploy P4 with `systemctl reload hermes-dashboard` (no restart); reuse token | 200 on /api/dashboard/codex-sessions |
| ISC-14 | `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2030_codex-parallel-p4-operator-surface/ISA.md ; echo $?` | `0` |
| ISC-15 | mock slow consumer; enqueue 110 codex-session events; check queue depth cap, oldest-drop behavior, `dropped_codex_events` counter | queue capped at 100; 10 oldest dropped; counter == 10 |

## Git Plan

- **Branch**: `feat/codex-parallel-p4-operator-surface` off `fork/main` (after P3 lands).
- **Commit cadence (early + incremental)**:
  1. `chore(isa): scaffold P4 ISA + work dir`
  2. `feat(dashboard): dashboard_codex_sessions APIRouter + snapshot route (ISC-1, ISC-2)`
  3. `feat(dashboard): detail + log routes (ISC-3, ISC-4)`
  4. `feat(dashboard): pause/resume + confirm-token destructive routes (ISC-5, ISC-6, ISC-7)`
  5. `feat(pulse): codex-session pulse_activity_iter source + kind discriminator (ISC-8, ISC-9)`
  6. `feat(dispatcher): /status slash returns dashboard deep-link (ISC-10)`
  7. `test(p4): dashboard integration + SSE smoke + token-not-rotated`
  8. `docs(p4): operator notes — dashboard tab URL + confirm-token reference`
- **Push**: `git push fork feat/codex-parallel-p4-operator-surface` after each commit.
- **PR**: against `fork/main` titled `feat(p4): Codex parallel workflow — /codex-sessions dashboard tab`.
- **Do NOT merge** until `phase: complete` per ISC-14.
- **Auto-merge label**: NO — touches `hermes_cli/web_server.py` (sensitive per `module-specs/merge-broker.md` §5).

## Decisions

_(filled during execute)_

## Changelog

_(filled on each correction — 4-tuple format per ISA-SPEC §8)_

## Verification

_(filled during verify — probe output pasted verbatim, one block per [x] ISC)_

## Handback

- On complete: `mvms_record_completion` under project `codex-parallel-workflow` linking branch + PR + ISA path + dashboard URL.
- For each Changelog entry: `mvms_record_lesson` under project `codex-parallel-workflow`.
- Discord notification via `~/.hermes/scripts/discord-notify.sh`.
- Kanban: `kanban_complete <card>`.
