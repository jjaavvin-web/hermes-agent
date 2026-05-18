# Coding Agent Swarm — Functional Spec

**Source:** `/home/josep/.hermes/PROJECT.mp4` (3:31, 2580x1080, no audio)
**Method:** 62 high-res frames extracted (54 scene-change + 8 evenly-spaced) → batched through `gemini-3-flash` via Kyma at native 2580x1080, cross-checked against direct Claude vision reads
**Generated:** 2026-05-18 by Claude (Opus 4.7) + Gemini-3-flash (Kyma)
**Cost to produce:** $0.13 (multimodal API)
**Backing artifacts:** `/tmp/project-hires/*.jpg` (62 frames), `/tmp/frame-readings.md` (verbatim text dump per frame)

This document is the source of truth for what we are building. Every claim is grounded in the video — if it's not here, it wasn't shown.

---

## 0. What this is

A web application at **`https://goal.ruv.io/agents`** — part of ruv.io's "GOAP Multi-Agent System" (Goal-Oriented Action Planning). The video is a screen recording of one user-flow: the operator opens the **Coding Agent Swarm** view, types a coding objective ("create an ict indicator for weekly profiles"), generates a research plan, reviews it, approves & launches development, and then explores the development phase + configuration modal.

The video is the **functional spec** for a Hermes-side dashboard tab we are about to build (presumably evolving / replacing the existing `ruflo-goap-control` plugin at `~/.hermes/plugins/ruflo-goap-control/`).

---

## 1. Identification

| Field | Value (verbatim) |
|---|---|
| URL | `https://goal.ruv.io/agents` |
| Product name (parent) | `GOAP Multi-Agent System` |
| Product tagline | `AI-powered research planning using A* pathfinding and dynamic agent coordination` |
| Sub-product name | `Coding Agent Swarm` |
| Sub-product tagline | `Intelligent multi-agent system for collaborative software development` |
| Footer attribution | `RuFlo Research · Created with ❤️ by rUv.io` |
| OS timestamp visible | `10:13–10:16 AM 5/18/2026` (taskbar clock) |
| Weather widget visible | `66°F Mostly sunny` (taskbar weather, Windows) |

Browser tabs open during recording (verbatim, truncated by tab-width):
1. `(1) you recognise this place, b…` — possibly a chat
2. `RuFlo Research — Autonomous AI…` — the parent page
3. `Videos | Library | Loom` — Loom recording library

---

## 2. Navigation model

Two-level navigation:

**Top-level (parent → child):**
- `Define Research Objective` (home / landing)
  - Tools shown: `Widget Demo`, `Agent Swarm`, `Create Widget`
  - 8 AI-Generate category chips: `Finance` `Business` `Marketing` `Medical` `Education` `Coding` `Technical` `AI & ML`
  - User clicks **Coding** category (or **Agent Swarm**) → routes to `Coding Agent Swarm`
- `Coding Agent Swarm` (the focus of this spec)
  - `← Back to Research` returns to parent

**Mode toggle within Coding Agent Swarm:**
After plan is generated, the view has two "modes":
- **Research Review** — view/audit the plan that was generated
- **Development Phase** — view the live build progress

Each mode has the SAME 5 sub-tabs: `Dashboard | Tasks | Execution | Quality | Logs`.
Toggle buttons: `View Research Results` (when in Development) ↔ `Back to Development` (when in Research Review).

URL is `/agents` throughout — this is a single-page app with internal route state. No URL changes observed.

---

## 3. Coding Agent Swarm — Initial / Empty State

Visible elements when the page first loads:

- Top-left product chrome:
  - Heading: **`Coding Agent Swarm`**
  - Subtitle: `Intelligent multi-agent system for collaborative software development`
  - Button (top-right): `← Back to Research`

- Center: `Coding Objective` panel
  - Label: `Define what you want the agent swarm to build`
  - Text input — placeholder: `e.g., Build REST API with JWT authentication and PostgreSQL`
  - Trailing button: `Advanced Settings` (opens the configuration modal — see §10)
  - Primary CTA button: `Generate Plan`

