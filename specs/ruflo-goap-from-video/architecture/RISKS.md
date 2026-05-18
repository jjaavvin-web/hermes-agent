# RISKS.md — Risk Register & Open-Decision Defaults

**Hive:** 1 — ARCHITECT
**Related:** `ARCHITECTURE.md` (the target state), `DATA-MODEL.md` (the persistence story), `BUILD-PLAN.md` (the per-hive work), `RESEARCHER-NOTES.md` (frame-evidence catalogue from sonnet-researcher worker).

This document is the risk register for the GOAP build chain. It ranks the top 10 risks by impact × likelihood, decides defaults for every SPEC.md §15 item that the video didn't show, and resolves every conflict between goal.ruv.io's UX implications and the Hermes safety model.

---

## 1. Top 10 risks (ranked impact × likelihood)

Scoring rubric: impact ∈ {1 nuisance, 2 minor, 3 user-visible regression, 4 data corruption, 5 chain-halt}; likelihood ∈ {1 unlikely, 3 plausible, 5 expected absent mitigation}. Score = impact × likelihood. Ordered descending.

### R1. Live `ms` project mutated mid-build (score: 5×3=15)

- **Trigger:** any hive's worker calls `POST /runs/rg_91b80749ac82/stop` or writes to `~/.hermes/ruflo-work/ms-20260518T100822Z/` while the run is live; OR a test fixture accidentally targets the live registry instead of a tmp_path copy; OR the registry's atomic-replace gets interrupted and corrupts the file mid-write.
- **Blast radius:** the only currently-running production Ruflo+Claude swarm dies; user loses in-flight work; tracking card on kanban gets stale; `projects.json` may be left in a half-written state and crash every subsequent dashboard read.
- **Mitigation:**
  1. `preflight.sh` takes a pre-build sha256 of `projects.json` and `~/.hermes/ruflo-work/ms-*/` directory listing; chain refuses to start if `ms` is not in `running` status (because that would mean it already died and the chain shouldn't run on top of that without explicit operator action).
  2. Every test uses `monkeypatch.setenv("RUFLO_GOAP_CONTROL_ROOT", str(tmp_path / "control"))` per the existing pattern at `tests/test_plugin_api_closeout.py:16-20`. PR review enforces this.
  3. `DELETE /projects/{run_id}` hard-rejects on slug `ms` until the project moves to a terminal status (per `ARCHITECTURE.md §6` house-keeping note).
  4. Atomic write pattern at `plugin_api.py:184-188` already in place; we don't relax it.
- **Rollback:** `~/.hermes/ruflo-goap-control/projects.json.bak.<TS>` taken by preflight; restore via `cp`. If the workdir itself is corrupted, re-launch via `bash $WORKDIR/launch.sh` (the launcher is idempotent on existing tmux/watcher state).

### R2. Bundle ships React (host-React contract broken) (score: 5×3=15)

- **Trigger:** Hive 4's Vite config forgets to externalize `react` and `react-dom` so the bundle ships its own copy. The host dashboard injects `window.__HERMES_PLUGIN_SDK__.React` (a different React instance), and the plugin invokes hooks via its bundled copy. Host- and plugin-React disagree about hooks dispatcher state → silent hook errors, no UI rendered, or duplicate React invariants.
- **Blast radius:** entire plugin tab is broken; user sees blank or error state; could go unnoticed because nothing crashes loudly — hooks may just silently misbehave.
- **Mitigation:**
  1. `vite.config.ts` MUST set `build.rollupOptions.external: ["react", "react-dom"]` and use the host's React via `window.__HERMES_PLUGIN_SDK__.React`. Hive 4 ships this as the first commit in their PR.
  2. Build-time assertion: post-`npm run build`, a tiny script greps `dist/index.js` for `React.createElement` patterns and verifies the bundle does NOT contain a `react.development.js` or `react.production.min.js` signature. Fail the build if found.
  3. Runtime guard: the plugin's entry-point asserts `if (!SDK || !SDK.React) throw new Error("host SDK missing")` (matches the existing IIFE pattern at `dashboard/dist/index.js:3-7`).
- **Rollback:** the existing 41KB IIFE bundle is preserved at `dashboard/dist/index.js.bak.<TS>` by Hive 4's first commit. If the new bundle breaks, `mv` back and restart dashboard.

### R3. SSE generator file-descriptor exhaustion (score: 4×3=12)

- **Trigger:** Hive 2's `log_stream.py` or `progress_stream.py` SSE generators open file handles in their bodies but fail to close them on `ClientDisconnect`. After ~1000 open/close cycles (which happens during normal use over a few days), the FastAPI process exhausts its open-fd budget and stops accepting any HTTP requests.
- **Blast radius:** dashboard appears dead; only fix is `systemctl --user restart hermes-dashboard.service`, which kills any in-flight Ruflo launchers.
- **Mitigation:**
  1. Every generator wraps its `yield` loop in `try/finally` that closes the file handle.
  2. Hive 2 ships `tests/test_sse_no_leak.py` that opens 100 SSE connections, closes them, and asserts `len(psutil.Process().open_files()) <= baseline + 5`.
  3. SSE generators have a hard max lifetime of 600s (per `ARCHITECTURE.md §6` notes); client must reconnect. Limits the worst-case fd count to `max_concurrent_clients × 1`.
- **Rollback:** revert the SSE PRs and fall back to polling-only (the existing `GET /runs/{id}/logs` snapshot is acceptable for v1 if SSE proves fragile).

### R4. `projects.json` schema field added without `Optional[]` default (score: 4×3=12)

- **Trigger:** Hive 2 adds a new field to the `Project` model without `Optional` + default, OR with `extra="forbid"`. The 13 existing records fail to validate, dashboard returns 500 on `GET /projects`, frontend shows error state.
- **Blast radius:** all 13 projects invisible; user can't manage the running `ms` or any other registered run; dashboard tab is effectively broken until rollback.
- **Mitigation:**
  1. `DATA-MODEL.md §3.4` is normative — every new field is `Optional[X] = None`; no required new fields.
  2. Hive 2 ships `tests/test_existing_records_load.py` that copies the live `projects.json`, runs it through the new `Project` pydantic model, asserts all 13 round-trip losslessly.
  3. `_load_registry` keeps its current permissive shape (`extra="ignore"` default in pydantic v2).
- **Rollback:** projects.json daily backup at `~/.hermes/ruflo-goap-control/projects.json.bak.<TODAY>`; restore via `cp`. The 13-record snapshot also lives in the chain workdir from preflight.

### R5. Hermes dashboard symlink invalidated (frontend changes invisible) (score: 4×3=12)

- **Trigger:** a `pip install` (e.g., by hermes update during the chain) clobbers the symlink at `/home/josep/.local/share/hermes-agent/venv/lib/python3.11/site-packages/hermes_cli/web_dist`, restoring the baked copy. Hive 4 builds a new bundle, restarts the dashboard, browser hard-refresh still shows the old UI — exactly the lesson recorded in memory `hermes-dashboard-venv-shadows-source.md`.
- **Blast radius:** Hive 4 spends hours debugging "did the build work?" with no visible evidence of failure. The build succeeds; the bundle is on disk; the daemon serves the wrong asset hashes.
- **Mitigation:**
  1. Hive 4's `smoke-tests` step (see `BUILD-PLAN.md §3.3`) explicitly `ls -la` the symlink and `cmp` the source vs venv files. If they differ, queen re-applies the symlink and re-restarts.
  2. The plugin's `dashboard/dist/` is served from the plugin path NOT the dashboard's `web_dist/` — the symlink shadow affects the dashboard SPA shell, not per-plugin bundles. Verify which side is broken: SPA shell shadowing causes the `/ruflo-goap` tab to disappear entirely; per-plugin bundle would show old plugin content.
  3. Add to Hive 6's docs: "before fixing UI bugs, check the symlink."
- **Rollback:** re-apply the symlink documented in the memory: `ln -s /home/josep/.local/share/hermes-agent/hermes_cli/web_dist /home/josep/.local/share/hermes-agent/venv/lib/python3.11/site-packages/hermes_cli/web_dist`.

### R6. Subprocess env leakage (API key reaches Ruflo/Claude spawn) (score: 5×2=10)

- **Trigger:** Hive 3's real `quality_runner` invokes `ruff/pytest/bandit` via `subprocess.run(..., env=os.environ)` instead of `env=_env()`. The Hive 1 environment may have `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` set (CI common). The child inherits it; the next layer down (Ruflo, Claude Code) starts billing the paid API instead of OAuth/Max.
- **Blast radius:** silent paid-API spend (per `PROVIDER-STACK.md`: "Default: do not route to OpenAI. Reserve for the slots above. Anthropic Max gets your text/agentic work for free; rotating in OpenAI without reason just spends money."). Also `h2reviewer-anthropic-routes-to-paid-api` lesson is a precedent for this exact bug.
- **Mitigation:**
  1. Every subprocess in Hive 3 uses the existing `_env()` helper at `plugin_api.py:147-151`.
  2. **GAP TO FIX (raised by reviewer):** the existing `ENV_SCRUB` list at `plugin_api.py:36-42` includes ANTHROPIC_* + CLAUDE_CODE_OAUTH_TOKEN + OPENAI_API_KEY but is MISSING `GEMINI_API_KEY` (and any other `GEMINI_*` vars). GAMEPLAN safety rule 5 explicitly lists `GEMINI_*` as required-scrub. Hive 2 MUST extend `ENV_SCRUB` to add `GEMINI_API_KEY` before Hive 3 wires subprocess work. ARCHITECTURE.md §7.4 carries the same callout. Test in `tests/test_safety.py`: set `GEMINI_API_KEY=fake-test-key`, invoke any new subprocess-using route, dump child environ, assert `fake-test-key` not present.
  3. The existing launch template already double-scrubs ANTHROPIC and OPENAI via `env -u` at `plugin_api.py:358`. Hive 2 extends this `env -u` chain to also include `GEMINI_API_KEY` for belt-and-suspenders.
  4. Hive 3 ships the probe test described in (2) covering ANTHROPIC_API_KEY, GEMINI_API_KEY, and CLAUDE_CODE_OAUTH_TOKEN as the canary set.
- **Rollback:** the env scrub is defensive; once invoked correctly, no rollback needed. If the bug ships and is discovered, replace the affected subprocess call sites and re-deploy.

### R7. Stress test (Hive 5) discovers CRITICAL defects (score: 4×3=12)

- **Trigger:** Hive 5's button-by-button matrix finds a backend route that doesn't honor the UI's request, or a UI component that fails a real backend response, or a real-Ruflo end-to-end that breaks the safety model.
- **Blast radius:** chain halts at Decision Gate 3 (`chain.sh:191-195`). Triage required; either re-fire H2/H3/H4 with defect list as input, or escalate to fundamental-redesign discussion. Possibly multi-hour delay.
- **Mitigation:**
  1. Hive 2 and Hive 3 ship their own smoke tests so most defects are caught upstream.
  2. The 4 existing pytest tests are the canary for backend safety regressions — they run as the final step of every hive.
  3. Hive 4's component tests catch most frontend regressions; visual fidelity diff catches most UI drift.
  4. Hive 5 is the BACKSTOP not the first line of defense.
- **Rollback:** chain.sh's halt is the rollback — nothing has merged yet. Joseph triages from the pause state.

### R8. Hand-rolled SVG graph performance / accessibility degradation (score: 3×3=9)

- **Trigger:** Hive 4 hand-rolls the Execution graph in SVG to avoid a React Flow dependency (per `ARCHITECTURE.md §8`). At 5 nodes it's fine; if SPEC ever grows or if we accidentally render duplicates, it stutters. Also: SVG without ARIA labels fails screen-reader expectations; mobile-touch on SVG nodes is awkward.
- **Blast radius:** UX feels janky; accessibility audit (if Joseph ever runs one) flags issues; v2 graph features (zoom, pan, fit) require rewrite.
- **Mitigation:**
  1. SPEC §8/§8b cap the graph at 5 linear nodes; this is firm — we don't speculate beyond it.
  2. SVG nodes carry `role="button"` + `aria-label="${step.label}, cost ${step.cost}"` so screen readers can announce them.
  3. Mobile: tap-handlers on rectangles; minimum 44×44 px tap targets per Apple HIG.
  4. If a v2 needs richer graph features, swap in React Flow then — don't pre-optimize.
- **Rollback:** revert the SVG component and ship a static `<table>` listing of nodes + costs as a temporary fallback.

### R9. Kanban CLI flakiness or change in output format (score: 3×3=9)

- **Trigger:** `hermes kanban` CLI changes its output format between Hive 3 build and Hive 5 stress test (Hermes is updated mid-chain). Or kanban operations time out under load. Or a card label doesn't apply correctly.
- **Blast radius:** Tasks sub-tab shows wrong cards or empty; drag-drop writes fail; Hive 5 flags as CRITICAL.
- **Mitigation:**
  1. Hive 3 uses the `--json` output flag exclusively; JSON output is the more stable contract.
  2. Hive 3 ships a defensive parser: ignore unknown fields, fail open (show empty state) rather than crash.
  3. `hermes update` is gated by `preflight.sh` — chain refuses to run if Hermes was updated <1h ago and tests haven't been re-run against the new version.
  4. 5s read-through cache in `task_store` softens transient CLI failures.
- **Rollback:** Tasks sub-tab falls back to the Hive 2 stub data with a banner "Live kanban sync paused; showing cached data". User can still see the rest of the dashboard.

### R10. The 6th "Research Summary" sub-tab (researcher-flagged) (score: 3×3=9)

- **Trigger:** RESEARCHER-NOTES.md §E item 3 + §F item 1 flag that frames `scene03` and `scene09` show a 6th sub-tab `Research Summary` (and `scene09` shows a variant strip `Dashboard / Files / Execution / Quality / Scope`). SPEC.md §2 lists only 5 sub-tabs. This was not in Hive 1's coverage matrix in `ARCHITECTURE.md §10`.
- **Blast radius:** Hive 4 builds the 5 tabs per SPEC; Hive 5 may flag "missing 6th tab" or "wrong tab labels" as a defect, gating the chain.
- **Mitigation:**
  1. **Decision (this hive):** treat the 6th tab + Files/Scope variant as **probable mid-transition render artifacts** rather than real distinct screens. SPEC.md §2 is normative; we build the 5-tab strip exactly. RESEARCHER-NOTES.md §F item 1 acknowledges these may be "older UI version captured mid-transition."
  2. **Justification:** the 6th tab is faded/disabled in scene03 — typical for transient render state. Files/Scope in scene09 doesn't match any other frame in the dataset. The SPEC's mapping (Dashboard/Tasks/Execution/Quality/Logs) is consistent across §3, §6, §7, §8, §9, §10 — high evidentiary weight.
  3. **If Hive 5 disagrees:** treat as a Hive 6 polish item, not a chain-halt. Add `Research Summary` as a 6th tab in Hive 6 if the data justifies it; reference RESEARCHER-NOTES.md §F item 1 as the source of the ambiguity.
- **Rollback:** since the default ships the 5-tab SPEC layout, no rollback is needed — only forward-add if Hive 5 demands.

---

## 2. SPEC §15 inferred-not-shown — chosen defaults

For each of the 10 items SPEC §15 flagged as inferred-not-shown, the queen picks a default and justifies it. (Researcher's first-pass defaults in RESEARCHER-NOTES.md §D were used as the basis; queen overrides where the safety model demands a stronger position.)

### §15.1 — What does `Request Revision` do?

**Default:** clicking `Request Revision` opens a small inline panel BELOW the Research-Complete summary (not a separate modal) with a textarea ("Tell the planner what to change") and two buttons: `Submit Revision` (which POSTs to `/plan/revise` with the textarea contents folded into the objective) and `Cancel` (which collapses the panel). After submit, the page returns to plan-generation-early state with the GOAP State Assessment animating from 0%.

**Justification:** Researcher's default (reset to objective-typed state) loses information — the operator's revision intent is wasted. An inline panel keeps the operator in flow, captures the revision text, and gives the planner something concrete to act on. Modal would be heavier than the rest of the SPEC's compact "Approve & Launch" affordance. Stay consistent with §11 modal as the only true modal in the app.

### §15.2 — `Regenerate Plan` after development started

**Default:** show a centered modal with title "Discard current build?" body "Regenerating will stop the running tmux session (rg_xxx) and clear the development phase state. Logs and final report are preserved at $WORKDIR for audit. Continue?" and buttons `Cancel` / `Regenerate (typed-confirmation required)`. The confirmation requires typing `REGENERATE_RUFLO_GOAP_PLAN` exactly (mirrors START/STOP confirmation pattern at `plugin_api.py:667-668`). On confirm: POST to `/runs/{id}/regenerate-plan`, which internally calls stop + new plan generation.

**Justification:** Researcher proposed inline-banner. Queen overrides because regenerating discards a live tmux session — that's a START/STOP-class operation and deserves the same confirm-phrase pattern. Inline banners are too easy to dismiss accidentally.

### §15.3 — Tasks sub-tab drag-and-drop / assign-agents semantics

**Default:** Dragging between columns is a column-move (updates `column` field). Dropping onto an agent name in the "Agent Activity" inner-tab of the Execution sub-tab is an assignment (updates `assigned_to` field). The `Mode: distributed` dropdown has two values: `distributed` (default; each new task auto-assigns to the next available agent) and `manual` (operator drags every task). Mode is per-project, persisted in `project.advanced_config.execution.assignment_mode`.

**Justification:** Researcher's PATCH endpoint shape is correct; queen extends with the mode dropdown's second value (`manual`) which the SPEC implies but doesn't name. Two-mode design matches the broader pattern of Hermes operator console (manual vs autonomous always toggleable).

### §15.4 — Where does Coding Objective persist? Refresh behavior?

**Default:** Objective text persists in `localStorage` under key `ruflo-goap-objective` AND, once a plan is generated, in the staged project record on disk. On page load: rehydrate from localStorage if present; if a `?run_id=...` query param is present, load that project's objective server-side. No URL changes on plain typing — only on `Approve & Launch`, when the URL gets `?run_id=rg_xxx`.

**Justification:** Researcher's localStorage default is right; queen adds the `?run_id=` param so users can bookmark/share specific runs. URL stability is a Hermes pattern (cross-references to runs are common in operator workflows).

### §15.5 — Agent Activity / Event Log inner-tabs content

**Default:** `Agent Activity` renders a real-time table: columns = `Agent | Status | Current Step | Last Active | Steps Completed`. Polls `/runs/{id}/execution/activity` every 3s (or via the same SSE channel as progress events). `Event Log` renders a chronological list of events from the run's `actions.jsonl` filtered to this `run_id`, rendered with the same `▶/•/✓/⚠` glyph convention as the Logs sub-tab.

**Justification:** Researcher's default split (Agent Activity = polling table, Event Log = log feed) is right and aligns with the routes already in `ARCHITECTURE.md §3.4`. Queen extends Event Log to specifically draw from `actions.jsonl` (not `hive-mind.log`) so it's structured operator-event data, not raw log noise — the Logs sub-tab covers raw logs already.

### §15.6 — GOAP modal sub-tab controls

**Default:** GOAP sub-tab has 6 controls matching the existing `agents/spec` route's enumeration (`plugin_api.py:541`): `Planning Algorithm` dropdown (A* / Dijkstra / Greedy), `Heuristic Function` dropdown (Manhattan / Hamming / Custom), `Cost Calculation Method` dropdown (Hybrid / Effort-only / Risk-only), `Optimization` toggle (default ON), `Parallel Action Detection` toggle (default ON), `Redundancy Removal` toggle (default ON). No sliders in this tab — depth and weight are hidden behind future "advanced" expansion.

**Justification:** Researcher's 4-control default is too narrow; the existing plugin's `agents/spec` already enumerates 6 fields and the modal should expose all of them. Default values match `_build_plan`'s defaults at `plugin_api.py:388-390`.

### §15.7 — `Widget Demo` and `Create Widget` links on parent page

**Default:** Out of scope. The parent page (`Define Research Objective` view at `/ruflo-goap` root) is OUT of scope for this build — we ship the `Coding Agent Swarm` view (SPEC §3+) as the plugin tab content. `Widget Demo` and `Create Widget` are not visited in the video and are likely goal.ruv.io marketing affordances not relevant to a Hermes operator. The existing `GET /widget/embed` endpoint stays for any user who wants to embed the Hermes plugin in another page.

**Justification:** Researcher's "out-of-scope" verdict stands. Building speculative features wastes hive budget.

### §15.8 — Authentication / multi-tenancy

**Default:** No new auth in the plugin. The plugin trusts the Hermes dashboard's existing session-token check (the `X-Hermes-Session-Token` header is already passed by the existing IIFE bundle's `fetchJSON` at `dashboard/dist/index.js`). Multi-tenancy is out of scope.

**Justification:** Researcher's default is correct. SPEC §1 confirms the demo had no login; SPEC §15.8 explicitly flags this as a non-issue. The plugin module docstring at `plugin_api.py:6` says "intentionally self-contained and user-local."

### §15.9 — API contract / request-response shapes

**Default:** Authoritative API contract is `ARCHITECTURE.md §6`. We do not reverse-engineer goal.ruv.io's network shapes; we define our own canonical shapes (Pydantic models). Any future parity work is out of scope for this build.

**Justification:** Researcher's default is correct; SPEC §15.9 explicitly notes the absence of HAR data.

### §15.10 — Static vs computed metric values

**Default:** All numeric outputs (12 components, 42 files, 124 tests, etc.) in v1 are deterministic outputs of `goap_planner._build_plan`. They are derived from the objective text + config (e.g., `max_agents` directly determines `Agents: N`). They are NOT random and NOT computed by an LLM. Frontend renders them as-shown. v2 may swap in real LLM plan synthesis — out of scope for this build.

**Justification:** Researcher's default is right and matches the existing `_build_plan` behavior. The numbers are honest representations of the deterministic plan; honesty > "real-looking" placeholders.

---

## 3. Safety-model conflicts — when goal.ruv.io UX implies behavior that breaks Hermes safety

There are 5 places where the goal.ruv.io UX strongly implies one-click destructive actions that the Hermes safety model intentionally gates behind confirmations or allowlists. For each: which wins, why.

### Conflict A — "Approve & Launch Development" appears one-click

- **UX implies:** clicking the purple-gradient button spawns the build immediately, no further interaction.
- **Hermes safety says:** `POST /runs/{run_id}/start` requires `confirm: "START_RUFLO_GOAP_RUN"` (`plugin_api.py:667-668`) — that's a typed-string confirm.
- **Winner:** **Hermes safety.** The frontend MAY auto-send the confirm phrase on button click (since the operator already explicitly clicked the labeled button), but the backend still validates the phrase. This preserves the safety invariant while honoring the UX.
- **Why:** the confirm phrase isn't actually visible to the user in the SPEC UI — the frontend just adds it to the request body. The safety property (no accidental launch from a non-UI HTTP client) is preserved; the UX matches the video. Both are honored. No conflict in practice.

### Conflict B — "Regenerate Plan" appears one-click on a running build

- **UX implies:** mid-build, click Regenerate, get a new plan.
- **Hermes safety says:** stopping a running tmux session is a destructive op that requires confirm (`STOP_RUFLO_GOAP_RUN` at `plugin_api.py:709-710`).
- **Winner:** **Hermes safety.** Regenerate-while-running shows a confirm modal with typed `REGENERATE_RUFLO_GOAP_PLAN` (per §15.2 default above). Regenerate-while-staged-or-stopped goes straight through (no live state to lose).
- **Why:** the kill-then-rerun pattern is exactly the kind of "easy to do, hard to undo" action the Hermes guardrails exist to slow down.

### Conflict C — Tasks sub-tab drag-and-drop appears un-gated

- **UX implies:** any operator can drag any task to any column at any time.
- **Hermes safety says:** unregistered agent names must be rejected; task moves should be auditable.
- **Winner:** **Hermes safety, with no UX cost.** Drag-drop is allowed for any column move, but the backend validates `assigned_to ∈ plan.swarm.agents[].name`. Unknown agents return 400. Every move is logged to `actions.jsonl` as `task_move` or `task_assign` event (per `DATA-MODEL.md §6.2`).
- **Why:** the operator-facing action doesn't change; the audit trail comes for free. The validation only fires if the frontend has bugged out and is sending agent names that don't match the plan (which is itself a defect Hive 5 would catch).

### Conflict D — `Enable Fallback` toggle in Model sub-tab is ON by default in SPEC

- **UX implies:** automatic fallback to other providers (paid API) when Claude OAuth/Max fails.
- **Hermes safety says:** `PROVIDER-STACK.md` explicitly says "Default: do not route to OpenAI" and the existing `_build_plan` config defaults to `fallback: false` and `max_cost: 0`.
- **Winner:** **Hermes safety.** Default in the Hermes build is `Enable Fallback: OFF`. The SPEC's default OF "ON" is a goal.ruv.io demo choice; we override.
- **Why:** silent paid-API spend is the failure mode the OAuth/Max policy is designed to prevent. The toggle remains exposed in the modal so an operator CAN turn it on — but the default refuses. Tooltip on the toggle: "Off: OAuth/Max only (recommended). On: allows paid API fallback (incurs cost)."

### Conflict E — Modal `Save Configuration` may persist a config that exceeds safety limits

- **UX implies:** any combination of values in the modal is saveable.
- **Hermes safety says:** `max_agents > 20` would spawn an unbounded swarm; `timeout_seconds > 7200` would never time out; `max_cost > 100` would let single-request runaway spend.
- **Winner:** **Hermes safety.** `POST /config/validate` enforces the pydantic field-level bounds (`SwarmConfig.max_agents: int = Field(..., le=20)` etc., per `ARCHITECTURE.md §5.6`). Out-of-range values return 422 with a `warnings: [...]` array that the frontend renders inline.
- **Why:** these are hard limits, not preferences. Documented in tooltips next to each field. Operator can still hit max-value (20 agents, 7200s, $100) — they just can't exceed it.

---

## 4. Things RESEARCHER-NOTES.md surfaced that warrant explicit dispositions

| # | Researcher observation | Hive 1 decision |
|---|---|---|
| R-1 | 6th "Research Summary" sub-tab in scene03/scene09 | Treat as mid-transition artifact. Build 5-tab SPEC layout. See Risk R10. |
| R-2 | `Files`/`Scope` variant strip in scene09 | Same — variant render. Build SPEC's labels. |
| R-3 | Docked `Chat < >` widget alongside Research Review (scene47-48) | OUT of scope. This is the Hermes-host chat sidebar (HERMIN persona), not a GOAP feature. Researcher correctly identified item E1 (HERMIN chat bubble) as an OS/Hermes layer artifact. |
| R-4 | Development header badge `Building... → Done` transition lag | Implement the transition explicitly: status badge updates to `Done` 500ms after the final card flips `Done` (matches SPEC's overall easing budget). |
| R-5 | Parent-page `Advanced` button (scene02/49) | Out of scope. Parent page (`Define Research Objective` view) is not built; we ship `Coding Agent Swarm` view (SPEC §3+). |
| R-6 | `Timeline View` toggle in Execution graph never activated | Implement minimal placeholder: vertical list of `<node_label> (Cost: N)` ordered by graph topology. Hive 6 polish item if Hive 5 flags. |
| R-7 | Open Q on `Request Revision` flow | Resolved by §15.1 default above (inline panel with textarea). |
| R-8 | Open Q on Agent Activity content | Resolved by §15.5 default above (polling table). |
| R-9 | Open Q on Tasks drag-drop backend contract | Resolved by §15.3 default above (PATCH column + PATCH assigned_to). |

---

## 5. Rollback playbook (consolidated)

For every high-impact risk, the rollback path is concrete:

| Risk | Rollback action | Restoration time |
|---|---|---|
| R1 (ms mutation) | `cp ~/.hermes/ruflo-goap-control/projects.json.bak.<TS> ~/.hermes/ruflo-goap-control/projects.json && systemctl --user restart hermes-dashboard.service` | < 2 minutes |
| R2 (React contract broken) | `cp dashboard/dist/index.js.bak.<TS> dashboard/dist/index.js && systemctl --user restart hermes-dashboard.service` | < 2 minutes |
| R3 (fd exhaustion) | revert SSE PR; fall back to existing snapshot `/runs/{id}/logs` polling | revert PR + restart: < 5 minutes |
| R4 (schema break) | revert Hive 2's `models.py` PR; restore from `projects.json.bak.<TODAY>` | < 5 minutes |
| R5 (symlink) | re-run the symlink command from `hermes-dashboard-venv-shadows-source` memory | < 1 minute |
| R6 (env leak) | replace affected subprocess call to use `_env()`; redeploy; no data restoration needed (the leak is forward-looking) | < 10 minutes |
| R7 (Hive 5 critical) | chain auto-halts at Decision Gate 3; nothing merged; triage from pause state | n/a (no rollback needed — nothing shipped) |
| R8 (SVG perf) | revert graph component; ship `<table>` fallback | < 30 minutes (fallback ships immediately, polish later) |
| R9 (kanban CLI) | Tasks sub-tab falls back to stub data + banner; existing routes unaffected | automatic via defensive parser; no manual rollback |
| R10 (6th tab) | no rollback needed; forward-add only if Hive 5 demands | n/a |

Every high-impact risk has a < 30 min rollback. The chain's per-hive PR-stacking pattern (per `GAMEPLAN.md`) means we can revert individual hives without unwinding the whole build.

---

## 6. What we are NOT mitigating

Honest disclosure of accepted risks:

- **Mid-chain Hermes update:** if `hermes update` runs between hives and changes the kanban CLI format, we'll discover it in Hive 5. Mitigation is preventative (preflight check) but not exhaustive.
- **Joseph being asleep during the chain:** chain.sh pings Telegram on every decision gate, but if Joseph misses the ping, the chain pauses (at H4) or halts (at gate-2 failure) until he resumes. This is by design.
- **Browser MCP availability for Hive 5:** if browser MCP is unavailable, Hive 5 can't run. Mitigation: chain pauses at H4-done and Telegram-pings Joseph who can manually run the stress test pattern (it's documented in `hive5-stress-test/INSTRUCTIONS.md`).
- **Disk space:** sidecar files and per-project workdirs grow over time. We don't add an auto-cleanup. Joseph manually prunes via `DELETE /projects/{id}` for terminal-state projects.
- **The 13th project (`zasd`):** there's a `zasd` project staged but never started. We treat it as terminal-staged. No special handling.

---

**End of RISKS.md.**
