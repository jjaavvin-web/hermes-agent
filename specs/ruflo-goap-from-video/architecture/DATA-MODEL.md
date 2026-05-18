# DATA-MODEL.md — Ruflo GOAP Operator Console Persistence

**Hive:** 1 — ARCHITECT
**Audience:** primarily Hive 2 (BACKEND-CORE) — Hive 3 also reads §6 (event log) and §7 (config override semantics)
**Related:** `ARCHITECTURE.md` §2.3, §5, §6. Schemas defined here are referenced by API routes there.

This document specifies every persisted object the plugin reads or writes, the storage backend for each, the schema migration story for the 13 existing projects in `~/.hermes/ruflo-goap-control/projects.json`, and the event-log/SSE-stream shape for live observability.

---

## 1. Storage choice — flat files, not SQLite

### 1.1 The recommendation

**Keep `projects.json` as the canonical registry. Do NOT migrate to SQLite.** Add per-project sidecar files inside each `$WORKDIR` for live state (research phases, dev phases, tasks, quality runs, config override), and one new global file (`~/.hermes/ruflo-goap-control/config.json`) for the default Advanced Configuration.

### 1.2 Rationale

| Criterion | Flat files (recommended) | SQLite (rejected) |
|---|---|---|
| Current record count | 13 projects (`projects.json` is ~12KB; ~10x headroom available before any felt latency) | n/a |
| Realistic 1-year growth | At Joseph's observed cadence (~3-10 staged projects per day during dev periods, ~0-1 per day idle) → ~500-1500 records | n/a |
| Concurrent writers | One: the FastAPI process (single uvicorn worker per dashboard daemon, verified) | Solves a problem we don't have |
| Atomic write story | Already implemented at `plugin_api.py:182-188` via tmp-write + `replace()` | SQLite WAL adds machinery for a non-issue |
| Operator inspectability | `jq`, `less`, `cat`, `grep` all work directly on disk | Requires `sqlite3` CLI + remembering schema |
| Backup / rollback | `cp projects.json projects.json.bak.<ts>` (already done by `preflight.sh` per GAMEPLAN §"Rollback") | `sqlite3 .backup` ceremony |
| Migration story | Schema is additive; no migration needed (see §3 below) | Requires a one-time export-import |
| Test fixture creation | Write a dict, dump to JSON, point env at it (this is exactly what `tests/test_plugin_api_closeout.py:15-27` does) | Build a SQL fixture or use an ORM |
| Audit-friendliness | Plain text on disk; existing tooling (`hermes`, `jq`, watchers) reads it without a SQL driver | Requires teaching every tool about a SQL schema |

The only argument FOR SQLite would be cross-row queries (e.g., "show me all running projects in the Medical category"), but the current `/projects` route already loads-all-and-filters-in-Python at line 582-591, which scales fine to 5-digit row counts on Joseph's hardware. SQLite is the right answer at a different scale; we're not there.

### 1.3 Where each kind of data lives

| Object | Storage | Path | Why here |
|---|---|---|---|
| Project registry | flat JSON | `~/.hermes/ruflo-goap-control/projects.json` | Existing; 13 records; atomic-write already shipped |
| Per-project research phases | flat JSON (sidecar) | `$WORKDIR/.research-phases.json` | Co-located with the run for blast-radius isolation |
| Per-project dev phases | flat JSON (sidecar) | `$WORKDIR/.dev-phases.json` | Same |
| Per-project GOAP state | flat JSON (sidecar) | `$WORKDIR/.goap-state.json` | Same |
| Per-project tasks snapshot | flat JSON (sidecar) | `$WORKDIR/.tasks.json` | Backed by `hermes kanban` reads at refresh; cached here |
| Per-project quality runs | flat JSON (sidecar) | `$WORKDIR/.quality.json` | Includes `ran_at`, `evidence_path` |
| Per-project config override | flat JSON (sidecar) | `$WORKDIR/.config.json` | Honored by next launch via env var |
| Global default config | flat JSON | `~/.hermes/ruflo-goap-control/config.json` | One per machine |
| Status sidecar (existing) | flat JSON | `$WORKDIR/.ruflo-status.json` | Already written by `watcher.sh` — KEEP, untouched |
| Tracking card ID (existing) | text file | `$WORKDIR/.tracking-card` | Already written by `launch.sh` — KEEP |
| Watcher PID (existing) | text file | `$WORKDIR/.watcher.pid` | Already written by `launch.sh` — KEEP |
| Plan (existing) | flat JSON | `$WORKDIR/GOAP-PLAN.json` | Already written by `stage_project` — KEEP, extended shape per ARCHITECTURE §5.1 |
| Run manifest (existing) | flat JSON | `$WORKDIR/RUN-MANIFEST.json` | Already written; extended to include `advanced_config_version` |
| Action event log | append-only JSONL | `~/.hermes/ruflo-goap-control/logs/actions.jsonl` | Already exists; new event kinds appended |
| Hive-mind log (existing) | text | `$WORKDIR/hive-mind.log` | tee'd by `launch.sh` — KEEP |
| Watcher log (existing) | text | `$WORKDIR/watcher.log` | KEEP |
| Final report (existing) | markdown | `$WORKDIR/FINAL-REPORT.md` | KEEP — must remain at workdir root (lesson `6f42c8b1-ff20-4e58-8cb0-baef778b89f6`) |

