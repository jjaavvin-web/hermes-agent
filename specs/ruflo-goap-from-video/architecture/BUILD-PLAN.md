# BUILD-PLAN.md — Per-Hive Decomposition

**Hive:** 1 — ARCHITECT
**Audience:** Hive 2, 3, 4, 5, 6 queens (each reads their own section + the sequencing graph)
**Related:** `ARCHITECTURE.md` (the *what*), `DATA-MODEL.md` (the *how-to-persist*), this doc (the *who-builds-what-when*).

This document decomposes the build into concrete sub-tasks per hive, names the worker tier each sub-task belongs on, states acceptance criteria precisely enough to be testable, gives a LOC estimate so workers can plan their parallelism, and calls out the smoke tests each hive must execute before signalling done.

The per-hive `objective.md` files at `~/.hermes/ruflo-work/goap-build-20260518T180000Z/hive{2,3,4,5,6}-*/objective.md` are normative on completion criteria. This document is the *plan to satisfy* those criteria — the concrete sub-task list.

---

## 0. Sequencing graph

```
Hive 1 (ARCHITECT) — this hive
        │ produces ARCHITECTURE.md + DATA-MODEL.md + BUILD-PLAN.md + RISKS.md
        ▼
Hive 2 (BACKEND-CORE) — fakes-but-consistent backend
        │ produces extended plugin_api.py + new modules + tests + stubs
        ▼
Hive 3 (REAL-RUFLO-WIRING) — swap stubs for reals
        │ produces real planner/task_store/log_stream/quality_runner + smoke-e2e.sh
        ▼  (Decision Gate 2 — smoke test passes)
Hive 4 (FRONTEND) — React tab matching SPEC
        │ produces dashboard/src/ + dashboard/dist/* + component tests
        ▼  (Decision Gate — chain.sh pauses; Claude takes over)
Hive 5 (STRESS-TEST, Claude-driven) — browser MCP click-every-button
        │ produces STRESS-TEST-REPORT.md + DEFECTS.md + results.jsonl
        ▼  (Decision Gate 3 — zero critical defects)
Hive 6 (POLISH) — defect fixes + animations + docs + memory
        │ produces final dist + docs + memory updates
        ▼
MERGE bottom-up: H1 → H2 → H3 → H4 → H6 on fork/main
```

### 0.1 Could anything parallelize?

In theory, Hive 4 (frontend) could start as soon as Hive 2 (backend stubs) ships, since the stubs return SPEC-shaped fakes the frontend can develop against. We deliberately do NOT parallelize that:
- Hive 3 may discover an integration constraint that forces a backend route change. If Hive 4 has been building against the wrong contract, that's wasted work.
- The Hermes dashboard is a single mount point per plugin; parallel frontend work would compete for `dist/` ownership.

So the chain stays sequential. The only artificial gate is the Hive 4 pause for Hive 5 (Claude-driven stress test), which `chain.sh` enforces via `PAUSE_AFTER_HIVE_4=1` (`chain.sh:26`).

---

## 1. Hive 2 — BACKEND-CORE

**Worker budget:** Opus queen + 3 sonnet-coder + 1 sonnet-tester (5 workers; matches `hive2-backend/objective.md` line 6).

### 1.1 Decomposed sub-tasks

| # | Sub-task | Worker tier | Est. LOC | Depends on |
|---|---|---|---|---|
| 2A | Bootstrap `models.py` with all pydantic models from `DATA-MODEL.md §2` | sonnet-coder #1 | 250 | — |
| 2B | Add `config_store.py`: load/save global config, override merge, 4 presets | sonnet-coder #1 | 180 | 2A |
| 2C | Add `goap_planner.py`: extend `_build_plan` to populate research_phases / status_cards / execution_plan_summary | sonnet-coder #1 | 200 | 2A |
| 2D | Add `task_store.py` STUB: returns SPEC §7's 6 cards + 5 edges as in-memory fixture | sonnet-coder #2 | 120 | 2A |
| 2E | Add `log_stream.py`: SSE generator for `/runs/{id}/logs/stream`; tails files via `select` loop with 200ms debounce | sonnet-coder #2 | 220 | — |
| 2F | Add `progress_stream.py`: SSE generator multiplexing research_phase / dev_phase / goap_state / heartbeat events | sonnet-coder #2 | 180 | 2A, 2E |
| 2G | Add `quality_runner.py` STUB: returns the 3 SPEC §9 gates with the SPEC values (`Compile:passed`, `TestCoverage:100/80`, `SecurityScan:95/90`) | sonnet-coder #3 | 100 | 2A |
| 2H | Wire 22 new routes into `plugin_api.py` per `ARCHITECTURE.md §6` API contract | sonnet-coder #3 | 600 | 2A–2G |
| 2I | Extend `/projects/stage` and `/plan/generate` for new response shapes | sonnet-coder #3 | 80 | 2H |
| 2J | Add `DELETE /projects/{run_id}` with `ms`-slug guard rail | sonnet-coder #3 | 60 | 2H |
| 2K | Pydantic-model fixtures + test data (DATA-MODEL §9) | sonnet-tester | 200 | 2A |
| 2L | New tests: 1+ per route via `TestClient` (22 routes → ~30 test cases) | sonnet-tester | 600 | 2H |
| 2M | Safety tests: path traversal, unregistered run, API-key in env, `ms` mutation refusal | sonnet-tester | 150 | 2H |
| 2N | Coverage report + `openapi-check.json` snapshot vs ARCHITECTURE §6 | sonnet-tester | 50 | 2L |

