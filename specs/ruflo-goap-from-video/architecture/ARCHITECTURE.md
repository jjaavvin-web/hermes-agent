# ARCHITECTURE.md — Ruflo GOAP Operator Console

**Hive:** 1 — ARCHITECT
**Audience:** Hives 2 (BACKEND-CORE), 3 (REAL-RUFLO-WIRING), 4 (FRONTEND), 5 (STRESS-TEST), 6 (POLISH)
**Source spec:** `/home/josep/.hermes/specs/ruflo-goap-from-video/SPEC.md` (the 16-section breakdown of the goal.ruv.io Coding Agent Swarm video)
**Existing plugin:** `/home/josep/.hermes/plugins/ruflo-goap-control/` (`dashboard/plugin_api.py` 767 LOC, `dashboard/dist/index.js` 41KB IIFE, `dashboard/dist/style.css` 10KB, 4 pytest closeout tests at `tests/test_plugin_api_closeout.py`)
**Live runtime:** `~/.hermes/ruflo-goap-control/projects.json` — 13 registered projects, 4 currently flagged `running` (the `ms` Medical project is the canonical live run flagged read-only by the chain).

---

## 1. What we're building (executive summary)

We are turning the existing `ruflo-goap-control` Hermes dashboard plugin into the production operator console for real Ruflo+Claude swarms, with the visual language and interaction model from the `goal.ruv.io/agents` Coding Agent Swarm video. The plugin already exposes the safety-correct slice (12 routes for blueprint, readiness, planning, project staging, registered-run start/stop/logs/final-report, action log, widget embed) and ships a hand-rolled IIFE React bundle that the Hermes dashboard mounts at `/ruflo-goap`. We are not throwing any of that away. We are extending the backend with the screen-specific endpoints SPEC.md implies (per-run research phases, dev phases, task board, execution graph, quality gates, live log SSE, persisted advanced config) and rebuilding the frontend to match SPEC §3–§14 verbatim — empty state, GOAP State Assessment progress, 5-phase research progress, Research-Complete summary, Research↔Development mode toggle, the 5 sub-tabs (Dashboard, Tasks, Execution, Quality, Logs) in both modes, and the Advanced Agent Configuration modal with all 4 sub-tabs.

What we are explicitly NOT doing: we are not cloning goal.ruv.io's backend bit-for-bit, we are not replacing the safety model (registered-IDs-only, allowlisted root, scrubbed launch env, no arbitrary shell, dry-run-available), and we are not regressing the 4 existing pytest tests or the 13 staged projects in the live registry. Every SPEC behavior is implemented on top of the existing safety primitives, and where the SPEC's UX implies behavior that would weaken safety (e.g., "type a coding objective and we'll spawn an autonomous swarm with one click"), the safety model wins and we add a visible confirmation step. The full conflict register is in `RISKS.md` §"Safety model conflicts".

---

## 2. Current state inventory

### 2.1 Existing API routes (12)

Captured via `curl -s http://127.0.0.1:9119/openapi.json` against the live dashboard. All paths prefixed with `/api/plugins/ruflo-goap-control`.

| # | Method | Path | Source | Disposition |
|---|---|---|---|---|
| 1 | GET | `/blueprint` | `plugin_api.py:461-479` | **KEEP** — static blueprint metadata; cheap and harmless |
| 2 | GET | `/readiness` | `plugin_api.py:482-524` | **KEEP** — readiness check is invoked by SPEC §3 empty state to render "service alive" indicator |
| 3 | GET | `/agents/spec` | `plugin_api.py:527-545` | **KEEP / MODIFY** — extend with the labels for SPEC §11 modal sub-tabs |
| 4 | POST | `/plan/generate` | `plugin_api.py:548-552` | **MODIFY** — extend response with `research_phases[5]`, `execution_plan_summary`, `status_cards[4]` shaped per SPEC §4b + §5 |
| 5 | GET | `/widget/embed` | `plugin_api.py:555-579` | **KEEP** — used by parent `/agents` widget embed flow |
| 6 | GET | `/projects` | `plugin_api.py:582-591` | **KEEP** — backs the project picker; runtime sidecar already merged |
| 7 | POST | `/projects/stage` | `plugin_api.py:594-660` | **MODIFY** — accept the full advanced config object and persist it into `project.advanced_config` (already partial today; ensure round-trip) |
| 8 | POST | `/runs/{run_id}/start` | `plugin_api.py:663-702` | **KEEP** — the "Approve & Launch Development" button hits this with the `START_RUFLO_GOAP_RUN` confirm phrase |
| 9 | POST | `/runs/{run_id}/stop` | `plugin_api.py:705-740` | **KEEP** — used for graceful abort and "Regenerate Plan" cleanup |
| 10 | GET | `/runs/{run_id}/logs` | `plugin_api.py:743-749` | **KEEP** — file-tail snapshot used as initial-state for log sub-tab |
| 11 | GET | `/runs/{run_id}/final-report` | `plugin_api.py:752-760` | **KEEP** — used by Quality/Dashboard sub-tabs when a run has finished |
| 12 | GET | `/action-log` | `plugin_api.py:763-766` | **KEEP** — used by an "operator events" affordance in the Logs sub-tab |

**Zero routes are being deprecated.** Every existing route maps to something SPEC.md asks for, and several get extended response shapes. The 4 pytest tests in `tests/test_plugin_api_closeout.py` exercise `_render_templates`, `_runtime`, `_safe_file_under_workdir`, and the watcher template's status-sidecar logic — those internals are untouched.

### 2.2 Frontend bundle (1 file × 2)

| File | Size | Disposition |
|---|---|---|
| `dashboard/dist/index.js` | 41,487 B | **REPLACE** — hand-rolled IIFE; build a new bundle from a real Vite+TSX source tree, output as IIFE with React external |
| `dashboard/dist/style.css` | 10,172 B | **REPLACE** — build via Tailwind CLI from new source; ship precompiled CSS so we don't need PostCSS at runtime |
| `dashboard/manifest.json` | 447 B | **KEEP / bump `version` 0.1.0→0.2.0** — `entry`, `css`, `api` fields are correct |
| `dashboard/README.md` | 527 B | **MODIFY** (Hive 6) — rewrite to describe the production console, all routes, safety model, dev-loop |

