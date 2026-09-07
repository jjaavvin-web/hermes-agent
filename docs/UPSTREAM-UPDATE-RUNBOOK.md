# Upstream Update Runbook

Canonical, self-contained procedure for merging a new upstream Hermes Agent
release (`NousResearch/hermes-agent`) into this fork without losing
fork-local behavior. Supersedes the repo-side mechanics in
`~/.hermes/skills/autonomous-ai-agents/hermes-agent/references/fork-preserving-hermes-update-candidate.md`
(still valid for generic "diverged checkout" scenarios outside a tagged
release). Distilled from the real v0.20.1 update — tag `v2026.8.13`,
commit `f80f453ae0679347e38abc917c7f94f717bf96c5` — executed 2026-08-13;
full evidence: `~/.hermes/audits/v0201-upstream-merge-20260813/VERIFICATION-REPORT.md`.

## 1. Principles

1. **Never run `hermes update` in a live checkout or serving deployment.**
   It wipes every fork-local surface in `docs/FORK.md`. Upstream updates
   are MERGE projects, run from an isolated candidate worktree.
2. **Merge the upstream tag into the certified fork line** — not raw
   `origin/main`, not the operator's dirty working checkout. The base is
   whatever branch last carried an `upstream-merge-guard` PASS verdict
   (today: `fork/v020-upstream-merge`).
3. **Fork rails are behavioral invariants, not text.** Every subsystem in
   `docs/FORK.md` (security rails, F4 worktree broker, READY_SPEC, kanban
   tooling, dashboard/Nexus, backup exclusions, exact-pin policy…) has a
   named guard or test. A merge that keeps a file but drops its calling
   site fakes parity — verify behaviorally, not textually.