**Total LOC estimate (Hive 2):** ~3000 LOC across Python source + tests.

### 1.2 Acceptance criteria

Mirrors `hive2-backend/objective.md` §"Completion criteria" (lines 65-73) with the concrete checks:

1. `pytest -q ~/.hermes/plugins/ruflo-goap-control/tests/` → all 4 existing tests PASS + all new tests PASS.
2. `pytest --cov=plugin_api --cov=models --cov=config_store --cov=goap_planner --cov=task_store --cov=log_stream --cov=progress_stream --cov=quality_runner` → ≥ 75% on the new code.
3. Safety probes (in `tests/test_safety.py`, NEW): `assert TestClient.get("/runs/../etc/passwd/logs").status_code == 400`; `assert TestClient.post("/runs/rg_000000000000/start", json={...}).status_code == 404`; `assert "ANTHROPIC_API_KEY" not in launched_env`.
4. `sha256sum projects.json` BEFORE and AFTER Hive 2 work — bytes must be identical at the moment of PR submission (the live `ms` record AND every other record). Hive 2 may stage new throwaway records during testing but MUST remove them before sha256 check.
5. `systemctl --user restart hermes-dashboard.service` + `curl -s :9119/openapi.json | jq '.paths | keys | length'` → ≥ 34.
6. `FINAL-REPORT.md` exists at `~/.hermes/ruflo-work/goap-build-20260518T180000Z/hive2-backend/FINAL-REPORT.md` (workdir ROOT — per lesson `6f42c8b1`).
7. PR opened: `gh pr create --base feat/goap-build-h1-architect-20260518T180000Z --head feat/goap-build-h2-backend-20260518T180000Z` returns a URL.
8. Telegram + kanban + MVMS as standard chain protocol.

### 1.3 Smoke tests Hive 2 runs before signalling done

```bash
# Inside the hive's tmux:
pytest -q ~/.hermes/plugins/ruflo-goap-control/tests/
systemctl --user restart hermes-dashboard.service
sleep 5
ROUTES=$(curl -s :9119/openapi.json | jq '.paths | keys | length')
[[ "$ROUTES" -ge 34 ]] || exit 1
curl -s :9119/api/plugins/ruflo-goap-control/projects | jq '.projects | length'  # expect 13 (unchanged)
curl -s :9119/api/plugins/ruflo-goap-control/config/presets | jq '.presets | keys'  # expect [budget,development,production,quality]
sha256sum ~/.hermes/ruflo-goap-control/projects.json > $WORKDIR/sha-after.txt
diff $WORKDIR/sha-before.txt $WORKDIR/sha-after.txt || exit 1  # bytes unchanged
```

### 1.4 Risk hotspots Hive 2 must watch

- **`_load_registry` permissiveness** — if Hive 2's new pydantic `Project` model is too strict, the 13 existing records won't load. Mitigation: use `model_validate` with default `extra="ignore"`; all new fields `Optional` with defaults.
- **SSE generator memory leaks** — long-lived generators that forget to close file handles will exhaust fds. Mitigation: `try/finally` on every generator, `pytest-asyncio` test that opens 100 SSE connections and asserts fd count stable.
- **Test fixture isolation** — `tests/test_plugin_api_closeout.py` uses `monkeypatch.setenv("RUFLO_GOAP_CONTROL_ROOT", str(tmp_path / "control"))`. All new tests MUST do the same to avoid clobbering the live registry. Mitigation: queen-enforced lint in PR review.