**Net new files added by this build: 6 sidecars per project + 1 global config + new event kinds in the existing JSONL.** No filesystem layout changes elsewhere.

---

## 2. Schemas — every persisted object

All schemas expressed as Pydantic v2 models so Hive 2 can paste them verbatim into a `models.py` module.

### 2.1 Project (the registry record — extends existing)

```python
class Project(BaseModel):
    # Existing fields (preserve verbatim — projects.json has 13 records using this shape)
    run_id: str                                # "rg_[a-f0-9]{12}"
    name: str
    slug: str
    objective_preview: str                     # truncated to 500 chars
    category: str                              # "Coding"/"Medical"/etc. from SPEC §2 chips
    plan_summary: str
    plan_file: str                             # absolute path to $WORKDIR/GOAP-PLAN.json
    advanced_config: dict                      # opaque dict in existing records; typed in v0.2
    workdir: str                               # absolute path
    session: str                               # tmux session name "rfg-<slug>-<run_id[-4:]>"
    status: Literal["staged","running","stopped","failed","completed"]
    created_at: str                            # ISO 8601 Zulu
    updated_at: str
    tracking_card: Optional[str]               # kanban "t_..."
    watcher_pid: Optional[str]                 # PID as string (existing format)
    launch_script: str
    watcher_script: str
    launch_prompt: str
    hive_log: str
    watcher_log: str
    final_report: str
    template_hashes: dict[str, Optional[str]]  # {"launcher": "sha256...", "watcher": "sha256..."}
    source: str                                # "Hermes dashboard /ruflo-goap"
    # Existing optional fields seen in some records
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    launch_returncode: Optional[int] = None
    last_error: Optional[str] = None

    # NEW (additive — all optional; old records remain valid)
    plan: Optional[dict] = None                # full PlanResponse cached here too
    advanced_config_version: int = 1
    research_phases_path: Optional[str] = None # convenience pointer to $WORKDIR/.research-phases.json
    dev_phases_path: Optional[str] = None
    tasks_path: Optional[str] = None
    quality_path: Optional[str] = None
    config_override_path: Optional[str] = None
    deleted_at: Optional[str] = None           # soft-delete marker; record kept for audit trail
```

