---
isa:      20260524-2020_codex-parallel-p3-merge-broker
task:     "P3 Merge broker — serialized fork/main merges with isa_lint gate, classify_change, and auto-merge label"
tier:     E3
phase:    scaffold
progress: 0/16
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p3-merge-broker
hive:     "-"
owner:    ruflo-hive
started:  2026-05-24T20:20:00Z
updated:  2026-05-24T20:20:00Z
---

## Problem

After P2, an Opus APPROVE verdict transitions the session to MERGING but the actual merge to `fork/main` is still manual — the operator pushes the branch and opens the PR. With 4-8 concurrent sessions this is operator-bottlenecked and racy: two simultaneous pushes to `fork/main` could collide (`fork/main` race per `collision-matrix.md` §2), and there is no automated gate to ensure the ISA actually passed CheckCompleteness (`scripts/isa_lint.py`) before landing.

External research (RQ5) confirms GitHub's native auto-merge cannot be triggered by adding a label — the design needs either Mergify (recommended) or a GitHub Actions workflow on `pull_request.labeled` events. This ISA chooses Mergify as primary (cleanest label-gated path) with the Actions workflow as the no-third-party-tools alternative the operator can pick if they don't want to install Mergify.

## Goal

After this ISA: when P2 emits an APPROVE verdict, the dispatcher calls `MergeBroker.merge(session_id, worktree, branch, isa_path, summary)`. The broker acquires `flock ~/.hermes/codex-merge.lock` (single concurrent merge globally), runs `git fetch origin && git rebase origin/main` inside the worktree (surfacing conflicts as escalations), runs `python3 scripts/isa_lint.py <ISA>` and bails on non-zero, pushes the branch to `fork`, opens a PR via `gh pr create --base fork/main`, classifies the change (paths in `agent/`, `gateway/`, `auth/`, `migrations/`, `pyproject.toml`, `package*.json`, `.github/`, `scripts/isa_*.py`, `hermes_state.py`, `hermes_cli/web_server.py` → needs-human; otherwise → auto-merge), labels the PR accordingly, and posts the PR URL back to the Discord thread. Either Mergify (config file `.mergify.yml`) or a GitHub Actions workflow (`.github/workflows/auto-merge.yml`) handles the auto-merge label gating server-side — operator chooses which.

## Out of Scope

- Dashboard surface for merge queue / merge history — P4.
- Reverting a merge — out of scope; that's GitHub's native revert.
- Branch protection rules on `fork/main` — operator config, not in this ISA.
- Merge queue batching — Mergify supports it but P3 ships single-merge serialization only.
- Cross-PR dependency sequencing — out of scope (ISAs are independent units of work).

## Constraints

- **P2 must be landed** — broker is only invoked on APPROVE verdicts.
- **Single concurrent merge globally** — `flock ~/.hermes/codex-merge.lock` with 30-min timeout.
- **isa_lint MUST exit 0** before push — non-negotiable gate per ISA-SPEC §9.
- **No force-push to `fork/main`** ever. Per WORKFLOW-LESSONS §3 rule 4 + general git safety, the broker pushes the feature branch, not directly to `fork/main`. Mergify/Actions does the merge.
- **No `--no-verify` on git operations** per WORKFLOW-LESSONS §3 rule 4.
- **Auto-merge tooling choice (Mergify vs Actions) is a project-level decision** — P3 ISA delivers both config files and a doc explaining the trade-off; operator picks one by installing/enabling.
- **classify_change deny-list is intentionally broad on first ship** — better to over-flag for human review than to auto-merge a sensitive change. Operator can narrow it in P5 hardening based on real usage.
- **Discord notification on merge handoff** uses outbound `tools/discord_tool.py:908` — do not duplicate the REST client.
- **Worktree release happens AFTER the PR actually merges**, not after `gh pr create`. The broker emits a "ready" event; the dispatcher polls (60s tick) for PRs labeled `auto-merge` AND state=merged, then calls `WorktreeBroker.release(sid)`.

## Criteria