---

## 2. Hive 3 — REAL-RUFLO-WIRING

**Worker budget:** Opus queen + 1 sonnet-coder + 1 sonnet-debugger (3 workers; matches `hive3-ruflo-wiring/objective.md` line 6).

### 2.1 Decomposed sub-tasks

| # | Sub-task | Worker tier | Est. LOC | Depends on |
|---|---|---|---|---|
| 3A | Replace `task_store.stub_kanban_sync()` with real `hermes kanban ls --label goap-run:<run_id> --json` invocation | sonnet-coder | 150 | Hive 2 ships |
| 3B | Implement kanban write-back: `assign`, `move`, `block`, `unblock` via `hermes kanban` subprocess; allowlist agent names from `plan.swarm.agents[]` | sonnet-coder | 120 | 3A |
| 3C | Replace `log_stream.stub_log()` with real `select`-loop tail of `$WORKDIR/hive-mind.log` + `$WORKDIR/watcher.log`; 200ms debounce | sonnet-coder | 180 | Hive 2 ships |
| 3D | Replace `quality_runner.stub_gates()` with real `ruff check`, `pytest --json-report`, `bandit -r -f json` subprocess invocations; scrub env; 5-min timeout per gate | sonnet-coder | 220 | Hive 2 ships |
| 3E | Real `progress_stream`: subscribe to file-watcher events on sidecars; tail hive-mind.log; detect phase transitions via the regex constant `PHASE_COMPLETE_RE = re.compile(r"✓\s+Phase\s+(\d+)\s+Complete")` (committed as a module constant in `progress_stream.py`). Fallback: if no phase event detected within 120s of the previous event, emit a `heartbeat` SSE event with `{kind: "stale", last_phase_id: ..., last_seen_at: ...}` and log `WARNING phase pattern not matched` to `actions.jsonl`. The frontend treats `stale` as "still in previous phase" — no UI advance, no error. | sonnet-coder | 200 | 3C |
| 3F | Ensure `runner.start` writes resolved `AdvancedConfig` to `$WORKDIR/.config.json` and exports `RUFLO_GOAP_CONFIG=$WORKDIR/.config.json` in the spawned env | sonnet-coder | 40 | Hive 2 ships |
| 3G | Write `scripts/smoke-e2e.sh` per `hive3-ruflo-wiring/objective.md` lines 46-54 | sonnet-debugger | 200 | 3A–3F |
| 3H | Run smoke-e2e; triage any failures; record evidence in `$WORKDIR/smoke-result.json` | sonnet-debugger | — | 3G |
| 3I | Re-run safety probes from Hive 2 + 3 new probes (kanban-bypass, unregistered-agent-assign, quality-on-unstaged-run) | sonnet-debugger | 80 | 3A–3D |

**Total LOC estimate (Hive 3):** ~1200 LOC (mostly glue + the smoke script). Many stubs are 1:1 line-for-line replacements with subprocess invocations.

### 2.2 Acceptance criteria

1. `bash $WORKDIR/scripts/smoke-e2e.sh` exits 0. The script's transcript is captured to `$WORKDIR/smoke-result.json` with `{run_id, stages_passed: [...], evidence_paths: [...]}`.
2. All Hive 2 tests still pass (`pytest -q ...`).
3. Safety probes still pass (Hive 2 set + 3 new from 3I).
4. Live `ms` project sha256 unchanged (per Hive 2 §1.2 check 4).
5. Dashboard restart + smoke test re-runnable against the live dashboard.
6. `FINAL-REPORT.md` at workdir root with: stub→real mapping with file:line refs, smoke result, safety verification, openapi diff vs Hive 2 (expect empty — Hive 3 should add zero new routes).
7. PR opened against `feat/goap-build-h2-backend-...` (stacked).
8. Telegram ping for Decision Gate 2 with `smoke-result.json` contents.

### 2.3 Smoke tests Hive 3 runs

