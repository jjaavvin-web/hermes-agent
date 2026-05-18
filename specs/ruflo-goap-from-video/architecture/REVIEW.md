# REVIEW.md

## Verdict
REVISE

---

## Mandatory checks

### 1. SPEC coverage — PASS (with one gap)

Every SPEC §3–§11 screen has a (backend route, frontend component) pair in ARCHITECTURE.md §10. The coverage matrix is credible and well-structured.

One gap: ARCHITECTURE.md §10 maps §3 empty state to `<EmptyStateCard>` but that component does not appear in the component tree in §4. The tree shows `<CodingObjectivePanel>` with its children, but `<EmptyStateCard>` is neither a child of it nor separately listed. This is a minor authoring gap, not a missing screen.

### 2. Route dispositions — PASS

All 12 existing routes in ARCHITECTURE.md §2.1 have explicit KEEP / MODIFY / KEEP+MODIFY / REPLACE dispositions. No route is left without a disposition. Zero routes are deprecated, confirmed in the paragraph following the table.

### 3. Migration safety — PARTIAL FAIL

DATA-MODEL.md §3.2 claims that `_load_registry` is "already permissive" and that "Pydantic's `model_validate` with `extra='ignore'` (default) will accept old records that lack the new optional fields."

Reading `plugin_api.py:164-179`, `_load_registry` does NOT use Pydantic at all. It calls `json.loads` directly and returns a raw `dict[str, Any]`. The permissiveness claim is true for the current code (it accepts any dict), but it is true precisely because there is NO pydantic validation today. When Hive 2 introduces the new `Project` pydantic model and wires it into routes, the risk is that some route handler will call `Project.model_validate(record)` on an existing record that has fields with unexpected types or a shape the model doesn't expect.

The specific exposure: the existing `advanced_config` field is already present in `projects.json` as an "opaque dict." DATA-MODEL.md §2.1 types it as `advanced_config: dict`, which is fine — but if any existing record has this field typed as something other than a dict (e.g., null, or a list), model_validate will raise a ValidationError and the load will fail. The DATA-MODEL.md does not specify what `extra="ignore"` behavior is required on `advanced_config`'s sub-fields. The claim "old records remain valid" is not verified against the actual field values in the live `projects.json` — the doc says "Hive 2 must encode tests" but these tests are a future deliverable, not a current verification.

