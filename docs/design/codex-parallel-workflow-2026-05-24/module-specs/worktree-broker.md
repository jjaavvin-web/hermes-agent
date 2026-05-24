# Module Spec — Worktree Broker

**Implements:** DESIGN.md §6.2
**Phase:** P1 (allocate/release); P5 (gc)
**File created:** `agent/worktree_broker.py` (greenfield)

---

## 1. Purpose & scope

`WorktreeBroker` is the single module responsible for creating, tracking, and destroying per-session git worktrees under `~/.hermes/codex-wt/<sid>/`. It also owns the port-allocation side-table (`~/.hermes/codex-ports.json`). The `CodexSessionDispatcher` calls `allocate()` on thread creation and `release()` on thread archive or `/kill`; nothing else in the codebase touches worktree lifecycle. `gc()` ships in P5 only and is excluded from P1 scope.

---

## 2. Files created

| Path | Type | Notes |
|---|---|---|
| `agent/worktree_broker.py` | new Python module | the entire spec lives here |
| `~/.hermes/codex-wt/` | directory | created on first `allocate()` call if absent |
| `~/.hermes/codex-ports.json` | JSON file | created on first `allocate()` call if absent; flock-guarded |

`codex_sessions.json` is NOT owned by this module. The dispatcher owns it; `release()` does not touch it.

---

## 3. Public API

```python
from __future__ import annotations

import fcntl
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

@dataclass
class Worktree:
    session_id: str          # UUID4 assigned by dispatcher
    path: Path               # ~/.hermes/codex-wt/<sid>/
    branch: str              # codex/<sid>/<isa-slug>
    port: int | None         # None if no port was available or project has no dev server
    created_at: datetime

@dataclass
class WorktreeStatus:
    session_id: str
    path: Path
    branch: str
    port: int | None
    created_at: datetime
    path_exists: bool        # False if worktree was removed externally
    tmux_alive: bool         # result of `tmux has-session -t codex-sess-<sid>`

class DiskPressureError(OSError):
    """Raised when ~/.hermes free space is below 4 GB before allocate."""

class BranchCollisionError(ValueError):
    """Raised when the branch codex/<sid>/<isa-slug> already exists on the remote.
    Structurally impossible with UUID4 sids, but handled explicitly."""

class RepoStateError(RuntimeError):
    """Raised when git worktree add fails due to dirty parent repo state
    (e.g. uncommitted modifications on the operator's working tree).
    Broker does NOT stash — that would silently discard operator work."""

class WorktreeBroker:
    def __init__(
        self,
        *,
        repo_root: Path,
        hermes_home: Path,
        port_range: Tuple[int, int] = (50000, 50008),
    ) -> None:
        """
        repo_root   — absolute path to the hermes-agent git repo
                      (the repo that worktrees branch off of).
        hermes_home — typically Path("~/.hermes").expanduser()
        port_range  — half-open [lo, hi); default covers 50000-50007 (8 ports).

        On init: ensures codex-wt/ directory exists; initialises
        codex-ports.json if absent (all ports null).
        Does NOT spawn subprocesses or write to disk unless those files are missing.
        """
        ...

    def allocate(self, session_id: str, *, isa_slug: str, base_branch: str = "origin/main") -> Worktree:
        """
        Create a git worktree and claim a port for the given session.

        Pre-conditions:
          - session_id is a UUID4 string (uniqueness enforced by dispatcher).
          - isa_slug is a short URL-safe slug derived from the ISA title;
            used only as the branch name suffix, not as an identifier.
          - base_branch must resolve in the repo (typically "origin/main").

        Steps (in order):
          1. Disk-pressure check: if df -P hermes_home reports < 4 GB free,
             raise DiskPressureError. If < 8 GB free, log a warning but continue.
          2. Idempotency check: if a Worktree for this session_id already exists
             in the internal registry, return it immediately without re-running git.
          3. Run `git -C <repo_root> worktree add <wt_path> -b <branch> <base_branch>`
             via the subprocess pattern from git_janitor.py:50-55 (capture_output=True,
             text=True, check=False). On nonzero returncode:
               - stderr contains "already exists" → raise BranchCollisionError
               - stderr contains "modified files" or "untracked files" → raise RepoStateError
               - anything else → raise RuntimeError with full stderr
          4. Allocate a port via _allocate_port(session_id). Returns None if all
             ports are occupied (non-fatal; session just won't have a dev-server port).
          5. Detect whether the worktree root contains package.json. If yes, set
             Worktree.has_js = True and register a first-touch hook (see §5).
             Does NOT run npm/pnpm at allocate time.
          6. Register the Worktree in self._registry (in-memory dict keyed by session_id).
          7. Return the Worktree dataclass.

        Post-conditions:
          - <wt_path> directory exists and is a valid git worktree on branch <branch>.
          - <branch> is checked out at <base_branch> HEAD.
          - Port is reserved in codex-ports.json (if allocated).

        Raises:
          DiskPressureError   — < 4 GB free; dispatcher surfaces to Discord thread.
          BranchCollisionError — branch already exists (operator escalation required).
          RepoStateError      — dirty parent repo; operator must clean up.
          RuntimeError        — unexpected git failure.
        """
        ...

    def release(self, session_id: str) -> None:
        """
        Tear down the worktree for the given session.

        Pre-conditions:
          - May be called even if session_id has no registered Worktree
            (idempotent — no-op if unknown).

        Steps (in order, each logged individually):
          1. tmux kill-session: `tmux kill-session -t codex-sess-<sid>`.
             Failure is logged and ignored — if the session is already dead
             that's fine; the rest of release still runs.
          2. git worktree remove: `git -C <repo_root> worktree remove --force <wt_path>`.
             --force is required because Codex may have left uncommitted changes
             if release was triggered mid-turn. The commit story is the session's
             responsibility (handled by ISA reconcile + merge broker before release).
             On failure: log stderr; do not re-raise (worktree directory may already
             be absent if removed externally — gc handles cleanup).
          3. Free the port: call _free_port(session_id).
          4. Remove session_id from self._registry.

        Post-conditions (best-effort):
          - tmux session codex-sess-<sid> is gone.
          - Worktree directory is gone from ~/.hermes/codex-wt/.
          - Port is null in codex-ports.json.
          - Session is absent from self._registry.

        Note: does NOT touch codex_sessions.json. Caller (dispatcher) owns that file.

        Idempotent: calling release() twice for the same sid is safe.
        """
        ...

    def gc(self) -> list[GcAction]:
        """
        P5 ONLY — do not implement in P1.

        Identify orphaned worktrees (present on disk, but no live tmux session,
        no row in codex_sessions.json, no open PR) and move them to the
        rename-to-deleted staging area.

        Returns a list of GcAction describing what was moved.
        See §9 for the full algorithm.
        """
        ...

    def free_port(self, port: int) -> None:
        """
        Release a specific port back to the pool.
        Used by callers that allocated a port independently (e.g. dispatcher
        reallocating after a failed session revive).
        Acquires flock on codex-ports.json; sets the port's value to null.
        No-op if port is already null.
        """
        ...

    def status(self, session_id: str) -> WorktreeStatus | None:
        """
        Return current status for session_id, or None if not registered.
        Reads path.exists() from the filesystem (one stat call).
        Checks tmux liveness via `tmux has-session -t codex-sess-<sid>`
        (returncode 0 = alive, 1 = dead).
        Does NOT modify any state.
        """
        ...
```