- [ ] ISC-1: a new file `agent/merge_broker.py` exists implementing `MergeBroker` per `module-specs/merge-broker.md` §3
- [ ] ISC-2: `merge()` acquires `flock ~/.hermes/codex-merge.lock` (blocking, 30-min timeout); concurrent calls serialize FIFO
- [ ] ISC-3: pre-merge sequence runs `git fetch origin && git rebase origin/main` inside the worktree; conflict raises `ConflictEscalation` and posts to Discord thread
- [ ] ISC-4: `python3 scripts/isa_lint.py <isa_path>` is invoked AFTER rebase; non-zero exit returns `MergeResult(ok=False, error="isa_lint failed: <stdout>")`
- [ ] ISC-5: branch push uses `git push fork <branch>` (no force, no force-with-lease — feature branch is per-sid UUID4, never previously pushed)
- [ ] ISC-6: `gh pr create --base main --head <branch>` (against `fork/main` since `fork` is the remote and `main` is its default branch) opens a PR and returns the PR number; idempotent if PR already exists for the branch
- [ ] ISC-7: `classify_change` walks `git -C <worktree> diff --name-only origin/main...HEAD` and returns `safe` only if NO file matches any prefix in the deny-list `{agent/, gateway/, auth/, migrations/, pyproject.toml, package, .github/, scripts/isa_, hermes_state.py, hermes_cli/web_server.py}`
- [ ] ISC-8: `safe` classification → `gh pr edit <pr#> --add-label auto-merge`; `sensitive` → `--add-label needs-human`
- [ ] ISC-9: PR description includes ISA path, ISC progress, Opus verdict rationale (from P2's Verdict.rationale), and a `## Verification` excerpt (verbatim probe outputs)
- [ ] ISC-10: Discord thread receives a post `"PR #N opened — auto-merge | needs-human — <url>"` after labeling
- [ ] ISC-11: `.github/workflows/auto-merge.yml` is committed per `module-specs/merge-broker.md §6.1` (GitHub Actions, operator decision 2026-05-24); NO `.mergify.yml` is committed — grep proves it (`grep -rl '.mergify' . | wc -l` returns 0)
- [ ] ISC-12: dispatcher poll detects merged PR (label `auto-merge` AND state=merged) within 120s of merge using `gh pr list --label auto-merge --state merged --head 'codex/*'`; on detect: `WorktreeBroker.release(sid)`, `kanban_complete <card>`, delete `codex_sessions.json` row, archive Discord thread. The `--head 'codex/*'` flag is mandatory — it prevents the cleanup loop from matching operator-labeled non-Codex PRs
- [ ] ISC-13: Anti: NO `git push --force` or `--force-with-lease` against `fork/main` anywhere in `agent/merge_broker.py` — grep proves it
- [ ] ISC-14: Anti: NO `--no-verify` / `--no-gpg-sign` flags on any git operation in the new module — grep proves it
- [ ] ISC-15: `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2020_codex-parallel-p3-merge-broker/ISA.md` exit 0 in `phase: complete`
- [ ] ISC-16: the flock is released after step 5 (push) and before step 6 (`gh pr create`). Acceptance criteria: (a) `module-specs/merge-broker.md §4` shows the flock-released marker between step 5 and step 6; AND (b) the implementation file `agent/merge_broker.py` emits a log line `flock_released = True` at INFO level at that point — verified by `grep -n 'flock_released' agent/merge_broker.py` returning at least 1 hit AND by the mutex serialization test (ISC-2) confirming the second caller can acquire the lock BEFORE the first caller's `gh pr create` subprocess completes

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from agent.merge_broker import MergeBroker; print(MergeBroker)"` | prints class |
| ISC-2 | spawn 2 concurrent merge() calls; trace lock acquisition | second call blocks until first releases |
| ISC-3 | prepare a worktree where origin/main has a conflicting change; call merge | raises ConflictEscalation; Discord receives conflict post |
| ISC-4 | mock an ISA where isa_lint fails; call merge | MergeResult.ok=False with stdout in error |
| ISC-5 | mock git push, observe args | `git push fork <branch>` with NO force flags |
| ISC-6 | run merge twice for same sid; check PR count | exactly 1 PR (second call detects existing) |
| ISC-7 | test 3 diffs: (a) all in `docs/`; (b) one in `agent/`; (c) one in `package-lock.json` | classify: (a) safe, (b) sensitive, (c) sensitive |
| ISC-8 | merge a `safe` change; `gh pr view <pr#> --json labels \| jq '.labels[].name'` | `auto-merge` present |
| ISC-9 | merge a session; `gh pr view <pr#> --json body \| jq -r .body \| grep -c 'ISA\|ISC\|rationale\|Verification'` | ≥ 4 hits |
| ISC-10 | merge a session; check Discord thread last 5 messages | one contains `PR #` and the URL |
| ISC-11 | `ls .github/workflows/auto-merge.yml && grep -c 'pull_request' .github/workflows/auto-merge.yml` | file exists; at least 1 trigger match; also `ls .mergify.yml 2>&1 \| grep "No such file"` confirms no Mergify config |
| ISC-12 | mark a codex/* test PR merged on GitHub; wait 120s; verify `gh pr list` mock is called with `--head 'codex/*'` flag | worktree released, kanban completed, json row gone, thread archived; mock asserts `--head 'codex/*'` in call args |
| ISC-13 | `grep -rnE 'push --force\|push.*-f\b\|force-with-lease' agent/merge_broker.py` | 0 hits |
| ISC-14 | `grep -rnE 'no-verify\|no-gpg-sign' agent/merge_broker.py` | 0 hits |
| ISC-15 | `python3 scripts/isa_lint.py ~/.hermes/work/20260524-2020_codex-parallel-p3-merge-broker/ISA.md ; echo $?` | `0` |
| ISC-16 | `grep -n 'flock_released' agent/merge_broker.py` returns ≥1 hit; concurrent merge test shows second caller acquires lock before first caller's `gh pr create` subprocess finishes | both checks pass |

## Git Plan

- **Branch**: `feat/codex-parallel-p3-merge-broker` off `fork/main` (after P2 lands).
- **Commit cadence (early + incremental)**:
  1. `chore(isa): scaffold P3 ISA + work dir`
  2. `feat(merge-broker): MergeBroker base class + flock mutex (ISC-1, ISC-2)`
  3. `feat(merge-broker): pre-merge fetch/rebase + isa_lint gate (ISC-3, ISC-4)`
  4. `feat(merge-broker): push + gh pr create + idempotency (ISC-5, ISC-6, ISC-9)`
  5. `feat(merge-broker): classify_change + label application (ISC-7, ISC-8)`
  6. `feat(dispatcher): Discord post on merge handoff + post-merge poll (ISC-10, ISC-12)`
  7. `chore(ci): .mergify.yml + .github/workflows/auto-merge.yml.disabled (ISC-11)`
  8. `test(p3): merge-broker integration tests including conflict + idempotency`
  9. `docs(p3): operator notes — choose Mergify vs Actions for auto-merge`
- **Push**: `git push fork feat/codex-parallel-p3-merge-broker` after each commit.
- **PR**: against `fork/main` titled `feat(p3): Codex parallel workflow — merge broker + auto-merge wiring`.
- **Do NOT merge** until `phase: complete` per ISC-15.
- **Mergify install / Actions activation**: separate operator action AFTER this PR merges; document in the PR description so operator knows the next step.
- **No auto-merge label** on this PR — new module under `agent/` + ISA tooling are sensitive per classify_change deny-list.

## Decisions

_(filled during execute — including operator choice of Mergify vs Actions, captured at execute time)_

## Changelog

_(filled on each correction — 4-tuple format per ISA-SPEC §8)_

## Verification

_(filled during verify — probe output pasted verbatim, one block per [x] ISC)_

## Handback

- On complete: `mvms_record_completion` under project `codex-parallel-workflow` linking branch + PR + ISA path + the operator's Mergify/Actions decision.
- For each Changelog entry: `mvms_record_lesson` under project `codex-parallel-workflow`.
- Discord notification via `~/.hermes/scripts/discord-notify.sh`.
- Kanban: `kanban_complete <card>`.