The deliverable IS the smoke test. The shell script:
```bash
#!/usr/bin/env bash
set -euo pipefail
B=localhost:9119/api/plugins/ruflo-goap-control
RID=$(curl -s -XPOST $B/projects/stage \
  -H 'Content-Type: application/json' \
  -d '{"objective":"write hello world in python","name":"goap-smoke-'$(date -u +%s)'","category":"Coding"}' | jq -r .project.run_id)
[[ "$RID" =~ ^rg_[a-f0-9]{12}$ ]] || { echo "bad run_id: $RID"; exit 1; }
SESSION=$(curl -s $B/projects | jq -r ".projects[] | select(.run_id==\"$RID\") | .session")
echo "staged: $RID session: $SESSION"
curl -s -XPOST $B/runs/$RID/start -H 'Content-Type: application/json' \
  -d '{"confirm":"START_RUFLO_GOAP_RUN"}' >/dev/null
# Confirm tmux session actually spawned (catches launcher-failed-but-returned-200)
sleep 3
tmux has-session -t "$SESSION" || { echo "tmux session $SESSION did not spawn"; exit 1; }
# Poll status; break early on completed OR failed
for i in $(seq 1 120); do
  STATUS=$(curl -s $B/projects | jq -r ".projects[] | select(.run_id==\"$RID\") | .runtime.effective_status")
  case "$STATUS" in
    completed) echo "completed in ${i}s"; break;;
    failed|blocked|exited|stopped|timeout|status_unreadable)
      echo "smoke FAILED — effective_status=$STATUS"; exit 1;;
  esac
  sleep 1
done
LOG=$(curl -s "$B/runs/$RID/logs?file=hive-mind.log" | jq -r .content)
[[ -n "$LOG" ]] || { echo "no logs"; exit 1; }
REPORT=$(curl -s $B/runs/$RID/final-report | jq -r .content)
[[ -n "$REPORT" ]] || { echo "no report"; exit 1; }
# Cleanup — guard against deleting `ms`
[[ "$RID" != "rg_91b80749ac82" ]] || { echo "WILL NOT delete ms"; exit 1; }
curl -s -XPOST $B/runs/$RID/stop -H 'Content-Type: application/json' \
  -d '{"confirm":"STOP_RUFLO_GOAP_RUN"}' >/dev/null
curl -s -XDELETE $B/projects/$RID -H 'Content-Type: application/json' \
  -d '{"confirm":"DELETE_RUFLO_GOAP_PROJECT"}' >/dev/null
jq -n --arg rid "$RID" --arg session "$SESSION" --arg log "${LOG:0:200}" \
  '{ok:true, run_id:$rid, session:$session, log_preview:$log}' > $WORKDIR/smoke-result.json
echo "OK"
```

### 2.4 Risk hotspots

- **Smoke test timing flakiness** — Ruflo + Claude takes 30s-2min for a "hello world" objective. If the polling loop times out, hive blocks. Mitigation: 120s polling window in script; if it fails, debugger investigates via tmux attach rather than re-running.
- **Kanban CLI lag** — `hermes kanban` invocations cost ~200-500ms each. Tasks-sub-tab refresh should debounce. Mitigation: 5s read-through cache in `task_store.real_sync`.
- **Subprocess env leakage** — `ruff`, `pytest`, `bandit` invocations MUST inherit the scrubbed `_env()` not `os.environ`. Tests Hive 3 ships must verify env scrub by setting a fake API key in the test runner's env and asserting it's absent in the subprocess.
- **Live `ms` collision** — if the smoke test accidentally targets `ms`, the live run dies. Mitigation: Hive 3's smoke script uses a fresh slug each invocation (`goap-smoke-<UNIX-TS>`) and the `DELETE` step refuses on the `ms` slug.

---

## 3. Hive 4 — FRONTEND

**Worker budget:** Opus queen + 2 sonnet-coder + 1 sonnet-reviewer (4 workers; matches `hive4-frontend/objective.md` line 6).

### 3.1 Decomposed sub-tasks