The migration claim is conditionally verifiable but the condition (Hive 2's test fixture matching the real live data) has not been satisfied yet, so the claim cannot be marked PASS at Hive 1 stage.

### 4. Safety rules preserved (10/10)

| # | Rule | Where honored | Verdict |
|---|---|---|---|
| 1 | No arbitrary shell from HTTP payloads | ARCHITECTURE.md §6: "No route accepts a raw shell argument"; §7.2 uses explicit arg lists, no shell=True; confirmed in `/tasks/{id}/assign` allowlist | HONORED |
| 2 | Runs created only in allowlisted root | DATA-MODEL.md §2.1 validation rules: "workdir must resolve under an allowed root (existing `_validate_root`)"; ARCHITECTURE.md §6 every new route validates run_id | HONORED |
| 3 | Start/stop only on registered run IDs | ARCHITECTURE.md §6: "Every new route validates `run_id` against the existing `_validate_run_id` regex"; existing route 8 KEEP | HONORED |
| 4 | tmux sessions stopped only if registered | ARCHITECTURE.md §2.1 route 9 KEEP; existing session-name pattern check preserved | HONORED |
| 5 | API-key env vars scrubbed | ARCHITECTURE.md §7.4 quality_runner: "scrubbed env (reuse `_env()`)"; RISKS.md R6 full analysis; ARCHITECTURE.md §7.5 "already real" | HONORED |
| 6 | Dry-run path exists | ARCHITECTURE.md §1: "dry-run-available" listed as preserved | HONORED (noted as preserved; no new dry-run routes added but existing path kept) |
| 7 | No git operations from hive workers | BUILD-PLAN.md §6.3: "hive queens commit/push/PR per the standard pattern; workers DO NOT touch git" | HONORED |
| 8 | No mutations to live "ms" project | RISKS.md R1 full mitigation; DATA-MODEL.md §4; ARCHITECTURE.md §6 DELETE hard-rejects ms slug; sha256 acceptance criterion BUILD-PLAN.md §1.2 check 4 | HONORED |
| 9 | Migration safety / backwards-compatible schema | DATA-MODEL.md §3: additive-only; preflight backup; tests in §3.4 | HONORED (but see mandatory check 3 above — partial confidence) |
| 10 | Browser-validate-before-shipped | BUILD-PLAN.md §3.2 acceptance criterion #2; Hive 5 explicit browser MCP click-every-button; RISKS.md R7; BUILD-PLAN.md §4.4 honest-failure constraint | HONORED |

All 10 rules have explicit callouts. Rule 6 is the weakest — "dry-run-available" appears only in the §1 summary and the architecture does not show how new routes participate in dry-run. This should be cross-checked by Hive 2.

### 5. Acceptance criteria concreteness — PARTIAL FAIL

Most criteria are concrete and testable. Flagged hedging:

- BUILD-PLAN.md §3.2 criterion 1: "All 24 SPEC-defined UI states render and behave **per spec**" — "per spec" is unquantified. The 27 states in RESEARCHER-NOTES.md §A don't match "24 SPEC-defined states." The number is unexplained and not cross-referenced to any enumeration. A worker cannot know when they have satisfied "24 states."
- BUILD-PLAN.md §3.2 criterion 2: "Every backend route called (verified by network tab observation **or** headless test)" — the "or" makes this non-deterministic. Network-tab observation is not a reproducible test artifact.
- BUILD-PLAN.md §5.2 criterion 2: "Hive 5 `results.jsonl` re-runnable with **strictly more passes than before**" — "strictly more" when re-running a deterministic clickthrough against the same code is not guaranteed and provides a weak bar. Should be "all previously passing trials still pass."
- BUILD-PLAN.md §5.1 task 6A: "Apply every Hive 5 MAJOR defect fix **in priority order**" — no priority ordering mechanism is defined. This is hand-wavy guidance, not a testable criterion.

### 6. Rollback completeness — PASS

Every risk scored ≥ 12 has a rollback:

| Risk | Score | Rollback in §5 |
|---|---|---|
| R1 (ms mutation) | 15 | cp .bak + restart — PRESENT |
| R2 (React contract) | 15 | cp index.js.bak + restart — PRESENT |
| R3 (fd exhaustion) | 12 | revert SSE PR + fallback to polling — PRESENT |
| R4 (schema break) | 12 | revert models.py PR + restore bak — PRESENT |
| R5 (symlink) | 12 | re-apply symlink command — PRESENT |
| R7 (Hive 5 critical) | 12 | chain auto-halts, nothing merged — PRESENT |

All ≥12 risks have rollbacks. No gaps.

---

## Numbered concerns

### C-1: SSE max-connection-age expiry unhandled on the client side
**Severity:** major
**Where:** ARCHITECTURE.md §5.2, §6 (logs/stream and progress/stream notes)
**Issue:** ARCHITECTURE.md §6 states "max-connection-age 600s, client must reconnect after that." The SSE spec says the browser's native `EventSource` will automatically reconnect after the server closes the connection — but it will re-send the `Last-Event-Id` header and reconnect to the beginning of the stream (or a cursor offset, per ARCHITECTURE.md §7.3). The server's "treat cursor as byte offset and clamp to file size" logic is only specified for `logs/stream`; it is NOT specified for `progress/stream`. If `progress/stream` reconnects after 600s, it will replay all phase events from the beginning, causing the frontend's Zustand slices to receive stale "phase P1: researching" events that overwrite the current (further-along) state — a silent regression mid-run.
**Suggested mitigation:** Specify that `progress/stream` reconnect emits only events with timestamps newer than `Last-Event-Id`, or suppress replayed events in the Zustand reducer by ignoring events with `at` values older than the current slice state. One of these must be chosen before Hive 2 builds `progress_stream.py`.

### C-2: Route path conflict — `GET /config/{run_id}` vs `GET /config/presets`
**Severity:** blocker
**Where:** ARCHITECTURE.md §3.7 and §6
**Issue:** FastAPI routes are matched in registration order. `GET /config/{run_id}` is a parameterized route; `GET /config/presets` is a literal path. If `GET /config/{run_id}` is registered first in `plugin_api.py`, then `GET /config/presets` will match the parameter route with `run_id="presets"`, which will then fail the `_validate_run_id` regex check and return 400 instead of the presets list. This is a classic FastAPI ordering trap.
**Suggested mitigation:** Either register `GET /config/presets` before `GET /config/{run_id}` (and document this constraint for Hive 2), or restructure the URL to avoid the conflict (e.g., `GET /config/preset-bundles` or `GET /configs/presets`). Must be called out explicitly in Hive 2's wiring instructions.

### C-3: `_load_registry` returns raw dict; Hive 2 pydantic adoption path is underspecified
**Severity:** major
**Where:** DATA-MODEL.md §3.2; plugin_api.py:164-179
**Issue:** The existing `_load_registry` returns `dict[str, Any]` and is called in 8+ places in `plugin_api.py`. DATA-MODEL.md §3.2 says Hive 2 introduces a `Project` pydantic model, but does not specify whether Hive 2 should (a) wrap only the new routes in model_validate, leaving existing routes using raw dicts, or (b) update `_load_registry` to return `list[Project]`. Option (a) creates two code paths for the same data. Option (b) is a larger refactor that could break the 4 existing tests if the model is too strict. The doc says "model_validate with extra='ignore'" but does not say which routes call model_validate vs raw-dict access, or whether `_load_registry` itself changes.
**Suggested mitigation:** Specify explicitly in DATA-MODEL.md §3.2 (or BUILD-PLAN sub-task 2A) whether `_load_registry` signature changes. The safest path is: keep `_load_registry` returning raw dict; new routes call `Project.model_validate(record, strict=False)` independently; old routes remain unchanged. State this explicitly so Hive 2 doesn't invent the strategy.

### C-4: `POST /runs/{run_id}/tasks/{task_id}/assign` agent allowlist source is ambiguous
**Severity:** major
**Where:** ARCHITECTURE.md §6, §7.2
**Issue:** ARCHITECTURE.md §6 states agent names must be "allowlisted" and §7.2 says "agent names must match one of `swarm.agents[].name` from the plan." But `swarm.agents` is typed as `dict` in DATA-MODEL.md §2.2 (`swarm: dict` — the plan's swarm block). There is no `SwarmAgent` model defining the `agents[]` array shape, and the plan's `swarm.agents[].name` field is not part of any defined schema. Hive 2 will implement the allowlist check against an undefined field path. If the `_build_plan` function's deterministic output doesn't actually populate `swarm.agents[].name` in the JSON, the allowlist will either always be empty (rejecting all assigns) or the code will raise an AttributeError.
**Suggested mitigation:** Add a `SwarmAgent` model to DATA-MODEL.md §2.2, or at minimum specify the exact JSON path in the plan object that Hive 2 should read for the agent allowlist. Cross-check against `plugin_api.py:388-432` where `_build_plan` constructs the swarm block to confirm `agents` is already populated there.

### C-5: `DELETE /projects/{run_id}` moves workdir but new sidecars are not removed
**Severity:** major
**Where:** DATA-MODEL.md §5 (lifecycle), ARCHITECTURE.md §6
**Issue:** DATA-MODEL.md §5 says "DELETE moves `$WORKDIR` to `$WORKDIR.deleted.<TS>` for 7-day soft-delete." However, the per-project sidecar files (`.research-phases.json`, `.dev-phases.json`, etc.) live inside `$WORKDIR`, so they are moved with the workdir — that part is fine. But the `project.research_phases_path`, `project.dev_phases_path` etc. fields in `projects.json` still point to the now-moved paths. After deletion, if `GET /projects` returns those records (with `deleted_at` set), any route that tries to open these paths will get FileNotFoundError. The architecture does not specify that `GET /projects` filters out soft-deleted records, nor that deleted records' sidecar paths are nulled out.
**Suggested mitigation:** Specify in DATA-MODEL.md §5 or ARCHITECTURE.md §6 that `GET /projects` filters records where `deleted_at` is set (or adds a `?include_deleted` param). Also specify that on deletion the sidecar path fields are nulled in the record before the registry write.

### C-6: LOC estimate overshoot is accepted without constraint revision
**Severity:** minor
**Where:** BUILD-PLAN.md §6.1
**Issue:** BUILD-PLAN.md §6.1 acknowledges the total is ~10,000-11,000 LOC vs the GAMEPLAN's stated "~6,000-8,000 LOC." The explanation given is "consequence of hand-rolling SVG instead of taking a graph-viz dep." But the SVG choice saves ~50KB runtime, not LOC — the LOC difference between using React Flow and hand-rolling SVG for 5 linear nodes is at most 150 LOC (per ARCHITECTURE.md §8), which does not explain a 2,000-3,000 LOC overshoot. The real driver is that Hive 4 alone is estimated at 5,000 LOC, which is ~60% of the GAMEPLAN's ceiling for the entire project. GAMEPLAN criterion #10 is "≥75% coverage" — at 5,000 LOC, achieving ≥60% coverage on the frontend requires 3,000 LOC of tests, which is not reflected in the Hive 4 estimates (400 LOC for 4O).
**Suggested mitigation:** Either revise the Hive 4 LOC estimate with a breakdown (component LOC vs test LOC vs config), or explicitly acknowledge the GAMEPLAN's LOC estimate was wrong and update it. Don't silently carry a 30% overshoot into the build.

### C-7: Smoke-e2e.sh polling loop has no tmux-alive guard
**Severity:** major
**Where:** BUILD-PLAN.md §2.3
**Issue:** The Hive 3 smoke script polls `effective_status` for up to 120 iterations (120s), checking for `"completed"`. If the Ruflo swarm fails fast (exits with error in <5s), the loop will run 120s unnecessarily before timing out. More critically, the loop checks `/projects` list via `jq ".projects[] | select(.run_id==\"$RID\")"` — if the `ms` project is also running, that jq filter must work correctly; but if the endpoint returns all 14 projects and one has a weird character in its run_id, the jq filter will silently produce no output and `STATUS` will be empty, causing the loop to run to timeout. There's also no check for `effective_status == "failed"` — a fast-failing run will not break the loop early, wasting 120s before `exit 1` via the "no logs" or "no report" check.
**Suggested mitigation:** Add `|| [[ "$STATUS" == "failed" ]] && { echo "run failed"; exit 1; }` to the polling break condition. Add a `tmux has-session -t "rfg-*"` check after the start call to confirm the session spawned before polling.

### C-8: Config `PUT /config/{run_id}` path conflict with `DELETE /projects/{run_id}` — `ms` slug guard not applied
**Severity:** minor
**Where:** ARCHITECTURE.md §6 (config routes)
**Issue:** `PUT /config/{run_id}` has no guard for the `ms` project. A caller can write a new config override to the running `ms` project's `$WORKDIR/.config.json`. The next time someone hits `POST /runs/rg_91b80749ac82/regenerate-plan`, the newly-saved config would be picked up. While this is not immediately destructive (the current run ignores it), it muddies the "ms is read-only" invariant. The `DELETE /projects/{run_id}` is guarded; `PUT /config/{run_id}` should be too, or the read-only invariant should be explicitly scoped to "no start/stop/delete mutations" rather than "no writes of any kind."
**Suggested mitigation:** Clarify in ARCHITECTURE.md §6 whether `PUT /config/{run_id}` is allowed for running projects. If it is (since it doesn't restart the run), document this explicitly. If not, add a 403 guard matching the DELETE guard logic.

### C-9: `GEMINI_API_KEY` not in ENV_SCRUB list
**Severity:** major
**Where:** plugin_api.py:36-42; ARCHITECTURE.md §7.4; RISKS.md R6
**Issue:** `plugin_api.py:36-42` defines `ENV_SCRUB = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"]`. GAMEPLAN.md §"Safety preservation rules" rule 5 states "API-key env vars scrubbed (`ANTHROPIC_*`, `OPENAI_*`, `GEMINI_*`, etc.)." `GEMINI_API_KEY` is explicitly listed in the GAMEPLAN safety rule but is not in `ENV_SCRUB`. RISKS.md R6 only references the existing list and says to reuse `_env()`, without flagging the omission. Hive 3's quality_runner subprocess invocations will not scrub Gemini keys if they are in the environment.
**Suggested mitigation:** Add `GEMINI_API_KEY` (and any other `GEMINI_*` vars) to `ENV_SCRUB` in `plugin_api.py` as part of Hive 2's work. ARCHITECTURE.md §7.4 should reference this addition explicitly. This is a gap between the GAMEPLAN's stated rule and the existing implementation that the architecture docs do not flag.

### C-10: `POST /config/validate` return shape inconsistency
**Severity:** minor
**Where:** ARCHITECTURE.md §6
**Issue:** `POST /config/validate` returns `{ ok: bool, config: AdvancedConfig (normalized), warnings: list[str] }` per §6. `PUT /config` and `PUT /config/{run_id}` return `{ ok: bool, config: AdvancedConfig, persisted_at: str }` per §6. The validate endpoint normalizes the config (e.g., clamps out-of-range values to bounds), but the architecture does not specify what "normalized" means when a value is out of range — is the field clamped to the nearest valid value (silent fix), or is the field rejected and a warning emitted? If clamped, a caller who submits `max_agents: 25` gets back `max_agents: 20` in the normalized config with a warning but no error — the 422 path is bypassed. This creates a behavioral inconsistency: the same payload that would 422 on PUT succeeds on validate with normalization.
**Suggested mitigation:** Specify explicitly in ARCHITECTURE.md §6 or DATA-MODEL.md §2.6 whether `validate` normalizes-and-warns vs rejects-and-422s for out-of-range values. Pick one; document it; apply it consistently.

### C-11: Tasks sub-tab in Research Review mode is unspecified
**Severity:** major
**Where:** ARCHITECTURE.md §10 (coverage matrix); RESEARCHER-NOTES.md §A note
**Issue:** ARCHITECTURE.md §10 maps SPEC §7 to `<TasksSubTab runId>` as a shared component across both research and development modes. RESEARCHER-NOTES.md §A notes: "The 'Research Review' sub-tab for Tasks was never clicked in any frame — the cursor hovers Tasks in scene13 but does not activate it. That tab's content is unknown." The architecture assumes the Tasks sub-tab content is the same in both modes (it passes only `runId`, not `mode`). But in research mode, no tasks exist yet (tasks are post-launch). The component tree in §4 shows `<TasksSubTab runId>` under `<SubTabs mode={mode} runId={runId}>` but says nothing about the research-mode rendering. If a user clicks Tasks in Research Review, they will either see an empty board (if tasks are unpopulated) or the same dev tasks (which are semantically incorrect in a pre-launch research context). Neither behavior is specified.
**Suggested mitigation:** Explicitly specify in ARCHITECTURE.md §4 or §10 what `<TasksSubTab>` renders in `mode="research"` — either an empty-with-explanation state ("Tasks will appear after launch"), or the component is hidden/disabled in research mode.

### C-12: `progress_stream.py` phase-detection in Hive 3 is fragile
**Severity:** major
**Where:** ARCHITECTURE.md §7 (Hive 3 spec), BUILD-PLAN.md §2.1 task 3E
**Issue:** BUILD-PLAN.md task 3E specifies: "Real progress_stream: subscribe to file-watcher events on sidecars; tail hive-mind.log to detect phase transitions ('✓ Phase N Complete')." Detecting phase transitions by grepping for the literal string `"✓ Phase N Complete"` in `hive-mind.log` is fragile: (a) `N` is a placeholder — the exact string format must be specified; (b) if Ruflo or Claude changes the log output format (even a minor phrasing change), the detection breaks silently and the progress stream emits no phase-advance events; (c) the `✓` character is a Unicode glyph that may not be preserved through all logging chains. The architecture provides no fallback for when the phase-transition string is not found.
**Suggested mitigation:** Specify the exact regex that Hive 3 uses for phase-transition detection (e.g., `r"✓ Phase \d+ Complete"`) and commit it as a constant in `progress_stream.py`. Specify a fallback: if no phase-transition event is seen within 120s of the previous event, emit a `heartbeat` and let the frontend infer "still in previous phase." Document that this is a known brittleness and log a WARNING to `actions.jsonl` when the pattern is not matched.

---

## Items intentionally NOT flagged

1. **The 5,000 LOC Hive 4 estimate for the frontend.** This looks large but is plausible for a full TypeScript+TSX+Tailwind rewrite of a 5-sub-tab, 2-mode, modal-bearing React app with loading/error/empty states for every panel. The 2-coder split is the right call. I considered flagging this as inflated but concluded it's defensible.

2. **The section-level config merge in DATA-MODEL.md §7.2.** A reviewer could argue this is surprising behavior (you can't partially override the `swarm` section — it's all-or-nothing per section). I considered flagging this but the rationale ("the modal exposes sections atomically") is sound and the behavior is documented. Not a bug.

3. **The absence of rate limiting on `POST /runs/{run_id}/quality/run`.** A caller could spam quality runs. I considered flagging this but the architecture already specifies a `.quality-running.pid` lock file (DATA-MODEL.md §5) and the endpoint returns 409 if a gate is already running (ARCHITECTURE.md §6). The concurrency guard is adequate for a single-operator tool.

4. **The RESEARCHER-NOTES.md §F item 2 (GOAP modal sub-tab controls unconfirmed).** The queen resolves this by cross-referencing `agents/spec` at `plugin_api.py:541`. That cross-reference is valid and the resolution is sound. Not a gap.

5. **The stacked-PR strategy with the plugin outside git management.** Option B (specs-repo mirror + symlink/copy) is pragmatic and matches the existing Hermes install pattern. The alternative (initializing git in `~/.hermes/plugins/`) is more complex and creates the remote-management burden the doc identifies. I considered flagging the Option B ambiguity (who runs `hermes update` after merge?) but this is an ops question already deferred to Joseph, not an architecture defect.

