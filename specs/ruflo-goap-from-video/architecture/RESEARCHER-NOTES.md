# RESEARCHER-NOTES.md
**Produced by:** Hive 1 research worker, 2026-05-18
**Inputs read:** SPEC.md (467 lines), frame-readings.md (2279 lines, 62 frames), plugin_api.py (767 lines)
**Coverage:** All 62 frames catalogued. Frames are identified as `even01`–`even08` and `scene01`–`scene54`.

---

## A. Distinct UI States (Screen Inventory)

27 distinct states identified across the 62 frames. States are ordered by first appearance.

| # | State Name | Frame IDs | Summary |
|---|---|---|---|
| 1 | `parent-landing` | scene02, scene49 | "Define Research Objective" full-page view with category chips and tagline; GOAP parent shell before entering Coding Agent Swarm |
| 2 | `empty-state` | even01, scene01, scene03 (partial), scene50, scene54 | Coding Agent Swarm with blank input and "Ready to Plan" placeholder card; no objective entered; `Generate Plan` CTA |
| 3 | `objective-typed-pre-generate` | scene03 (partial) | Input field populated with "create an ict indicator for weekly profiles"; sub-tabs and `Regenerate Plan` visible but faded/disabled; plan not yet run |
| 4 | `plan-generation-early` | scene04, scene05, scene06 | GOAP State Assessment at 38% / 3/8 complete; Goal Assessment phase Complete, Architecture Planning in `Researching...` state |
| 5 | `plan-generation-mid` | scene07, scene08 | GOAP counter at 63% / 5/8 complete; both Goal Assessment and Architecture Planning Complete; State Transitions section now empty (transitions satisfied) |
| 6 | `plan-generation-complete-scroll` | even02 (scrolled mid-page) | Plan generation final stages — `code implemented false → true` transition visible; Implementation Strategy `Researching...`; Action Plan shows 3 steps remaining |
| 7 | `research-complete-summary` | even06, scene10, scene11, scene28, scene29, scene30 | Full "Research Complete — Ready for Review" panel at page bottom; 4 status cards; Execution Plan Summary; two action buttons |
| 8 | `research-complete-scroll-top` | scene31 | Same research-complete content but scrolled to show Goal Assessment and Architecture Planning phases at top |
| 9 | `dev-dashboard-building-partial` | scene12, scene13, scene14, scene17, scene19, even05 | Development Phase active; Dashboard sub-tab; 2–3 of 5 phase cards `Done`, ≥1 still `Building...`; status badge: "Building..." |
| 10 | `dev-dashboard-building-init` | scene21 | Development Phase active; Dashboard sub-tab shows only a single vertical green line — transient "initializing" state between approve and first card appearing |
| 11 | `dev-dashboard-done` | scene22, scene23, scene24, scene25 | All 5 dev phase cards `Done`; status badge still reading "Building..." at top in some frames (possible slight lag before it updates) |
| 12 | `dev-dashboard-done-scrolled-bottom` | scene23 | Scrolled to bottom — Deployment card visible with `Status: Live` metric |
| 13 | `tasks-kanban` | even04 | Tasks sub-tab; Kanban board with 4 columns; Task Dependencies flowchart below |
| 14 | `execution-graph-dev` | scene15, scene20, even05 (cursor on Execution tab) | Development Execution sub-tab — Execution Plan inner-tab; 5-node graph (Setup Architecture → Deploy); `5 Actions • Cost: 15 • Est. 8m` |
| 15 | `execution-graph-research` | scene32 | Research Review Execution sub-tab — "Research Execution Plan"; 5 phase nodes; `5 Actions • Cost: 20 • Est. 10m`; inner-tabs: `Research Plan | Agent Activity | Event Timeline` |
| 16 | `quality-gates-dev` | scene16 | Development Quality sub-tab; Test Coverage 85% / threshold 80%; Security Scan 92% / threshold 90%; no Research Quality Metrics section |
| 17 | `quality-gates-research` | scene33, scene34, scene35, scene36, scene37, scene38, scene39, scene40, scene41, scene42, scene43, even06 (partial) | Research Review Quality sub-tab; Test Coverage 100% / threshold 80%; Security Scan 95% / threshold 90%; Research Quality Metrics panel (Completeness 100%, Coverage Complete, Readiness Ready) |
| 18 | `logs-research` | scene46, scene47, scene48 | Research Review Logs sub-tab; "Research Execution Logs" heading; timestamped log entries with `▶` `•` `✓` glyphs; Approve button visible at bottom |
| 19 | `research-review-dashboard` | scene27 | Research Review mode Dashboard sub-tab; all 5 research phase cards Complete; "Research Summary" pill shown as Complete |
| 20 | `research-review-mode-transition` | scene26 | Transitional frame: page content dimmed/blurred; toggle buttons "Research Review" and "Back to Development" both visible; cursor hovering "Back to Development" |
| 21 | `modal-swarm-tab` | even07, scene51, scene52 | Advanced Agent Configuration modal, Swarm sub-tab active; all swarm controls visible; backdrop blur applied |
| 22 | `modal-execution-tab` | even08 | Advanced Agent Configuration modal, Execution sub-tab active; cursor over "Budget" preset pill |
| 23 | `modal-model-tab` | scene53 | Advanced Agent Configuration modal, Model sub-tab active; Primary Provider and Routing Strategy dropdowns; Max Cost slider; Enable Fallback toggle |
| 24 | `modal-open-animating` | scene51 | Modal partially transparent/fading in over empty-state background — visible transition state |
| 25 | `toast-saved` | scene53 (appearing), scene54 (visible) | "Settings Saved" toast visible bottom-right; modal either closing or closed; text: "Your advanced configuration has been saved." |
| 26 | `modal-closed-toast-visible` | scene54 | Modal fully faded out; empty-state restored; toast still on screen |
| 27 | `parent-landing-hover-coding` | scene49 | Parent landing with "Coding" category chip in hovered/highlighted state (light-blue highlight observed) |