### 2.3 Data files (4)

| File | Disposition | Notes |
|---|---|---|
| `~/.hermes/ruflo-goap-control/projects.json` | **MODIFY (additive)** | New optional keys per project: `research_phases`, `dev_phases`, `tasks`, `quality`, `advanced_config_version`. Existing 13 projects continue to validate; the `ms` running project is read-only for the duration of this build. Schema details in `DATA-MODEL.md`. |
| `~/.hermes/ruflo-goap-control/logs/actions.jsonl` | **KEEP / EXTEND** | Append-only event log; add new event kinds (`plan_revise`, `dev_phase_advance`, `task_assign`, `quality_run`, `config_save`). |
| `~/.hermes/scripts/templates/ruflo-launch.template.sh` | **KEEP** | Already API-key-scrubs, already creates kanban breadcrumb. Hive 3 will template a per-project `LAUNCH.sh` from this, not edit it. |
| `~/.hermes/scripts/templates/ruflo-watcher.template.sh` | **KEEP** | Already writes `.ruflo-status.json` sidecar and handles BLOCKED reports; tested by `test_watcher_template_writes_status_sidecar_and_handles_blocked_reports`. |

### 2.4 Test surface

| Test | Disposition |
|---|---|
| `test_render_templates_creates_launch_prompt_and_exports_hermes` | **KEEP** — exercises template substitution invariants Hive 3 depends on |
| `test_runtime_prefers_watcher_status_sidecar_over_stale_running_status` | **KEEP** — exercises the effective-status fallback chain |
| `test_safe_file_allowlist_exposes_launch_prompt_and_status_sidecar` | **KEEP** — proves path-traversal blocked and allowlist is correct |
| `test_watcher_template_writes_status_sidecar_and_handles_blocked_reports` | **KEEP** — proves the BLOCKED-report short-circuit works |