**Validation rules** (Hive 2's pydantic validators):
- `run_id` regex matches existing `_validate_run_id` (`plugin_api.py:127-130`)
- `session` regex matches existing `_validate_session` (`plugin_api.py:133-136`)
- `workdir` must resolve under an allowed root (existing `_validate_root`)
- All paths are absolute and start with `$HOME` expanded
- `template_hashes.launcher` and `.watcher` SHOULD be present for any project staged after this build; OK to be missing on the 13 existing records

### 2.2 Plan

```python
class StateItem(BaseModel):
    key: str
    label: str                                 # "project defined"
    current_value: bool
    target_value: bool

class Transition(BaseModel):
    key: str
    from_value: bool
    to_value: bool
    applied_at: Optional[str]

class ResearchPhase(BaseModel):
    id: str                                    # "P1".."P5"
    name: str                                  # "Goal Assessment" etc.
    description: str
    sub_steps: list[str]
    outputs: list[str]                         # ["Complexity: Medium", "Estimated Time: 2-4 weeks"]
    status: Literal["pending","researching","complete"] = "pending"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

class DevPhase(BaseModel):
    id: str                                    # "D1".."D5"
    name: str                                  # "Project Setup" etc.
    description: str
    sub_steps: list[str]
    outputs: dict[str, Any]                    # {"Files Created": 12, "Dependencies": 24}
    status: Literal["pending","building","done","failed"] = "pending"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

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

class SwarmAgent(BaseModel):
    name: str                                  # "planner","researcher","builder","reviewer","integrator"
    role: str                                  # human-readable role description

class Swarm(BaseModel):
    topology: str                              # "hierarchical-mesh","mesh","star","ring"
    max_agents: int = Field(..., ge=1, le=20)
    parallel_tasks: int = Field(..., ge=1, le=20)
    agents: list[SwarmAgent]                   # CANONICAL location of agent allowlist

class Plan(BaseModel):
    ok: bool = True
    timestamp: str
    blueprint: str
    mode: Literal["research","development","agents"]
    category: str
    objective: str
    summary: str
    goap: dict                                 # algorithm, heuristic, cost, start_state, goal_state
    swarm: Swarm                               # typed — supports the task-assign allowlist per ARCHITECTURE §6
    steps: list[dict]                          # existing S1..S6 shape
    quality_gates: list[str]                   # textual descriptions (existing)
    expected_artifacts: list[str]              # existing
    research_phases: list[ResearchPhase]       # NEW — 5 entries
    dev_phases: list[DevPhase]                 # NEW — 5 entries (populated at "Approve & Launch")
    goap_state_assessment: dict                # NEW — system_state[], goal_state[], transitions[]
    status_cards: list[StatusCard]             # NEW — 4 entries
    execution_plan_summary: ExecutionPlanSummary  # NEW
```

The plan is persisted in two places: `$WORKDIR/GOAP-PLAN.json` (canonical, written by `stage_project`) and as `project.plan` in `projects.json` (cached for `GET /projects` to avoid one file read per project).

### 2.3 Task

```python
class TaskCard(BaseModel):
    id: str                                    # kanban card id "t_..."
    title: str
    role: Literal["Architecture","Implementation","Testing","Documentation","Code Review","DevOps"]
    priority: Literal["low","medium","high"]
    column: Literal["todo","in_progress","blocked","done"]
    assigned_to: Optional[str]                 # agent name (must be in plan.swarm.agents)
    created_at: str
    updated_at: str
    source: Literal["kanban","stub"]           # so frontend can render a "stubbed" badge in dev mode

class TaskDependency(BaseModel):
    from_role: str
    to_role: str

class TasksSnapshot(BaseModel):
    run_id: str
    columns: dict[str, int]                    # counts
    cards: list[TaskCard]
    dependencies: list[TaskDependency]
    refreshed_at: str
```

Persisted at `$WORKDIR/.tasks.json`. Refreshed on every `GET /runs/{id}/tasks` (read-through cache; refresh budget 5s via `If-Modified-Since` style ETag header — `staleness` field returned in response).

### 2.4 Run (live runtime view)

This is a derived (not persisted) object — it merges the persisted `Project` with live tmux/watcher/sidecar state. The existing `_runtime()` helper at `plugin_api.py:221-251` already produces most of it; we extend it.

```python
class Runtime(BaseModel):
    tmux_alive: bool
    watcher_alive: bool
    final_report_exists: bool
    status_sidecar_exists: bool
    status_reason: Optional[str]
    status_updated_at: Optional[str]
    effective_status: str                      # one of: staged|running|exited|stopped|failed|blocked|timeout|completed|status_unreadable
    # NEW (additive)
    sse_clients: int                           # how many active SSE connections to this run
    last_log_line_at: Optional[str]            # for "is the log stream live?" indicator
    quality_running: bool
```

### 2.5 Log entry

`hive-mind.log` and `watcher.log` are plain text. The SSE wrapper structures each line:

```python
class LogEntry(BaseModel):
    ts: str                                    # ISO 8601 if parsable, else file insert time
    source: Literal["hive-mind","watcher","launch"]
    glyph: Optional[Literal["▶","•","✓","✗","⚠"]]
    message: str                               # _sanitize_text'd; max 4000 chars per line
    raw: Optional[str] = None                  # original line, only included when ?raw=1
```

### 2.6 Config (Advanced Agent Configuration)

Pydantic shapes in ARCHITECTURE §5.6. Restated for completeness:

```python
class AdvancedConfig(BaseModel):
    preset: Literal["development","production","budget","quality","custom"] = "production"
    swarm: SwarmConfig
    goap: GoapConfig
    execution: ExecutionConfig
    model: ModelConfig
    version: int = 1
    saved_at: Optional[str] = None
    saved_by: Optional[str] = None             # for future multi-user; "operator" today
```

Persisted at:
- `~/.hermes/ruflo-goap-control/config.json` (global default)
- `$WORKDIR/.config.json` (per-project override; takes precedence)

### 2.7 ResearchPhase / DevelopmentPhase / QualityGate

ResearchPhase and DevPhase already defined in §2.2 (they live inside Plan). The lists are also persisted separately at `$WORKDIR/.research-phases.json` and `$WORKDIR/.dev-phases.json` for fast loading without re-reading the full plan.

```python
class ResearchPhasesFile(BaseModel):
    run_id: str
    phases: list[ResearchPhase]
    updated_at: str

class DevPhasesFile(BaseModel):
    run_id: str
    phases: list[DevPhase]
    updated_at: str
```

QualityGate:

```python
class QualityGate(BaseModel):
    key: Literal["compile_check","test_coverage","security_scan"]
    label: str                                 # "Compile Check" etc.
    current: Optional[float]
    threshold: Optional[float]
    status: Literal["passed","failed","pending","skipped"]
    evidence_path: Optional[str]               # ".pytest.json" / ".bandit.json" / "ruff.txt"
    ran_at: Optional[str]
    duration_seconds: Optional[float]

class QualityRunFile(BaseModel):
    run_id: str
    gates: list[QualityGate]
    started_at: str
    completed_at: Optional[str]
    triggered_by: Literal["auto","manual"]
```

Persisted at `$WORKDIR/.quality.json`. Each new run REPLACES the file (not appended) — historical quality runs are recoverable from `actions.jsonl` if needed.

---

## 3. Migration strategy — schema changes are ADDITIVE-ONLY

### 3.1 The contract

**Every change to `projects.json` is additive.** New fields are optional with sensible defaults. Old records (the 13 existing) MUST validate against the new schema as-is. No required field is added; no existing field is renamed, retyped, or removed.

### 3.2 Why this works

Reading `plugin_api.py:164-179` (`_load_registry`), the current code does NOT use Pydantic — it returns `dict[str, Any]` directly via `json.loads`. It is permissive *because* it never validates. Hive 2's introduction of the new `Project` model creates a fork in the road. Decision:

**`_load_registry` keeps its current raw-dict signature.** It continues to return `dict[str, Any]` and continues to be called as-is by the existing 8+ call sites. The 4 existing pytest tests rely on this shape and MUST keep passing.

**New routes call `Project.model_validate(record, strict=False)` independently** at the point of use. The pydantic model is a typing contract for the new routes; it is NOT a load-time gate. This means:

- A malformed record (e.g., one with `advanced_config: null`) still loads via `_load_registry` (the raw dict survives).
- Old routes that read `project["status"]` directly continue to work.
- New routes that call `Project.model_validate(record, strict=False)` get type safety and pydantic's coercion for fields like `advanced_config: dict` (with `strict=False`, `None` becomes `{}` per pydantic's coercion rules for `dict` types where `default_factory=dict` is set).