**Notes:**
- The "Research Review" sub-tab for Tasks was never clicked in any frame — the cursor hovers Tasks in scene13 but does not activate it. That tab's content is unknown [SPEC §15 item 5 partial].
- `modal-goap-tab` was opened (tab visible in even07, scene51, scene52) but no frame captured its content. [SPEC §11b]

---

## B. Interactive Element Catalogue

All button, input, toggle, slider, dropdown, and draggable elements visible across the 62 frames.

| Element | Type | Screen(s) | Verbatim Label / Placeholder | Inferred Backend Route | Notes |
|---|---|---|---|---|---|
| Coding Objective input | Text input | empty-state, all dev/research states | `e.g., Build REST API with JWT authentication and PostgreSQL` | (local state only; sent on Generate Plan) | Cursor visible inside field [frame even01] |
| Generate Plan | Button (primary) | empty-state | `Generate Plan` | `POST /api/plugins/ruflo-goap-control/plan/generate` | Body: `{objective, category, config, mode}` [plugin_api.py line 548] |
| Regenerate Plan | Button (primary) | post-plan states | `Regenerate Plan` | `POST /api/plugins/ruflo-goap-control/plan/generate` INFERRED | Replaces `Generate Plan` once plan exists [frame scene09, scene20] |
| Advanced Settings | Button (secondary) | empty-state, all dev/research states | `Advanced Settings` | (opens modal, no network call) INFERRED | [frame even01, even07] |
| Back to Research | Button (nav) | all Coding Agent Swarm states | `← Back to Research` | (SPA route change) INFERRED | [frame even01, scene08] |
| Approve & Launch Development | Button (CTA, purple gradient) | research-complete-summary, quality-gates-research (bottom), logs-research (bottom) | `Approve & Launch Development` | `POST /api/plugins/ruflo-goap-control/projects/stage` then `POST /api/plugins/ruflo-goap-control/runs/{run_id}/start` INFERRED | Requires confirm string `START_RUFLO_GOAP_RUN` [plugin_api.py line 667] |
| Request Revision | Button (secondary) | research-complete-summary | `Request Revision` | INFERRED — unknown; possibly re-enters plan-generation or opens modal | Not demonstrated [SPEC §15 item 1] |
| View Research Results | Button (mode toggle) | dev-dashboard states | `View Research Results` | (SPA state toggle) INFERRED | [frame scene12, scene19] |
| Back to Development | Button (mode toggle) | research-review states | `Back to Development` | (SPA state toggle) INFERRED | Hovered in scene26, showing white glow highlight |
| Dashboard sub-tab | Tab button | dev and research-review states | `Dashboard` | (SPA sub-tab change) INFERRED | Active/selected state visible [frame scene25] |
| Tasks sub-tab | Tab button | dev and research-review states | `Tasks` | (SPA sub-tab change) INFERRED | Cursor hover visible [scene13]; content never activated in research-review |
| Execution sub-tab | Tab button | dev and research-review states | `Execution` | (SPA sub-tab change) INFERRED | Cursor hover/click visible [scene15, scene20, scene32] |
| Quality sub-tab | Tab button | dev and research-review states | `Quality` | (SPA sub-tab change) INFERRED | Active state visible [scene16, scene33] |
| Logs sub-tab | Tab button | dev and research-review states | `Logs` | (SPA sub-tab change) INFERRED | Cursor hover/click [scene44, scene45]; active state [scene46] |
| Graph View toggle | Toggle button (pill) | execution-graph-dev, execution-graph-research | `Graph View` | (UI state only) INFERRED | Paired with `Timeline View`; Graph View appears default [scene15, scene32] |
| Timeline View toggle | Toggle button (pill) | execution-graph-dev, execution-graph-research | `Timeline View` | (UI state only) INFERRED | Never shown as selected across all frames |
| Execution inner-tab: Execution Plan | Inner tab | execution-graph-dev | `Execution Plan` | (SPA inner-tab change) INFERRED | Active state shown [scene15, scene20] |
| Execution inner-tab: Current Step | Inner tab | execution-graph-dev | `Current Step` | `GET /api/plugins/ruflo-goap-control/runs/{run_id}/status` INFERRED | Visible label; never clicked [SPEC §8] |
| Execution inner-tab: Agent Activity | Inner tab | execution-graph-dev | `Agent Activity` | INFERRED | Visible label; never clicked [SPEC §15 item 5] |
| Execution inner-tab: Event Log | Inner tab | execution-graph-dev | `Event Log` | `GET /api/plugins/ruflo-goap-control/runs/{run_id}/logs` INFERRED | Maps to `hive-mind.log` [plugin_api.py line 744] |
| Research inner-tab: Research Plan | Inner tab | execution-graph-research | `Research Plan` | (display only) INFERRED | Active by default in research-review Execution [scene32] |
| Research inner-tab: Agent Activity | Inner tab | execution-graph-research | `Agent Activity` | INFERRED | Visible; never clicked |
| Research inner-tab: Event Timeline | Inner tab | execution-graph-research | `Event Timeline` | INFERRED | Visible; never clicked |
| Mode dropdown (Tasks) | Dropdown | tasks-kanban | `distributed` | INFERRED — likely `PATCH /api/.../runs/{run_id}/config` | Other modes unknown [SPEC §15 item 3] |
| Kanban cards (Task Assignment Board) | Draggable cards | tasks-kanban | (task title + role badge) | INFERRED — `PATCH /api/.../runs/{run_id}/tasks/{id}` | 6 cards visible [frame even04] |
| Quick Preset: Development | Pill button | modal-swarm-tab, modal-execution-tab, modal-model-tab | `Development` / `development` / `Fast iteration, verbose logging` | (applies preset values to form fields) INFERRED | [frame even07] |
| Quick Preset: Production | Pill button | modal tabs | `Production` / `production` / `Optimized performance, strict validation` | INFERRED | [frame even07] |
| Quick Preset: Budget | Pill button | modal tabs | `Budget` / `budget` / `Cost-optimized, slower execution` | INFERRED | Cursor hover [frame even08] |
| Quick Preset: Quality | Pill button | modal tabs | `Quality` / `quality` / `Maximum quality, higher cost` | INFERRED | [frame even07] |
| Modal Swarm sub-tab | Tab button | modal | `Swarm` | (modal tab change) INFERRED | Default active [even07] |
| Modal GOAP sub-tab | Tab button | modal | `GOAP` | (modal tab change) INFERRED | Tab visible; content never captured [SPEC §11b] |
| Modal Execution sub-tab | Tab button | modal | `Execution` | (modal tab change) INFERRED | Active [even08] |
| Modal Model sub-tab | Tab button | modal | `Model` | (modal tab change) INFERRED | Active [scene53] |
| Topology dropdown | Dropdown | modal-swarm-tab | `Hierarchical - Tree structure with coordinators` | Stored in config body sent to `POST /plan/generate` INFERRED | [frame even07] |
| Maximum Agents slider | Slider | modal-swarm-tab | `Maximum Agents: 10` | Config body INFERRED | Grab cursor observed [SPEC §12]; [frame even07] |
| Distribution Strategy dropdown | Dropdown | modal-swarm-tab | `Adaptive - Dynamic based on load` | Config body INFERRED | [frame even07] |
| Auto-Scaling toggle | Toggle | modal-swarm-tab | `Auto-Scaling` (default ON) | Config body INFERRED | When ON, reveals Min/Max/Threshold sub-fields [frame even07] |
| Min Agents input | Number input (revealed) | modal-swarm-tab | `Min Agents: 2` | Config body INFERRED | Only visible when Auto-Scaling ON |
| Max Agents input | Number input (revealed) | modal-swarm-tab | `Max Agents: 20` | Config body INFERRED | Only visible when Auto-Scaling ON |
| Scale Up Threshold slider | Slider (revealed) | modal-swarm-tab | `Scale Up Threshold (%): 80` | Config body INFERRED | Blue progress indicator visible [frame scene52] |
| Scale Down Threshold slider | Slider (revealed) | modal-swarm-tab | `Scale Down Threshold (%): 20` | Config body INFERRED | Blue progress indicator visible [frame scene52] |
| Execution Strategy dropdown | Dropdown | modal-execution-tab | `Adaptive - Dynamic based on load` | Config body INFERRED | [frame even08] |
| Max Parallel Tasks slider | Slider | modal-execution-tab | `Max Parallel Tasks: 5` | Config body INFERRED | [frame even08] |
| Timeout slider | Slider | modal-execution-tab | `Timeout (seconds): 300` | Config body INFERRED | [frame even08] |
| Enable Quality Gates toggle | Toggle | modal-execution-tab | `Enable Quality Gates` (default ON) | Config body INFERRED | Subtext: `Run compile checks, test coverage, code quality, and security scans` [frame even08] |
| Primary Provider dropdown | Dropdown | modal-model-tab | `Anthropic - Highest quality` | Config body; maps to `primary_provider` field [plugin_api.py line 388] | [frame scene53] |
| Routing Strategy dropdown | Dropdown | modal-model-tab | `Balanced - Optimize all factors` | Config body INFERRED | [frame scene53] |
| Max Cost Per Request slider | Slider | modal-model-tab | `Max Cost Per Request ($): 1.00` | Config body INFERRED | [frame scene53] |
| Enable Fallback toggle | Toggle | modal-model-tab | `Enable Fallback` (default ON) | Config body; intentionally disabled in Hermes safety model | Subtext: `Automatically fallback to alternative providers on failure` [frame scene53] |
| Reset to Defaults | Button (secondary) | all modal tabs | `Reset to Defaults` | (resets form state) INFERRED | [frame even07] |
| Save Configuration | Button (primary) | all modal tabs | `Save Configuration` | `POST /api/plugins/ruflo-goap-control/plan/generate` with updated config body INFERRED | Cursor hover state [frame scene53]; fires toast |
| Finance category chip | Pill button | parent-landing | `Finance` | Loads Finance-category agent swarm INFERRED | [frame scene02, scene49] |
| Business category chip | Pill button | parent-landing | `Business` | INFERRED | [frame scene49] |
| Marketing category chip | Pill button | parent-landing | `Marketing` | INFERRED | [frame scene49] |
| Medical category chip | Pill button | parent-landing | `Medical` | INFERRED | [frame scene49] |
| Education category chip | Pill button | parent-landing | `Education` | INFERRED | [frame scene49] |
| Coding category chip | Pill button | parent-landing | `Coding` | Routes to Coding Agent Swarm [frame scene49] | Hover highlight observed [frame scene49] |
| Technical category chip | Pill button | parent-landing | `Technical` | INFERRED | [frame scene49] |
| AI & ML category chip | Pill button | parent-landing | `AI & ML` | INFERRED | [frame scene49] |
| Generate Research Plan | Button (primary) | parent-landing | `Generate Research Plan` | INFERRED — parent-level plan endpoint | [frame scene02, scene49] |
| Widget Demo | Button/link | parent-landing | `Widget Demo` | Unknown — not demonstrated [SPEC §15 item 7] | [frame scene49] |
| Agent Swarm | Button/link | parent-landing | `Agent Swarm` | Routes to Coding Agent Swarm INFERRED | [frame scene49] |
| Create Widget | Button/link | parent-landing | `Create Widget` | Unknown — not demonstrated [SPEC §15 item 7] | [frame scene49] |
| Advanced button (parent) | Button | parent-landing | `Advanced` | INFERRED — opens parent-level config | [frame scene02, scene49] |