4. **Lock-enforced everything.** `uv sync --frozen` / `uv lock --check` for
   Python, a regenerated `npm` lock for Node. Leaving `uv pip install -e`
   to resolve ranges fresh can silently pull an incompatible pin (§4, #3).
5. **Evidence-or-it-didn't-happen.** Every phase writes to a timestamped
   dir under `~/.hermes/audits/`. A verdict without file:line evidence or
   a real pass/fail count does not count.

## 2. Pipeline

Run from the repo root unless noted. `<TAG>`, `<BASE>`, `<CANDIDATE_HEAD>`
are captured fresh each run, never carried over from a prior update.

### Phase 0 — Recapture & custody

Snapshot live topology first: remotes/branches, service state, config hash,
state-DB sizes, and every runtime pin (CLI wrapper, gateway
`ExecStart`/`PATH`/`PYTHONPATH`, dashboard `WorkingDirectory`, systemd
drop-ins under `~/.config/systemd/user/hermes-{gateway,dashboard}.service.d/`).
Then take an online backup — do not stash/commit/reset/clean the dirty
operator checkout; the candidate is built somewhere else entirely.

```sh
hermes backup --quick -o ~/hermes-backup-pre-update-$(date -u +%Y%m%dT%H%M%SZ).zip -l pre-update
```

### Phase 1 — Fetch tag, candidate worktree off the certified base

```sh
git fetch origin --tags
git rev-parse <TAG>                                     # confirm exact commit before merging it
git fetch fork
git worktree add -b <candidate-branch> \
  ../hermes-agent-worktrees/<candidate-branch> fork/<certified-base>
cd ../hermes-agent-worktrees/<candidate-branch>
git merge <TAG>                                          # dozens of conflicts is normal at a ~1000+ commit delta
```

### Phase 2 — Resolve conflicts

Split by risk, not file count.

- **Security-critical files — lead resolves directly, no fan-out.**
  Precedent: `hermes_cli/backup.py`, `tools/approval.py`,
  `tools/terminal_tool.py`, `*auth*.py`, `*config*.py`, `pyproject.toml`,
  `package.json`, `uv.lock`, `package-lock.json`. Read both sides' intent;
  never take "ours" or "theirs" wholesale on these.
- **Mechanical conflicts — fan out**, one file per worker, each returning
  per-file evidence (what changed, which side won, why) and a risk flag.
  Merge resolutions back individually; never batch-accept.

```sh
git diff --name-only --diff-filter=U          # must be empty before continuing
```

### Phase 3 — Preflight sweep

One command, five checks — bundles the ad-hoc probes that caught real P1s
on 2026-08-13 so the next merge gets them for free:

```sh
python scripts/upstream_merge_preflight.py --base <BASE>
```

1. conflict-marker scan (`git diff --check` + a direct `git grep` for
   `^<<<<<<<`/`^>>>>>>>`).
2. CLI-boot smoke — imports/boots `hermes_cli.main` in a fresh
   `HERMES_HOME`; catches argparse subcommand collisions (§4, #1).
3. AST duplicate-definition sweep over every `.py` changed since `--base`
   (§4, #2) — getter/setter pairs are not findings.
4. config-schema key diff — `DEFAULT_CONFIG` key paths in
   `hermes_cli/config_defaults.py`, `--base` vs `HEAD`; a dropped key is a
   FAIL, an added one is informational.
5. web route-manifest drift — the CI dashboard-route-drift gate, runnable
   standalone against `tests/fixtures/dashboard_route_manifest.json`.

Non-zero exit on any FAIL; a results table prints regardless. Then
regenerate lockfiles, in the candidate only:

```sh
uv lock && uv lock --check
# Root package.json declares a WORKSPACE lock ("workspaces" array, includes
# web/). A verbatim-merged package-lock.json under-covers fork web/ devDeps
# upstream's web/ never had — `npm ci` EUSAGE-fails until this regenerates
# it. Delta must be additive, registry-only, zero pin drift.
npm install --package-lock-only
```

### Phase 4 — Guards

```sh
venv/bin/pytest tests/security -q                                       # fork-parity + merge invariants
venv/bin/pytest tests/security/test_merge_invariants.py -q
python scripts/fork_parity_guard.py --out-dir <audit-dir>/fork-parity   # machine-readable evidence half
```

Two guard-maintenance moves are legitimate when upstream touches a pinned
rail, not scope creep: bump `tests/security/cve_pin_baseline.txt`'s floor
in the same commit as `uv lock`; re-point a `tests/security/fork_parity_lib.py`
manifest anchor to a new call site when upstream renames/splits the
function it anchors to (locked-vs-wrapper split) — never delete the anchor.

Supply-chain review (subagent or manual): diff `pyproject.toml`, `uv.lock`,
`package.json`, `package-lock.json` against `<BASE>` — every new/changed
pin must be registry-sourced, additive, exact (no bare ranges outside the
pyproject-documented exceptions). CI mirrors this in
`.github/workflows/{osv-scanner,supply-chain-audit,uv-lockfile-check,lockfile-diff}.yml`.

### Phase 5 — Full suite

```sh
uv sync --frozen --extra dev --extra messaging --extra dingtalk --extra slack   # match live extras
python scripts/run_tests_parallel.py -j "$(nproc)"
```

Match CI's environment exactly — do **not** export a global `HERMES_HOME`
for the run (§4, #6: it pollutes cross-file state and inflates failures):

```sh
OPENROUTER_API_KEY= OPENAI_API_KEY= NOUS_API_KEY= \
  venv/bin/pytest tests/hermes_cli/test_dashboard_smoke.py -q   # named CI gate; hangs without the empty keys
```

### Phase 6 — Triage against baseline

```sh
python scripts/triage_against_baseline.py <audit-dir>/full-suite.log
```

Buckets every failure against `tests/known_test_debt.json`: **NEW**
(blocks — a regression this branch introduced or an undocumented flake),
**KNOWN debt** (informational, pre-existing at the fork base), **RESOLVED
debt** (informational — prune the entry). Exit 0 only when NEW is empty and
no baseline entry is UNREVIEWED. First time a failure appears, add it with
`--update` (appends as UNREVIEWED — a human must edit the `reason` field
before the gate accepts it); `--allow-unreviewed` unblocks CI on entries
already reviewed-but-not-yet-edited. Disputed cases — is a failure really
pre-existing, or is the log itself polluted (§4, #7) — fall back to a
manual isolated rerun: `HERMES_HOME="$(mktemp -d)" venv/bin/pytest <file> -q`.

### Phase 7 — Live-state rehearsal

Never let the candidate's first DB open be against the real `HERMES_HOME`.

```sh
hermes backup --quick -o <audit-dir>/state-rehearsal.zip -l rehearsal
mkdir -p /tmp/rehearsal-home && unzip <audit-dir>/state-rehearsal.zip -d /tmp/rehearsal-home

sqlite3 /tmp/rehearsal-home/state.db .schema | sha256sum > <audit-dir>/schema-before.sha
HERMES_HOME=/tmp/rehearsal-home ./.venv/bin/hermes sessions list          # candidate opens + migrates
sqlite3 /tmp/rehearsal-home/state.db .schema | sha256sum > <audit-dir>/schema-after.sha
diff <audit-dir>/schema-before.sha <audit-dir>/schema-after.sha           # expect a diff — read what changed

# rollback proof: the OLD binary must still read the migrated copy
HERMES_HOME=/tmp/rehearsal-home <OLD_DEPLOYMENT>/.venv/bin/hermes sessions list
```

If the OLD binary can't read the migrated copy, the migration is not
additive-only and rollback is unsafe — stop, do not cut over.

### Phase 8 — Build deployment

```sh
scripts/build_deployment.sh <CANDIDATE_HEAD> --dry-run    # print the plan first, no writes
scripts/build_deployment.sh <CANDIDATE_HEAD> --activate
```

In-repo generalization of the one-off `build-v0201-deployment.sh` used on
2026-08-13, driven entirely by `deploy/DEPLOYMENT-MANIFEST.toml` (extras,
`lock_enforced = true`, version gates, out-of-band packages, layout) so
extending it never means editing the script. It gates on `EXPECTED_HEAD` +
a clean tree, `git archive`-exports the committed tree (no
`.git`/venv/`node_modules`), `uv sync --frozen` (never a non-frozen
fallback — §4, #3), `npm ci --ignore-scripts`, preflights (`hermes
--version`, `pause`/`unpause`) against an isolated `HERMES_HOME`, then
`--activate` atomically repoints `deployments_root/current` at the new
build and prints the previous target. **`--activate` never restarts a
service and the live systemd units/CLI wrapper do not yet follow that
symlink** — the 5 files needing that follow-on migration are listed in
`deploy/DEPLOYMENT-MANIFEST.toml`'s `[layout]` section; until it lands,
Phase 9 still repoints them by hand.

### Phase 9 — Cutover (gated)

Repoint every runtime pin together, out of band (never restart the gateway
from its own child process): CLI wrapper (`~/.local/bin/hermes`), gateway
unit `ExecStart`/`PATH`/`PYTHONPATH`/`VIRTUAL_ENV`, dashboard unit
`WorkingDirectory` + `~/.hermes/scripts/cockpit-dashboard.sh`. Back up the
pin files first so rollback is a repoint, not a rebuild.

```sh
systemctl --user restart hermes-gateway.service      # own unit
systemctl --user restart hermes-dashboard.service     # own unit — NOT hermes-gateway
```

Post-checks: process/cwd provenance points only at the new deployment;
`hermes --version` reports the new release; dashboard root `200`
unauthenticated, sensitive `/api/dashboard/*` `401` unauthenticated / `200`
with `X-Hermes-Session-Token`; a live cron→Discord smoke fires once. This
is josep's literal-message-authorized gate — announce before restarting;
never infer authorization from an earlier, differently-scoped approval.

## 3. Rollback map

| Symptom | Action |
|---|---|
| Candidate P0/P1 found pre-cutover | Discard candidate branch/worktree; live deployment untouched |
| Cutover post-check fails | Repoint pin files back to the prior deployment dir; restart both units |
| Feature pilot fails post-cutover, base smoke green | Revert the feature's config key; keep the release |
| State migration proven non-additive (Phase 7 fails) | Do not cut over; state restore required before retry |

Keep the previous immutable deployment directory
(`~/.local/share/hermes-agent-deployments/<prior>`) until the new one has
run clean for a full day.

## 4. Named failure modes (2026-08-13)

| # | Failure | Detector |
|---|---|---|
| 1 | argparse subcommand collision — upstream added `hermes resume` (global ESTOP lift), fork already had `hermes resume` (session pickup); no textual conflict, pure semantic collision | CLI-boot smoke: `hermes --help` plus every subparser's `--help` |
| 2 | Silent double-keep — merge kept both definitions of `lookup_by_session_key` | AST dupe sweep, Phase 3 step 3 |
| 3 | Fresh-range venv resolve — `uv pip install -e` pulled `fastapi==0.141.1` against the lock's `0.133.1`, silently breaking `include_router` | Lock-enforced `uv sync --frozen` + explicit version gate in the build script |
| 4 | Monolithic router try-block hiding mount failures | Per-router mounts (fixed in code this merge — `gateway/run.py`) |
| 5 | Route-manifest drift when upstream adds routes | Regenerate deliberately, review each new route's auth posture: `python -c "from hermes_cli.web_server import app; from hermes_cli.dashboard_smoke import route_manifest; import json; print(json.dumps(route_manifest(app), indent=2))" > tests/fixtures/dashboard_route_manifest.json`, diff, eyeball every added row |
| 6 | Stale fork-invariant tests — assertions target upstream's default, not the fork's | `review_dispatch`/auto-routing/specialist-guard tests failing post-merge — update the assertion to the fork's value, never weaken the rail |
| 7 | Shared-`HERMES_HOME` suite pollution — one exported `HERMES_HOME` for the whole run inflates the failure count via cross-file state bleed | Compare against a fresh-`HERMES_HOME`-per-file rerun (Phase 6) before trusting any failure count |

## 5. Cadence

Track upstream **per tagged release**, not a fixed calendar — conflict
surface scales with commit delta, not elapsed time. Ten days behind
produced 1,444 commits / ~656 PRs and 37 conflict files today; a tighter
cadence against a steadier release rhythm shrinks that proportionally.
Division of labor stays fixed regardless of cadence: the executor runs
Phases 0–8 end to end; Hermes/MOTHERSHIP is strictly advisor (reviews the
plan, never substitutes for the executor); josep holds the two hard gates
no peer approval can substitute for — Phase 9 restarts, and any push/PR.
