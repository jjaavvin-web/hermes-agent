# This fork vs upstream NousResearch/hermes-agent

This document maps every major fork-local surface in this repository against
upstream `NousResearch/hermes-agent`. Upstream knows nothing about these
subsystems; each is a candidate for silent reversion during an upstream merge.
Read this before touching an upstream merge, and see the
[Upstream update doctrine](#upstream-update-doctrine) section for the
mandatory post-merge checklist.

Conventions used below:

- **Key files** are repo-relative paths that exist in this tree today.
- **Verify** is one command runnable from the repo root (test runner is
  `venv/bin/pytest`; repo addopts already exclude `integration` and apply a
  per-test timeout).
- Anything not directly confirmed in code is marked `(unverified)`.

---

## 1. Security rails (exfil / deny / approval / credential-taint floors)

The fork carries a layered pre-execution command-guard system that upstream
does not have. `tools/approval.py` is the single source of truth: dangerous
command detection, per-session approval state, the credential-exfiltration
deny patterns (`CREDENTIAL_EXFIL_DENY_PATTERNS`, a hardline SEC-1 floor
enforced *before* any yolo/mode-off bypass), a two-step credential-taint
floor (stage-then-exfil detection via `is_session_credential_tainted`), and
per-session deny pattern registries (`get_session_deny_pattern_strings`).
YOLO mode is frozen at module import so a skill cannot flip it mid-process.

The codex command-guard keystone is `check_all_command_guards` in
`tools/approval.py`; `tools/terminal_tool.py` delegates to it for every
terminal command, so it is the true runtime enforcement boundary that
declaration-level gates (READY_SPEC, stop_gates) lean on.

Isolation rails (F1–F5c family, from the t_0113eacc program):

- `agent/codex_session_context.py` — ContextVar-based per-lane worktree
  binding + fail-closed confinement: `require_worktree_confinement_without_worktree`
  arms confinement when a persisted worktree is GONE on autonomous resume
  (F5, escape vector V4), so file tools deny writes rather than fall back to
  process cwd.
- `agent/codex_runtime.py` — refuses the process-cwd fallback when isolation
  is lost on resume.
- `gateway/session.py` — restart-resume floor: resuming a session re-arms the
  autonomous floor, the DISP-5 push/PR/workflow floor, the per-session
  terminal-deny list, and worktree isolation, failing CLOSED if arming fails
  (pinned by `tests/security/test_merge_invariants.py::test_restart_resume_rearms_disp5_floor`).

Key files: `tools/approval.py`, `tools/terminal_tool.py`,
`agent/codex_session_context.py`, `agent/codex_runtime.py`, `gateway/session.py`.

Config/env: `HERMES_YOLO_MODE` (frozen at import; do not rely on it at runtime).

Tests: `tests/security/test_exfil_rail.py`, `tests/security/test_deny_rails.py`,
`tests/security/test_deny_rail_regression.py`,
`tests/security/test_credential_taint.py`,
`tests/security/test_credential_persistence.py`,
`tests/security/test_codex_exec_approval_matrix.py`.

Verify:

```sh
venv/bin/pytest tests/security -q
```

---

## 2. F4 per-delivery worktree broker + lane lease ledger

Upstream's webhook adapter runs agent turns in the live checkout. The fork
adds a per-delivery git-worktree broker so each webhook (Loki lane) delivery
can execute on its own branch/worktree instead of the shared relay worktree.
`agent/worktree_broker.py` owns worktree lifecycle (allocate/release/gc)
under `~/.hermes/codex-wt/<sid>/` plus the port side-table
`~/.hermes/codex-ports.json`, with typed failure modes (`DiskPressureError`,
`LeaseCapacityError`, `RepoStateError` — the broker never stashes operator
work) and a `max_active_leases` cap.

`gateway/platforms/webhook.py` wires it in behind a double gate that is OFF
by default: `HERMES_WEBHOOK_WORKTREE` (master relay-worktree gate) AND
`HERMES_WEBHOOK_PER_DELIVERY_WT` (F4 per-delivery broker). Base branch comes
from `HERMES_WEBHOOK_BASE_BRANCH` (default `fork/main`). The adapter keeps a
per-finalizer lease map (`_lease_by_finalizer`), verifies at run end that
every recorded subprocess cwd stayed inside the leased worktree
(`_runtime_cwds_match_lease`), re-hydrates existing leases on restart with
trust checks (`_hydrate_per_delivery_sessions`), and releases refused leases.

Key files: `agent/worktree_broker.py`, `gateway/platforms/webhook.py`,
`agent/codex_session_context.py`.

Config/env: `HERMES_WEBHOOK_WORKTREE`, `HERMES_WEBHOOK_PER_DELIVERY_WT`,
`HERMES_WEBHOOK_BASE_BRANCH`.

Tests: `tests/agent/test_worktree_broker.py`,
`tests/agent/test_worktree_broker_gc.py`,
`tests/gateway/test_webhook_per_delivery_worktree.py`,
`tests/gateway/test_webhook_f4_binding_failclosed.py`,
`tests/gateway/test_webhook_broker_regression_matrix.py`,
`tests/gateway/test_webhook_finalize_lifecycle.py`.

Verify:

```sh
venv/bin/pytest tests/agent/test_worktree_broker.py tests/gateway/test_webhook_per_delivery_worktree.py -q
```

---

## 3. READY_SPEC trust compiler (kanban dispatch gate)

A pure, fail-closed validator that makes a kanban card's `ready` status
*provably* safe-to-dispatch, not just mechanically claimable. A card declares
a machine-checkable contract in a fenced ` ```ready-spec ` YAML block in its
body; `validate_ready_spec` checks it before `dispatch_once` claims the card.
Five fields, only `scope` required; parse/validation errors fail CLOSED
(`ok=False`), never a half-parsed PASS. READY_SPEC is a declaration gate —
the command-guard floor (section 1) remains the runtime boundary.

The pure validator is `hermes_cli/ready_spec.py` (no I/O, never raises);
enforcement wiring lives in `dispatch_once` in `hermes_cli/kanban_db.py`
(around the `READY_SPEC trust-compiler dispatch gate` block, ~line 6329):
additive and OFF by default, controlled by env
`HERMES_KANBAN_ENFORCE_READY_SPEC` (unset/off → skipped, `warn`, `enforce`)
with a grandfather epoch via `HERMES_KANBAN_READY_SPEC_FLIP_EPOCH`.

Key files: `hermes_cli/ready_spec.py`, `hermes_cli/kanban_db.py`.

Config/env: `HERMES_KANBAN_ENFORCE_READY_SPEC`,
`HERMES_KANBAN_READY_SPEC_FLIP_EPOCH`.

Tests: `tests/hermes_cli/test_kanban_ready_spec.py`.

Verify:

```sh
venv/bin/pytest tests/hermes_cli/test_kanban_ready_spec.py -q
```

---

## 4. Kanban tooling (pr / decompose / specify / swarm / diagnostics + watchers)

The fork's kanban kernel (`hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`)
carries a tooling layer upstream lacks:

- `hermes_cli/kanban_pr.py` — `open_pr`: pushes a completed code card's
  branch and opens a `needs-human` PR via `gh pr create`; labels come from
  the same deny-list classifier the codex pipeline uses
  (`MergeBroker.classify_change`), so engine changes can never auto-merge.
- `hermes_cli/kanban_decompose.py` — `hermes kanban decompose`: fans a triage
  task into a child task graph via the auxiliary LLM.
- `hermes_cli/kanban_specify.py` — `hermes kanban specify`: fleshes a
  one-liner triage card into a real spec (LLM-backed; may time out).
- `hermes_cli/kanban_swarm.py` — thin swarm topology (planning root →
  parallel specialists → verifier) on top of the existing kernel; no second
  scheduler.
- `hermes_cli/kanban_diagnostics.py` — machine-readable distress signals
  (kind/severity) for stuck/crash-looping/hallucinated cards.
- Watchers: the gateway's kanban-notifier watcher tails `task_events`
  (`hermes_cli/kanban_db.py`, notifier read/claim is single-owner across
  concurrent gateway watcher processes on the same board DB).

Tests: `tests/hermes_cli/test_kanban_pr.py`, `test_kanban_decompose.py`,
`test_kanban_specify.py`, `test_kanban_swarm.py`, `test_kanban_diagnostics.py`,
`test_kanban_dispatch.py`, `test_kanban_notify.py` (plus ~20 more
`test_kanban_*` modules in `tests/hermes_cli/`).

Verify:

```sh
venv/bin/pytest tests/hermes_cli -k kanban -q
```

---

## 5. Dashboard: OS tab + Nexus, truth API, session-token auth

Fork-local dashboard (served by `hermes-dashboard` on :9119) with an "OS"
tab that visualizes the whole agent infrastructure. `web/src/pages/OSPage.tsx`
renders `/api/dashboard/os` with view modes `live | nexus | connectome |
grid | git | projects`; the **Live** view iframes the dashboard's own
same-origin `/nexus` V6 truth surface (`<iframe src="/nexus" …>`).

Backend surfaces (FastAPI routers in `hermes_cli/`):

- `hermes_cli/dashboard_os.py` — the OS snapshot API.
- `hermes_cli/dashboard_nexus.py` — read-only all-territories Nexus truth API
  (`/api/dashboard/nexus`), static V6 topology + normalized truth objects.
- `hermes_cli/dashboard_nexus_slice.py` — `/api/dashboard/nexus/slice/backup-offbox`
  normalized truth slice.
- `hermes_cli/dashboard_nexus_actions.py` — safe-summon action tickets
  (W2B), DISARMED by default: without `$HERMES_HOME/state/nexus-actions/ARMED`
  preflight returns 501 and no capability is minted; live dispatch only
  calls the fixed `loki_send.py` chokepoint. Rate constants:
  `_CAPABILITY_TTL_SECONDS = 300`, `_REJECTION_FLOOD_WINDOW_SECONDS = 600`
  (a dedicated constant — a past regression silently coupled it to the TTL),
  `_MINT_LIMIT_PER_600S = 10`, `_DISPATCH_LIMIT_PER_600S = 3`.

CSP / framing model (`hermes_cli/web_server.py`,
`security_headers_middleware`): every response gets a report-only CSP with
`frame-ancestors 'none'` and `X-Frame-Options: DENY` — EXCEPT the two
same-origin framing exceptions: `/nexus` (framed by the OS tab; gets
`frame-ancestors 'self'` + `X-Frame-Options: SAMEORIGIN`) and
`/_gitnexus-app/` paths (framed by the Explorer tab). `/api/dashboard/nexus*`
API routes stay DENY.

Auth model: every dashboard API requires the `X-Hermes-Session-Token`
header; SSE endpoints consumed via `EventSource` (which cannot set headers)
additionally accept `?token=` as a constant-time-compared query param, on a
deliberately narrow route list (`web_server.py` `_has_valid_query_token`).

Key files: `hermes_cli/web_server.py`, `hermes_cli/dashboard_os.py`,
`hermes_cli/dashboard_nexus.py`, `hermes_cli/dashboard_nexus_slice.py`,
`hermes_cli/dashboard_nexus_actions.py`, `hermes_cli/nexus_action_registry.py`,
`web/src/pages/OSPage.tsx`, `web/src/components/os/OSNexus.tsx`.

Ops note (machine-local, not in-repo): `hermes-dashboard.service` is its own
systemd unit — restart it, not the gateway, for dashboard changes; on deploy
branches `web_dist` may be gitignored (`git add -f web_dist`).

Tests: `tests/hermes_cli/test_web_csp.py`,
`tests/hermes_cli/test_dashboard_security_headers.py`,
`tests/hermes_cli/test_dashboard_nexus.py`, `test_dashboard_nexus_slice.py`,
`test_dashboard_nexus_actions.py`, `tests/security/test_dashboard_auth_boundary.py`.

Verify:

```sh
venv/bin/pytest tests/hermes_cli -k "nexus or csp or security_headers" -q
```

---

## 6. claude-cli-subprocess runtime provider (Max-OAuth lane substrate)

A fork-local turn executor that delegates an entire conversation turn to the
locally installed Claude Code CLI running as an interactive TUI inside tmux,
authenticated by the user's claude.ai OAuth / Max subscription — zero metered
Anthropic API spend. Hard guarantees (module docstring,
`agent/claude_cli_runtime.py`): never emits `-p`/print mode; strips Anthropic
API env vars (`_BLOCKED_AUTH_ENV`: `ANTHROPIC_API_KEY`, `ANTHROPIC_TOKEN`,
`ANTHROPIC_AUTH_TOKEN`, …) so an injected key cannot reroute to the paid API;
file-based handoff (turn packet in → `result.md` out, no screen scraping);
fails loud with no paid fallback.

Provider name is `claude-cli-subprocess` (api_mode `claude_cli_subprocess`).
**Operational invariant: a lane using this substrate must set
`model.provider: claude-cli-subprocess`, never `anthropic`** — the
`anthropic` provider is the metered HTTP API and, in the local Max-OAuth
setup, misrouting has caused fallback errors. Config sections read:
`claude_cli` / `claude_cli_subprocess` / `claude_code` (see
`_runtime_options` resolution around line 236).

Key files: `agent/claude_cli_runtime.py`.

Tests: `tests/agent/test_claude_cli_runtime.py`.

Verify:

```sh
venv/bin/pytest tests/agent/test_claude_cli_runtime.py -q
```

---

## 7. Concurrency cap (gateway.max_concurrent_agent_runs)

The fork gates concurrent webhook-spawned agent runs behind a semaphore whose
size comes from `resolve_max_concurrent_agent_runs` in
`hermes_cli/active_sessions.py` (default
`DEFAULT_MAX_CONCURRENT_AGENT_RUNS = 4`). The canonical config key is
**`gateway.max_concurrent_agent_runs`** in `config.yaml`; the webhook adapter
also honors a `platforms.webhook.extra.max_concurrent_agent_runs` override by
feeding it through the same resolver (`gateway/platforms/webhook.py` ~line 192).

Do not confuse this with the *session-lease* knob in the same module
(`coerce_max_concurrent_sessions` / `max_concurrent_sessions`), which caps
open chat surfaces, not agent runs. (Upstream's similarly-named
`max_live_sessions` knob was not found in this tree — the fork resolver is
the only thing that sizes the run semaphore. (unverified upstream name))

Key files: `hermes_cli/active_sessions.py`, `gateway/platforms/webhook.py`.

Config: `gateway.max_concurrent_agent_runs` (config.yaml);
`platforms.webhook.extra.max_concurrent_agent_runs` (route-level override).

Tests: `tests/hermes_cli/test_active_sessions.py`,
`tests/gateway/test_webhook_backpressure.py`, and the semaphore-cap pins in
`tests/security/test_merge_invariants.py`.

Verify:

```sh
venv/bin/pytest tests/hermes_cli/test_active_sessions.py tests/gateway/test_webhook_backpressure.py -q
```

---

## 8. Security / redteam test estate + hermetic guards

`tests/security/` is entirely fork-local:

- Rail regressions: `test_exfil_rail.py`, `test_deny_rails.py`,
  `test_deny_rail_regression.py`, `test_credential_taint.py`,
  `test_credential_persistence.py`, `test_codex_exec_approval_matrix.py`,
  `test_path_security.py`, `test_dashboard_auth_boundary.py`,
  `test_recall_clean_pool_parity.py`.
- Redteam corpus: `tests/security/fixtures/redteam_cases.jsonl` — 53 attack
  cases fed through the live pre-exec approval chokepoint by
  `tests/security/run_redteam.py` (hermetic: nothing executed, no model, no
  network) and `test_redteam_runner.py`. Report:
  `tests/security/REDTEAM_REGRESSION_REPORT.md`.
- Merge-invariant pins: `test_merge_invariants.py` — asserts fork-local
  security/operator invariants survive an upstream merge (dispatch defaults,
  secret-exclusion sets, CVE pins via `cve_pin_baseline.txt`, restart-resume
  floor re-arming), designed to turn a NAMED CI check red before merge.

Hermetic guards in `tests/conftest.py`: all credential-shaped env vars unset
per test; `HERMES_HOME` isolated to a tempdir; deterministic TZ/LANG/hashseed;
no `HERMES_SESSION_*` inheritance; and a browser guard that neuters
`webbrowser.*` and drops `BROWSER` so a test can never open a real browser
(a broad sweep once popped real OAuth tabs). Blast-radius test selection —
computing the affected-test set from touched files instead of a fixed list —
lives in `scripts/blast_radius_tests.py`; known flakes are registered in
`tests/KNOWN_FLAKES.md`.

Verify:

```sh
venv/bin/pytest tests/security -q && venv/bin/python tests/security/run_redteam.py
```

---

## 9. Supply-chain policy: exact pins + PLW1514 encoding gate

- **Exact-pin policy**: `[project] dependencies` in `pyproject.toml` are
  pinned `==X.Y.Z` (rationale in the inline comment block: smaller
  `dependencies` = smaller blast radius for the next supply-chain attack;
  optional extras carry their own pins, e.g. the `alibabacloud-tea-openapi==0.3.16`
  pin exists purely to make a resolver decision visible). Bump pins and
  regenerate with `uv lock`; never hand-edit `uv.lock`. Documented pin-range
  exceptions per the upstream-merge-guard: fastapi, uvicorn, ptyprocess,
  pywinpty.
- **PLW1514 gate**: ruff's unspecified-encoding rule is the ONLY selected
  lint rule (`pyproject.toml` `[tool.ruff.lint] select = ["PLW1514"]`,
  `preview = true`) and `.github/workflows/lint.yml` blocks merge on it —
  every text-mode `open()` / `read_text()` / `write_text()` needs explicit
  `encoding=`. Exempt: `tests/**`, `skills/**`, `optional-skills/**`,
  `plugins/**` (per-file-ignores).
- CVE pin baseline for merge invariants: `tests/security/cve_pin_baseline.txt`.

Verify:

```sh
venv/bin/ruff check . && venv/bin/pytest tests/security/test_merge_invariants.py -q
```

---

## 10. Upstream update doctrine

**Fork updates are MERGE projects. NEVER run `hermes update`** — it wipes the
fork's local surface (every section above). The TUI will occasionally suggest
it; refuse.

Procedure:

1. Fetch upstream, merge into a branch, resolve — treating every conflict in
   a file named in this document as security-sensitive.
2. **Run the upstream-merge-guard checklist after every merge** —
   `.claude/agents/upstream-merge-guard.md` (a Claude Code subagent) walks the
   MUST-SURVIVE list and outputs SURVIVED/REVERTED/UNCLEAR per item with
   file:line evidence. Precedent: the 0.16.0 merge **silently reverted** the
   backup.py `.env`/`auth.json` secret-exclusion patches, re-fixed in PR #70.
   A missing regression *test* post-merge counts as REVERTED even if the
   patch survived.
3. Run the merge-invariant pins (they exist precisely for this moment):

```sh
venv/bin/pytest tests/security/test_merge_invariants.py tests/security -q
```

Current MUST-SURVIVE list (extend as fork patches accrue — keep
`.claude/agents/upstream-merge-guard.md` and
`tests/security/test_merge_invariants.py` in sync with this document):

1. backup.py secret exclusion (`.env`, `auth.json` excluded from backups).
2. Kanban code→PR layer (`hermes_cli/kanban_pr.py::open_pr`).
3. Exact-pin dependency policy (section 9).
4. Ruff PLW1514 gate (section 9).
5. Everything in sections 1–8 of this document.

Machine-local git topology note (not enforceable in-repo): local `main`
tracks the NousResearch upstream; the fork remote is the push/PR target;
live deploy branches are `deploy/*`.