---

## C. Animations & Timing (SPEC §14 Expansion)

| §14 Item | Animation | Frame Evidence | Observed Duration / Timing |
|---|---|---|---|
| §14 item 1 | GOAP percentage counter ticks 0% → 100% | scene04 (38%), scene07 (63%) both captured; 100% implied at research-complete | Log timestamps show Phase 1 at 10:14:58, Phase 2 complete at 10:15:14 — 16s for 2 phases; total progression ~30s across 5 phases. Counter appears continuous, not stepped. |
| §14 item 2 | Research Phase cards `Researching... → Complete` (sequential) | scene04/scene05: Goal Assessment Complete, Architecture Researching. scene06: Architecture still Researching. scene07: Architecture Complete. | Architecture Planning transition happened between scene06 and scene07 — approximately 5–8s per phase. [frame scene04] confirms green vs purple color coding. |
| §14 item 3 | State-list booleans flip red→green | scene04: `architecture planned (false)`, scene07: `architecture planned (true)` | Transition occurs between 38% and 63% checkpoint. Exact animation frame duration: assumed (no sub-second frame). |
| §14 item 4 | Quality Gate progress bars fill with easing | scene16 (dev: 85%, 92%), scene33 (research: 100%, 95%) — bars shown in filled state | Fill animation: assumed (only end-state captured in frames). |
| §14 item 5 | Modal open/close — fade + scale-up, backdrop blur ramps | scene51 (partially transparent/loading), scene52 (fully opaque), scene54 (faded out) | Fade-in: between scene51 and scene52, approximately 1 video frame (≤1s). Fade-out: scene54 shows modal "nearly invisible" per frame notes. |
| §14 item 6 | Sub-tab active-state underline-slide animation | scene25 labels Dashboard as "(Selected)"; scene33 Quality active; scene46 Logs active | Sub-tab changes observed across multiple frames; exact slide duration: assumed. |
| §14 item 7 | "Settings Saved" toast slide-up + fade, auto-dismiss | scene53: `Settings Saved` text visible alongside open modal. scene54: toast bottom-right, modal faded out. | Toast visible for ≥1 video segment (~1–3s visible window). Auto-dismiss timing: assumed (a few seconds per SPEC). |
| §14 item 8 | Hover states on preset pills, sub-tabs, sliders | even08: cursor on "Budget" pill. scene26: "Back to Development" white glow on hover. scene49: "Coding" chip light-blue highlight. | Hover transitions: assumed instantaneous / CSS transition ~150ms. Grab cursor on slider handles [SPEC §12]. |