All new pydantic fields are `Optional[X] = None`. The `advanced_config` field is `dict = Field(default_factory=dict)` so a `null` in JSON becomes `{}` after validation. No record in the live registry can fail to round-trip — verified by Hive 2's `test_existing_records_load.py` (see §3.4).

### 3.3 The two-line migration

There is no migration script. There is a preflight check:

```python
def _backup_registry_once_per_day() -> Path:
    """Idempotent daily backup of projects.json. Returns backup path."""
    src = _registry_path()
    if not src.exists():
        return src
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    dst = src.with_suffix(f".bak.{stamp}.json")
    if not dst.exists():
        shutil.copy2(src, dst)
    return dst
```

Called once on plugin import (lazily, on first registry read). The existing `preflight.sh` (per `GAMEPLAN.md` §"Rollback") also takes a backup before any hive starts — `projects.json.bak.<TS>` — so we have double protection.

### 3.4 Backwards-compat assertions Hive 2 must encode as tests

1. **The 13 existing records load without error.** Test: copy the live `projects.json` to a tmp path, load via `_load_registry`, assert 13 records returned, assert each round-trips through the new `Project` pydantic model.
2. **The `ms` project record bytes are unchanged after a read.** Test: sha256 the projects.json file → load → save (no edits) → re-sha256 → assert equal. (Catches accidental key-reordering or whitespace drift.)
3. **A `Project` round-trip produces identical JSON.** Test: `dump(load(file))` must equal `read_text(file)` after sort_keys+indent normalization.
4. **`GET /projects` returns all 13 records and includes the new optional fields as null.** Test: `TestClient` against the route.

