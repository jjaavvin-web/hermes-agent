# Module Spec — Merge Broker

**File:** `agent/merge_broker.py` (new, P3 ISA)
**Phase:** P3 — Merge Automation
**Implements:** DESIGN.md §6.4; architecture-diagram.md §5
**Prereqs:** P1 (WorktreeBroker), P2 (PeerReviewOrchestrator), PR #34 on fork/main

---

## 1. Purpose & scope

MergeBroker is the single serialized gate that converts an Opus-approved Codex worktree into a PR on `fork/main`. Only one merge runs at a time globally — the `flock ~/.hermes/codex-merge.lock` mutex enforces this, preventing the non-fast-forward rejections that would result from two sessions pushing and opening PRs concurrently (collision-matrix.md §2, row "Push to fork/main race"). The broker owns the full sequence from `git fetch` through PR label assignment; it does not own worktree release (that's the dispatcher's job after the PR actually merges). Every session that reaches `MERGING` state in the dispatcher state machine calls `MergeBroker.merge()` — no other caller exists.

---

## 2. Files created

| Path | Status | Notes |
|------|--------|-------|
| `agent/merge_broker.py` | New | Main module; keep under 500 lines |
| `~/.hermes/codex-merge.lock` | Created at runtime by `flock` | Never checked in; created on first broker run |
| `.mergify.yml` | **P3 ISA decision** — primary option | If P3 ISA selects Mergify (recommended) |
| `.github/workflows/auto-merge.yml` | **P3 ISA decision** — fallback option | If P3 ISA selects the Actions path |

The P3 ISA must pick exactly one auto-merge tooling option (§6 below) and land the corresponding file. Both files MUST NOT coexist; they implement the same gate via different mechanisms.

---

## 3. Public API

```python
class MergeBroker:
    def __init__(
        self,
        *,
        repo_root: Path,
        hermes_home: Path,
        github_remote: str = "fork",
        base_branch: str = "main",
        policy: MergePolicy,
    ): ...

    def merge(
        self,
        *,
        session_id: str,
        worktree: Path,
        branch: str,
        isa_path: Path,
        summary: str,
    ) -> MergeResult: ...

    def classify_change(
        self,
        *,
        worktree: Path,
        branch: str,
        base: str,
    ) -> ChangeClass: ...


@dataclass
class MergeResult:
    ok: bool
    pr_number: int | None
    pr_url: str
    classification: ChangeClass
    auto_merge_applied: bool
    needs_human: bool
    conflict: bool
    error: str | None


@dataclass
class MergePolicy:
    safe_paths_pattern: list[str]      # prefixes/globs eligible for auto-merge
    sensitive_paths_pattern: list[str] # prefixes/globs that force needs-human
    require_isa_lint: bool = True
    require_review_approval: bool = True


class ChangeClass(str, Enum):
    SAFE = "safe"
    SENSITIVE = "sensitive"
```

`ChangeClass.SAFE` → label `auto-merge` (Mergify rule fires).
`ChangeClass.SENSITIVE` → label `needs-human` (no auto-merge).

Exceptions raised (never swallowed silently):

```python
class ConflictEscalation(Exception):
    conflicting_files: list[str]

class IsaLintFailed(Exception):
    output: str

class ManualBranchInterferenceError(Exception):
    reason: str
```

---

## 4. Merge sequence