All 4 tests must continue to pass after each hive. Hive 2 adds ≥1 new test per new route + ≥1 safety test per new attack surface. Hive 4 adds React Testing Library smoke tests. Coverage target ≥ 75% on the new code (per `GAMEPLAN.md` success criterion #10).

---

## 3. Target state — route-by-route

After all hives ship, the plugin exposes the routes in §2.1 **plus** the following new routes. All paths prefixed with `/api/plugins/ruflo-goap-control`.

### 3.1 Planning (extends existing)

```yaml
POST /plan/generate          # extended — see §5.1 for the new response shape
POST /plan/revise            # NEW — handles SPEC §5 "Request Revision" button
GET  /plan/{run_id}          # NEW — fetch the persisted plan for a staged/running project
```

### 3.2 Live progress (research + development modes)

```yaml
GET  /runs/{run_id}/research-phases       # NEW — array of 5 phases per SPEC §4b
GET  /runs/{run_id}/dev-phases            # NEW — array of 5 phases per SPEC §6
GET  /runs/{run_id}/goap-state            # NEW — SPEC §4a state-assessment percentages
GET  /runs/{run_id}/progress/stream       # NEW — SSE; pushes phase/state updates as they change
```

### 3.3 Tasks sub-tab (SPEC §7)

```yaml
GET  /runs/{run_id}/tasks                          # NEW — kanban columns + cards
POST /runs/{run_id}/tasks/{task_id}/assign         # NEW — drag-and-drop assign agent
GET  /runs/{run_id}/tasks/dependencies             # NEW — directed edges for the dependency graph
```

### 3.4 Execution sub-tab (SPEC §8 + §8b)

```yaml
GET  /runs/{run_id}/execution/plan         # NEW — nodes + cost badges + edges, mode-aware
GET  /runs/{run_id}/execution/activity     # NEW — per-agent currently-running step
GET  /runs/{run_id}/execution/event-log    # NEW — timeline events
```

### 3.5 Quality sub-tab (SPEC §9)

```yaml
GET  /runs/{run_id}/quality/gates          # NEW — Compile / TestCoverage / SecurityScan
GET  /runs/{run_id}/quality/metrics        # NEW — research-mode Completeness/Coverage/Readiness
POST /runs/{run_id}/quality/run            # NEW — re-run gates on demand (admin / debug)
```

### 3.6 Logs sub-tab (SPEC §10)

```yaml
GET  /runs/{run_id}/logs                   # KEEP — initial snapshot
GET  /runs/{run_id}/logs/stream            # NEW — SSE; tails hive-mind.log + watcher.log
```

### 3.7 Advanced Agent Configuration modal (SPEC §11)

```yaml
GET  /config                               # NEW — global default config
PUT  /config                               # NEW — write global default config
GET  /config/presets                       # NEW — the 4 preset bundles (dev/prod/budget/quality)
POST /config/validate                      # NEW — validate without persisting; returns normalized shape
GET  /config/{run_id}                      # NEW — per-project override
PUT  /config/{run_id}                      # NEW — write per-project override
```

**REGISTRATION ORDER IS LOAD-BEARING.** FastAPI matches routes in registration order. The literal-path routes (`/config`, `/config/presets`, `/config/validate`) MUST be registered BEFORE the parameterized `/config/{run_id}`, otherwise `GET /config/presets` will be routed to `GET /config/{run_id}` with `run_id="presets"` and return 400 from `_validate_run_id`. Hive 2 wires routes in the exact order shown above. A test in `tests/test_routes.py` asserts `curl /api/plugins/ruflo-goap-control/config/presets` returns the presets dict, not 400.

### 3.8 House-keeping additions

```yaml
DELETE /projects/{run_id}                  # NEW — operator-confirm delete (NOT for `ms`)
POST   /runs/{run_id}/regenerate-plan      # NEW — wraps stop + new plan + restage scaffold
```

**Total new routes: 22.** Combined with the existing 12, the final surface is 34 routes. Every one is enumerated by the API contract in §6 with method, path, request schema, response schema, and error cases.

---

## 4. Frontend component tree

The plugin continues to mount via `window.__HERMES_PLUGINS__.register({...})` using `window.__HERMES_PLUGIN_SDK__.React` (host-provided; the bundle MUST NOT include its own React). The new source tree lives at `~/.hermes/plugins/ruflo-goap-control/dashboard/src/` and builds via Vite to `dist/index.js` + `dist/style.css`. The IIFE wrapper, the externals (React, ReactDOM), and the registration call are the contract with the host shell.

```
<RufloGoapTab>                                  // root mount; reads session token, owns global state via Zustand
├── <TabHeader>                                 // "Coding Agent Swarm" + subtitle + "← Back to Research" link
├── <CodingObjectivePanel>                      // §3 panel — always visible
│   ├── <ObjectiveInput>                        // text input with placeholder, ↵ submits
│   ├── <AdvancedSettingsButton>                // opens <AdvancedConfigModal>
│   ├── <GenerateOrRegenerateButton>            // "Generate Plan" empty → "Regenerate Plan" after launch
│   ├── <CategoryChipRow>                       // §2 Finance/Business/.../AI&ML chips
│   └── <EmptyStateCard>                        // "Ready to Plan" card per SPEC §3; rendered when plan==null
├── <ModeSwitch>                                // hidden until plan exists; toggles Research ↔ Development
├── <ResearchView>                              // visible when mode === "research"
│   ├── <GoapStateAssessment>                   // §4a percentage counter + 2-column state lists
│   ├── <ResearchPhaseProgress>                 // §4b 5 phase cards transitioning
│   ├── <ResearchCompleteSummary>               // §5 — 4 status cards + execution-plan-summary + action bar
│   └── <SubTabs mode="research">               // dashboard/tasks/execution/quality/logs (research variants)
├── <DevelopmentView>                           // visible when mode === "development"
│   ├── <DevSwarmProgress>                      // §6 5 dev phase cards
│   └── <SubTabs mode="development">            // dashboard/tasks/execution/quality/logs (dev variants)
├── <AdvancedConfigModal>                       // §11 — controlled-open, backdrop blur
│   ├── <PresetRow>                             // 4 preset chip-buttons
│   ├── <ConfigTabs>                            // Swarm / GOAP / Execution / Model
│   ├── <ModalFooter>                           // Reset to Defaults | Save Configuration
└── <ToastHost>                                 // bottom-right; consumes Zustand "toasts" slice
```

`SubTabs` is generic over the mode. Each tab is its own component:

```
<SubTabs mode={mode} runId={runId}>
├── <DashboardSubTab mode runId>     // §6 in dev mode; §5 summary in research mode
├── <TasksSubTab runId>              // §7 kanban + dependency graph
├── <ExecutionSubTab mode runId>     // §8 dev / §8b research; inner tabs vary
│   ├── <ExecutionPlanInnerTab>      // graph + timeline view toggle
│   ├── <CurrentStepInnerTab>        // (dev only)
│   ├── <AgentActivityInnerTab>      // both modes
│   └── <EventLogInnerTab>           // (dev) / EventTimeline (research)
├── <QualitySubTab mode runId>       // §9 gates + (research only) metrics
└── <LogsSubTab runId>               // §10 SSE-bound log stream
```

**State lives in Zustand.** Slices: `selectedProject`, `plan`, `researchPhases`, `devPhases`, `goapState`, `tasks`, `executionPlan`, `qualityGates`, `logs`, `config`, `mode` (research|development), `modalOpen`, `toasts`. SSE subscriptions update slices on push; sub-tabs subscribe via `useStore(state => state.slice)` selectors. Loading and error states live per-slice (`tasks.loading`, `tasks.error`) so each panel can render its own loading/error/empty affordance.

**Props flow** is shallow: `<SubTabs>` passes `mode` and `runId` to children; children pull data from Zustand directly. We deliberately avoid drilling state through component trees because the SPEC has multiple panels reading the same slices.

**Why Zustand and not Redux/Recoil/Jotai**: zero provider boilerplate, ~3KB, works without ReactDOM hooks contracts, and the existing IIFE bundle pattern is friendlier to a library that doesn't need a Provider wrapping the whole tree. Justification: this is one of two new deps proposed; the other is React Flow (§7), and React Flow we're going to *reject* in favor of hand-rolled SVG.

---

## 5. Response shapes for the SPEC's screens

This section pins down the request/response shapes for the routes most likely to be ambiguous. Full pydantic models are in §6.

### 5.1 `POST /plan/generate` — extended response

The current implementation (`plugin_api.py:548-552` → `_build_plan` at lines 372-432) returns a deterministic plan with `summary`, `goap`, `swarm`, `steps`, `quality_gates`, `expected_artifacts`. We extend it to include the SPEC §4b research phases and §5 status cards so the frontend can render the whole "Generate Plan" flow from a single response.

```python
class ResearchPhase(BaseModel):
    id: str                           # "P1".."P5"
    name: str                         # "Goal Assessment" etc.
    description: str
    sub_steps: list[str]              # 2-3 bullets per SPEC §4b
    outputs: list[str]                # 2 metric strings per phase
    status: Literal["pending","researching","complete"] = "pending"

class StatusCard(BaseModel):
    key: Literal["goal_assessment","architecture","implementation","testing"]
    label: str
    status: Literal["completed","ready","blocked"]
    summary: str

class ExecutionPlanSummary(BaseModel):
    total_phases: int
    estimated_duration_seconds: int
    agents_required: int
    complexity: Literal["Low","Medium","High"]

class PlanResponse(BaseModel):
    ok: bool
    timestamp: str
    blueprint: str
    mode: str
    category: str
    objective: str
    summary: str
    goap: dict
    swarm: dict
    steps: list[dict]
    quality_gates: list[str]
    expected_artifacts: list[str]
    # NEW:
    research_phases: list[ResearchPhase]              # §4b — 5 entries
    goap_state_assessment: dict                       # §4a — start_state, goal_state, transitions
    status_cards: list[StatusCard]                    # §5 — 4 entries
    execution_plan_summary: ExecutionPlanSummary      # §5
```

### 5.2 `GET /runs/{run_id}/progress/stream` — SSE shape

```
event: research_phase_update
id: 17050000-research-P2
data: {"phase_id":"P2","status":"researching","at":"2026-05-18T18:14:59Z"}

event: goap_state_update
id: 17050000-goap-63
data: {"percent":63,"complete":5,"total":8,"transitioned":["architecture planned"]}

event: dev_phase_update
id: 17050000-dev-D1
data: {"phase_id":"D1","status":"done","outputs":{"files_created":12,"dependencies":24}}

event: heartbeat
id: 17050009-hb
data: {"ts":"2026-05-18T18:15:09Z"}
```

Single SSE channel multiplexes all progress event kinds. Heartbeat every 30s so the frontend can detect dead connections. Backpressure handled by FastAPI's `StreamingResponse` generator; if the client disconnects the generator raises and we close the file handles.

**Reconnect / cursor semantics (C-1 mitigation).** Each SSE event carries an `id:` line of the form `<ts_unix>-<kind>-<key>`. When the browser's native `EventSource` reconnects after the 600s max-connection-age expiry, it sends `Last-Event-Id: <last_id>` automatically. The server's `progress_stream` generator:
1. Reads the cursor from `Last-Event-Id` header, parses the leading `<ts_unix>` integer.
2. Re-emits all events from the sidecar files (`$WORKDIR/.research-phases.json` etc.) whose `updated_at` is GREATER THAN the cursor — NOT from the beginning.
3. Falls back to "emit current state once" if `Last-Event-Id` is absent or unparseable.

Additionally, the frontend's Zustand reducer is idempotent on phase-status updates: writing `phase.status="complete"` over an already-complete phase is a no-op. A reducer that receives a stale "researching" event for a phase already marked `complete` IGNORES it (rule: a phase's status can only advance forward, never regress). This dual server+client guard ensures replayed events do not corrupt mid-run state.