Internal helpers (not public):

```python
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        """Mirror of git_janitor.py:50-55:
        subprocess.run(["git", "-C", str(self.repo_root), *args],
                       capture_output=True, text=True, check=False)
        """

    def _disk_free_bytes(self) -> int:
        """df -P self.hermes_home; parse 'Available' column; return bytes."""

    def _allocate_port(self, session_id: str) -> int | None:
        """Atomic port claim. See §4."""

    def _free_port(self, session_id: str) -> None:
        """Atomic port release. See §4."""
```

---

## 4. Port broker

**File:** `~/.hermes/codex-ports.json`

**Schema:**
```json
{
  "50000": "<sid-or-null>",
  "50001": "<sid-or-null>",
  "50002": "<sid-or-null>",
  "50003": "<sid-or-null>",
  "50004": "<sid-or-null>",
  "50005": "<sid-or-null>",
  "50006": "<sid-or-null>",
  "50007": "<sid-or-null>"
}
```

Values are either a UUID4 session ID string or JSON `null`.

**Allocate flow (atomic):**

```
1. open(codex-ports.json, "r+") → fd
2. fcntl.flock(fd, LOCK_EX)
3. data = json.load(fd)
4. find first key whose value is null
5. if none: flock release, return None  (no port available)
6. data[port] = session_id
7. fd.seek(0); json.dump(data, fd); fd.truncate()
8. fcntl.flock(fd, LOCK_UN)
9. return int(port)
```

Writes are in-place with seek+truncate (not atomic-rename) because the flock already serialises all writers. Atomic rename is not required here — flock ensures no reader sees a partial write, and only `WorktreeBroker` instances write this file.