```
caller                          broker                        github / OS
  │                               │
  │── merge(sid, worktree, ...) ──►│
  │                               │ 1. flock ~/.hermes/codex-merge.lock
  │                               │    (blocking; O_CREAT if not exists;
  │                               │     LOCK_EX; timeout 30 min → raise TimeoutError)
  │                               │
  │                               │ 2. git -C <worktree> fetch origin
  │                               │    (subprocess.run, check=True)
  │                               │    [pattern: git_janitor.py:50-55]
  │                               │
  │                               │ 3. git -C <worktree> rebase origin/<base_branch>
  │                               │    on non-zero exit:
  │                               │      parse conflicting files from stderr
  │                               │      do NOT run git rebase --abort
  │                               │      raise ConflictEscalation(conflicting_files)
  │                               │      → release lock, return MergeResult(conflict=True)
  │                               │
  │                               │ 4. python3 scripts/isa_lint.py <isa_path>
  │                               │    (subprocess.run; check=False)
  │                               │    exit != 0:
  │                               │      raise IsaLintFailed(stdout+stderr)
  │                               │      → release lock, return MergeResult(ok=False,
  │                               │          error="isa_lint failed: <output>")
  │                               │    [ISA gate: ISA-SPEC.md §9 CheckCompleteness]
  │                               │    [lint rules: cluster-C-kanban-memory-isa.md §"isa_lint.py"]
  │                               │
  │                               │ 5. git -C <worktree> push <github_remote> <branch>
  │                               │    (force-with-lease NOT used — new branch, first push)
  │                               │    push rejected unexpectedly:
  │                               │      raise ManualBranchInterferenceError
  │                               │
  │                               │ ── FLOCK RELEASED HERE (after step 5) ─────────
  │                               │    flock_released = True  [logged at INFO]
  │                               │    Critical section: steps 1-5 only.
  │                               │    Steps 6-9 run UNLOCKED — PR creation and
  │                               │    labeling are idempotent against fork/main.
  │                               │
  │                               │ 6. gh pr create \
  │                               │      --base <base_branch> \       ──────────►│ PR opened
  │                               │      --head <branch> \                        │
  │                               │      --title "<isa_slug>: <summary>" \        │
  │                               │      --body  "<ISA frontmatter + summary>"   │
  │                               │    capture PR number from stdout              │
  │                               │    if PR already exists → capture # from      │
  │                               │      stderr ("already exists" path — §9)      │
  │                               │
  │                               │ 7. classify_change(worktree, branch, base)
  │                               │    → walk `git diff --name-only origin/<base>...HEAD`
  │                               │    → ChangeClass.SAFE or SENSITIVE
  │                               │
  │                               │    if SAFE:
  │                               │      gh pr edit <pr#> --add-label auto-merge  ──►│
  │                               │      auto_merge_applied = True
  │                               │    else:
  │                               │      gh pr edit <pr#> --add-label needs-human  ──►│
  │                               │      needs_human = True
  │                               │
  │                               │ 8. discord_tool.post(thread_id, msg):
  │                               │      "PR #<N> opened (<auto-merge|human-queue>): <url>"
  │                               │    [via tools/discord_tool.py:908]
  │                               │
  │                               │ 9. (flock already released at step 5)
  │◄── MergeResult ───────────────│
```

Steps 3 and 4 are the only steps that exit without completing the merge. In both cases the lock is released before returning.

**Invariant:** The flock critical section covers only steps 1–5 (`fetch → rebase → push`). This is the minimal scope needed to prevent non-fast-forward push collisions on `fork/main`. Steps 6–9 (`gh pr create`, classify, label, Discord post) are idempotent against `fork/main` and run unlocked. A stuck GitHub API call during PR creation (10-30 s on slow networks, indefinite during GitHub incidents) does NOT block other sessions from completing their own push.

---

## 5. classify_change policy

```
git -C <worktree> diff --name-only origin/<base>...HEAD
```

**Default sensitive prefixes** (any match → `SENSITIVE`):

| Prefix / glob | Rationale |
|---|---|
| `agent/` | Core session and broker logic |
| `gateway/` | Discord adapter, all platform code |
| `auth/` | Auth flows |
| `migrations/` | DB schema changes |
| `pyproject.toml` | Dependency manifest |
| `package*.json` | JS dependency manifest |
| `.github/workflows/` | CI definition |
| `scripts/isa_*.py` | ISA enforcement tooling |
| `hermes_state.py` | Global state machinery |
| `hermes_cli/web_server.py` | Dashboard web server |

**Default safe**: any file whose path does NOT match any sensitive prefix.

Implementation: prefix match via `str.startswith()` on each path returned by `git diff --name-only`. Order: check sensitive first; any match short-circuits to `SENSITIVE`.

