# Hermes Dashboard Visual Stress-Test Defects — 2026-06-10

## Verdict

**FAIL / needs fixes before trust.** Most tabs render and basic safe controls respond, but `/get-some` — the never-visually-verified Command Center tab — is **critical-fail stuck on Loading Command Center**. `/nexus-health` and `/profiles` also fail the render-settle gate. `/git-health` is **PASS** after exercising Refresh/Tree/Map: it renders populated readiness and lane-map views.

## Method

- Browser automation via Playwright/Chromium plus Hermes browser snapshots/vision screenshots against `http://127.0.0.1:9119`.
- Enumerated every built-in sidebar route plus plugin routes visible in the sidebar: root/welcome/chat/pulse/sessions/analytics/models/logs/cron/skills/explorer/system-health/hives/codex-sessions/get-some/git-health/plugins/mcp/channels/webhooks/pairing/profiles/config/env/system/docs/kanban/trt/achievements.
- Injected `X-Hermes-Session-Token` for API-pane fetches; SSE endpoints observed with `?token=` and token values redacted in text artifacts.
- Clicked safe route-local controls (refresh, tab/view toggles, expansions, filter/search UI where no submit was needed). Skipped destructive/mutating actions: restart/update, save/install/delete/disable/archive/dispatch/run/enable selectors, task selection, and API POST operations in Swagger docs.
- No git/config/provider/source/service changes were made. No destructive/confirm actions were clicked.

## Evidence map

- Raw automation results: `/home/josep/.hermes/audits/20260610-dashboard-stress/results-v3.json`
- Coverage summary: `/home/josep/.hermes/audits/20260610-dashboard-stress/summary-v3.json`
- Route screenshots: `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/`
- Additional browser evidence screenshots:
  - `/get-some` loading: `/home/josep/.hermes/cache/screenshots/browser_screenshot_0da0c8238e964cde9138771cfc7d0fda.png`
  - `/git-health` Map render: `/home/josep/.hermes/cache/screenshots/browser_screenshot_169b442336274bfcb2c4756b808a9e52.png`
  - `/profiles` spinner: `/home/josep/.hermes/cache/screenshots/browser_screenshot_cf778ff85f7d4a21973e77d8a5a6fbf9.png`
  - `/nexus-health` blank after navigation timeout: `/home/josep/.hermes/cache/screenshots/browser_screenshot_c6c9236d95ad473187b52a286eb0b8d9.png`

## Defects

### DASH-STRESS-001 — CRITICAL — /get-some — Command Center data load
- **Expected:** Command Center should resolve beyond the skeleton and render architecture/project/decision/live/stalled operator cards.
- **Actual:** Visual page remains stuck at “Loading Command Center…” after direct browser navigation. Direct authenticated probe of /api/dashboard/command-center timed out after 25s; no fallback/error state is rendered. This is the never-visually-verified merged 06-05 tab and it fails the primary visual gate.
- **Screenshot ref:** `/home/josep/.hermes/cache/screenshots/browser_screenshot_0da0c8238e964cde9138771cfc7d0fda.png`
- **Evidence:** results-v3.json route /get-some; terminal authenticated probe: /api/dashboard/command-center TimeoutError after 25.02s

### DASH-STRESS-002 — HIGH — /nexus-health — System Health route/data load
- **Expected:** System Health should render the infrastructure command center/topology or at least a clear error state.
- **Actual:** Automated route run saw persistent “Loading System Health…”. Later direct browser navigation to /nexus-health timed out and the screenshot showed a blank teal page. Direct authenticated /api/dashboard/nexus-health probe returned 503 after ~12s.
- **Screenshot ref:** `/home/josep/.hermes/cache/screenshots/browser_screenshot_c6c9236d95ad473187b52a286eb0b8d9.png`
- **Evidence:** summary-v3.json /nexus-health status loading-stuck?; terminal probe: HTTP 503 Service Unavailable