| # | Sub-task | Worker tier | Est. LOC | Depends on |
|---|---|---|---|---|
| 4A | Bootstrap `dashboard/src/`: `package.json`, `vite.config.ts` (lib mode, React external, IIFE output), `tailwind.config.js` (dark theme + purple-violet palette per SPEC §12), `tsconfig.json` | sonnet-coder #1 | 200 | Hive 3 ships |
| 4B | Zustand store: slices per ARCHITECTURE §4 (selectedProject, plan, researchPhases, devPhases, goapState, tasks, executionPlan, qualityGates, logs, config, mode, modalOpen, toasts) | sonnet-coder #1 | 250 | 4A |
| 4C | Page shell: `<RufloGoapTab>`, `<TabHeader>`, `<CodingObjectivePanel>` (input, advanced settings button, generate/regenerate button, category chips) | sonnet-coder #1 | 300 | 4B |
| 4D | Mode switch + Research view: `<ModeSwitch>`, `<GoapStateAssessment>` (animated %), `<ResearchPhaseProgress>` (5 cards), `<ResearchCompleteSummary>` (4 status cards + execution summary + action bar with Approve & Launch / Request Revision) | sonnet-coder #1 | 600 | 4C |
| 4E | Development view: `<DevSwarmProgress>` (5 dev phase cards) | sonnet-coder #1 | 250 | 4C |
| 4F | Sub-tabs scaffold: `<SubTabs>` parent with active-tab underline animation; 5 children stubbed | sonnet-coder #1 | 200 | 4D, 4E |
| 4G | `<DashboardSubTab>`, `<TasksSubTab>` (kanban columns with HTML5 drag-drop + hand-rolled SVG dependency graph) | sonnet-coder #2 | 600 | 4F |
| 4H | `<ExecutionSubTab>` (inner tabs: Execution Plan / Current Step / Agent Activity / Event Log) with hand-rolled SVG graph view + timeline view toggle | sonnet-coder #2 | 600 | 4F |
| 4I | `<QualitySubTab>` (3 gate cards + research-only metrics) + `<LogsSubTab>` (SSE-bound log stream) | sonnet-coder #2 | 400 | 4F |
| 4J | `<AdvancedConfigModal>` with 4 sub-tabs (Swarm/GOAP/Execution/Model) + 4 presets + Save/Reset; native `<dialog>` + focus trap | sonnet-coder #2 | 500 | 4B |
| 4K | Toasts: `<ToastHost>` + Zustand-driven queue + slide-up + auto-dismiss animation | sonnet-coder #2 | 100 | 4B |
| 4L | Loading / error / empty states for every async component (per `hive4-frontend/objective.md` line 40) | sonnet-coder #2 | 200 | 4G–4I |
| 4M | Mobile-responsive pass: test at 375px, 768px, 1280px, 2580px breakpoints | sonnet-coder #2 | 150 | 4G–4K |
| 4N | Animations per SPEC §14 (8 items) | sonnet-coder #2 | 200 | 4G–4K |
| 4O | Component tests with Vitest + React Testing Library: ≥60% coverage on new components | sonnet-reviewer | 400 | 4G–4K |
| 4P | Visual fidelity diff: capture screenshot per SPEC state, compare to frame; save to `$WORKDIR/visual-diff/<state>/{actual,expected,diff}.png` | sonnet-reviewer | 100 | 4G–4N |
| 4Q | Bundle build + symlink verification per `hermes-dashboard-venv-shadows-source` lesson | sonnet-reviewer | 50 | 4O |

**Total LOC estimate (Hive 4):** ~5000 LOC (TS+TSX+CSS). This is the biggest hive by LOC; the 2-coder split is essential.

### 3.2 Acceptance criteria

1. **All 27 distinct UI states from `RESEARCHER-NOTES.md §A` render correctly.** Each state has a Vitest test that mounts the corresponding component(s) with the SPEC's data shape and asserts the key visible text strings appear. (Researcher-noted §A enumeration is the source of truth, not "24" — that earlier number was a draft estimate from the SPEC summary section count, superseded by the per-frame inventory.)
2. **Every backend route from `ARCHITECTURE.md §6` is exercised by an automated headless test.** `tests/test_api_calls.test.tsx` mounts the relevant component, asserts the network call is fired against a mocked fetch, asserts the request shape matches the API contract. Network-tab observation in a real browser is supplementary evidence, NOT the test.
3. Mobile viewport verified at 375px via Vitest's `@testing-library/react` `render({width: 375})` setup OR Playwright screenshot test at 375×667 captured to `$WORKDIR/visual-diff/mobile-375/`.
4. Zero browser console errors on any state. Hive 4 ships `scripts/console-error-check.sh` that uses Playwright to load each state and asserts `page.on('console', e => e.type() !== 'error')`.
5. `npm run build` in `dashboard/` produces fresh `dist/index.js` + `dist/style.css`.
6. Symlink check passes (per `hermes-dashboard-venv-shadows-source.md` memory): `ls -la /home/josep/.local/share/hermes-agent/venv/lib/python3.11/site-packages/hermes_cli/web_dist` shows the symlink. Plugin's `dist/` is read directly by host shell.
7. Hard refresh shows new UI after `systemctl --user restart hermes-dashboard.service`.
8. `FINAL-REPORT.md` at workdir root with visual fidelity scorecard, API call inventory, defect callbacks.
9. PR opened against `feat/goap-build-h3-ruflo-wiring-...` (stacked).