### 5.3 `GET /runs/{run_id}/tasks` — kanban shape

```python
class TaskCard(BaseModel):
    id: str                                # kanban card id, e.g. "t_de470bcd"
    title: str
    role: Literal["Architecture","Implementation","Testing","Documentation","Code Review","DevOps"]
    priority: Literal["low","medium","high"]
    column: Literal["todo","in_progress","blocked","done"]
    assigned_to: Optional[str]             # agent name or null

class TasksResponse(BaseModel):
    ok: bool
    run_id: str
    columns: dict[str, int]                # counts per column
    cards: list[TaskCard]
```

Hive 3 wires this to the real `hermes kanban` CLI; Hive 2 stubs it with the 6 SPEC §7 cards so the frontend can develop against it.

### 5.4 `GET /runs/{run_id}/execution/plan` — graph shape

```python
class ExecutionNode(BaseModel):
    id: str
    label: str
    cost: int
    status: Literal["pending","active","done"]

class ExecutionEdge(BaseModel):
    from_id: str  = Field(..., alias="from")
    to_id: str    = Field(..., alias="to")

class ExecutionPlanResponse(BaseModel):
    ok: bool
    run_id: str
    mode: Literal["research","development"]
    total_actions: int
    total_cost: int
    estimated_minutes: int
    nodes: list[ExecutionNode]
    edges: list[ExecutionEdge]
```

The nodes are a simple linear chain in both research (5 nodes per SPEC §8b) and dev (5 nodes per SPEC §8). Frontend renders as hand-rolled SVG (see §7 — Tech stack decisions).

### 5.5 `GET /runs/{run_id}/quality/gates`

```python
class QualityGate(BaseModel):
    key: Literal["compile_check","test_coverage","security_scan"]
    label: str
    current: Optional[float]               # null for binary checks
    threshold: Optional[float]
    status: Literal["passed","failed","pending","skipped"]
    evidence_path: Optional[str]

class QualityGatesResponse(BaseModel):
    ok: bool
    run_id: str
    gates: list[QualityGate]
    ran_at: Optional[str]
```

### 5.6 Config

```python
class SwarmConfig(BaseModel):
    topology: Literal["hierarchical-mesh","mesh","star","ring"] = "hierarchical-mesh"
    max_agents: int = Field(10, ge=1, le=20)
    distribution: Literal["adaptive","round-robin","priority"] = "adaptive"
    auto_scaling: bool = True
    min_agents: int = Field(2, ge=1)
    max_agent_cap: int = Field(20, ge=1, le=20)
    scale_up: int = Field(80, ge=0, le=100)
    scale_down: int = Field(20, ge=0, le=100)

class GoapConfig(BaseModel):
    algorithm: Literal["A* - optimal pathfinding","Dijkstra","Greedy"] = "A* - optimal pathfinding"
    heuristic: str = "Manhattan Distance / prerequisite distance"
    cost: str = "Hybrid effort/risk/latency"
    optimize: bool = True
    parallel_actions: bool = True
    remove_redundant: bool = True

class ExecutionConfig(BaseModel):
    strategy: Literal["adaptive","aggressive","conservative"] = "adaptive"
    max_parallel_tasks: int = Field(5, ge=1, le=20)
    timeout_seconds: int = Field(300, ge=10, le=7200)
    quality_gates: bool = True

class ModelConfig(BaseModel):
    primary_provider: Literal["Claude Code OAuth/Max","Anthropic API","Gemini","OpenAI"] = "Claude Code OAuth/Max"
    routing: Literal["balanced","cost","quality","latency"] = "balanced"
    max_cost: float = Field(0.0, ge=0.0, le=100.0)    # 0 == OAuth-only (no paid fallback)
    fallback: bool = False

class AdvancedConfig(BaseModel):
    preset: Literal["development","production","budget","quality","custom"] = "production"
    swarm: SwarmConfig
    goap: GoapConfig
    execution: ExecutionConfig
    model: ModelConfig
    version: int = 1
```

The `Literal` types here become OpenAPI enums for the frontend to drive its dropdowns. The `max_cost: 0.0` default enforces the Hermes posture (Claude OAuth/Max only; no paid API fallback) — increasing this requires explicit opt-in, which is what the modal's slider in §11d controls.

---

## 6. API contract (full)