### DASH-STRESS-003 — HIGH — /profiles — Profiles list load
- **Expected:** Profiles page should render profile cards/list or an actionable error.
- **Actual:** Route stays on “Loading...” spinner and never renders profile cards in visual check. Direct authenticated /api/profiles probe timed out after 10s.
- **Screenshot ref:** `/home/josep/.hermes/cache/screenshots/browser_screenshot_cf778ff85f7d4a21973e77d8a5a6fbf9.png`
- **Evidence:** summary-v3.json /profiles status loading-stuck?; terminal probe: /api/profiles TimeoutError timed out

### DASH-STRESS-004 — MEDIUM — /pulse — Copy/share buttons / clipboard interactions
- **Expected:** Clicking visible copy/share controls should either copy successfully or degrade without page errors.
- **Actual:** Stress-clicking Pulse controls generated repeated browser page errors: “Failed to execute writeText on Clipboard: Write permission denied.” This is expected in hardened/headless browser contexts but should be handled as a visible toast or no-op, not an uncaught page error.
- **Screenshot ref:** `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/pulse.png`
- **Evidence:** results-v3.json /pulse pageErrors x3

### DASH-STRESS-005 — LOW — global/all SPA routes — /api/auth/me polling
- **Expected:** Unauthenticated optional auth probe should not spam console errors when dashboard root injects a local session token and AUTH_REQUIRED=false.
- **Actual:** Most routes emit console errors for 401 /api/auth/me. The app still renders, but the console is noisy and masks real defects during QA.
- **Screenshot ref:** `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/root.png`
- **Evidence:** summary-v3.json: recurring httpErrors/console on /api/auth/me across routes

## Coverage table