### 3.3 Smoke tests Hive 4 runs

```bash
cd ~/.hermes/plugins/ruflo-goap-control/dashboard
npm install
npm run lint
npm run test           # vitest
npm run build          # vite
[[ -f dist/index.js && -f dist/style.css ]] || exit 1
# Bundle size sanity check
[[ $(stat -c%s dist/index.js) -lt 200000 ]] || { echo "bundle too big"; exit 1; }
# Symlink check
ls -la /home/josep/.local/share/hermes-agent/venv/lib/python3.11/site-packages/hermes_cli/web_dist
systemctl --user restart hermes-dashboard.service
sleep 5
# Asset-hash verification: fetch HTML, grep for new bundle hash
curl -s http://127.0.0.1:9119/ruflo-goap | grep -q "ruflo-goap-control/dist/index.js" || exit 1
echo "Hive 4 smoke OK"
```

### 3.4 Risk hotspots

- **The host React contract** — the existing bundle uses `window.__HERMES_PLUGIN_SDK__.React` not bundled React. Vite must be configured with `react` and `react-dom` as `rollupOptions.external`. If we miss this, the bundle ships its own React and the host's hooks-rules invariants break in subtle ways.
- **Tailwind purge over-aggression** — Tailwind by default purges unused classes. If the dynamic `className={isOpen ? 'block' : 'hidden'}` patterns are missed, prod CSS is missing classes. Mitigation: explicit safelist for dynamic classes.
- **Symlink invalidation** — per the `hermes-dashboard-venv-shadows-source` lesson, a `pip install` could clobber the symlink. Mitigation: Hive 4 prepends a check + re-symlink step to its build script.
- **SVG performance** — hand-rolled SVG for the execution graph is fine at 5 nodes; if SPEC ever grows to 20+, performance degrades. Mitigation: defer optimization; the SPEC's 5-node ceiling is firm.
- **HTML5 drag-drop on touch devices** — native dnd doesn't work on touch. Mitigation: for mobile, render an "Assign" button on each card that opens a column-picker; native dnd remains for desktop.

---

## 4. Hive 5 — STRESS-TEST (Claude-driven, NOT a Ruflo hive)

**Worker budget:** 1 Claude session (Opus 4.7), no ruflo workers.

### 4.1 Decomposed sub-tasks

Hive 5 is not decomposed into worker dispatch — it's Claude running the playbook in `hive5-stress-test/INSTRUCTIONS.md` with browser MCP. The high-level sequence:

| # | Step | What Claude does |
|---|---|---|
| 5A | Open + verify clean state | `browser_open http://127.0.0.1:9119/ruflo-goap`; screenshot; snapshot; curl baseline; console-error check |
| 5B | Button-by-button click matrix | For every SPEC button: snapshot-before, click, wait, screenshot-after, curl the expected backend endpoint, assert state, record to `results.jsonl` |
| 5C | Edge case matrix (10 cases per INSTRUCTIONS.md §3) | Empty objective, shell metacharacters, concurrent stage, network failure, mid-run abort, refresh-during-running, multiple modal opens, Reset to Defaults completeness, Save Configuration round-trip, browser refresh during running state |
| 5D | Viewport matrix (5 widths: 2580/1920/1366/768/375) | Navigate to each screen at each width; verify no layout break |
| 5E | Real Ruflo end-to-end | "hello world in python" → verify real tmux session, real log stream, real quality gates, real kanban cards |
| 5F | Defect classification | CRITICAL / MAJOR / MINOR per definitions in INSTRUCTIONS.md §6 |
| 5G | Outputs | `STRESS-TEST-REPORT.md`, `DEFECTS.md`, `results.jsonl`, `screenshots/`, `recordings/`, `kanban-bridge-trace.md` |

### 4.2 Acceptance criteria

1. `results.jsonl` contains one line per button-click trial with `{button, expected, actual, pass, evidence, severity}`.
2. Every SPEC §3-§11 button has at least one trial.
3. `DEFECTS.md` exists with classifications. If any CRITICAL, chain HALTS per `chain.sh:191-195`.
4. Visual evidence in `screenshots/` for every trial.
5. Real-Ruflo run completed: tmux spawn observed, log stream observed, quality gates ran, kanban cards present.
6. Touch the resume gate file `~/.hermes/ruflo-work/goap-build-20260518T180000Z/.gate-post-h4-frontend-resume` when done.