- Below: `Ready to Plan` empty-state card
  - Body: `Enter a coding objective above and click "Generate Plan" to see the agent swarm in action`
  - Example 1: `"Build REST API with JWT authentication and PostgreSQL"`
  - Example 2: `"Create a React dashboard with charts and real-time data"`

---

## 4. Plan Generation (live state machine)

When the user clicks `Generate Plan`, the page replaces the `Ready to Plan` card with two live-updating panels.

### 4a. GOAP State Assessment panel
- Heading: `GOAP State Assessment`
- Subtitle: `Real-time state progression tracking`
- Live counter: `38%` → `63%` → `100%` (animates over ~13s)
- Sub-counter: `3/8 complete` → `5/8 complete` → `8/8 complete`

Two state-list columns:

| `System State (Current)` | `Goal State (Target)` |
|---|---|
| `project defined (true)` | `project defined (true)` |
| `requirements clear (true)` | `requirements clear (true)` |
| `agents ready (true)` | `agents ready (true)` |
| `architecture planned (false → true)` | `architecture planned (true)` |
| `code implemented (false → true)` | `code implemented (true)` |

Below the state lists:
- `State Transitions` section — shows pending `<var>: false → true` transitions
- `Action Plan` panel — `N steps remaining` counter, numbered list (4 entries seen):
  1. `Architecture needs to be designed`
  2. `Implementation plan required`
  3. `Test strategy must be defined`
  4. `Deployment strategy needed`

### 4b. Research Phase Progress panel
Shows 5 phase cards, each transitioning `Researching... → Complete`. Each has:
- Phase icon + name
- Description (italicized)
- 2–3 sub-step bullets
- 2 metric outputs

The 5 phases (verbatim labels and outputs):

| # | Phase | Description | Sub-steps | Outputs |
|---|---|---|---|---|
| 1 | `Goal Assessment` | `Analyzing project requirements and current state` | Parse coding objective · Identify required technologies · Assess complexity & feasibility | `Complexity: Medium` · `Estimated Time: 2-4 weeks` |
| 2 | `Architecture Planning` | `Designing system structure and component interactions` | Research architecture patterns · Design API contracts · Plan database schema | `Components: 12` · `API Endpoints: 8` |
| 3 | `Implementation Strategy` | `Planning development approach and milestones` | Define development phases · Identify agent responsibilities · Research best practices | `Milestones: 5` · `Agents: 6` |
| 4 | `Testing Strategy` | `Planning quality assurance approach` | Define test coverage goals · Research testing frameworks | `Target Coverage: 85%` · `Test Types: 3` |
| 5 | `Deployment Planning` | `Preparing production deployment strategy` | Research deployment options · Plan monitoring & observability | `Services: 4` · `Environments: 3` |

Phases turn from purple "Researching..." → green "Complete" sequentially. Visible timing from log timestamps: ~5–6 seconds per phase, so a full 5-phase plan generation completes in ~30 seconds.

---

## 5. Research Complete — Ready for Review

Appears at the bottom of the page once all 5 phases are complete.

- Heading: `Research Complete - Ready for Review`
- Subtitle: `Review the research findings and execution plan before launching development`

- Section: `Project Goal`
  - Shows the user's typed objective verbatim (e.g., `create an ict indicator for weekly profiles`)

- Four status cards in a row (each showing status pill + 1-line summary):

| Card | Status | Summary |
|---|---|---|
| `Goal Assessment` | `Completed` | `Requirements analyzed, agents identified` |
| `Architecture` | `Completed` | `System design, API contracts planned` |
| `Implementation` | `Ready` | `42 files, 1,247 LOC planned` |
| `Testing` | `Ready` | `124 tests, 87% coverage target` |

- Section: `Execution Plan Summary` (4 stats)
  - `Total Phases: 5 phases`
  - `Estimated Duration: ~40 seconds`
  - `Agents Required: 6 agents`
  - `Complexity: Medium`

