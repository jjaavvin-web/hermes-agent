---
isa:      20260524-2020_codex-parallel-p3-merge-broker
task:     "P3 Merge broker — serialized fork/main merges with isa_lint gate, classify_change, and auto-merge label"
tier:     E3
phase:    complete
progress: 12/15
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p3-merge-broker
hive:     "-"
owner:    claude-code
started:  2026-05-25T14:30:00Z
updated:  2026-05-25T14:30:00Z
---

## Problem

After P2.5, an Opus APPROVE verdict transitions a session to `MERGING`
but the actual merge to `fork/main` is still manual — operator pushes
the branch and opens the PR. With 4-8 concurrent sessions this is
operator-bottlenecked and racy (two simultaneous pushes to `fork/main`
could collide, per `collision-matrix.md` §2), and there's no automated
gate ensuring the ISA passed `isa_lint` before landing.

## Goal

After this ISA: when P2.5 emits an APPROVE verdict, the dispatcher
calls `MergeBroker.merge(session_id, worktree, branch, isa_path,
summary)`. The broker acquires `flock ~/.hermes/codex-merge.lock`
(single concurrent merge globally), runs `git fetch origin && git
rebase origin/main` inside the worktree (surfacing conflicts as
escalations), runs `python3 scripts/isa_lint.py <ISA>` and bails on
non-zero, pushes the branch to `fork`, opens a PR via `gh pr create
--base main`, classifies the change (paths in `agent/`, `gateway/`,
`auth/`, `migrations/`, `pyproject.toml`, `package*`, `.github/`,
`scripts/isa_*`, `hermes_state.py`, `hermes_cli/web_server.py` →
needs-human; otherwise → auto-merge), labels the PR accordingly, and
posts the PR URL back to the Discord thread. Either Mergify
(`.mergify.yml` committed) or a GitHub Actions workflow
(`.github/workflows/auto-merge.yml.disabled` — operator renames to
activate) handles the auto-merge label gating server-side.

## Out of Scope

- Dashboard surface for merge queue / merge history — P4.
- Dispatcher poll for merged PR → release worktree → kanban_complete
  → archive Discord thread — TOMBSTONED to P3.5 (needs a poll task
  similar to CodexPhaseWatcher; bounded scope on its own).
- Reverting a merge — GitHub's native revert.
- Branch protection rules on `fork/main` — operator config.
- Merge queue batching — Mergify supports it; P3 ships single-merge
  serialization only.

## Constraints

- **P2.5 must be landed** — broker is invoked from `_apply_verdict`'s
  APPROVE branch.
- **Single concurrent merge globally** — `flock ~/.hermes/codex-merge.lock`
  with 30-min timeout.
- **`isa_lint` MUST exit 0** before push — non-negotiable.
- **No force-push** ever. Broker pushes the feature branch via
  `git push fork <branch>`; Mergify/Actions does the actual merge.
- **No `--no-verify` / `--no-gpg-sign`** on any git operation.
- **flock release after push, NOT after `gh pr create`** — C3 critique
  fix; gh + Discord I/O are slow paths that shouldn't hold the merge
  mutex for the whole fleet.
- **Deny-list is intentionally broad** — over-flag > miss; P5 can
  narrow it based on real usage.

## Criteria

- [x] ISC-1: `agent/merge_broker.py` exists implementing `MergeBroker`
  per `module-specs/merge-broker.md` §3
- [x] ISC-2: `merge()` acquires `flock ~/.hermes/codex-merge.lock`
  (blocking, 30-min timeout); release is released-on-exit via
  contextmanager so concurrent calls serialize
- [x] ISC-3: pre-merge sequence runs `git fetch origin && git rebase
  origin/main` inside the worktree; conflict aborts the rebase and
  returns `MergeResult(ok=False, error="conflict: …")`
- [x] ISC-4: `python3 scripts/isa_lint.py <isa_path>` is invoked AFTER
  rebase; non-zero exit returns
  `MergeResult(ok=False, error="isa_lint failed: <stdout>")`
- [x] ISC-5: branch push uses `git push fork <branch>` (no force, no
  force-with-lease — feature branch is per-sid UUID4, never previously
  pushed)
- [x] ISC-6: `gh pr create --base main --head <branch>` opens a PR and
  returns the PR number; idempotent — if a PR already exists for the
  branch the existing one is reused
- [x] ISC-7: `classify_change` walks `git diff --name-only
  origin/main...HEAD` and returns `safe` only if NO file matches any
  prefix in the deny-list
- [x] ISC-8: `safe` classification → `gh pr edit <pr#> --add-label
  auto-merge`; `sensitive` → `--add-label needs-human`
- [-] ISC-9: PR description includes ISA path, ISC progress, Opus
  verdict rationale, and a `## Verification` excerpt — partial: ISA
  path + progress + verdict in body; full Verification excerpt is best
  effort (depends on ISA content available at merge time). Not
  load-bearing for the merge gate.
