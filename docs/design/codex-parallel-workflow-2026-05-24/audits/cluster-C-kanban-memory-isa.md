# Cluster C — Kanban + Memory + ISA

## Files audited (path:lines)
- `/home/josep/.local/share/hermes-agent/hermes_cli/kanban_db.py` — 4952 lines
- `/home/josep/.local/share/hermes-agent/tools/kanban_tools.py` — 1140 lines
- `/home/josep/.local/share/hermes-agent/agent/memory_manager.py` — 556 lines
- `/home/josep/.hermes/ISA-SPEC.md` — 307 lines
- `/home/josep/.hermes/wt/isa-enforcement-clean/scripts/isa_lint.py` — 223 lines
- `/home/josep/.hermes/wt/isa-enforcement-clean/scripts/isa_reconcile.py` — 300 lines
- `/home/josep/.hermes/wt/isa-enforcement-clean/scripts/isa_common.py` — 415 lines

---

## Kanban schema and CAS

### SQLite schema — table names + key columns
Defined in `kanban_db.py:753-882` (`SCHEMA_SQL`):

| Table | Key columns |
|---|---|
| `tasks` | `id TEXT PK`, `status`, `claim_lock`, `claim_expires`, `current_run_id`, `consecutive_failures`, `worker_pid`, `last_heartbeat_at`, `max_runtime_seconds`, `skills`, `max_retries` |
| `task_links` | `(parent_id, child_id) PK` — directed dependency edges |
| `task_comments` | `id AUTOINCREMENT`, `task_id`, `author`, `body` |
| `task_events` | `id AUTOINCREMENT`, `task_id`, `run_id`, `kind`, `payload` |
| `task_runs` | `id AUTOINCREMENT`, `task_id`, `status`, `claim_lock`, `claim_expires`, `worker_pid`, `outcome`, `summary`, `metadata` |
| `kanban_notify_subs` | `(task_id, platform, chat_id, thread_id) PK` |

### WAL mode set where
`kanban_db.py:927-928`: `apply_wal_with_fallback(conn, db_label=...)` — imported from `hermes_state`, called on every `connect()`. Falls back to DELETE journal on network filesystems with one WARNING.

### Claim CAS — where is the transaction ensuring one-and-only-one claimant?
`kanban_db.py:1876-1971` (`claim_task()`). The CAS is a `write_txn` (BEGIN IMMEDIATE) containing:

```sql
UPDATE tasks
   SET status = 'running', claim_lock = ?, claim_expires = ?, started_at = COALESCE(started_at, ?)
 WHERE id = ?
   AND status = 'ready'
   AND claim_lock IS NULL
```
— `kanban_db.py:1922-1931`. `cur.rowcount != 1` → returns `None` (loser path). SQLite's WAL lock serializes concurrent writers; at most one claimant wins. Documented in module docstring `kanban_db.py:64-66`.