Pydantic schemas referenced above. Below is the OpenAPI-flavored route summary for the 22 new routes (existing 12 are unchanged in signature; only `/plan/generate` and `/projects/stage` change in response/request shape).

```yaml
# Planning ----------------------------------------------------------
POST /plan/generate:
  request: PlanBody (existing) + optional config: AdvancedConfig
  response: PlanResponse (extended — see §5.1)
  errors: 400 (objective too short), 422 (config invalid)

POST /plan/revise:
  request: { run_id: str, revision_notes: str (max 4000 chars) }
  response: PlanResponse  # re-runs _build_plan with notes folded into objective
  errors: 404 (run not found), 409 (run already started)

GET /plan/{run_id}:
  response: PlanResponse
  errors: 404

# Live progress -----------------------------------------------------
GET /runs/{run_id}/research-phases:
  response: { ok: bool, run_id: str, phases: list[ResearchPhase] }
  errors: 404

GET /runs/{run_id}/dev-phases:
  response: { ok: bool, run_id: str, phases: list[DevPhase] }
  errors: 404

GET /runs/{run_id}/goap-state:
  response: { ok: bool, percent: int, complete: int, total: int,
              system_state: list[StateItem], goal_state: list[StateItem],
              transitions: list[Transition], action_plan: list[str] }
  errors: 404

GET /runs/{run_id}/progress/stream:
  response: text/event-stream (SSE) — see §5.2
  errors: 404
  notes: heartbeat 30s, max-connection-age 600s, client must reconnect after that

# Tasks -------------------------------------------------------------
GET /runs/{run_id}/tasks:
  response: TasksResponse (§5.3)
  errors: 404

POST /runs/{run_id}/tasks/{task_id}/assign:
  request: { agent: str (allowlisted), column: Optional[str] }
  response: { ok: bool, task: TaskCard }
  errors: 400 (agent not allowlisted), 404, 409 (card moved by external source)
  notes: |
    Agent allowlist is built by reading plan.swarm.agents from the project's
    GOAP-PLAN.json and projecting field .name (typed as SwarmAgent in
    DATA-MODEL.md §2.2). Reject any agent name not in that set.
    Reference impl: agents = {a["name"] for a in plan["swarm"]["agents"]}

GET /runs/{run_id}/tasks/dependencies:
  response: { ok: bool, edges: list[{from: str, to: str}] }
  errors: 404

# Execution ---------------------------------------------------------
GET /runs/{run_id}/execution/plan?mode=research|development:
  response: ExecutionPlanResponse (§5.4)
  errors: 404, 400 (bad mode)

GET /runs/{run_id}/execution/activity:
  response: { ok: bool, agents: list[{name: str, current_step: str|null, since: str|null}] }
  errors: 404

GET /runs/{run_id}/execution/event-log?since=ISO8601:
  response: { ok: bool, events: list[ExecutionEvent] }
  errors: 404, 400 (bad since)

# Quality -----------------------------------------------------------
GET /runs/{run_id}/quality/gates:
  response: QualityGatesResponse (§5.5)
  errors: 404

GET /runs/{run_id}/quality/metrics:
  response: { ok: bool, completeness: float, coverage: str, readiness: Literal[...] }
  errors: 404

POST /runs/{run_id}/quality/run:
  request: { gates: list[str] (allowlisted: compile_check|test_coverage|security_scan) }
  response: QualityGatesResponse
  errors: 404, 400, 409 (a gate is already running)

# Logs --------------------------------------------------------------
GET /runs/{run_id}/logs/stream?file=hive-mind.log|watcher.log:
  response: text/event-stream — emits each new line as { ts, glyph, message }
  errors: 404, 400 (file not allowlisted)
  notes: identical allowlist to `_safe_file_under_workdir` at plugin_api.py:274-281

# Config ------------------------------------------------------------
GET /config:
  response: { ok: bool, config: AdvancedConfig }
PUT /config:
  request: AdvancedConfig
  response: { ok: bool, config: AdvancedConfig, persisted_at: str }
  errors: 422
GET /config/{run_id}:
  response: { ok: bool, config: AdvancedConfig, source: "global"|"override" }
  errors: 404
PUT /config/{run_id}:
  request: AdvancedConfig
  response: { ok: bool, config: AdvancedConfig, persisted_at: str }
  errors: 404, 422, 403 (project slug == 'ms')
  notes: |
    The `ms` slug guard from DELETE applies here too. PUT to ms returns 403
    with detail "live ms project is read-only — wait for terminal status".
    Writing to a different project's override never affects ms.
GET /config/presets:
  response: { ok: bool, presets: dict[str, AdvancedConfig] }     # dev/prod/budget/quality
POST /config/validate:
  request: AdvancedConfig
  response: { ok: bool, config: AdvancedConfig (normalized), warnings: list[str] }
  errors: 422
  notes: |
    "Normalized" means: typed-string enums case-corrected; missing optional
    fields populated with section defaults from the resolved preset.
    Out-of-range numeric fields are NEVER silently clamped — they raise 422
    with the validation error in `detail`. This makes validate's success
    behavior identical to PUT's success behavior: same payload, same outcome.
    Warnings array carries non-fatal advisories (e.g., "max_cost > 0 means
    paid API fallback is enabled; consider OAuth/Max-only for safety").

# House-keeping -----------------------------------------------------
DELETE /projects/{run_id}:
  request: { confirm: "DELETE_RUFLO_GOAP_PROJECT" }   # mirrors START/STOP confirm pattern
  response: { ok: bool, run_id: str, removed: bool, backup_path: str }
  errors: 400 (no confirm), 403 (run is currently `running` per _runtime), 404
  notes: |
    REFUSES to delete the live `ms` project by hard-coded slug exclusion
    until the project moves to a terminal status. On successful delete:
    (1) the project record's sidecar path fields (research_phases_path,
        dev_phases_path, tasks_path, quality_path, config_override_path,
        plan_file, hive_log, watcher_log, final_report) are nulled
        BEFORE the workdir is moved, so subsequent reads of the record
        cannot follow dangling paths;
    (2) deleted_at is set to ISO 8601 now;
    (3) the project record is RETAINED in projects.json (soft-delete) but
        is filtered out of GET /projects by default. Pass
        ?include_deleted=true to surface them (for audit/restore).

POST /runs/{run_id}/regenerate-plan:
  request: { confirm: "REGENERATE_RUFLO_GOAP_PLAN" }
  response: { ok: bool, run_id: str, plan: PlanResponse }
  errors: 404, 409 (run is currently active — must stop first)
```