The operator may extend `MergePolicy.sensitive_paths_pattern` at instantiation. The default list above is encoded as `MergePolicy` default values (DESIGN.md §13 open question #3 defers tuning to operator).

---

## 6. Auto-merge tooling: decision — GitHub Actions

**Decision: GitHub Actions (operator decision, 2026-05-24). See §6.1 for config.**

Mergify has been evaluated and rejected. Its `#approved-reviews-by >= 1` condition would require the peer-review orchestrator to call `gh pr review --approve` as part of the APPROVE path — a coordination not wired in P2 that would add cross-module coupling. The operator has chosen GitHub Actions as the canonical auto-merge mechanism. There is no Mergify option; `.mergify.yml` must NOT be committed.

**Kodiak: do not propose.** Unmaintained as of 2026 (external-research.md RQ5 finding #4).

---

### §6.1 — GitHub Actions workflow config (canonical)

**File:** `.github/workflows/auto-merge.yml`

```yaml
name: auto-merge on label
on:
  pull_request:
    types: [labeled]

jobs:
  auto-merge:
    if: contains(github.event.pull_request.labels.*.name, 'auto-merge')
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: write
    steps:
      - name: Enable auto-merge
        run: gh pr merge --auto --squash --pull-request ${{ github.event.pull_request.number }}
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Requirements: GitHub-native auto-merge must be enabled on the repo (Settings → General → Allow auto-merge). Squash strategy is squash; to change it, edit the `--squash` flag. No third-party app install required.

| Factor | Detail |
|--------|--------|
| Label gating | Via `pull_request.labeled` trigger + `if` condition |
| No third party | Uses only GitHub primitives |
| Activation | Operator enables "Allow auto-merge" in repo Settings → General once |

---

## 7. Conflict escalation

On `ConflictEscalation`:

1. Broker leaves the worktree in the conflicted rebase state — **do not run `git rebase --abort`**. The operator may want to inspect or resolve manually.
2. Broker posts to the Discord thread (via `discord_tool.py:908`):
   > "Rebase conflict on: `<files>`. Worktree at `<path>`. Resolve conflicts, commit, then `/resume` to retry merge."
3. Dispatcher transitions session to `NEEDS_HUMAN` state (architecture-diagram.md §2 state machine).
4. Lock is released before the Discord post — the mutex must not be held while waiting for operator action.

---

## 8. Failure modes

| Error | Detection | Recovery | Operator-visible message |
|-------|-----------|----------|--------------------------|
| Rebase conflict | `git rebase` non-zero exit | Leave worktree conflicted; release lock; post Discord message | "Rebase conflict on `<files>`. Resolve and `/resume`." |
| `isa_lint` fails | `python3 scripts/isa_lint.py` exit != 0 | Release lock; return `ok=False` | "ISA lint failed for `<isa_path>`: `<lint output>`" |
| `git push` rejected (unexpected) | non-zero exit + stderr contains "rejected" | Raise `ManualBranchInterferenceError`; release lock | "Push rejected — branch `<branch>` may have been pushed manually. Inspect and retry." |
| `gh pr create` fails (not "already exists") | non-zero exit | Release lock; return `ok=False, error=stderr` | "PR create failed: `<stderr>`" |
| Mutex timeout (>30 min waiting) | `fcntl.flock` timeout | Raise `TimeoutError`; caller retries or escalates | "Merge queue backed up — waited 30 min for merge lock. Retry or investigate." |
| `isa_path` does not exist | `isa_lint.py` returns FileNotFoundError-equivalent | Exit non-zero → same path as lint failure | "ISA file not found at `<path>`. Cannot gate merge." |
| `gh` not authenticated | `gh pr create` exits non-zero with auth error | Release lock; return `ok=False` | "gh CLI not authenticated. Run `gh auth status` and retry." |
| Mergify uninstalled mid-flight | Label added, never acted on | Dispatcher poll detects "labeled but not merged after 60 min" | "PR #N has `auto-merge` label but has not merged after 60 min. Mergify may be uninstalled. Check manually." |
| Discord post fails | `discord_tool` raises | Log WARNING; do not fail the merge (PR is already open and labeled) | (operator sees PR on GitHub; may miss Discord notification) |

---

## 9. Idempotency

The idempotency key is the **branch name**, which is `codex/<session_id>/<isa-slug>`. Because `session_id` is a UUID4 assigned once at session allocate and never reused (DESIGN.md §5), the branch is globally unique per session.

If `merge()` is called a second time for the same session:

1. `git push` will push the same commits (fast-forward or no-op).
2. `gh pr create` will fail with "a pull request for branch `<branch>` already exists." The broker parses this specific error from stderr, captures the existing PR number, and proceeds to the label step.
3. If the label is already present, `gh pr edit --add-label` is a no-op on GitHub's side.
4. The broker returns the same `MergeResult` shape with `ok=True` and the captured PR number.

This means a dispatcher retry after a partial failure (e.g. network drop after push but before PR create) is safe.

---

## 10. Post-merge cleanup

MergeBroker does NOT release the worktree. That responsibility belongs to the dispatcher, triggered when GitHub confirms the PR actually merged.

Detection path (dispatcher side, not broker side):

```
Dispatcher polls every 60s:
  gh pr list --label auto-merge --state merged --head 'codex/*' --json number,headRefName
  for each merged PR:
    resolve session_id from headRefName (branch = codex/<sid>/*)
    notify session: "merged"
    WorktreeBroker.release(session_id)
    kanban_complete(task_id)      # kanban_tools.py:360
    delete row from codex_sessions.json (flock + atomic_replace)
    archive Discord thread
```

**Scope invariant:** the `--head 'codex/*'` flag restricts the poll to branches matching `codex/<sid>/<slug>`. Without this flag, the poll would match any merged PR with the `auto-merge` label, including operator-labeled non-Codex PRs — causing the cleanup loop to fail when it tries to extract a session_id from an unrecognized branch name pattern.

The 60-second poll is intentionally cheap — it is a single `gh` CLI call, not a webhook. A webhook would require a public endpoint; the current design (single-host WSL2) does not have one. This is acceptable for the P3 phase.

---

## 11. Security

- `gh` CLI authentication: relies on operator's existing `gh auth status` configuration (WORKFLOW-LESSONS.md §6). No token is stored in this module.
- `git push` authentication: relies on the fork remote's stored credential. No credential stored in this module.
- No credentials, tokens, or secrets appear in logs. Subprocess output is captured to variables, not written to disk.
- Mergify install scope: per-repo only (`jjaavvin-web/hermes-agent`), NOT org-wide. The install step is operator-performed once; grant minimum permissions (read PR metadata, write PR status). Review Mergify's requested permissions before accepting.
- ISA path is validated to exist under `~/.hermes/work/` before passing to `isa_lint.py` — prevents path traversal if a caller passes a crafted path (input validation at module boundary per CLAUDE.md rules).

---

## 12. Edge cases

| Situation | Broker behavior |
|-----------|----------------|
| `isa_path` doesn't exist | `isa_lint.py` exits non-zero → broker returns `ok=False, error="isa_lint failed: <output>"` |
| `gh pr create` fails because PR already exists | Parse PR number from stderr; proceed to label step (idempotency — §9) |
| Push rejected because remote branch is ahead | Raise `ManualBranchInterferenceError`; operator must reconcile manually |
| Mergify uninstalled mid-flight | Label added but never acted on; dispatcher 60 min timeout triggers operator ping (§8 failure table) |
| `git rebase` produces an empty commit (no changes vs base) | `git rebase` may exit 0 with "nothing to commit"; push is a no-op; PR may be empty — `gh pr create` will still succeed. ISA lint gates on ISA quality, not diff size. |
| Session completes with a sub-agent slice not yet reconciled | Caller (dispatcher) must ensure `isa_reconcile.py` runs before calling `merge()`. Broker does not reconcile — it only lints. |
| `classify_change` called on a branch with zero commits ahead of base | `git diff --name-only` returns empty; classify as `SAFE` (no sensitive files changed). |
| `HERMES_HOME` not set | `hermes_home` is passed explicitly to `__init__`; broker does not read env directly — avoids silent misconfiguration. |

---

## 13. Test strategy

Assertions for the execution hive's test suite (`tests/agent/test_merge_broker.py`):

1. **Happy path — safe change:** given a worktree with only non-sensitive files changed, `merge()` returns `MergeResult(ok=True, auto_merge_applied=True, needs_human=False, conflict=False)` and `gh pr edit --add-label auto-merge` was called.

2. **Happy path — sensitive change:** given a worktree with `agent/foo.py` changed, `merge()` returns `MergeResult(ok=True, auto_merge_applied=False, needs_human=True)` and `gh pr edit --add-label needs-human` was called.

3. **Rebase conflict:** mock `git rebase` to exit non-zero with a conflict message. Assert `ConflictEscalation` is raised, lock is released (verify via a second broker call succeeding), and `MergeResult(conflict=True)` is returned.

4. **ISA lint failure:** mock `python3 scripts/isa_lint.py` to exit 1. Assert `IsaLintFailed` is raised and `MergeResult(ok=False, error="isa_lint failed: ...")` contains the lint output.

5. **Idempotency:** call `merge()` twice for the same branch. Second call must parse the "already exists" stderr, return the same PR number, and not raise.

6. **Mutex serialization:** two threads call `merge()` concurrently. Assert the second blocks until the first releases the lock (test with a mock that holds the lock for N ms, verify wall-clock ordering).

7. **Lock timeout:** mock `fcntl.flock` to never return within 30 min. Assert `TimeoutError` is raised and the lock file is not left in a corrupt state.

8. **`classify_change` deny-list coverage:** for each prefix in the default sensitive list, assert a file under that prefix yields `ChangeClass.SENSITIVE`.

9. **`classify_change` safe path:** a file at `docs/readme.md` yields `ChangeClass.SAFE`.

10. **No credentials in output:** assert no subprocess call in any path includes a token or password string.

All tests use subprocess mocks (`unittest.mock.patch("subprocess.run")`); no real `git` or `gh` calls in the test suite.

---

## 14. ASCII state — broker internal

```
                ┌─────────┐
         ──────►│  IDLE   │◄───────────────────────────────┐
                └────┬────┘                                │
                     │ merge() called                      │
                     ▼                                     │
                ┌─────────┐                                │
                │ LOCKING │  flock (blocking, 30 min TO)   │
                └────┬────┘                                │
                     │ acquired                            │
                     ▼                                     │
                ┌──────────┐                               │
                │ FETCHING │  git fetch origin             │
                └────┬─────┘                               │
                     │                                     │
                     ▼                                     │
                ┌──────────┐                               │
                │ REBASING │  git rebase origin/<base>     │
                └──┬───┬───┘                               │
              ok   │   │ conflict → ConflictEscalation     │
                   │   └───────────────────────────────────┤ (release lock)
                   ▼                                       │
                ┌──────────┐                               │
                │ LINTING  │  python3 scripts/isa_lint.py  │
                └──┬───┬───┘                               │
              ok   │   │ fail → IsaLintFailed              │
                   │   └───────────────────────────────────┤ (release lock)
                   ▼                                       │
                ┌──────────┐                               │
                │ PUSHING  │  git push fork <branch>       │
                └────┬─────┘                               │
                     │                                     │
                     ▼                                     │
                ┌───────────┐                              │
                │ PR_CREATE │  gh pr create                │
                └────┬──────┘                              │
                     │                                     │
                     ▼                                     │
                ┌─────────────┐                            │
                │ CLASSIFYING │  classify_change           │
                └──────┬──────┘                            │
                       │                                   │
                       ▼                                   │
                ┌────────────┐                             │
                │  LABELING  │  gh pr edit --add-label     │
                └──────┬─────┘                             │
                       │                                   │
                       ▼                                   │
                ┌────────────┐                             │
                │  NOTIFYING │  discord_tool.post          │
                └──────┬─────┘                             │
                       │ release lock                      │
                       └───────────────────────────────────┘
```

---

## 15. Citations

| Claim | Citation |
|-------|---------|
| Global merge mutex via flock | collision-matrix.md §2 row "Push to fork/main race"; DESIGN.md §6.4 |
| git subprocess pattern | `git_janitor.py:50-55` |
| ISA lint as CheckCompleteness gate | `ISA-SPEC.md §9`; cluster-C-kanban-memory-isa.md §"isa_lint.py — rules enforced" |
| isa_lint 13-rule accumulating pass | cluster-C-kanban-memory-isa.md §"isa_lint.py — rules enforced"; `scripts/isa_lint.py:39-180` |
| kanban_complete tool | `kanban_tools.py:360`; cluster-C-kanban-memory-isa.md §"Kanban tool API" |
| GitHub native auto-merge cannot be label-triggered | external-research.md RQ5 finding #1 |
| Mergify label-gated rule syntax | external-research.md RQ5 finding #3 |
| GitHub Actions workaround for label trigger | external-research.md RQ5 finding #2 |
| Kodiak unmaintained | external-research.md RQ5 finding #4 |
| discord_tool outbound post | `tools/discord_tool.py:908`; DESIGN.md §4 step 12 |
| Branch naming convention (codex/<sid>/<slug>) | architecture-diagram.md §3; DESIGN.md §5 |
| merge target = fork/main | DESIGN.md §2 Decision D; git remotes origin=NousResearch, fork=jjaavvin-web |
| Dispatcher poll for merged PRs (60 s) | DESIGN.md §4 step 12; architecture-diagram.md §5 |
| ISA reconcile must precede merge | DESIGN.md §2 Decision B; `scripts/isa_reconcile.py:146-261` |
| Session state machine MERGING → COMPLETE | architecture-diagram.md §2 |
| WorktreeBroker.release is dispatcher's job | DESIGN.md §6.4; architecture-diagram.md §3 |
| Mergify install scope: per-repo only | external-research.md RQ5; §11 security above |
| gh CLI auth assumption | DESIGN.md §11 (WORKFLOW-LESSONS.md §6 reference) |