| Route | Verdict | Controls found | Safe clicked | Skipped destructive/mutating | Other skipped | Screenshot | Notes |
|---|---:|---:|---:|---:|---:|---|---|
| `/` | PASS | 46 | 17 | 2 | 28 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/root.png` | skipped: Restart Gateway, Update Hermes |
| `/welcome` | PASS | 46 | 12 | 2 | 32 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/welcome.png` | skipped: Restart Gateway, Update Hermes |
| `/chat` | PASS | 37 | 7 | 2 | 28 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/chat.png` | skipped: Restart Gateway, Update Hermes |
| `/pulse` | WARN | 70 | 12 | 11 | 47 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/pulse.png` | clipboard writeText page errors; skipped: Restart Gateway, Update Hermes, t_a697b5 Repoint dashboard -> fork/main (restore `hermes resume` + ship river viz) 8d, t_64c83b Phase-0 hygiene spine + run-registry (stop sprawl regrowth) 8d, t_ae2704 Resume: OUZY KB Memory Architecture Phase 0 8d (+6 more) |
| `/sessions` | PASS | 31 | 7 | 2 | 23 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/sessions.png` | skipped: Restart Gateway, Update Hermes |
| `/analytics` | PASS | 30 | 4 | 2 | 24 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/analytics.png` | skipped: Restart Gateway, Update Hermes |
| `/models` | PASS | 32 | 7 | 2 | 23 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/models.png` | skipped: Restart Gateway, Update Hermes |
| `/logs` | PASS | 53 | 12 | 2 | 39 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/logs.png` | skipped: Restart Gateway, Update Hermes |
| `/cron` | PASS | 30 | 6 | 2 | 23 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/cron.png` | skipped: Restart Gateway, Update Hermes |
| `/skills` | PASS | 110 | 12 | 2 | 96 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/skills.png` | skipped: Restart Gateway, Update Hermes |
| `/explorer` | PASS | 34 | 5 | 2 | 27 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/explorer.png` | skipped: Restart Gateway, Update Hermes |
| `/nexus-health` | FAIL | 34 | 5 | 2 | 27 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/nexus-health.png` | stuck/blank; API 503; skipped: Restart Gateway, Update Hermes |
| `/hives` | PASS | 121 | 12 | 21 | 88 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/hives.png` | skipped: Restart Gateway, Update Hermes, ict-brain-plan-review-20260527T033001Z 358h 9m You are the **Opus queen** of a Ruflo hive (Claude Max OAuth, `claude-opu, hello-world-in-python-h5-stress-test-20260518T210832Z.deleted.20260518T214745Z tmux: rfg-hello-world-in-pyt-11cb 556h 40, smoke-closeout-20260518T173046Z tmux: rfg-smoke-closeout-50ef 560h 17m t_6dcf262a Smoke-test Ruflo GOAP closeout only. S (+16 more) |
| `/codex-sessions` | PASS | 65 | 6 | 2 | 57 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/codex-sessions.png` | skipped: Restart Gateway, Update Hermes |
| `/get-some` | FAIL | 53 | 12 | 2 | 39 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/get-some.png` | CRITICAL: stuck Loading Command Center; skipped: Restart Gateway, Update Hermes |
| `/git-health` | PASS | 33 | 8 | 2 | 23 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/git-health.png` | Refresh/Tree/Map exercised; Map renders lane cards; skipped: Restart Gateway, Update Hermes |
| `/plugins` | PASS | 35 | 7 | 3 | 25 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/plugins.png` | skipped: Restart Gateway, Update Hermes, INSTALL |
| `/mcp` | PASS | 48 | 9 | 12 | 27 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/mcp.png` | skipped: Restart Gateway, Update Hermes, Disable, Delete, INSTALL |
| `/channels` | PASS | 30 | 5 | 2 | 23 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/channels.png` | skipped: Restart Gateway, Update Hermes |
| `/webhooks` | PASS | 40 | 8 | 6 | 27 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/webhooks.png` | skipped: Restart Gateway, Update Hermes, DISABLE, Delete |
| `/pairing` | PASS | 34 | 5 | 2 | 27 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/pairing.png` | skipped: Restart Gateway, Update Hermes |
| `/profiles` | FAIL | 30 | 6 | 2 | 23 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/profiles.png` | stuck Loading spinner; skipped: Restart Gateway, Update Hermes |
| `/config` | PASS | 88 | 12 | 4 | 72 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/config.png` | skipped: Restart Gateway, Update Hermes, Reset General to defaults, SAVE |
| `/env` | PASS | 128 | 12 | 2 | 114 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/env.png` | skipped: Restart Gateway, Update Hermes |
| `/system` | PASS | 72 | 12 | 22 | 38 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/system.png` | skipped: Restart Gateway, Update Hermes, Update now, Pause, Run now (+9 more) |
| `/docs` | WARN | 887 | 13 | 169 | 705 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/docs.png` | blank; skipped: POST /api/curator/run Run Curator, /api/curator/run, post ​/api​/curator​/run, POST /api/ops/prompt-size Run Prompt Size, POST /api/ops/dump Run Dump (+164 more) |
| `/kanban` | PASS | 270 | 12 | 132 | 126 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/kanban.png` | skipped: Restart Gateway, Update Hermes, + New board, Archive, Clear filters (+127 more) |
| `/trt-cypionate` | PASS | 34 | 5 | 2 | 27 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/trt-cypionate.png` | skipped: Restart Gateway, Update Hermes |
| `/achievements` | PASS | 157 | 12 | 0 | 145 | `/home/josep/.hermes/audits/20260610-dashboard-stress/screenshots-v3/achievements.png` |  |

## Explicit skipped destructive / mutating controls

### `/`
- Restart Gateway
- Update Hermes

### `/welcome`
- Restart Gateway
- Update Hermes

### `/chat`
- Restart Gateway
- Update Hermes

### `/pulse`
- Restart Gateway
- Update Hermes
- t_a697b5 Repoint dashboard -> fork/main (restore `hermes resume` + ship river viz) 8d
- t_64c83b Phase-0 hygiene spine + run-registry (stop sprawl regrowth) 8d
- t_ae2704 Resume: OUZY KB Memory Architecture Phase 0 8d
- t_2b321a Resume: Workflow Optimization Phase 3 + merge backlog 8d
- t_4570da Resume: A2 daily-audit-digest implementer (+ dashboard restart follow-up) 8d
- t_daee0b [P1] Add confirm-gate to Restart Gateway, Update Hermes, Cron Trigger-Now, Plugin Install buttons 5d
- t_e1118a [P1] Resume ICT bulk ingest via SUBAGENT after MREC-001 backup taken (190 untouched files) 5d
- t_fa607d [P1] Build run-registry/ spine before any future dispatch_in_gateway=true flip 5d
- t_c579fb [P0] Extend mvms-backup.sh to pg_dump ict.* schema + take manual pre-restart snapshot 5d

### `/sessions`
- Restart Gateway
- Update Hermes

### `/analytics`
- Restart Gateway
- Update Hermes

### `/models`
- Restart Gateway
- Update Hermes

### `/logs`
- Restart Gateway
- Update Hermes

### `/cron`
- Restart Gateway
- Update Hermes

### `/skills`
- Restart Gateway
- Update Hermes

### `/explorer`
- Restart Gateway
- Update Hermes

### `/nexus-health`
- Restart Gateway
- Update Hermes

### `/hives`
- Restart Gateway
- Update Hermes
- ict-brain-plan-review-20260527T033001Z 358h 9m You are the **Opus queen** of a Ruflo hive (Claude Max OAuth, `claude-opu
- hello-world-in-python-h5-stress-test-20260518T210832Z.deleted.20260518T214745Z tmux: rfg-hello-world-in-pyt-11cb 556h 40
- smoke-closeout-20260518T173046Z tmux: rfg-smoke-closeout-50ef 560h 17m t_6dcf262a Smoke-test Ruflo GOAP closeout only. S
- impl-ict-brain-live-ingest-20260517T044212Z 597h 6m You are a Ruflo hive (Claude Max OAuth, Sonnet). Autonomous; STOP on
- impl-ict-brain-no-age-rewrite-20260517T021906Z 599h 30m You are a Ruflo hive (Claude Max OAuth, Sonnet). Autonomous; STO
- audit-tg-first-dispatch-20260516T161704Z 609h 24m **Hive type:** single sequential hive, 6-8 specialist workers + Queen 
- impl-ict-brain-reasoning-slim-20260516T094752Z 615h 53m You are a Ruflo hive (Claude Max OAuth, Sonnet). Autonomous; sto
- ict-trading-brain-research-20260515T224601Z 0s You are a Ruflo hive of **15 specialist research workers** (Max OAuth ONL
- impl-otel-replay-ccusage-20260516T010752Z 0s You are a Ruflo hive (Max OAuth Sonnet, **NOT Opus** — user is on extra-usa
- impl-mvms-validity-windows-20260515T234451Z 0s You are a Ruflo hive (Max OAuth ONLY). Autonomous; stop only on §5 gates.
- viz-research-arch-and-work-20260515T232839Z 0s You are a Ruflo hive of **15 specialist research workers** (Max OAuth ONL
- impl-kanban-md-patterns-20260515T234451Z 0s You are a Ruflo hive (Max OAuth ONLY). Autonomous; stop only on §5 gates. Wo
- impl-must-9-10-20260516T011015Z 0s You are a Ruflo hive (Sonnet — extra-usage). Autonomous; stop only on §5 gates. Workd
- impl-tg-dispatch-phase1-20260516T161704Z 608h 29m **Parent audit:** `t_4ff8be07` — `/home/josep/.hermes/ruflo-work/audit
- impl-ict-brain-phase1-20260516T031319Z 622h 23m You are a Ruflo hive (Sonnet, Claude Max OAuth). Autonomous; stop only o
- live-dashboard-api-smoke-20260518T083011Z 0s Live dashboard activation smoke: stage a harmless Ruflo GOAP packet, then v
- real-start-stop-smoke-20260518T083611Z 0s Scratch-only operational smoke for the Ruflo GOAP dashboard. Launch Ruflo/Clau
- ruflo-goap-widget-scratch-smoke-test-20260518T074516Z 0s Scratch-only widget smoke test. Stage a harmless Ruflo GOAP lau
- kanban-linked-start-stop-smoke-20260518T084031Z 0s Scratch-only operational smoke for the Ruflo GOAP dashboard after Her

### `/codex-sessions`
- Restart Gateway
- Update Hermes

### `/get-some`
- Restart Gateway
- Update Hermes

### `/git-health`
- Restart Gateway
- Update Hermes

### `/plugins`
- Restart Gateway
- Update Hermes
- INSTALL

### `/mcp`
- Restart Gateway
- Update Hermes
- Disable
- Delete
- INSTALL

### `/channels`
- Restart Gateway
- Update Hermes

### `/webhooks`
- Restart Gateway
- Update Hermes
- DISABLE
- Delete

### `/pairing`
- Restart Gateway
- Update Hermes

### `/profiles`
- Restart Gateway
- Update Hermes

### `/config`
- Restart Gateway
- Update Hermes
- Reset General to defaults
- SAVE

### `/env`
- Restart Gateway
- Update Hermes

### `/system`
- Restart Gateway
- Update Hermes
- Update now
- Pause
- Run now
- RESTART
- STOP
- Reset MEMORY.md
- Reset USER.md
- Remove credential
- Run doctor
- Create backup
- Update skills
- NEW HOOK

### `/docs`
- POST /api/curator/run Run Curator
- /api/curator/run
- post ​/api​/curator​/run
- POST /api/ops/prompt-size Run Prompt Size
- POST /api/ops/dump Run Dump
- POST /api/ops/config-migrate Run Config Migrate
- POST /api/ops/debug-share Run Debug Share Endpoint
- POST /api/gateway/restart Restart Gateway
- /api/gateway/restart
- post ​/api​/gateway​/restart
- POST /api/hermes/update Update Hermes
- /api/hermes/update
- post ​/api​/hermes​/update
- GET /api/hermes/update/check Check Hermes Update
- /api/hermes/update/check
- get ​/api​/hermes​/update​/check
- PUT /api/config Update Config
- DELETE /api/env Remove Env Var
- delete ​/api​/env
- POST /api/messaging/telegram/onboarding/start Start Telegram Onboarding
- /api/messaging/telegram/onboarding/start
- post ​/api​/messaging​/telegram​/onboarding​/start
- DELETE /api/messaging/telegram/onboarding/{pairing_id} Cancel Telegram Onboarding
- delete ​/api​/messaging​/telegram​/onboarding​/{pairing_id}
- POST /api/messaging/telegram/onboarding/{pairing_id}/apply Apply Telegram Onboarding
- /api/messaging/telegram/onboarding/{pairing_id}/apply
- post ​/api​/messaging​/telegram​/onboarding​/{pairing_id}​/apply
- PUT /api/messaging/platforms/{platform_id} Update Messaging Platform
- DELETE /api/providers/oauth/{provider_id} Disconnect Oauth Provider
- delete ​/api​/providers​/oauth​/{provider_id}
- POST /api/providers/oauth/{provider_id}/start Start Oauth Login
- /api/providers/oauth/{provider_id}/start
- post ​/api​/providers​/oauth​/{provider_id}​/start
- POST /api/providers/oauth/{provider_id}/submit Submit Oauth Code
- /api/providers/oauth/{provider_id}/submit
- post ​/api​/providers​/oauth​/{provider_id}​/submit
- DELETE /api/providers/oauth/sessions/{session_id} Cancel Oauth Session
- delete ​/api​/providers​/oauth​/sessions​/{session_id}
- POST /api/sessions/bulk-delete Bulk Delete Sessions Endpoint
- /api/sessions/bulk-delete
- ... 129 more task/API/action controls omitted from this list; full inventory in results-v3.json

### `/kanban`
- Restart Gateway
- Update Hermes
- + New board
- Archive
- Clear filters
- Select task t_ec67cb37
- t_a697b510 P85 Repoint dashboard -> fork/main (restore `hermes resume` + ship river viz) unassigned 💬 1 8d ago
- Select task t_a697b510
- Select task t_f66f14ba
- t_64c83b00 P65 Phase-0 hygiene spine + run-registry (stop sprawl regrowth) unassigned 8d ago
- Select task t_64c83b00
- Select task t_04234b9e
- Select task t_5856056e
- Select task t_4de0d41c
- t_2b321a7c P30 Resume: Workflow Optimization Phase 3 + merge backlog unassigned 8d ago
- Select task t_2b321a7c
- t_ae270426 P30 Resume: OUZY KB Memory Architecture Phase 0 unassigned 8d ago
- Select task t_ae270426
- t_4570da52 P25 Resume: A2 daily-audit-digest implementer (+ dashboard restart follow-up) unassigned 8d ago
- Select task t_4570da52
- t_c579fb7d [P0] Extend mvms-backup.sh to pg_dump ict.* schema + take manual pre-restart snapshot unassigned 5d ago
- Select task t_c579fb7d
- Select task t_d22e2bf0
- Select task t_654ac1a8
- Select task t_ec024149
- Select task t_ba8e769c
- Select task t_cb2e400e
- Select task t_a9a31a1a
- Select task t_d767cc55
- Select task t_2d7cf43c
- Select task t_93b6f500
- Select task t_6f8253cc
- t_fa607d5f [P1] Build run-registry/ spine before any future dispatch_in_gateway=true flip unassigned 💬 1 5d ago
- Select task t_fa607d5f
- Select task t_238cf5cc
- t_e1118a8b [P1] Resume ICT bulk ingest via SUBAGENT after MREC-001 backup taken (190 untouched files) unassigned 5d ago
- Select task t_e1118a8b
- Select task t_274e95d5
- t_daee0ba9 [P1] Add confirm-gate to Restart Gateway, Update Hermes, Cron Trigger-Now, Plugin Install buttons unassigned 
- Select task t_daee0ba9
- ... 92 more task/API/action controls omitted from this list; full inventory in results-v3.json

### `/trt-cypionate`
- Restart Gateway
- Update Hermes

## Per-tab verdicts

- `/` — **PASS** — Rendered and safe-click pass completed.
- `/welcome` — **PASS** — Rendered and safe-click pass completed.
- `/chat` — **PASS** — Rendered and safe-click pass completed.
- `/pulse` — **WARN** — Renders, but copy/share controls throw clipboard permission page errors in browser QA.
- `/sessions` — **PASS** — Rendered and safe-click pass completed.
- `/analytics` — **PASS** — Rendered and safe-click pass completed.
- `/models` — **PASS** — Rendered and safe-click pass completed.
- `/logs` — **PASS** — Rendered and safe-click pass completed.
- `/cron` — **PASS** — Rendered and safe-click pass completed.
- `/skills` — **PASS** — Rendered and safe-click pass completed.
- `/explorer` — **PASS** — Rendered and safe-click pass completed.
- `/nexus-health` — **FAIL** — System Health did not settle; loading/blank with backend 503.
- `/hives` — **PASS** — Rendered and safe-click pass completed.
- `/codex-sessions` — **PASS** — Rendered and safe-click pass completed.
- `/get-some` — **FAIL** — Explicit verdict for `/get-some`: **FAIL**. The Command Center skeleton renders but the operator view never loads; authenticated command-center API timed out.
- `/git-health` — **PASS** — Explicit verdict for `/git-health`: **PASS**. Refresh, Tree, and Map controls render; Map shows branch/lane cards and readiness.
- `/plugins` — **PASS** — Rendered and safe-click pass completed.
- `/mcp` — **PASS** — Rendered and safe-click pass completed.
- `/channels` — **PASS** — Rendered and safe-click pass completed.
- `/webhooks` — **PASS** — Rendered and safe-click pass completed.
- `/pairing` — **PASS** — Rendered and safe-click pass completed.
- `/profiles` — **FAIL** — Profiles did not settle; spinner remained and /api/profiles timed out.
- `/config` — **PASS** — Rendered and safe-click pass completed.
- `/env` — **PASS** — Rendered and safe-click pass completed.
- `/system` — **PASS** — Rendered and safe-click pass completed.
- `/docs` — **PASS** — Swagger/OpenAPI docs render; automation blank heuristic was false-positive because #root is not the SPA root on docs.
- `/kanban` — **PASS** — Rendered and safe-click pass completed.
- `/trt-cypionate` — **PASS** — Rendered and safe-click pass completed.
- `/achievements` — **PASS** — Rendered and safe-click pass completed.

## Non-actions / gates preserved

- Did **not** click Restart Gateway, Update Hermes, Save config, Install plugins, Enable/Disable/Delete MCP/plugin/webhook items, Kanban new/archive/dispatch/task selection, System restart/stop/run/update/remove actions, or Swagger POST/PUT/DELETE execute operations.
- Did **not** mutate git, config, services, providers, credentials, Kanban, cron, or live data intentionally.
- Theme/language menus were opened for UI coverage; no alternate option was selected.