Every new route validates `run_id` against the existing `_validate_run_id` regex (`plugin_api.py:127-130`) before doing any work. Every confirm-phrase mirrors the existing START/STOP convention. Every file path goes through `_safe_file_under_workdir` or its config-store equivalent. No route accepts a raw shell argument.

---

## 7. Real-Ruflo integration points (for Hive 3)

This section is Hive 3's specification. Hive 2 will stub each integration with a deterministic fake that returns SPEC-shaped data; Hive 3 swaps each stub for a real implementation.

### 7.1 `goap_planner.real_plan(objective, config)`

- **Source:** extend `_build_plan` (`plugin_api.py:372-432`) to populate `research_phases` from a template parameterized by `objective` + `config.swarm.max_agents`.
- **No LLM call in v1.** The deterministic template covers the SPEC's flow. v2 (out of scope for this build) can swap in `claude -p` for natural-language plan synthesis.
- **Persistence:** plan written to `$WORKDIR/GOAP-PLAN.json` (already done by `stage_project`) AND mirrored into `project.plan` on `projects.json` (additive).

### 7.2 `task_store.real_sync(run_id)`

- **CLI:** `hermes kanban --board hermes-kanban-control ls --label goap-run:<run_id> --json`
- **Mapping:** kanban card status → kanban column: `todo|in_progress|blocked|done` map straight; map labels `role:Architecture`, `priority:high` to TaskCard fields.
- **Drag-drop write-back:** `hermes kanban --board hermes-kanban-control assign <card_id> <agent>` and `hermes kanban --board hermes-kanban-control move <card_id> <column>`. Both invoked via `subprocess.run` with explicit arg lists (no shell=True).
- **Allowlist:** agent names must match one of `swarm.agents[].name` from the plan. Reject unknown agents with 400.

### 7.3 `log_stream.tail_sse(run_id, file)`

- **Algorithm:** open `$WORKDIR/{file}` in binary mode, seek to end on connect, then `select.select([fd], [], [], 1.0)` loop. New bytes are split on `\n`, decoded, sanitized via existing `_sanitize_text`, and yielded as SSE `data:` frames with 200ms debounce so we don't emit one frame per line under burst.
- **Backpressure:** `StreamingResponse` generator; if FastAPI raises `ClientDisconnect`, close the fd and exit.
- **Allowlist:** `file` must be in the existing allowlist at `plugin_api.py:275`. Reuse `_safe_file_under_workdir`.
- **Reconnect:** SSE clients reconnect automatically on close. We add a `Last-Event-Id` header so the client can request bytes since a cursor; on the server, treat the cursor as a byte offset into the file and clamp to file size.

### 7.4 `quality_runner.run(run_id, gates)`