---

## D. Inferred-Not-Shown Behaviors (SPEC §15 Expansion)

**Default behavior proposals, each with justification referencing shown behavior or Hermes safety model.**

**§15 Item 1 — `Request Revision` action**
Default: clicking "Request Revision" resets the page to the `objective-typed-pre-generate` state (input preserved, plan panels cleared) without opening a modal. Justification: no revision-comment flow appeared in the video; the closest shown behavior is the "Back to Research" nav which discards view state and returns to input. Preserving the objective text avoids user friction from re-typing.

**§15 Item 2 — `Regenerate Plan` after development started**
Default: show an inline confirmation inline-banner ("This will discard the current build. Continue?") with two options: "Confirm" and "Cancel." If confirmed, clear dev-phase state and return to plan-generation-early state. Justification: the Hermes safety model (plugin_api.py) requires explicit confirmation strings (`START_RUFLO_GOAP_RUN`) for destructive operations — the same pattern should apply to state resets that discard a running build [plugin_api.py lines 667–668].

**§15 Item 3 — Tasks sub-tab drag-and-drop / "assign agents" semantics**
Default: dragging a card between Kanban columns updates its status (To Do → In Progress etc.) and emits a `PATCH /api/plugins/ruflo-goap-control/runs/{run_id}/tasks/{task_id}` call with `{status, assigned_agent}`. The `Mode: distributed` dropdown likely switches between `distributed` (auto-assign from pool) and `manual` (operator assigns). The mode dropdown is read-only in the demo — default to `distributed`. Justification: the heading "Drag and drop tasks to assign agents" is the only description shown [frame even04]; the closest analogous shown behavior is the quality-gate pass/fail cards which are display-only. The `distributed` mode dropdown implies at least one other value exists but was not demonstrated.