**Release flow (atomic):**

Same flock + read + set-null + write sequence. Key is looked up by value (session_id), not by port number, so `_free_port(session_id)` scans values for the sid and nulls the matching key.

**Recovery on broker init:**

During `__init__`, after loading codex-ports.json, cross-reference each non-null value against `~/.hermes/codex_sessions.json`. Any port whose sid has no row in codex_sessions.json is stale; set it to null and write back. This handles the case where the broker or bot crashed between port allocation and session-row write.

---

## 5. node_modules / pnpm strategy

**Detection** (at `allocate()`, step 5):

Scan the worktree root for these files in order:

| File | Signals |
|---|---|
| `pnpm-lock.yaml` | pnpm project |
| `package-lock.json` | npm project |
| `yarn.lock` | yarn project |
| `package.json` (no lock) | JS project, no lock file |

Store the detected lock type on the `Worktree` dataclass (`lock_type: str | None`).

**Install policy (LOCKED — objective §7 #6):**

Do NOT run any installer at `allocate()` time. Install runs on-demand at the first JS-touching turn, triggered by the dispatcher calling `broker.install_deps(session_id)` when it detects a turn that reads or writes JS files.

**Install command precedence:**

| lock_type | Command |
|---|---|
| `pnpm` | `pnpm install --dir <worktree_path>` |
| `npm` | `npm ci --prefix <worktree_path>` |
| `yarn` | `yarn install --frozen-lockfile --cwd <worktree_path>` |
| `None` | skip |

**pnpm recommendation:**

If the project uses pnpm (pnpm-lock.yaml detected), and if `pnpm-workspace.yaml` does not already contain `enableGlobalVirtualStore: true`, the broker logs an INFO-level advisory:

> "Project uses pnpm. Adding enableGlobalVirtualStore: true to pnpm-workspace.yaml would reduce per-worktree disk from ~500 MB to near-zero (external-research RQ4). Operator can apply this; broker will not modify the file automatically."

The broker never writes `pnpm-workspace.yaml`. It is the operator's or project's choice.

**Disk monitoring post-install:**

After install completes, log `du -sh <worktree_path>/node_modules` at INFO level. This feeds the `/codex-sessions` tab's per-session disk column (P4).

---

## 6. Lifecycle

```
                   allocate(sid)
                        │
                        ▼
               ┌──────────────────┐
               │   ALLOCATING     │
               │  disk check      │
               │  git wt add      │
               │  port claim      │
               └───────┬──────────┘
                       │ ok
                       ▼
               ┌──────────────────┐
               │     ACTIVE       │◄─── status() returns WorktreeStatus
               │  wt on disk      │     tmux_alive = True (usually)
               │  branch live     │
               │  port reserved   │
               └───────┬──────────┘
                       │ release(sid) called
                       ▼
               ┌──────────────────┐
               │   RELEASING      │
               │  tmux kill       │
               │  git wt remove   │
               │  port free       │
               └───────┬──────────┘
                       │ complete
                       ▼
               ┌──────────────────┐
               │    RELEASED      │
               │  not in registry │
               └──────────────────┘

               Parallel path (P5 only):

               ACTIVE ──► (no tmux, no PR, no session row)
                                       │
                                       ▼
                              ┌──────────────────┐
                              │     ORPHANED     │
                              │  gc() detects    │
                              └────────┬─────────┘
                                       │ rename, not rm -rf
                                       ▼
                              ┌──────────────────┐
                              │    GC'D          │
                              │  .deleted-<ts>/  │
                              │  reaper purges   │
                              │  after 7 days    │
                              └──────────────────┘
```

---

## 7. Disk-pressure handling

| Condition | Action |
|---|---|
| `df -P hermes_home` free < 4 GB | Raise `DiskPressureError` before any git call. Dispatcher catches it, posts to Discord thread: "Cannot allocate worktree: less than 4 GB free on ~/.hermes. Run /gc or free disk and retry." |
| free < 8 GB | Log `WARNING: disk pressure (<N> GB free); allocating anyway. Consider running gc.` Continue normally. |
| P5 auto-gc trigger | When free < 8 GB, gc() auto-fires every 5 min (P5 scheduler tick). Not wired in P1. |

The 4 GB floor comes from collision-matrix.md §2. The 8 GB soft threshold leaves headroom for one additional heavy JS worktree (~2 GB worst case, external-research RQ4) before hitting the hard floor.

---

## 8. Release semantics

The exact sequence matters. Deviating from this order risks leaving a live Codex subprocess in a destroyed worktree.

```
1. tmux kill-session -t codex-sess-<sid>
     → kills the Codex subprocess and its parent hermes process cleanly
     → failure: log, continue (session may already be dead)

2. git -C <repo_root> worktree remove --force <wt_path>
     → --force required: Codex may have left uncommitted changes if
       release was triggered mid-turn (e.g. operator /kill during execute phase).
       The ISA reconcile and merge broker handle the commit story before release
       reaches this point in the normal flow; --force covers the abnormal path.
     → failure: log stderr, continue

3. _free_port(session_id)
     → flock + null the port entry

4. del self._registry[session_id]
```

The dispatcher calls `release()` only after it has removed the row from `codex_sessions.json`. The broker does not touch `codex_sessions.json`.

---

## 9. gc() — P5 only

`gc()` must not be called or implemented in P1. This section specifies it for P5 implementation.

**Inputs:**

- `git -C <repo_root> worktree list --porcelain` — the same call that `git_janitor.py:68` already makes; reuse `_git()` helper
- `~/.hermes/codex_sessions.json` — set of live session IDs
- `tmux ls -F '#{session_name}'` — set of live tmux sessions
- `gh pr list --head 'codex/*' --state open --json headRefName` — branches with open PRs

**Orphan definition:** a worktree path under `~/.hermes/codex-wt/` that satisfies all three:
1. No row in codex_sessions.json with matching sid
2. No tmux session named `codex-sess-<sid>`
3. No open PR whose branch matches `codex/<sid>/*`

**Action per orphan (per WORKFLOW-LESSONS §3 rule 5 — never `rm -rf`):**

```
ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
dest = hermes_home / "codex-wt" / f".deleted-{ts}" / sid
dest.parent.mkdir(parents=True, exist_ok=True)
Path(wt_path).rename(dest)
git -C repo_root worktree prune    # prune git's internal worktree refs
```

After 7 days, a background reaper (outside this module's scope) purges `.deleted-*` directories. The reaper is a simple cron or P5 scheduler tick; it is not part of `WorktreeBroker`.

**Return value:** `list[GcAction]` where `GcAction` is a dataclass `{sid, old_path, new_path, reason}`. The dispatcher logs these to the `/codex-sessions` tab.

---

## 10. Error modes

| Error | Detection | Recovery | Operator-visible message |
|---|---|---|---|
| `DiskPressureError` | `df -P` free < 4 GB before allocate | Dispatcher does not allocate. Operator frees disk or runs /gc. | "Cannot start session: disk pressure (N GB free, need 4 GB). Free space and retry." |
| `BranchCollisionError` | git stderr contains "already exists" | Operator escalation — UUID4 collision is astronomically unlikely; suspect corrupt state if it occurs. | "Branch codex/<sid>/<slug> already exists. Operator intervention required." |
| `RepoStateError` | git stderr contains "modified files" / "untracked files" | Operator must clean working tree of hermes-agent repo root. Broker will NOT stash. | "git worktree add failed: repo has uncommitted changes in <repo_root>. Clean or stash them and retry." |
| Port exhaustion | All ports non-null in codex-ports.json | Allocate continues; `Worktree.port = None`. Session cannot run a dev server. Log warning. | Logged at WARNING; no Discord message unless dispatcher chooses to surface it. |
| git worktree remove fails on release | nonzero returncode | Log stderr; continue release. Path may be manually cleaned by operator or by gc(). | Logged; no operator ping unless gc() later detects the path. |
| tmux kill-session fails on release | nonzero returncode | Log; continue. Session may already be dead. | Logged at DEBUG. |
| codex-ports.json missing or corrupt JSON | `json.JSONDecodeError` on load | Re-initialise file with all ports null; log WARNING. | Logged at WARNING. |

---

## 11. Edge cases

**Session killed mid-edit (uncommitted changes in worktree).** `release()` uses `git worktree remove --force`, which discards uncommitted work. This is intentional. Per design, any session in a mid-turn state was already in execute/verify phase — its commit policy is the session's responsibility, not the broker's. The merge broker and ISA reconcile must complete before `release()` is called in the normal flow; `--force` is the abnormal-path safety valve.

**Two `allocate()` calls for the same sid (idempotency).** Step 2 of `allocate()` checks `self._registry` first. If the sid is already registered, return the existing `Worktree` immediately. No git call, no port claim. This handles the case where the dispatcher retries after a transient failure.

**Branch already exists on remote (collision).** UUID4 sids make this structurally impossible, but git will refuse the worktree add with "branch already exists." The broker raises `BranchCollisionError` — the dispatcher surfaces it to the operator as a critical error requiring manual investigation.

**`worktree add` fails because parent repo has dirty state.** The broker raises `RepoStateError` and tells the operator exactly which repo needs cleaning. It does NOT run `git stash` — that would silently hide the operator's in-progress work. Stash vs. commit is the operator's decision.

**Worktree removed externally while session is ACTIVE.** `status()` will return `path_exists = False`. The dispatcher should transition the session to NEEDS_REVIVE. `release()` called on such a session will fail at step 2 (git wt remove) with a non-fatal error; the port will still be freed and the registry entry removed.

**`codex_sessions.json` absent during port-recovery in `__init__`.** Treat as empty — all non-null ports are stale. Null them and continue. Log at WARNING.

---

## 12. Test strategy (high-level)

Assertions the test suite must cover:

| # | Assertion |
|---|---|
| 1 | `allocate()` creates `~/.hermes/codex-wt/<sid>/` as a valid git worktree |
| 2 | `allocate()` creates branch `codex/<sid>/<isa-slug>` at the correct base commit |
| 3 | `allocate()` claims a port in codex-ports.json and sets value to sid |
| 4 | `allocate()` twice with same sid returns identical `Worktree` without a second `git worktree add` call |
| 5 | `release()` removes the worktree directory, nulls the port, removes the registry entry |
| 6 | `release()` called on unknown sid is a no-op (no exception) |
| 7 | `DiskPressureError` raised when mocked `df` reports < 4 GB free |
| 8 | `RepoStateError` raised when mocked git returns "modified files" in stderr |
| 9 | `BranchCollisionError` raised when mocked git returns "already exists" in stderr |
| 10 | Port exhaustion (all 8 ports occupied) → `allocate()` succeeds with `Worktree.port = None` |
| 11 | Port recovery at `__init__` nulls ports whose sids are absent from codex_sessions.json |
| 12 | `install_deps()` runs pnpm when pnpm-lock.yaml is present; npm ci when package-lock.json is present; skip when no package.json |
| 13 | `gc()` (P5) renames orphaned worktree to `.deleted-<ts>/`; does not `rm -rf` |
| 14 | `gc()` skips worktrees that have an open PR for their branch |
| 15 | `_git()` uses `subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)` — no `check=True` |

Tests live in `tests/agent/test_worktree_broker.py`. Mock git and tmux subprocesses; do not run real git operations in the test suite.

---

## 13. Citations

| Claim | Source |
|---|---|
| Subprocess pattern: `subprocess.run(["git","-C",repo,*args], capture_output=True, text=True, check=False)` | `git_janitor.py:50-55` |
| `git worktree list --porcelain` already used in codebase | `git_janitor.py:68` |
| Worktree path `~/.hermes/codex-wt/<sid>/` | DESIGN.md §6.2, architecture-diagram.md §8 |
| Branch naming `codex/<sid>/<isa-slug>` | DESIGN.md §5, collision-matrix.md §2 |
| Port range 50000-50007 | DESIGN.md §7, collision-matrix.md §4 |
| Port broker via flock on `codex-ports.json` | collision-matrix.md §2, architecture-diagram.md §3 |
| Per-worktree on-demand install (LOCKED) | DESIGN.md §7 objective #6, collision-matrix.md §2 |
| pnpm `enableGlobalVirtualStore: true` for near-zero disk overhead | external-research.md RQ4 |
| pnpm concurrent installs into same global store are safe | external-research.md RQ4 |
| npm: 8 worktrees = 8 full copies (~1.6-4 GB) vs pnpm ~500 MB total | external-research.md RQ4 |
| Per-worktree isolation matches industry consensus | external-research.md RQ1 |
| 4 GB disk floor before refusing allocate | collision-matrix.md §2 |
| 8-session worst-case disk ~16 GB (npm); ~0.5 GB (pnpm) | collision-matrix.md §4, external-research.md RQ4 |
| Rename-to-deleted pattern, never `rm -rf` | WORKFLOW-LESSONS.md §3 rule 5 (cluster-D-worktree-dashboard.md §"WORKFLOW-LESSONS.md quotes") |
| `tmux kill-session` before `worktree remove` | architecture-diagram.md §3 (release flow) |
| `git worktree remove --force` required for mid-turn release | architecture-diagram.md §3 |
| gc() is P5 only | DESIGN.md §8 |
| WorktreeBroker is greenfield — no existing module manages worktrees in the dashboard layer | cluster-D-worktree-dashboard.md §"Existing git-shell-out patterns" |
| flock atomic pattern: compare `telegram.py:1077-1133` atomic_replace for the same flock+write idiom | collision-matrix.md §2 |