- **Tools invoked:** `ruff check $WORKDIR/`, `pytest $WORKDIR/tests/ --maxfail=5 --tb=no -q --json-report --json-report-file=$WORKDIR/.pytest.json`, `bandit -r $WORKDIR/ -f json -o $WORKDIR/.bandit.json`.
- **All invocations:** explicit arg list, scrubbed env (reuse `_env()`), 5-minute timeout per tool, cwd = `$WORKDIR`.
- **ENV_SCRUB gap to fix (C-9 from review):** the existing `ENV_SCRUB` constant at `plugin_api.py:36-42` lists ANTHROPIC_* + CLAUDE_CODE_OAUTH_TOKEN + OPENAI_API_KEY but DOES NOT include `GEMINI_API_KEY`. The GAMEPLAN safety rule 5 explicitly lists `GEMINI_*` as something to scrub. Hive 2 adds `GEMINI_API_KEY` (and any other `GEMINI_*` vars in the launcher's exec env) to `ENV_SCRUB` before Hive 3 wires the subprocess quality runner. A test in `tests/test_safety.py` sets a fake `GEMINI_API_KEY=test-key`, invokes any new subprocess-using route, and asserts the child env does NOT contain it.
- **Mapping:** `compile_check` → ruff `exit 0`. `test_coverage` → pytest pass/total ratio (% threshold from config). `security_scan` → bandit issue count divided by file count, inverted (% threshold from config).
- **Concurrency:** one quality run per project at a time; track via `$WORKDIR/.quality-running.pid`.

### 7.5 `runner.start` real wiring

- **Already real** (`plugin_api.py:663-702` invokes the rendered `launch.sh` via subprocess). No change needed beyond ensuring `LAUNCH.sh` and `watcher.sh` carry the project's `advanced_config` into Ruflo via `RUFLO_GOAP_CONFIG=$WORKDIR/.config.json` env var.
- **Smoke test:** Hive 3's `scripts/smoke-e2e.sh` (per `hive3-ruflo-wiring/objective.md`) stages a throwaway project, hits `/runs/<id>/start`, polls `/runs/<id>/progress/stream`, verifies a tmux session named `rfg-*` exists, then stops cleanly.

### 7.6 Live event surfaces

Two files Ruflo writes that Hive 3 must subscribe to:
- `$WORKDIR/hive-mind.log` — appended by the spawned `ruflo hive-mind spawn ... | tee hive-mind.log` pipe in the launcher
- `$WORKDIR/.ruflo-status.json` — written by `watcher.sh` (status sidecar; tested by `test_watcher_template_writes_status_sidecar_and_handles_blocked_reports`)

A third file lives in the audit dir glob per the `watcher-must-glob-audit-dir` lesson — but in this plugin's launch flow, `FINAL-REPORT.md` is written by Claude/Ruflo directly into `$WORKDIR`, not into a separate audit dir. The watcher already checks `$WORKDIR/FINAL-REPORT.md` (`ruflo-watcher.template.sh` line near `if [[ -f "$WORKDIR/FINAL-REPORT.md" ]]`), so the lesson applies to the *next* layer (the chain.sh expecting `$HIVE1/FINAL-REPORT.md`) not the dashboard's per-project polling. This hive's `FINAL-REPORT.md` MUST go at `$WORKDIR` root, not in any subdirectory — per the `6f42c8b1-ff20-4e58-8cb0-baef778b89f6` watcher-path-bug lesson called out in the objective.

---

## 8. Tech stack decisions

| Concern | Decision | Rationale |
|---|---|---|
| Language (backend) | Python 3.11, FastAPI, Pydantic v2 | Already the stack of `plugin_api.py`; no migration cost |
| Language (frontend) | React 18 (host-provided), TypeScript, JSX → IIFE bundle | The host already injects React via `window.__HERMES_PLUGIN_SDK__`; TSX lets us delete a class of bugs. The existing hand-rolled `React.createElement` bundle is unmaintainable as the SPEC grows. |
| Build tool | Vite 5 with `build.lib` + React externalized | Vite produces small IIFE bundles; React stays external to honor the host contract. Output: `dashboard/dist/index.js` + `dist/style.css` (same file paths the manifest declares). |
| State management | Zustand 4 | ~3KB, no Provider needed, ergonomic with TSX. Trivial to bundle without conflicting with host React. |
| CSS | Tailwind 3 via CLI build → precompiled `style.css` | The existing 10KB `style.css` is plain CSS; Tailwind via CLI keeps the runtime contract unchanged. SPEC §12 dark theme with purple accent maps cleanly to a Tailwind config with a `purple-violet` palette + `dark` mode default. |
| Graph viz (Execution + Tasks dependencies) | Hand-rolled SVG | SPEC §8 chains are 5 linear nodes; SPEC §7 dependencies are 5 linear edges. A React Flow dependency would add ~50KB for layout we can do in ~150 LOC of SVG. Reject the dep. |
| Animations | CSS transitions + `@keyframes`; no JS animation lib | SPEC §14's 8 animations are all CSS-expressible (counter ease, fade+scale modal, slide-up toast, underline-slide, color fade). No Framer Motion dependency. |
| Log streaming (server) | FastAPI `StreamingResponse` with manual SSE generator | Avoid `sse-starlette` dep; the generator is ~20 LOC and we already manage backpressure manually. |
| Log streaming (client) | Native `EventSource` API | Browser-native, automatic reconnect, no library |
| Modal | Hand-rolled with `<dialog>` element + focus trap | Native `<dialog>` covers backdrop + ESC + focus trap for free in modern browsers. SPEC §11 modal is centered + backdrop-blur — both CSS. |
| Toasts | Hand-rolled, Zustand-driven queue | One CSS file, ~40 LOC. Avoids react-hot-toast dep. |
| Drag-and-drop (Tasks kanban) | Native HTML5 drag-and-drop API | SPEC §7 has only column-level drops; no need for dnd-kit. |
| Routing | None — single-page within the host's `/ruflo-goap` route | URL stays `/ruflo-goap` per SPEC §2 ("No URL changes observed"); internal state lives in Zustand. |
| Tests (frontend) | Vitest + React Testing Library | Vitest plays nice with Vite; ≥60% coverage target per `hive4-frontend/objective.md` criterion 5. |
| Tests (backend) | pytest + FastAPI `TestClient` | Already in use; ≥75% coverage target per GAMEPLAN criterion #10. |

**Two new runtime dependencies total** (Zustand frontend, none backend) plus two build-time deps (Vite, Tailwind). Everything else is hand-rolled to keep the bundle small and the dependency graph short.

---

## 9. Branch / commit strategy

Per `GAMEPLAN.md` §"Branch / PR strategy" and the existing stacked-PR pattern Joseph prefers:

1. **Hive 1 (this hive)** opens branch `feat/goap-build-h1-architect-20260518T180000Z` off `fork/main`. PR contains the 4 architecture docs at `~/.hermes/specs/ruflo-goap-from-video/architecture/`. The plugin source tree at `~/.hermes/plugins/ruflo-goap-control/` is NOT touched by Hive 1.
2. **Hive 2** branches off Hive 1's branch as `feat/goap-build-h2-backend-20260518T180000Z`. Adds backend modules + tests under `~/.hermes/plugins/ruflo-goap-control/`. Stages but does not modify the live `projects.json` (the `ms` project is read-only per safety rule #8).
3. **Hive 3** branches off Hive 2's branch as `feat/goap-build-h3-ruflo-wiring-...`. Swaps stubs for real wiring; ships `scripts/smoke-e2e.sh`.
4. **Hive 4** branches off Hive 3's branch as `feat/goap-build-h4-frontend-...`. Adds `dashboard/src/`, `dashboard/package.json`, `dashboard/vite.config.ts`, `dashboard/tailwind.config.js`, and rebuilds `dashboard/dist/`.
5. **Hive 5** (Claude-driven, not a ruflo hive) writes only `~/.hermes/ruflo-work/goap-build-.../hive5-stress-test/{STRESS-TEST-REPORT.md, DEFECTS.md, results.jsonl, screenshots/}` — no plugin-source changes.
6. **Hive 6** branches off Hive 4's branch as `feat/goap-build-h6-polish-...`. Polish + docs + memory updates.

**The plugin lives outside git management.** `~/.hermes/plugins/ruflo-goap-control/` is a user-local install, not under a tracked git repo. Two options:
- **Option A — initialize git**: `cd ~/.hermes/plugins/ruflo-goap-control && git init`, then each hive's branch lives in this repo. Pro: PRs work as documented. Con: divorces from `fork/main`, requires the user to manage a per-plugin remote.
- **Option B — stage under specs repo**: each hive's PR against `fork/main` contains a `plugins/ruflo-goap-control/` subdirectory mirror in the Hermes specs repo. Plugin is symlinked or copied into `~/.hermes/plugins/` post-merge by `hermes update`.

**Recommendation:** **Option B.** It matches how the rest of the Hermes plugin ecosystem ships (per `~/.local/share/hermes-agent/plugins/*` install layout) and lets `fork/main` review-and-merge the whole change. Hive 2 will create the directory structure in the specs repo first PR, then subsequent hives diff against it. Hive 1 opens its PR against `fork/main` with ONLY the 4 architecture docs under `~/.hermes/specs/ruflo-goap-from-video/architecture/`; no plugin source edits.

**Commit cadence:** one commit per logical unit; commit messages follow the project convention (`feat:`, `fix:`, `docs:`, `test:` prefixes). Each hive's queen tags the final commit with the hive ID for traceability: `feat(h2-backend): extend plan/generate with research_phases`.

**Merge order:** bottom-up after Hive 6 completion and Joseph's sign-off: H1 → H2 → H3 → H4 → H6. Hive 5's artifacts (stress-test report, defects list) are committed to the chain workdir and may be referenced in the final PR description but don't need their own merge — they're documentation, not source.

---

## 10. Coverage matrix — every SPEC.md screen is covered

| SPEC section | Screen | Backend route(s) | Frontend component(s) |
|---|---|---|---|
| §2 navigation model | Tab mount + back-link | host shell; no plugin route | `<RufloGoapTab>` + `<TabHeader>` |
| §3 empty state | "Ready to Plan" card | `/readiness`, `/agents/spec`, `/blueprint` (existing) | `<CodingObjectivePanel>` + `<EmptyStateCard>` |
| §4a GOAP State Assessment | Animated counter + state lists | `/runs/{id}/goap-state` (NEW); via SSE `/runs/{id}/progress/stream` | `<GoapStateAssessment>` |
| §4b Research Phase Progress | 5 phase cards transitioning | `/plan/generate` (extended) + `/runs/{id}/research-phases` (NEW) + SSE | `<ResearchPhaseProgress>` |
| §5 Research Complete | 4 status cards + execution summary + action bar | `/plan/generate` (extended) + `/runs/{id}/start` (existing) + `/plan/revise` (NEW) | `<ResearchCompleteSummary>` |
| §6 Dev Dashboard | 5 dev phase cards building→done | `/runs/{id}/dev-phases` (NEW) + SSE | `<DevSwarmProgress>` |
| §7 Tasks Kanban + Dependencies | 4-column kanban + 5-edge dependency graph | `/runs/{id}/tasks` (NEW) + `/runs/{id}/tasks/dependencies` (NEW) + `/runs/{id}/tasks/{id}/assign` (NEW) | `<TasksSubTab mode runId>` + `<KanbanColumn>` + `<DependencyGraph>`. **Research mode (pre-launch) renders an explicit empty state**: heading "Task board appears after launch", body "Tasks are populated when development phase starts. Approve & Launch Development to populate." with the same Approve button as the Quality/Logs sub-tabs use. Frame evidence: RESEARCHER-NOTES.md §A notes Tasks was never clicked in Research Review mode (scene13 cursor-hover only), so this is an inferred-default per RISKS.md "inferred behaviors" treatment. |
| §8 Dev Execution | 5-node graph + Cost badges; inner tabs | `/runs/{id}/execution/plan` (NEW) + `/runs/{id}/execution/activity` (NEW) + `/runs/{id}/execution/event-log` (NEW) | `<ExecutionSubTab>` + `<GraphView>` + `<TimelineView>` |
| §8b Research Execution | 5-node graph (research variant) | same routes, `?mode=research` query | same components, mode prop |
| §9 Quality Gates | 3 gate cards + (research) metrics | `/runs/{id}/quality/gates` (NEW) + `/runs/{id}/quality/metrics` (NEW) | `<QualitySubTab>` + `<QualityGateCard>` |
| §10 Logs | Timestamped streaming logs | `/runs/{id}/logs` (existing) + `/runs/{id}/logs/stream` (NEW SSE) | `<LogsSubTab>` + `<LogStreamView>` |
| §11 Advanced Modal | Centered modal, 4 sub-tabs, 4 presets | `/config` + `/config/{run_id}` + `/config/presets` + `/config/validate` (all NEW) | `<AdvancedConfigModal>` + `<ConfigTabs>` |
| §11f toast | "Settings Saved" bottom-right | none (client-emitted on PUT success) | `<ToastHost>` |
| §12 visual design system | dark theme, purple accent | n/a | Tailwind config in `tailwind.config.js` |
| §13 state machine | Mode toggle Research↔Development | n/a | Zustand `mode` slice + `<ModeSwitch>` |
| §14 animations (8) | hover, fade, slide, ease | n/a | CSS-only |
| §15 inferred behaviors | 10 items | see `RISKS.md` "inferred-not-shown" section for the chosen default per item | n/a |

**Every SPEC screen is covered by a (route, component) pair.** Where the SPEC didn't show backend behavior (§15 inferred items), we choose a default and document it in RISKS.md.

---

## 11. Out of scope (deferred to v2)

- **Real LLM plan generation.** v1 uses the deterministic template in `_build_plan`. v2 swaps in `claude -p` for natural-language plan synthesis when Joseph wants it.
- **Multi-tenant / auth.** SPEC §15.8 flags this — out of scope. The plugin trusts the host dashboard's session-token check (`X-Hermes-Session-Token` header is already passed by the existing IIFE bundle's `fetchJSON`).
- **Multi-board kanban.** Tasks sub-tab binds to the single `hermes-kanban-control` board. Multi-board support is a v2 affordance.
- **Network-tab parity with goal.ruv.io.** SPEC §15.9 notes we can't capture goal.ruv.io's network shapes. We don't pursue parity — we pursue feature parity at the UI level.
- **Auto-scaling enforcement.** SPEC §11a's auto-scaling toggle is captured and persisted, but enforcement requires Ruflo CLI changes that are out of scope. v1 stores the value and the modal honors it on UI; the actual swarm spawned uses `swarm.max_agents` as the upper bound.

---

**End of ARCHITECTURE.md.** See `DATA-MODEL.md` for storage schemas, `BUILD-PLAN.md` for per-hive decomposition, and `RISKS.md` for the risk register + safety-conflict resolution.