If any of these fail, Hive 2 BLOCKS (per its honest-failure path in `hive2-backend/objective.md`).

### 3.5 Forward-compat — what happens to records this hive writes when v0.3 comes?

Hive 2 writes a `advanced_config_version: 1` field on every new project record. Future schema changes bump the version. The plugin reads any version it knows about; unknown versions are logged as a `WARNING` to `actions.jsonl` and the record is treated as if the unknown fields don't exist (forward-compat is one-directional: newer plugin reads older records gracefully).

---

## 4. The 13 existing projects — what stays, what's added

Live snapshot from `projects.json` (counts via `python3 -c "..."`):

- **Total records:** 13
- **By status:** 4 `running`, 1 `staged`, 8 `stopped` (note: the runtime sidecar overrides `running` to `exited`/`completed` for sessions where tmux has died; only `ms` (rg_91b80749ac82) is genuinely live per the GAMEPLAN guard rail)
- **The protected record:** `name="MS"`, `run_id="rg_91b80749ac82"`, `slug="ms"`, `status="running"` — Hive 1 and all downstream hives treat this as read-only

For each of the 13 records, after this build:
- ALL existing keys remain at their existing values (zero mutations)
- The new optional keys (`plan`, `advanced_config_version`, `research_phases_path`, etc.) are NOT backfilled — they're populated only when the project is next opened in the UI (lazy migration)
- If a UI interaction triggers a write (e.g., editing the advanced config of a stopped project), only the touched fields are added; sibling fields stay verbatim

This means: **a user could downgrade the plugin tomorrow and the 13 records would still load.** The version bump from 0.1.0 → 0.2.0 in `manifest.json` is an additive minor.

---

## 5. Sidecar files — placement and lifecycle

```
$WORKDIR/                           # e.g. ~/.hermes/ruflo-work/ms-20260518T100822Z/
├── objective.md                    # existing, written by stage_project
├── LAUNCH-PROMPT.md                # existing
├── GOAP-PLAN.json                  # existing, EXTENDED shape per §2.2
├── RUN-MANIFEST.json               # existing, extended with advanced_config_version
├── launch.sh                       # existing, generated from template
├── watcher.sh                      # existing
├── hive-mind.log                   # existing, tee'd by launch.sh
├── watcher.log                     # existing
├── launch-output.log               # existing
├── FINAL-REPORT.md                 # written by the spawned ruflo/claude
├── .ruflo-status.json              # existing, written by watcher.sh
├── .tracking-card                  # existing
├── .watcher.pid                    # existing
# NEW SIDECARS (all optional; absent until first relevant write)
├── .research-phases.json           # written when /plan/generate succeeds
├── .dev-phases.json                # written when /runs/{id}/start succeeds
├── .goap-state.json                # updated by SSE-pushing background task
├── .tasks.json                     # cached on /runs/{id}/tasks read
├── .quality.json                   # written by quality_runner
├── .config.json                    # written by PUT /config/{run_id}
└── .quality-running.pid            # lock file; absent except during a quality run
```

**Lifecycle:**
- Sidecars are created lazily by the route that needs them.
- Sidecars are NOT cleaned up on `/runs/{id}/stop` — they remain as a post-mortem artifact.
- Sidecars MUST be named with a leading `.` so they don't clutter ordinary `ls` and they're filtered out of any UI directory listing.
- **`DELETE /projects/{run_id}` lifecycle (C-5 mitigation):**
  1. Sidecar path fields on the project record (`research_phases_path`, `dev_phases_path`, `tasks_path`, `quality_path`, `config_override_path`, `plan_file`, `hive_log`, `watcher_log`, `final_report`) are nulled in the projects.json record BEFORE the workdir is moved. This ensures no subsequent read of the soft-deleted record can follow a dangling path.
  2. `$WORKDIR` is moved to `$WORKDIR.deleted.<TS>` for 7-day soft-delete.
  3. `project.deleted_at` is set to ISO 8601 now; the record is RETAINED.
  4. `GET /projects` filters out records where `deleted_at IS NOT NULL` by default. Add `?include_deleted=true` to surface them (for audit or restore).
  5. After the 7-day soft-delete window, an external cleanup job (NOT part of this plugin) may physically remove `$WORKDIR.deleted.<TS>` — the plugin does not auto-prune.

