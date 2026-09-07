# Cost tab depth report

## Status

BLOCKED on the strict live-service success gate, but the lane implementation is built, committed, and verified on an isolated dashboard process from this worktree.

Reason: `hermes-dashboard.service` is wired to `/home/josep/.hermes/scripts/cockpit-dashboard.sh`, whose `WorkingDirectory` and `cd` target are `/home/josep/.local/share/hermes-agent`, not this lane worktree. Restarting the approved unit kept the service active but naturally reloaded the live checkout, which does not contain this lane branch. I did not copy lane changes into the live checkout, alter the systemd unit, change PYTHONPATH, merge, push, or restart gateway.

## Commit

- Lane branch: `burn/burn-01-cost-tab-30-day-spend-trend-cach`
- Code/evidence commit SHA: `4a8af93b36860a35128146c0637700f9079151a0`

## PROOF 1 — backend JSON

### Isolated lane dashboard proof (`http://127.0.0.1:9129/api/dashboard/cost`)

Source artifact: `/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/proof1-isolated-snippet.json`

```json
{
  "dailySeries": [
    { "date": "2026-06-20", "costUsd": 0, "totalTokens": 23011936, "turns": 29 },
    { "date": "2026-06-21", "costUsd": 0, "totalTokens": 33899962, "turns": 31 },
    { "date": "2026-06-22", "costUsd": 0.130782, "totalTokens": 56411437, "turns": 53 },
    { "date": "2026-06-23", "costUsd": 0, "totalTokens": 200988213, "turns": 78 },
    { "date": "2026-06-24", "costUsd": 0, "totalTokens": 22563387, "turns": 32 }
  ],
  "cacheLatency7d": {
    "cacheHitRatio": 0.9055,
    "avgLatencyMs": 360231.9,
    "p95LatencyMs": 1364980.1
  },
  "today": { "totalCostUsd": 0, "totalTokens": 223551600, "totalTurns": 110, "groups": "present" },
  "last7d": { "totalCostUsd": 0.130782, "totalTokens": 336874935, "totalTurns": 223, "groups": "present" },
  "meteredLeak": "present",
  "meteredLeakCount": 5,
  "meteredLeakCostUsd": 0.130782
}
```

Result: PASS on isolated lane process. Note the series length is 5 now, not 4, because a new UTC day accrued while the packet was running.

### Live `:9119` after approved `hermes-dashboard.service` restart

Source artifacts:
- `/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/live-after-restart-check.txt`
- `/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/proof1-live-after-restart-snippet.json`

Observed after restart:

```text
token_found True
status 200
live_has_dailySeries False
live_has_cacheLatency7d False
live_legacy {'today': True, 'last7d': True, 'meteredLeak': True, 'meteredLeakCount': True, 'meteredLeakCostUsd': True}
```

Result: BLOCKED for strict live backend proof. The live service is healthy but serving the live checkout rather than this lane worktree.

## PROOF 2 — frontend browser assertion + screenshot

Source artifact: `/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/verify_cost_output.json`

```json
{
  "ok": true,
  "baseUrl": "http://127.0.0.1:9129",
  "dailySeriesLength": 5,
  "rectCount": 5,
  "screenshotPath": "/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/cost-verify.png",
  "apiPath": "/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/cost-api-verify.json",
  "cacheLatency7d": {
    "cacheHitRatio": 0.9055,
    "avgLatencyMs": 360231.9,
    "p95LatencyMs": 1364980.1
  },
  "legacyPresent": true
}
```

Result: PASS on isolated lane browser verification. The Playwright script asserted:
- `svg[data-testid="cost-daily-spark"]` exists.
- Rect count: `5`.
- `cost-cache-chip` exists.
- `cost-latency-chip` exists.
- Screenshot saved at `/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/cost-verify.png`.

Visual check also confirmed the spend bars plus cache/latency chips are visible.

## PROOF 3 — service healthy post-restart

Source artifact: `/home/josep/hermes-lane-wt/burn-01-cost-tab-30-day-spend-trend-cach/scratchpad/service-active-after-restart.txt`

```text
active
```

Result: PASS. Restarted `hermes-dashboard.service` only. Did not restart `hermes-gateway.service`.

## PROOF 4 — no regression on existing fields

- Isolated lane API: `today`, `last7d`, `meteredLeak`, `meteredLeakCount`, and `meteredLeakCostUsd` all present in `/scratchpad/proof1-isolated-snippet.json`.
- Live `:9119` API after restart: same legacy fields all present in `/scratchpad/live-after-restart-check.txt`.

Result: PASS for legacy-field presence. Existing `_rollup`, `_metered_leaks`, and existing snapshot keys were not modified/renamed.

## Build verification

Command run from `web/` after resolving the workspace install/type mismatch:

```text
npm run build
```

Result: PASS.

Key output:

```text
vite v7.3.5 building client environment for production...
✓ 3277 modules transformed.
../hermes_cli/web_dist/index.html
../hermes_cli/web_dist/assets/index-qWuH7si1.css
../hermes_cli/web_dist/assets/index-rkiD2TDX.js
✓ built in 6.69s
```

`hermes_cli/web_dist` was regenerated and force-added.

## One-line-per-file change summary

- `hermes_cli/dashboard_cost.py` — added `_daily_series()` and `_cache_latency_rollup()` read-only helpers; added `dailySeries` and `cacheLatency7d` to normal and zero snapshots.
- `web/src/lib/api.ts` — added `CostDailyPoint` and `CostCacheLatency`; extended `CostSnapshot` with optional `dailySeries` and `cacheLatency7d`.
- `web/src/pages/CostPage.tsx` — added 30-day spend SVG bars plus cache-hit and latency chips with requested `data-testid`s.
- `web/vite.config.ts` — switched to `vitest/config` defineConfig and cast plugin results to survive the current mixed Vite 7/8 workspace type graph during build.
- `hermes_cli/web_dist/*` — regenerated production dashboard bundle from the lane worktree.
- `scratchpad/verify_cost.mjs` — Playwright/API verifier for isolated/lane dashboard proof.
- `scratchpad/check_live_current.py` — token-safe live `:9119` API probe for post-restart proof.
- `scratchpad/*.json`, `scratchpad/*.txt`, `scratchpad/cost-verify.png` — captured verification artifacts.

## Preserved gates / non-actions

- No push, merge, or PR.
- No gateway restart.
- No full-corpus, re-embed, or vector rebuild.
- No writable SQLite access; backend helpers use the existing read-only connection path.
- No live checkout mutation or service unit rewrite to force `:9119` onto this lane branch.

## Next gate

To make strict PROOF 1 live pass, the owner/integration lane needs to deploy or merge this lane commit into `/home/josep/.local/share/hermes-agent` (the checkout actually used by `hermes-dashboard.service`) or explicitly approve a temporary dashboard-service worktree target/PYTHONPATH override. Until then, live `:9119` will stay healthy but old-schema.