### 4.3 Smoke tests Hive 5 runs

The deliverable IS the smoke test. There is no internal smoke check beyond the per-button assertions.

### 4.4 Risk hotspots

- **Honest-failure constraint** — per `hive5-stress-test/INSTRUCTIONS.md` line 88 and the `feedback-validate-ui-before-claiming-shipped.md` memory: NEVER claim "shipped" based on screenshots alone. Every claim must be backed by a curl+jq backend-state assertion in `results.jsonl`. Claude must enforce this on themselves.
- **`ms` collision** — if the real-Ruflo test step accidentally targets `ms`, the live run dies. Mitigation: Hive 5 uses a fresh slug for the real-Ruflo run.
- **Critical-defect threshold gaming** — there will be a temptation to downgrade a real CRITICAL to MAJOR to avoid halting the chain. Don't. The gate exists for a reason.

---

## 5. Hive 6 — POLISH

**Worker budget:** Opus queen + 1 sonnet-coder + 1 sonnet-writer (3 workers; matches `hive6-polish/objective.md` line 6).

### 5.1 Decomposed sub-tasks

| # | Sub-task | Worker tier | Est. LOC | Depends on |
|---|---|---|---|---|
| 6A | Apply every Hive 5 MAJOR defect fix in priority order. **Priority rule:** sort `results.jsonl` failures by `severity` desc (MAJOR before MINOR), then by `evidence.affected_routes` count desc (failures affecting more routes first), then by alphabetical `button` for stable ordering. Document the applied order in `$WORKDIR/fix-order.json`. Re-run Hive 4 component tests + Hive 2 pytest after each fix. | sonnet-coder | varies | Hive 5 DEFECTS.md |
| 6B | Apply every Hive 5 MINOR defect fix; defer items beyond effort budget to kanban | sonnet-coder | varies | 6A |
| 6C | Animation polish per SPEC §14 — 8 items checklist | sonnet-coder | 150 | 6A |
| 6D | Update plugin `dashboard/README.md` per `hive6-polish/objective.md` line 42 | sonnet-writer | — | 6A |
| 6E | Write plugin `dashboard/CONTRIBUTING.md` per line 43 | sonnet-writer | — | — |
| 6F | Write `~/.hermes/specs/ruflo-goap-from-video/architecture/CHANGELOG.md` per line 44 | sonnet-writer | — | 6A–6C |
| 6G | New memory file: `ruflo-goap-plugin-operator-console.md` per line 47 | sonnet-writer | — | — |
| 6H | Update `MEMORY.md` index per line 48 | sonnet-writer | — | 6G |
| 6I | Save MVMS completion record (importance 4) tying all 6 hives | sonnet-writer | — | 6F |
| 6J | `kanban-suggestions.md` per line 52 | sonnet-writer | — | 6B |
| 6K | Final smoke: re-run Hive 3 e2e + Hive 5 minimum-viable click-through | queen | — | 6A–6C |
| 6L | `FINAL-REPORT.md` per line 55 | queen | — | 6A–6K |

### 5.2 Acceptance criteria

Per `hive6-polish/objective.md` lines 63-71:
1. Hive 5 critical + major defects all addressed.
2. Hive 5 `results.jsonl` re-runnable with **all previously-passing trials still passing AND zero NEW failures introduced**. (Not "strictly more passes" — re-runs against unchanged code should give identical results; new passes only come from polish work fixing previously-failing defects.) Hive 6's queen produces `re-run-delta.json` with `{baseline_pass: N, baseline_fail: M, post_polish_pass: N+K, post_polish_fail: M-K, regressions: 0}`.
3. Animation checklist 8/8.
4. Documentation present (README, CONTRIBUTING, CHANGELOG).
5. Memory updated.
6. Final smoke passes.
7. PR opened against `feat/goap-build-h4-frontend-...` (stacked).
8. Final Telegram ping: "GOAP build chain complete. 6 PRs ready for review + merge bottom-up."

### 5.3 Smoke tests Hive 6 runs

```bash
# Re-run Hive 3 smoke
bash $HIVE3/scripts/smoke-e2e.sh
# Re-run Hive 5 minimum-viable click-through (subset of results.jsonl)
bash $HIVE5/scripts/re-run-clickthrough.sh  # Hive 5 ships this for re-runnability
# Frontend tests
cd ~/.hermes/plugins/ruflo-goap-control/dashboard && npm run test
# Backend tests
pytest -q ~/.hermes/plugins/ruflo-goap-control/tests/
# Coverage report
pytest --cov=... > $WORKDIR/coverage.txt
```