**§15 Item 4 — Objective persistence across refresh**
Default: objective is stored in `localStorage` under key `ruflo-goap-objective` and rehydrated on mount; no server-side project-list view is built. Justification: no login screen or project list was shown [SPEC §15 item 4]; the Hermes plugin stores objectives in `objective.md` in the workdir only after staging, not before [plugin_api.py line 612]. The SPA has no visible URL changes, confirming all state is in-memory or localStorage.

**§15 Item 5 — Agent Activity / Event Log inner-tabs content**
Default: "Agent Activity" shows a live table of agent name, current task, and status (updating via polling or SSE). "Event Log" streams the `hive-mind.log` tail via the existing `GET /api/plugins/ruflo-goap-control/runs/{run_id}/logs?file=hive-mind.log` endpoint [plugin_api.py line 744]. Justification: the Logs sub-tab already mirrors this pattern with timestamped entries [frame scene46, scene47]; the inner-tab "Event Log" in the Execution tab is the most natural surface for the same feed.

**§15 Item 6 — GOAP modal sub-tab controls**
Default: the GOAP sub-tab contains four controls: (a) Planning Algorithm dropdown (default: `A* - optimal pathfinding`), (b) Heuristic Function dropdown (default: `Manhattan Distance / prerequisite distance`), (c) Cost Calculation Method dropdown (default: `Hybrid effort/risk/latency`), (d) Max Plan Depth slider (default: `10`). Justification: these four fields are exactly what `plugin_api.py`'s `_build_plan` function reads from `config.goap` [plugin_api.py lines 388–390]; the `agents/spec` endpoint also lists `["Planning Algorithm", "Heuristic Function", "Cost Calculation Method", "Optimization", "Parallel Action Detection", "Redundancy Removal"]` as GOAP tab fields [plugin_api.py line 541].