- Bottom action bar:
  - Primary: `Approve & Launch Development` (large, purple gradient background)
  - Secondary: `Request Revision`

---

## 6. Development Phase — Dashboard sub-tab

After approve, the layout changes:
- The same `Coding Objective` panel stays at top (now with `Regenerate Plan` instead of `Generate Plan`)
- A new heading appears: `Development Phase`
- Button: `View Research Results` (toggles back to Research Review mode)
- The 5-sub-tab strip becomes visible: `Dashboard | Tasks | Execution | Quality | Logs`

**Dashboard sub-tab content:**
- Heading: `Development Swarm Progress`
- Status: `Building...` (until done)
- 5 phase cards (each transitioning `→ Done`):

| # | Phase | Description | Sub-steps | Outputs |
|---|---|---|---|---|
| 1 | `Project Setup` | `Initializing codebase and dependencies` | Setup project structure · Configure development environment | `Files Created: 12` · `Dependencies: 24` |
| 2 | `Core Implementation` | `Building main application features` | Implement authentication module · Build REST API endpoints · Integrate database layer | `Files: 42` · `Total LOC: 1,247` |
| 3 | `Testing & Quality` | `Validating code quality and functionality` | Write unit tests · Run security analysis · Code review | `Tests: 124` · `Coverage: 87%` |
| 4 | `Documentation` | `Creating comprehensive project documentation` | Generate API documentation · Write developer guides | `Documents: 8` · `Pages: 24` |
| 5 | `Deployment` | `Deploying to production environment` | Setup CI/CD pipeline · Deploy to production | `Environments: 3` · `Status: Live` |

Each card uses the same visual shape as the Research Phase Progress cards (icon + title + description + sub-bullets + metric outputs + status pill).

---

## 7. Development Phase — Tasks sub-tab

Heading: `Task Assignment Board`
Subtitle: `Drag and drop tasks to assign agents`
Mode dropdown (next to heading): `distributed`

**Kanban columns** (with live count badges):
- `To Do 3`
- `In Progress 2`
- `Blocked 1`
- `Done 0`

**Task cards** (title + role-badge with priority):
- To Do: `Design database schema` (Architecture, high) · `Write unit tests for auth` (Testing, medium) · `Document API endpoints` (Documentation, low)
- In Progress: `Implement user authentication` (Implementation, high) · `Setup CI/CD pipeline` (DevOps, medium)
- Blocked: `Review authentication code` (Code Review, high)

**Below the Kanban: Task Dependencies panel**
- Inner heading: `Task Dependencies`
- Subtitle: `Workflow execution order`
- Edges shown (rendered as a graph):
  - `Architecture → Implementation`
  - `Implementation → Testing`
  - `Testing → Code Review`
  - `Code Review → Documentation`
  - `Documentation → DevOps`

---

## 8. Development Phase — Execution sub-tab

**Inner-tabs:** `Execution Plan | Current Step | Agent Activity | Event Log`

**Execution Plan inner-tab content:**
- Heading: `Execution Plan`
- Stats line: `5 Actions • Cost: 15 • Est. 8m`
- View toggle: `Graph View | Timeline View`

Action nodes in the flowchart (Cost values are visible badges on each node):
1. `Setup Architecture` (Cost: 3)
2. `Design API` (Cost: 2)
3. `Implement Backend` (Cost: 5)
4. `Write Tests` (Cost: 4)
5. `Deploy` (Cost: 1)

(Edges between nodes are implied by the graph layout — likely the same dependency chain as Tasks sub-tab.)

---

## 8b. Research Review — Execution sub-tab (counterpart)

When the user is in Research Review mode and visits Execution, the inner-tabs and content are different:

**Inner-tabs:** `Research Plan | Agent Activity | Event Timeline`
- Heading: `Research Execution Plan`
- Caption: `5 Phases • All Completed • Goal: create an ict indicator for weekly profiles`
- Stats: `5 Actions • Cost: 20 • Est. 10m`
- View toggle: `Graph View | Timeline View`