### Claim TTL: value + enforcement
- Default TTL: `DEFAULT_CLAIM_TTL_SECONDS = 15 * 60` (900 s) — `kanban_db.py:101`.
- Enforced in `release_stale_claims()` (`kanban_db.py:2006-2116`): queries `WHERE status='running' AND claim_expires < now`. Live-PID extension path at `kanban_db.py:2039-2073` extends instead of reclaims if the host-local PID is alive (fix for slow-model no-tool-call issue #23025).

### Heartbeat protocol: interval + column updated
- Column updated: `tasks.claim_expires` and (when `current_run_id` exists) `task_runs.claim_expires` — `kanban_db.py:1993-2001`.
- `heartbeat_claim()` CAS: `WHERE id=? AND status='running' AND claim_lock=?` — `kanban_db.py:1990-1995`. No fixed interval enforced server-side; workers should call every few minutes per `kanban_db.py:97-100`.
- `heartbeat_worker()` (not shown above) updates `tasks.last_heartbeat_at` separately.

### Subprocess.Popen launch line
`kanban_db.py:4131-4139` — `_default_spawn()`:

```python
proc = subprocess.Popen(
    cmd,
    cwd=workspace if os.path.isdir(workspace) else None,
    stdin=subprocess.DEVNULL,
    stdout=log_f,
    stderr=subprocess.STDOUT,
    env=env,
    start_new_session=True,
)
```

`cmd` form (`kanban_db.py:4091-4118`): `[hermes, -p, <profile>, --skills, kanban-worker, [--skills, <extra>, ...], chat, -q, "work kanban task <id>"]`

### Env vars injected into the child
All set in `_default_spawn()` `kanban_db.py:4044-4089`:

| Var | Value | Line |
|---|---|---|
| `HERMES_HOME` | profile-scoped home | 4057 |
| `HERMES_TENANT` | `task.tenant` (if set) | 4065 |
| `HERMES_KANBAN_TASK` | `task.id` | 4066 |
| `HERMES_KANBAN_WORKSPACE` | workspace path | 4067 |
| `HERMES_KANBAN_RUN_ID` | `task.current_run_id` (if set) | 4069 |
| `HERMES_KANBAN_CLAIM_LOCK` | `task.claim_lock` (if set) | 4071 |
| `HERMES_KANBAN_DB` | resolved db path | 4078 |
| `HERMES_KANBAN_WORKSPACES_ROOT` | board workspaces root | 4079 |
| `HERMES_KANBAN_BOARD` | board slug | 4084 |
| `HERMES_PROFILE` | profile arg | 4089 |

---

## Kanban tool API

All tools registered in `tools/kanban_tools.py:1060-1139`.

| Tool | File:line (handler) | Behavior | Env gating |
|---|---|---|---|
| `kanban_show` | `kanban_tools.py:229` | Read-only: full task state, runs, comments, events, worker_context | `_check_kanban_mode` (HERMES_KANBAN_TASK OR kanban toolset) |
| `kanban_list` | `kanban_tools.py:300` | List tasks with filters; calls `recompute_ready` first | `_check_kanban_orchestrator_mode` (kanban toolset AND no HERMES_KANBAN_TASK) |
| `kanban_complete` | `kanban_tools.py:360` | Mark task done; enforces worker task ownership; validates `created_cards` | `_check_kanban_mode` |
| `kanban_block` | `kanban_tools.py:438` | Transition to blocked with reason; enforces ownership | `_check_kanban_mode` |
| `kanban_heartbeat` | `kanban_tools.py:473` | Extends claim TTL via `heartbeat_claim` AND records event via `heartbeat_worker`; reads `HERMES_KANBAN_CLAIM_LOCK` | `_check_kanban_mode` |
| `kanban_comment` | `kanban_tools.py:521` | Append comment; author locked to `HERMES_PROFILE` (not caller-supplied) to prevent poisoning | `_check_kanban_mode` |
| `kanban_create` | `kanban_tools.py:554` | Create child task; inherits `HERMES_TENANT`; validates skills list | `_check_kanban_mode` |
| `kanban_unblock` | `kanban_tools.py:630` | Move blocked→ready; orchestrator-only | `_check_kanban_orchestrator_mode` |
| `kanban_link` | `kanban_tools.py:655` | Add parent→child edge post-hoc; rejects cycles | `_check_kanban_mode` |

**Ownership enforcement:** `_enforce_worker_task_ownership()` at `kanban_tools.py:115-144` — if `HERMES_KANBAN_TASK` is set, rejects any mutating call on a `task_id` that differs from the env var. Orchestrators (no env var) bypass this.

---

## Memory layer

### Provider singleton
`memory_manager.py:200-226` (`add_provider()`): a boolean `_has_external` flag (line 200) rejects any second non-builtin provider with a WARNING. Builtin (`name=="builtin"`) always accepted; exactly one external allowed.

### Write serialization
There is **no explicit lock or queue** inside `MemoryManager`. `sync_all()` at `memory_manager.py:317-326` iterates providers sequentially in a for-loop, calling `provider.sync_turn()` one at a time. Serialization is therefore only as strong as the Python GIL. No asyncio, no threading.Lock, no queue. Failures in one provider are caught and logged (WARNING) but do not block others.

`on_memory_write()` (`memory_manager.py:483-511`) similarly iterates providers sequentially with try/except per provider.

### Namespacing
`initialize_all()` (`memory_manager.py:538-555`) injects `hermes_home` (resolved from `HERMES_HOME` env → `get_hermes_home()`) into `**kwargs` passed to each provider's `initialize(session_id=..., hermes_home=..., **kwargs)`. The `session_id` parameter is the per-session key; `hermes_home` provides the profile-scoped storage root. No `project_key` field exists in `memory_manager.py` — namespacing is entirely delegated to individual provider implementations.

`prefetch_all()` and `queue_prefetch_all()` pass `session_id` through (`memory_manager.py:285-313`).

### MVMS exposure
No `mcp__mvms-writer__*` tool mapping visible in `memory_manager.py` directly. The manager routes tool calls via `_tool_to_provider` dict keyed by schema `name` (`memory_manager.py:231-242`). The MVMS provider's tool names are registered at provider `add_provider()` time by introspecting `provider.get_tool_schemas()`. The actual `mcp__mvms-writer__*` names would live in the MVMS provider class (not in this file). INSUFFICIENT EVIDENCE for exact mcp__mvms-writer→method mapping without reading the MVMS provider implementation.

### Concurrent-write failure mode
`sync_all()` `memory_manager.py:317-326`: failures from a provider raise → caught by `except Exception as e` → logged at WARNING → silently dropped. This is last-write-wins at the session level (the last `sync_turn` to complete is what the provider sees). Under concurrent sessions writing to the same provider, there is no lock: the provider's own implementation determines thread safety. `memory_manager.py` itself provides **no concurrency guarantee beyond sequential-within-a-session iteration**.

---

## ISA-SPEC essentials

### §4 required sections
`ISA-SPEC.md:48-63` (Table of 11 sections, fixed order):

1. Problem, 2. Goal, 3. Out of Scope, 4. Constraints, 5. Criteria, 6. Test Strategy, 7. Git Plan, 8. Decisions, 9. Changelog, 10. Verification, 11. Handback

### §7 reconcile pattern — E4 ephemeral slices
`ISA-SPEC.md:107-113`:

> "Merge each slice back into the master by **ID only**:
> - ISC-N in slice, ISC-N in master → copy checkbox state + Verification block across.
> - ISC-N in slice, *not* in master → **abort and escalate.** That is drift — the worker invented or renumbered a criterion.
> - Never merge by line position or text similarity. ID, or abort."

### Tier definitions

| Tier | Use | Mandatory sections | ISCs |
|---|---|---|---|
| E1 | Trivial, <1hr, 1 file | Goal, Criteria, Verification | 1–5 |
| E2 | Standard, 1 session | + Out of Scope, Constraints, Test Strategy, Git Plan | 5–15 |
| E3 | Substantial / single hive | All 11 | 15–40 |
| E4 | Major / multi-hive | All 11 + ephemeral slices + Reconcile | 40+ |

`ISA-SPEC.md:67-73`

### Git Plan requirement
`ISA-SPEC.md:58` (table row): "Branch, commit points, push, PR. Spelled out — hives do not infer it."

Worked example `ISA-SPEC.md:211-213`:
> "Branch `fix/dashboard-token-persist` off `main`.
> One commit: the persistence change + the test added for ISC-3 / ISC-5.
> Push; open a PR against `main`; do not merge without review."

### isa_id naming convention
`ISA-SPEC.md:22`: "`<isa-id>` = `YYYYMMDD-HHMM_kebab-slug`. Assigned once. **Immutable** — it keys everything downstream."
`ISA-SPEC.md:29` (frontmatter example): `isa: 20260522-1530_dashboard-token-persist`

---

## ISA enforcement scripts (PR #34 / branch `feat/isa-enforcement-clean`)

Scripts live at `/home/josep/.hermes/wt/isa-enforcement-clean/scripts/`. **Not merged to main** per `ISA-SPEC.md:290-293`.

### isa_lint.py — rules enforced

13 checks in `lint()` (`isa_lint.py:39-180`):

| Check | Rule | Line |
|---|---|---|
| 1 | All `REQUIRED_FRONTMATTER` keys present | 64 |
| 2 | `tier` in VALID_TIERS (E1–E4) | 69 |
| 3 | `phase` in VALID_PHASES (scaffold/execute/verify/complete) | 75 |
| 4 | `progress` parses as N/M | 83 |
| 5 | Mandatory sections for tier are present | 91 |
| 6 | At least one ISC in Criteria | 99 |
| 7 | Every non-tombstone ISC has Test Strategy row (E2/E3/E4 only) | 103 |
| 8 | At least one non-tombstone `Anti:` ISC | 111 |
| 9 | Every `[x]` ISC mentioned in Verification body | 117 |
| 10 | Progress N matches actual checked count, M matches total | 126 |
| 11 | Every Changelog entry has all 4 required parts | 141 |
| 12 | (complete-only) No open `[ ]` ISCs remain | 156 |
| 13 | (complete-only) No mandatory section is an unfilled placeholder | 164 |

Pass condition: `len(failures) == 0` → `LintResult.ok=True` → exit 0. All checks accumulate (no short-circuit) — `isa_lint.py:44`.

### isa_reconcile.py — inputs/outputs

`reconcile(master_path, slice_paths, dry_run)` at `isa_reconcile.py:146-261`:

- **Inputs:** one master ISA.md + one-or-more `_ephemeral/<feature>.md` slice paths.
- **Drift check** (`isa_reconcile.py:185-192`): any slice ISC id absent from master → print DRIFT error to stderr → return 1 (abort, master NOT modified).
- **Merge algorithm:**
  1. Collect `desired_states: {isc_id → state}` from slices (last slice wins on overlap).
  2. Collect `slice_vblocks: {isc_id → block_text}` from slice Verification sections.
  3. Compute `state_changes` = ISCs where slice state differs from master.
  4. Surgical string edits on master raw text: swap checkbox states (`_swap_isc_states`), rewrite `## Verification` section, rewrite `progress` N field.
- **Outputs:** master ISA.md written in-place with updated checkboxes, Verification blocks, and progress frontmatter. Returns 0 on success, 1 on drift, 2 on file-not-found.
- `--dry-run` prints merge plan without writing.

### isa_common.py — shared parsing primitives

`isa_common.py` exports `parse_isa(path)` and `parse_isa_text(text, path)` → `Isa` dataclass.

**Frontmatter fields parsed** (`isa_common.py:50-53`): `isa, task, tier, phase, progress, card, board, branch, hive, owner, started, updated` (required minimum; extras allowed).

**ISC regex** (`isa_common.py:78`): `^-\s*\[([ xX\-])\]\s*(ISC-[0-9][0-9.]*)\s*:\s*(.*)$` — captures state, id, and text.

**Changelog parts** (`isa_common.py:70`): `("conjectured", "refuted by", "learned", "criterion now")` — all four required per entry.

**Tier section mapping** (`isa_common.py:56-67`): `TIER_SECTIONS` dict — E1: 3 sections, E2: 7, E3/E4: all 11.

**`find_isa_for_card(card_id)`** (`isa_common.py:395-414`): scans `~/.hermes/work/*/ISA.md` by frontmatter `card:` field — the Kanban bridge lookup.

---

## Concurrency-critical findings

### What is CAS-safe today

- **Claim acquisition:** `claim_task()` uses `BEGIN IMMEDIATE` + `UPDATE ... WHERE status='ready' AND claim_lock IS NULL` — exactly one writer wins per task per board (`kanban_db.py:1876-1972`). Per-board isolation: each board is a separate SQLite file, so multi-board installs have independent CAS (`kanban_db.py:63-66`).
- **Heartbeat extension:** `heartbeat_claim()` CAS on `claim_lock` identity prevents a racing reclaim from extending a stale lock (`kanban_db.py:1989-2003`).
- **Live-PID extension in `release_stale_claims()`:** secondary CAS guard `WHERE claim_expires < ?` prevents double-extend (`kanban_db.py:2040-2057`).
- **Worker ownership enforcement:** `_enforce_worker_task_ownership()` rejects cross-task mutations at the tool layer (`kanban_tools.py:115-144`).
- **`idempotency_key` race admitted:** `create_task()` checks for existing key BEFORE `write_txn`; two concurrent creators with the same key may both insert (`kanban_db.py:1329-1340`). Not CAS-safe.

### What is NOT safe under N concurrent sessions

- **`MemoryManager.sync_all()` has no lock.** Concurrent agent sessions writing memory simultaneously can interleave calls to the same provider (`memory_manager.py:317-326`). The provider's own implementation is the only guard. If two sessions call `sync_turn()` concurrently the behavior is provider-specific — the manager provides no serialization primitive.
- **`idempotency_key` double-insert admitted in source** (`kanban_db.py:1329`: "Race is acceptable: two concurrent creators with the same key might both insert"). For automation that expects exactly-once semantics on key-driven creation, this is a gap.
- **`_add_column_if_missing()` swallows `duplicate column name` errors** (`kanban_db.py:980-986`) as the idempotency mechanism for migrations — correct, but this means concurrent migration-triggering connections silently diverge at the DDL layer.
- **`_migrate_add_optional_columns()` backfill pass** (`kanban_db.py:1101-1153`) wraps in `write_txn` but the CAS guard `current_run_id IS NULL` on the UPDATE covers the invariant; a concurrent claimer could race the backfill and produce an orphan run row that gets marked `reclaimed` — handled but not eliminated.

---

## Open questions for the queen

- **MVMS provider tool names:** `mcp__mvms-writer__*` → which `MemoryProvider` methods? Need to read the MVMS provider implementation (not audited here).
- **Memory write serialization under concurrent agents:** if two kanban workers running in parallel sessions both trigger `on_memory_write()`, what is the MVMS provider's actual lock model? Last-write-wins? Append? The manager has no answer.
- **`idempotency_key` double-insert:** intentional or a known gap? The comment says "acceptable" but for multi-hive fan-out triggering the same task key concurrently, duplicate tasks are possible.
- **ISA enforcement scripts not on main:** `ISA-SPEC.md:290-293` says the scripts exist on `feat/isa-enforcement-clean` and are "pending operator-gated push." What is the gate condition? Is CI wiring (the Kanban bridge's card→Done ISA-phase check) also blocked on that merge?
- **`find_isa_for_card()` scans `~/.hermes/work/*/ISA.md` linearly** (`isa_common.py:407`): no index. At scale (many ISAs) the Kanban bridge's per-tick lookup will be O(n) disk reads. Worth noting before volume grows.
- **`isa_reconcile` "last slice wins" on ISC state conflicts:** if two hives produce different states for the same ISC, the last slice file in the argv list wins silently (`isa_reconcile.py:177`). No conflict detection — is this the intended policy?