### 5.4 Risk hotspots

- **Regression from defect fixes** — a fix can break a passing test. Mitigation: run all tests after every fix; queen enforces this in worker prompts.
- **Polish overrun** — animations are easy to over-engineer. Strict scope: only the 8 items in SPEC §14. Anything beyond goes to `kanban-suggestions.md`.
- **Memory accuracy** — the `ruflo-goap-plugin-operator-console.md` memory will be cited by future Claude sessions; it MUST reflect the as-shipped state, not the as-planned state. Mitigation: write 6G AFTER 6K (final smoke) so it reflects observed behavior.

---

## 6. Cross-hive concerns

### 6.1 LOC budget

| Hive | Est. LOC (source) | Est. LOC (tests) | Total | Worker count |
|---|---|---|---|---|
| H1 | n/a (4 docs, ~16,900 words) | n/a | n/a | 3 (queen + researcher + reviewer) |
| H2 | ~2000 (Python source) | ~1000 (pytest) | ~3000 | 5 |
| H3 | ~800 (Python glue) | ~400 (smoke + safety tests) | ~1200 | 3 |
| H4 | ~3000 (TSX) + ~600 (CSS/Tailwind) + ~400 (config/build) = ~4000 source | ~1000 (Vitest, 60% coverage on 4000 LOC) | ~5000 | 4 |
| H5 | n/a (Claude-driven, ~2-4h wall) | n/a | n/a | 1 (Claude) |
| H6 | ~500-1000 (defect fixes + animation polish) + ~1000 words docs | n/a (re-uses H2/H4 tests) | ~500-1000 | 3 |

**Total source LOC: ~6,300-6,800.** **Total source+test LOC: ~9,700-10,200.**

The GAMEPLAN's original "~6,000-8,000 LOC" estimate was about *source* LOC, not source+test. Source-only stays within the GAMEPLAN ceiling. The source+test figure is presented here for capacity planning — Hive 2 and Hive 4 testers should plan accordingly. If LOC estimates start drifting >20% over plan during a hive, the hive's queen escalates to Joseph before continuing.

### 6.2 Coverage targets summary

| Surface | Target | Enforcer |
|---|---|---|
| Backend (`plugin_api.py` + new modules) | ≥ 75% line coverage | Hive 2 sonnet-tester via pytest --cov |
| Frontend (new components) | ≥ 60% line coverage | Hive 4 sonnet-reviewer via vitest --coverage |
| Safety probes (path traversal, unregistered ID, env scrub, ms mutation refusal) | 100% pass | Hive 2 + Hive 3 |
| Existing 4 pytest tests | 100% pass after every hive | Every hive runs `pytest -q` as final check |

### 6.3 Chain-signal contract reminder

Every hive's `watcher.sh` writes one of `$WORKDIR/.chain-signal-{done,failed,blocked,timeout}` so `chain.sh:81-119` can advance. Hive queens MUST write `FINAL-REPORT.md` at `$WORKDIR` root (not in a subdir) — the lesson `6f42c8b1-ff20-4e58-8cb0-baef778b89f6` (referenced in the hive1 objective) records the cost of getting this wrong.

### 6.4 Decision-gate behaviors

Per `chain.sh:24-27`:
- `PAUSE_AFTER_HIVE_1=0` — autonomous; this hive completes and chain advances to H2 automatically.
- `PAUSE_AFTER_HIVE_3=0` — autonomous (BUT smoke-test failure still halts).
- `PAUSE_AFTER_HIVE_4=1` — ALWAYS pauses; Hive 5 requires Claude session.
- `PAUSE_AFTER_HIVE_5=0` — autonomous (BUT critical-defect count > 0 still halts via `chain.sh:191`).

This hive (H1) does NOT pause the chain by default; the gameplan's gate `PAUSE_AFTER_HIVE_1` is set to 0 because Joseph said "run straight through" (per the chain.sh comment line 24). If H1's review surfaces a fundamental architectural conflict, H1 writes `BLOCKED.md` instead of `FINAL-REPORT.md` and the chain halts at the failure-signal check in `wait_for_hive` (`chain.sh:97-106`).

---

**End of BUILD-PLAN.md.**