- [-] ISC-10: Discord thread receives a post `"PR #N opened —
  auto-merge | needs-human — <url>"` — wired in dispatcher's
  `_apply_verdict` APPROVE branch (PR #N + classification + URL)
- [x] ISC-11: `.mergify.yml` is committed at repo root + alternate
  `.github/workflows/auto-merge.yml.disabled` is committed so operator
  can rename to activate without installing Mergify
- [-] ISC-12: dispatcher poll for merged PR → release worktree —
  TOMBSTONED to P3.5 (needs a new poll task; bounded scope)
- [x] ISC-13: Anti: NO `git push --force` or `--force-with-lease` in
  `agent/merge_broker.py` — grep proves it
- [x] ISC-14: Anti: NO `--no-verify` / `--no-gpg-sign` flags on any
  git operation — grep proves it
- [x] ISC-15: `python3 scripts/isa_lint.py isas/P3-merge-broker.md`
  exit 0 in `phase: complete`

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from agent.merge_broker import MergeBroker; print(MergeBroker)"` | prints class |
| ISC-2 | `pytest tests/agent/test_merge_broker.py` — flock contextmanager exercised in every merge test | 12 tests pass |
| ISC-3 | `pytest tests/agent/test_merge_broker.py::test_rebase_conflict_returns_conflict_error` | pass |
| ISC-4 | `pytest tests/agent/test_merge_broker.py::test_isa_lint_failure_returns_lint_error` | pass |
| ISC-5 | `grep -nE "push --force\|--force-with-lease\|push -f" agent/merge_broker.py` | 0 hits |
| ISC-6 | `pytest tests/agent/test_merge_broker.py::test_existing_pr_is_not_recreated` (asserts NO `gh pr create` when PR exists) | pass |
| ISC-7 | `pytest tests/agent/test_merge_broker.py::TestClassifyChange` (5 cases: docs / agent / package / workflow / diff-failure-defaults-sensitive) | 5 pass |
| ISC-8 | `pytest tests/agent/test_merge_broker.py::test_merge_safe_change_opens_pr_with_auto_merge_label` + `test_merge_sensitive_change_labels_needs_human` | 2 pass |
| ISC-11 | `ls .mergify.yml .github/workflows/auto-merge.yml.disabled` | both exist |
| ISC-13 | `grep -rnE "push --force\|--force-with-lease\|push -f\b" agent/merge_broker.py` | 0 hits |
| ISC-14 | `grep -rnE "no-verify\|no-gpg-sign" agent/merge_broker.py` | 0 hits |
| ISC-15 | `python3 scripts/isa_lint.py isas/P3-merge-broker.md ; echo $?` | `0` |

## Git Plan

- **Branch**: `feat/codex-parallel-p3-merge-broker` off `fork/main`
  (post P2.5 merge at `0f32507e9`).
- **Single commit** covering broker + Mergify config + dispatcher
  integration + tests + ISA. P3.5 (dispatcher poll for merged PRs)
  is its own follow-up branch.

## Decisions

**D-1 (2026-05-25): flock release after push, not after `gh pr create`.**
Per C3 in the original design critique: holding the flock across
`gh pr create` + Discord post is a 30-min starvation window for the
whole session fleet if GitHub or Discord is slow. The mutex only
needs to cover `fetch → rebase → push` (the steps that race on the
remote). `gh pr create`, labeling, and Discord post are idempotent
against `fork/main`. Implemented via two-block structure in
`merge()` — `with self._merge_flock(): …push…`, then PR creation +
labeling outside the lock.

**D-2 (2026-05-25): Mergify primary, Actions alternative.**
`.mergify.yml` is the recommended config (cleanest label-gated
auto-merge). The alternate `.github/workflows/auto-merge.yml.disabled`
is the no-third-party-tools option — operator renames it to activate.
Both are committed so the choice doesn't block the merge broker from
shipping.

**D-3 (2026-05-25): Dispatcher poll for merged PRs deferred to P3.5.**
ISC-12 (poll detects merged PR within 120s → release worktree +
kanban_complete + archive thread) is a separate poll task with its
own state tracking. Marked as tombstoned `[-]`. The broker's job
ends at "label applied + Discord post"; the cleanup happens when the
operator (or P3.5) sees the PR landed on `fork/main`.

## Changelog

2026-05-25 — original P3 design held flock across PR creation
  conjectured:   one big lock around fetch → rebase → push → PR create
                 → label → Discord post was simplest
  refuted by:    critique.md C3 — that's a 30-min worst-case starvation
                 window if GitHub or Discord is slow; the whole fleet
                 stalls waiting on one PR's I/O
  learned:       the mutex only protects the `fork/main` race —
                 fetch/rebase/push are the racy steps; PR create is
                 idempotent against the remote, label is local to the
                 PR, Discord post has no concurrency requirements at all
  criterion now: D-1 added; `merge()` releases the flock immediately
                 after `git push`; PR create + label + Discord happen
                 unlocked. Tests don't exercise contention explicitly
                 (would require real fork + delay) but the structure
                 is correct.

## Verification

### ISC-1 / ISC-2 / ISC-3 / ISC-4 / ISC-6 / ISC-7 / ISC-8 — unit tests

```
$ pytest tests/agent/test_merge_broker.py -q
............                                                             [100%]
12 passed in 2.06s
```

### ISC-5 / ISC-13 — anti: no force push

```
$ grep -rnE 'push --force|--force-with-lease|push -f\b' agent/merge_broker.py
$ echo $?
1
```

(grep exit 1 == no matches.)

### ISC-14 — anti: no --no-verify / --no-gpg-sign

```
$ grep -rnE 'no-verify|no-gpg-sign' agent/merge_broker.py
$ echo $?
1
```

### ISC-11 — Mergify config + Actions alternative committed

```
$ ls .mergify.yml .github/workflows/auto-merge.yml.disabled
.github/workflows/auto-merge.yml.disabled
.mergify.yml
```

### ISC-15 — isa_lint at phase: complete

```
$ python3 scripts/isa_lint.py isas/P3-merge-broker.md
PASS: isas/P3-merge-broker.md
```

## Handback

**Project:** `codex-parallel-workflow-p3`. **Lesson:** when a serialized
operation has slow side effects, scope the mutex to the racy steps
only — release before doing the I/O that doesn't actually need
serialization. C3 in the design critique was right.

**Tombstoned for P3.5 (separate ISA):** dispatcher poll for merged PRs
→ release worktree + kanban_complete + archive Discord thread. ~150
LOC + tests. Mirrors `CodexPhaseWatcher` (P2.5) shape.