**§15 Item 7 — "Widget Demo" and "Create Widget" links**
Default: treat as out-of-scope for Hermes build. "Widget Demo" likely opens `/widget/embed` output in a preview iframe; "Create Widget" is unknown. Justification: `GET /api/plugins/ruflo-goap-control/widget/embed` already exists [plugin_api.py line 555]; neither link was clicked in the video so both remain INFERRED. Do not implement until clarified.

**§15 Item 8 — Authentication / multi-tenancy**
Default: no auth layer in the Hermes dashboard build. Access control is delegated to the Hermes dashboard's existing session gate. Justification: no login screen appeared in any of the 62 frames. The Hermes plugin is user-local (runs under `~/.hermes/ruflo-goap-control`) and the safety model assumes single-user operator context [plugin_api.py line 14: "This module is intentionally self-contained and user-local"].

**§15 Item 9 — API contract / request-response shapes**
Default: follow the existing `plugin_api.py` routes exactly. No HAR capture occurred. The plugin already defines `PlanBody`, `StageBody`, `StartBody`, `StopBody` Pydantic models [plugin_api.py lines 435–460] — these are the canonical shapes. Do not invent new schemas.

**§15 Item 10 — Static vs. computed metric values**
Default: treat all numeric outputs (12 components, 42 files, 124 tests, 6 agents, etc.) as static placeholder values generated by the canned `_build_plan` function, not computed by a real planner. Justification: the same numbers appear regardless of objective. The `_build_plan` function in `plugin_api.py` [lines 372–432] generates deterministic output with hardcoded step lists — it confirms these are templates, not LLM-computed values.