---

## 6. Event log — extending `actions.jsonl`

### 6.1 Existing shape (preserve verbatim)

From `plugin_api.py:190-195`:
```json
{"ts": "2026-05-18T10:08:42Z", "event": "start", "payload": {"run_id": "rg_91b80749ac82", "session": "rfg-ms-ac82", "tracking_card": "t_fe82d580", "workdir": "/home/josep/.hermes/ruflo-work/ms-20260518T100822Z"}}
```

Three keys: `ts`, `event`, `payload`. Tokens/secrets are scrubbed by the existing redactor.

### 6.2 New event kinds

| Event | Emitted by | Payload |
|---|---|---|
| `plan_generate` | existing | unchanged |
| `plan_revise` | `POST /plan/revise` | `{run_id, revision_notes_preview, plan_version}` |
| `dev_phase_advance` | SSE pump task on phase transition | `{run_id, phase_id, status, at}` |
| `research_phase_advance` | SSE pump task | `{run_id, phase_id, status, at}` |
| `goap_state_progress` | SSE pump task on percent change | `{run_id, percent, complete, total}` |
| `task_assign` | `POST /runs/{id}/tasks/{id}/assign` | `{run_id, task_id, agent, column}` |
| `task_move` | inferred from kanban diff during sync | `{run_id, task_id, from_column, to_column}` |
| `quality_run_start` | `POST /runs/{id}/quality/run` | `{run_id, gates, triggered_by}` |
| `quality_run_complete` | `quality_runner` on completion | `{run_id, gates, pass_count, fail_count, duration}` |
| `config_save` | `PUT /config` or `PUT /config/{id}` | `{scope: "global"\|"override", run_id?, preset, fields_changed}` |
| `project_delete` | `DELETE /projects/{id}` | `{run_id, backup_path}` |
| `regenerate_plan` | `POST /runs/{id}/regenerate-plan` | `{run_id, prior_status}` |
| `start` / `stop` / `start_dry_run` / `stop_dry_run` / `start_failed` / `stage` | existing | unchanged |

**Total event-kind catalog after this build: 16.** Hive 2 ships an `EVENT_KINDS` constant tuple at module level for grep-ability and test validation.

### 6.3 Why not move to SSE-only?

Some events (config_save, project_delete) are operator-triggered admin actions that have no UI subscriber in the moment; they need an audit trail. `actions.jsonl` is an append-only file that takes <1ms to write and supports `tail -f` for live operator inspection. SSE is a delivery mechanism for live UI; the JSONL is the durable record. We keep both.

### 6.4 Rotation policy

`actions.jsonl` is currently 36 lines / ~10KB. Realistic 1-year growth at observed rates: ~10K lines / 3 MB. We add a rotation step in `preflight.sh` that, if `actions.jsonl` exceeds 10 MB, moves it to `actions.<TS>.jsonl.gz` (gzip) and starts a fresh file. The plugin's `/action-log` route already tails the last 50KB so rotation is invisible to the UI.

---

## 7. Advanced configuration — persistence semantics

### 7.1 Two levels

- **Global default:** `~/.hermes/ruflo-goap-control/config.json`. One per machine. Read by the modal when no project is selected and as the fallback for projects without an override. Written by `PUT /config` (no run_id).
- **Per-project override:** `$WORKDIR/.config.json`. Optional. Written by `PUT /config/{run_id}`. Takes precedence over the global default when launching THIS project. Created on first `PUT` for the project; deleted if all overrides are reset to global.

### 7.2 The override-merge rule

```python
def resolve_config(run_id: Optional[str]) -> AdvancedConfig:
    global_cfg = _load_global_config() or _factory_default()
    if not run_id:
        return global_cfg
    project = _find_project(_load_registry(), run_id)
    override = _load_override(project)
    if not override:
        return global_cfg
    # shallow-merge at the section level; override takes precedence
    merged = global_cfg.model_dump()
    for section in ("swarm","goap","execution","model"):
        if section in override.model_dump(exclude_unset=True):
            merged[section].update(override.model_dump(exclude_unset=True)[section])
    if override.preset:
        merged["preset"] = override.preset
    return AdvancedConfig(**merged)
```