Phase nodes:
1. `Goal Assessment` (Cost: 2)
2. `Architecture Planning` (Cost: 3)
3. `Implementation Strategy` (Cost: 4)
4. `Testing Strategy` (Cost: 5)
5. `Deployment Planning` (Cost: 6)

---

## 9. Quality sub-tab

Shared shape in both Research Review and Development contexts; the values shift.

**Quality Gates panel:**
- Heading: `Quality Gates`
- Subtitle: `Automated quality assurance checkpoints`
- 3 gate cards (each shows `[passed]` pill):

| Gate | Current | Threshold |
|---|---:|---:|
| `Compile Check` | (binary: passed/failed) | — |
| `Test Coverage` | `100%` (Research) / `85%` (Dev) | `80%` |
| `Security Scan` | `95%` (Research) / `92%` (Dev) | `90%` |

**Research Quality Metrics panel** (only in Research Review):
- `Completeness` — `100%` — `All phases completed successfully`
- `Coverage` — `Complete` — `Architecture, implementation, testing & deployment`
- `Readiness` — `Ready` — `Ready to proceed to development`

The "Research Complete - Ready for Review" summary card from §5 also appears below in Research Review mode (it's shown in multiple sub-tabs for re-access to the launch button).

---

## 10. Logs sub-tab

Heading: `Research Execution Logs` (in Research Review) or presumably `Development Execution Logs` (in Development — not directly seen but inferred from symmetry).

Subtitle: `Detailed logs from all research phases`

Log entries are timestamped with millisecond precision (`[HH:MM:SS AM]`) and prefixed by glyphs:
- `▶` — phase start
- `•` — sub-step
- `✓` — completion / metric output

Sample entries (verbatim from video, Phase 1 + start of Phase 2):
```
[10:14:58 AM] ▶ Starting Phase 1: Goal Assessment
[10:14:58 AM] • Parse coding objective
[10:14:59 AM] • Identify required technologies
[10:15:00 AM] • Assess complexity & feasibility
[10:15:01 AM] ✓ Complexity: Medium
[10:15:01 AM] ✓ Estimated Time: 2-4 weeks
[10:15:06 AM] ✓ Phase 1 Complete
[10:15:06 AM] ▶ Starting Phase 2: Architecture Planning
[10:15:06 AM] • Research architecture patterns
[10:15:07 AM] • Design API contracts
[10:15:08 AM] • Plan database schema
[10:15:09 AM] ✓ Components: 12
[10:15:09 AM] ✓ API Endpoints: 8
[10:15:14 AM] ✓ Phase 2 Complete
[10:15:14 AM] ▶ Starting Phase 3: Implementation Strategy
...
```

Each phase takes ~5–8 seconds (in the demo data). The format is consistent across all 5 phases.

---

## 11. Advanced Agent Configuration (modal)

Triggered by the `Advanced Settings` button in §3. Opens as a centered modal with backdrop blur over the page.

- Title: `Advanced Agent Configuration`
- Subtitle: `Configure swarm topology, GOAP planning, execution strategy, and model routing`

**Quick Presets** (4 pill-buttons across the top):

| Preset | Subtitle |
|---|---|
| `Development` (`development`) | `Fast iteration, verbose logging` |
| `Production` (`production`) | `Optimized performance, strict validation` |
| `Budget` (`budget`) | `Cost-optimized, slower execution` |
| `Quality` (`quality`) | `Maximum quality, higher cost` |

**4 sub-tabs:** `Swarm | GOAP | Execution | Model`

### 11a. Swarm sub-tab
- **Topology** dropdown — observed value: `Hierarchical - Tree structure with coordinators`
- **Maximum Agents** slider — default `10`
- **Distribution Strategy** dropdown — observed value: `Adaptive - Dynamic based on load`
- **Auto-Scaling** toggle (default ON)
  - When ON, reveals:
    - **Min Agents**: `2`
    - **Max Agents**: `20`
    - **Scale Up Threshold (%)** slider — default `80`
    - **Scale Down Threshold (%)** slider — default `20`

### 11b. GOAP sub-tab
*(opened by user but specific content not captured in detail in this pass; symmetric layout implied — likely contains GOAP planning parameters like state-space size, planning depth, heuristic weights)*

### 11c. Execution sub-tab
- **Execution Strategy** dropdown — observed: `Adaptive - Dynamic based on load`
- **Max Parallel Tasks** slider — default `5`
- **Timeout (seconds)** slider — default `300`
- **Enable Quality Gates** toggle (default ON)
  - Subtext: `Run compile checks, test coverage, code quality, and security scans`

### 11d. Model sub-tab
- **Primary Provider** dropdown — observed: `Anthropic - Highest quality`
- **Routing Strategy** dropdown — observed: `Balanced - Optimize all factors`
- **Max Cost Per Request ($)** slider — default `1.00`
- **Enable Fallback** toggle (default ON)
  - Subtext: `Automatically fallback to alternative providers on failure`

### 11e. Modal footer
- Left button: `Reset to Defaults`
- Right button (primary): `Save Configuration`

### 11f. After save
- Modal fades out
- Toast notification appears bottom-right: **`Settings Saved`** — body: `Your advanced configuration has been saved.`

---

## 12. Visual / design system

- **Theme:** dark mode (near-black background `~#0a0a0e`); no light theme observed in video
- **Primary accent:** purple/violet gradient (`Approve & Launch Development` button; active sub-tab underline; modal primary action)
- **Status colors:**
  - Green for `Complete` / `Done` / `passed` / `Ready` / `Live`
  - Purple/blue for `Researching...` / `Building...` (in-progress)
  - Neutral gray for `false` state values
- **Pill / badge shape:** rounded-full with small padding; status pills appear in card top-right
- **Card style:** rounded corners (~12px), subtle border on dark background, generous padding, icon-left + content layout
- **Typography:** sans-serif throughout; medium-weight headings, regular body, monospace for timestamps in logs
- **Iconography:** outline-style icons (consistent set, possibly Lucide or Phosphor)
- **Modal:** centered, max-width ~640px, backdrop blur on overlay, fade-in animation
- **Cursor states:** hand pointer over interactive elements, glow/highlight on hovered primary buttons
- **Cursor over slider handles:** changes to grab cursor (observed in §11a Max Agents slider)

---

## 13. State machine (full)

```
Define Research Objective (parent)
    ↓ [pick "Coding" category OR click "Agent Swarm"]
Coding Agent Swarm — empty state ("Ready to Plan")
    ↓ [type objective + click "Generate Plan"]
Plan Generation (live; ~30s):
    GOAP State Assessment: 38% → 63% → 100%
    Research Phase Progress: 5 phases sequentially Researching... → Complete
    ↓ (auto-transition when all 5 complete)
Research Complete — Ready for Review
    ├── [click "Request Revision"] → presumably loops back to plan generation
    └── [click "Approve & Launch Development"]
        ↓
Development Phase — Building
    Dashboard sub-tab: 5 dev cards Building... → Done
    Tasks / Execution / Quality / Logs available throughout
    ↓ (auto-transition when all 5 complete)
Development Phase — Done
    Status: Live (Deployment phase output)
    ├── [click "View Research Results"] → Research Review mode (same sub-tabs)
    │       └── [click "Back to Development"] → return
    └── [click "Regenerate Plan"] → presumably wipes dev state, returns to plan generation
```

Modal `Advanced Agent Configuration` is orthogonal — opens from `Advanced Settings` button on the Coding Objective panel; available in any state where that panel is visible.

---

## 14. Brief / animation states (things shorter than a 26s frame sample would miss)

These were observed via the Gemini full-video analysis and cross-referenced with extracted frames:

1. **GOAP State Assessment percentage counter** ticks smoothly from 0% → 100% over ~13 seconds; sub-counter `N/8 complete` updates in lockstep
2. **Research Phase cards** transition `Researching... → Complete` with a green-fill animation; later cards remain in their `Researching...` state until earlier cards complete (sequential, not parallel rendering)
3. **State-list booleans** flip from `(false)` to `(true)` for each pending row as transitions are applied — animated text color change red → green
4. **Quality Gate progress bars** fill from 0 → final value with easing
5. **Modal open/close** uses fade + scale-up animation; backdrop blur ramps in
6. **Sub-tab active-state highlight** has a subtle underline-slide animation when switching
7. **"Settings Saved" toast** appears bottom-right with slide-up + fade, auto-dismisses after a few seconds
8. **Hover states** observed on: Quick Preset pills (background brightens), sub-tab labels (cursor changes to hand pointer + underline appears), card buttons, sliders (handle scales up)

---

## 15. Inferred but not directly shown

These would be needed to actually build but weren't demonstrated in the video. Flag for follow-up before implementation:

1. **What does `Request Revision` do?** Returns to objective editing? Opens a revision-comments modal? Re-runs planning with adjustments? Not demonstrated.
2. **What happens if the user clicks `Regenerate Plan` after development started?** Does it wipe build state? Confirm dialog? Not demonstrated.
3. **Tasks sub-tab drag-and-drop semantics:** subtitle says "Drag and drop tasks to assign agents" — what does "assign agents" mean? Single-agent vs distributed assignment? `Mode: distributed` suggests there are other modes. Not demonstrated.
4. **Where does the "Coding Objective" data persist?** Refresh behavior unknown; is there a project-list view we don't see?
5. **Agent Activity / Event Log inner-tabs** (under Execution sub-tab) were never opened — we know they exist but not their content.
6. **GOAP sub-tab in the modal** — opened but specific controls not captured in the frame extraction. Likely contains: state-space parameters, planning algorithm choice (A* / others), heuristic weights, max plan depth.
7. **"Widget Demo" and "Create Widget"** links on the parent page were never clicked — unknown what they produce.
8. **Authentication / multi-tenancy** — no login screen, no user identity visible. Either it's a public demo or auth is out-of-band.
9. **API contract** — no network tab observation; we don't know request/response shapes. If we need parity with goal.ruv.io's backend, we'd have to capture HAR or reverse-engineer.
10. **Numbers shown (12 components, 42 files, 124 tests, 8 endpoints, etc.)** — are these computed by the planner or static placeholder values? In the demo, the same numbers appear for both the "ict indicator" objective and (presumably) any objective. Looks like placeholder/dummy data.

---

## 16. Build-decision checkpoint

Before writing any code, decide:

1. **Are we cloning the UI exactly, or designing our own that does the same job?** The Hermes-side existing `ruflo-goap-control` plugin already has a working tab with blueprint cards + readiness posture + safe launch endpoints — that's an alternative interaction model. Do we replace it or layer the goal.ruv.io aesthetic on top?
2. **Is the plan generation real (LLM call) or scripted/canned for the demo?** The values (12 components, 5 phases, etc.) look static. We need to decide whether our build does real GOAP planning or canned templates initially.
3. **Frontend stack:** likely React + Tailwind based on visual cues; matches Hermes dashboard's current stack. Reuse `web_dist/` build pipeline.
4. **Backend stack:** matches existing `plugin_api.py` shape — FastAPI router mounted at `/api/plugins/<name>/`. The 12 existing routes we already shipped this morning give us most of what's needed; we'd add `/plan/research-phases`, `/build/dev-phases`, etc.
5. **What's the "Joseph" angle on this?** The existing tab is operations-focused (registered run IDs, allowlisted root, safety guardrails). The goal.ruv.io tab is research-focused (canned demo of capability). They're not the same product — clarify which we're building.

---

**Document end.** Backing data in same directory: `frames/` (62 high-res JPGs to be copied here), `frame-readings.md` (verbatim text per frame), `gemini-flow-pass.md` (timeline-focused video analysis).