---

## E. Items in frame-readings.md NOT in SPEC.md

These text strings appear in verbatim frame dumps but are absent from the structured SPEC.

| # | Verbatim Text | Frame | Why It Matters |
|---|---|---|---|
| 1 | `HERMIN -> Joseph McNally - Compacting context — summarizing earlier conversatio...` | scene11 | A Hermes chat notification bubble appeared at the bottom-right during the recording — this is an ambient UI element from the OS/Hermes layer, not the GOAP app |
| 2 | `Chat` label with `< >` navigation arrows next to "Back to Development" | scene47, scene48 | A "Chat" pane or widget was docked alongside the Research Review logs panel; the `< >` arrows suggest it is collapsible |
| 3 | `Research Summary` as a 6th sub-tab label (faded) | scene03, scene09 | In the early post-plan state, a sixth sub-tab "Research Summary" appears alongside the standard 5; it is `faded/disabled` in scene03 and shown as `Complete` (a pill state) in scene27. SPEC §2 lists only 5 tabs. |
| 4 | `Files` and `Scope` as alternative sub-tab labels | scene09 | The tab strip in scene09 reads `Research Review / Dashboard / Files / Execution / Quality / Scope` — different from the standard `Dashboard / Tasks / Execution / Quality / Logs` strip. This may be a transitional render or an earlier UI version. |
| 5 | `https://goal.ruv.io` in the browser status bar (not `/agents`) | scene49 (note in frame), scene01 (note) | Status bar hint differs from active URL; suggests the parent domain root also serves content |
| 6 | `A* pathfinding` in the parent page tagline | scene02, scene49 | Full tagline: `AI-powered research planning using A* pathfinding and dynamic agent coordination` — the algorithm choice is named on the landing page (SPEC §1 includes it but not the `/agents`-page tagline separately) |
| 7 | `Research deployment options` as a Deployment Planning sub-step | scene10, scene28, scene29 | SPEC §4b lists only `Research deployment options · Plan monitoring & observability` but scene10 shows this sub-step while scene11 omits it — one frame shows it, one does not, suggesting scroll position variance |
| 8 | `Milestones: 5` and `Agents: 6` as Phase 3 (Implementation Strategy) metric outputs | scene10, scene27 | SPEC §4b lists these correctly but SPEC §5's "Research Complete" summary card does not include them — they only appear in the full phase card view |
| 9 | `Documents: 8` and `Pages: 24` as Documentation phase outputs | scene23 | These metric outputs from the Documentation phase card appear only in the dev-dashboard-done-scrolled-bottom state — not mentioned in SPEC §6 table |
| 10 | `Status: Live` as Deployment phase output metric | scene23 | SPEC §6 lists `Status: Live` but notes it only obliquely; the frame confirms this is a literal metric label in the Deployment card, paired with `Environments: 3` |
| 11 | `3 steps remaining` in Action Plan (not 4) | even02 | SPEC §4a says "4 entries seen" for Action Plan but even02 shows the Action Plan at `3 steps remaining` with steps 1–3 (Architecture, Test strategy, Deployment), implying step 1 ("Architecture needs to be designed") was already consumed |
| 12 | `code implemented false` in System State column as a distinct row shown without parentheses | even02 | SPEC shows state values as `(true)`/`(false)` always; frame even02 shows `code implemented false` without parentheses — minor formatting variant to be consistent about |
| 13 | `66°F Mostly sunny` appearing at timestamp `10:16 AM` | scene53, scene54 | Clock advanced from 10:13–10:15 in most frames to 10:16 in the modal/toast frames — recording span is confirmed to be ~3 minutes total |