**Why section-level merge, not deep merge?** Pragmatic: the modal exposes sections atomically (Swarm sub-tab edits all swarm fields together), so partial-section overrides aren't useful and add confusion. Either you override the whole `swarm` block or you don't.

### 7.3 Honored where?

- `runner.start` reads the resolved config and writes it to `$WORKDIR/.config.json` (snapshot for audit). The launched `ruflo hive-mind spawn` invocation gets `-n <max_agents>` and `-t <topology>` from the resolved config.
- `quality_runner` reads `execution.quality_gates` to decide whether to auto-run after dev phases complete.
- `goap_planner` reads `swarm.max_agents` and `goap.algorithm` to populate the plan's swarm metadata.

### 7.4 Factory defaults

The 4 presets (development / production / budget / quality) ship as constants in `config_store.py`. Hive 2 derives them from SPEC §11 + the existing `DEFAULT_CONFIG` in `dashboard/dist/index.js` (lines visible in the IIFE bundle: max_agents=4, topology=hierarchical-mesh, max_cost=0, fallback=false). Joseph's preference for OAuth/Max-only execution is encoded as: `production` and `budget` presets both have `model.max_cost=0` and `model.fallback=false`. `development` allows `max_cost=0.50` and `fallback=true` for fast iteration. `quality` allows `max_cost=2.00`.

---

## 8. Concurrency model

Single-writer per file. The FastAPI process is the only writer to `projects.json` and the sidecar files. Reads outside the FastAPI process (e.g., a watcher reading `.ruflo-status.json`) tolerate partially-written files because all writers use the existing tmp-write + atomic-rename pattern at `plugin_api.py:182-188`.

`actions.jsonl` uses O_APPEND opens (`open("a")` at `plugin_api.py:194`) which on POSIX is atomic per-line up to PIPE_BUF (4KB on Linux). Our event records are well under that. No locking needed.

Two routes that mutate the same project record (e.g., concurrent `PUT /config/{id}` and `POST /tasks/{id}/assign`) serialize through a process-wide `asyncio.Lock` keyed on `run_id`. Hive 2 implements this as a dict of locks created lazily.

---

## 9. Test fixtures Hive 2 must ship

To keep the 4 existing tests green and add coverage for new code:

1. `fixtures/projects-13-snapshot.json` — copy of the live `projects.json` at build start (taken by `preflight.sh`). Used by `test_existing_records_load` and `test_ms_project_immutable`.
2. `fixtures/plan-sample.json` — example `Plan` with all new fields populated. Used by `test_plan_roundtrip`.
3. `fixtures/config-presets.json` — the 4 presets. Used by `test_preset_loads`.
4. `fixtures/tasks-snapshot.json` — the SPEC §7 6-card sample. Used by `test_tasks_stub`.
5. `fixtures/log-sample.log` — SPEC §10 timestamped log sample. Used by `test_log_parser`.
6. `fixtures/quality-sample.json` — pass + fail quality run. Used by `test_quality_serializer`.

The existing test file `tests/test_plugin_api_closeout.py` does not reference any of these fixtures, so adding them does not perturb the existing tests.

---

## 10. Summary — answers to the deliverable's required questions

- **Schema for every persisted object:** §2. 9 model families: Project, Plan, ResearchPhase, DevPhase, StatusCard, ExecutionPlanSummary, TaskCard / TaskDependency / TasksSnapshot, Runtime, LogEntry, AdvancedConfig (+ Swarm/Goap/Execution/Model sub-configs), QualityGate / QualityRunFile.
- **Storage choice:** §1 — keep flat-file `projects.json` + per-project sidecars in `$WORKDIR`. SQLite rejected for the reasons in §1.2.
- **Migration script outline:** §3 — none required. Schema is additive-only; old records remain valid; preflight takes a daily backup as belt-and-suspenders.
- **Live event log shape:** §6 — extend existing `actions.jsonl` with 8 new event kinds; SSE delivery for live UI is orthogonal (the JSONL is the durable record, the SSE channel is the wire).
- **How Advanced Agent Configuration persists:** §7 — global default at `~/.hermes/ruflo-goap-control/config.json` + optional per-project override at `$WORKDIR/.config.json`; section-level merge with override-precedence.
- **Backwards-compat constraints:** §3.4 — explicit pytest assertions in Hive 2 that the 13 existing records load, that `ms` bytes are unchanged after read, and that the `Project` model round-trips losslessly.

**End of DATA-MODEL.md.**