---

## F. Open Questions for the Queen

1. **"Research Summary" as 6th sub-tab** [frame scene03, scene09]: The disabled tab strip shows a 6th item `Research Summary` (and in scene09, `Files` and `Scope` replace `Tasks` and `Logs`). Is this a different route, an older UI version captured mid-transition, or a valid variant state that needs to be built? Cannot resolve from frames alone.

2. **GOAP sub-tab modal controls** [SPEC §11b]: The frame extraction never captured the GOAP sub-tab content beyond the tab label. The `agents/spec` endpoint [plugin_api.py line 541] lists 6 fields but it is unclear which are sliders, dropdowns, or toggles.

3. **Chat pane / `< >` widget** [frame scene47–scene48]: A "Chat" label with forward/back arrows appeared docked alongside the Research Review panel. Is this an Hermes-native chat sidebar that should be replicated, or an ambient browser extension / OS widget that is not part of the build scope?

4. **`Request Revision` flow** [SPEC §15 item 1]: No frame ever showed what happens after clicking this button. If it is meant to accept operator feedback text and pass it to the planner, a revision-comment input field is required — but that is pure inference.

5. **"Agent Activity" inner-tab content** [SPEC §15 item 5]: Neither the Development Execution tab's "Agent Activity" nor the Research Execution tab's "Agent Activity" was ever clicked. The backend endpoint to support it is unknown (no existing plugin_api.py route maps to it).

6. **"Timeline View" in the Execution graph** [SPEC §8, frame scene15]: The `Timeline View` toggle was never activated. What it renders — a Gantt chart, a sequential list, a calendar — is entirely unknown from the video.

7. **Kanban drag-and-drop backend contract** [SPEC §7, frame even04]: No network call for task reassignment exists in `plugin_api.py`. Whether drag-drop is local-only (display state) or requires a real endpoint is unresolved.

8. **Development Phase — final "Done" status badge**: Frames scene22–scene25 show all cards as `Done` but the top status badge still reads `Building...` in some. Whether this is a lag animation (badge updates after a final check) or a genuine second state (`Building... → Done` badge transition) is not confirmed — only the card-level `Done` pill is clearly shown, not the header badge changing.

9. **Parent-page "Advanced" button** [frame scene02, scene49]: A small `Advanced` label appears near the "Define Research Objective" input on the parent page. Its click target / behavior is unknown — it may open a parent-level config modal distinct from the "Advanced Settings" within the Coding Agent Swarm.
