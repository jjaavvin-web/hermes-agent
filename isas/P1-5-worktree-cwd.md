---
isa:      20260525-1300_codex-parallel-p1-5-worktree-cwd
task:     "P1.5 — Wire per-thread worktree as tool-call cwd via contextvars"
tier:     E2
phase:    complete
progress: 7/7
card:     "-"
board:    hermes-kanban-control
branch:   feat/codex-parallel-p1-5-worktree-cwd
hive:     "-"
owner:    claude-code
started:  2026-05-25T13:00:00Z
updated:  2026-05-25T13:00:00Z
---

## Problem

P1 (PR #37, merged as `2c20d0bb1`) shipped the Codex parallel workflow
substrate: per-thread git worktree allocation + dispatcher tracking +
Hermes-as-executor. But the dispatched messages still ran with the
gateway process's cwd (the live tree), not the assigned worktree. So
"parallel sessions with worktree isolation" was true for the *state*
(branch + port + JSON row) but false for the *execution* (bash / code
tool calls all landed in the same live checkout). Without this, P1 was
conversational-only, not actually load-bearing for parallel code work.

## Goal

After this ISA: when a Discord message arrives in a tracked codex thread,
`gateway/platforms/discord.py:on_message` sets a per-async-task
ContextVar to the row's `worktree_path`. `LocalEnvironment._run_bash`
in `tools/environments/local.py` consults that ContextVar before
falling back to `self.cwd`. The asyncio task model gives N concurrent
threads N isolated worktree contexts without explicit token
threading — `contextvars.ContextVar` is task-local by design.

## Out of Scope

- Routing the override through `register_task_env_overrides` (the
  existing batch_runner / RL hook) — that would require matching Hermes
  session IDs which are computed lazily inside `_handle_message`;
  contextvars avoid the matching problem entirely.
- Per-tool cwd injection for tools that already take absolute paths
  (`edit`, `write`, `read`) — they don't need cwd; the change is
  bash / code-execution-only.
- Removing the leftover dispatcher state (codex_sessions.json rows from
  the P1 live testing) — separate cleanup task.

## Constraints

- **ContextVar must remain async-task-local**, not module-global.
  Concurrent Discord threads must not see each other's worktree paths.
- **Defense against deleted worktrees**: if the contextvar points at
  a path that no longer exists, fall back to `self.cwd` rather than
  letting `subprocess.Popen` raise `FileNotFoundError`.
- **Backward compatibility**: when no codex thread context is active
  (e.g., DM, non-codex channel, or no dispatcher), `LocalEnvironment`
  behaves exactly as before.
- **Lazy import** in `local.py` so the file remains importable when
  `agent.codex_session_context` isn't present (e.g., partial install).

## Criteria

- [x] ISC-1: new file `agent/codex_session_context.py` exposes
  `set_active_worktree`, `get_active_worktree`, `reset_active_worktree`
  backed by a `contextvars.ContextVar`
- [x] ISC-2: `tools/environments/local.py` `LocalEnvironment._run_bash`
  consults `get_active_worktree()` before falling back to `self.cwd`;
  override is ignored if the path doesn't exist on disk
- [x] ISC-3: `gateway/platforms/discord.py` `on_message` sets the
  contextvar from the dispatcher row's `worktree_path` BEFORE falling
  through to `_handle_message`, for tracked codex threads only
- [x] ISC-4: ContextVar is task-isolated — two concurrent asyncio tasks
  setting different values each see their own
- [x] ISC-5: Anti: when no codex thread is the source, `_run_bash` uses
  `self.cwd` unchanged (zero behavior change for the default path)
- [x] ISC-6: Anti: when the contextvar points at a missing directory,
  `_run_bash` falls back to `self.cwd` (no `FileNotFoundError` from
  Popen, no crash)
- [x] ISC-7: `isa_lint isas/P1-5-worktree-cwd.md` exit 0 at
  `phase: complete`

## Test Strategy

| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `python -c "from agent.codex_session_context import set_active_worktree, get_active_worktree, reset_active_worktree; print('ok')"` | `ok` |
| ISC-2 | `pytest tests/agent/test_codex_session_context.py::TestLocalEnvironmentIntegration::test_run_bash_uses_contextvar_when_set` | pass |
| ISC-3 | `grep -nE 'set_active_worktree' gateway/platforms/discord.py \| wc -l` | ≥ 1 |
| ISC-4 | `pytest tests/agent/test_codex_session_context.py::TestAsyncTaskIsolation` | 3 pass |
| ISC-5 | `pytest tests/agent/test_codex_session_context.py::TestLocalEnvironmentIntegration::test_run_bash_uses_self_cwd_when_contextvar_unset` | pass |
| ISC-6 | `pytest tests/agent/test_codex_session_context.py::TestLocalEnvironmentIntegration::test_run_bash_ignores_contextvar_when_dir_missing` | pass |
| ISC-7 | `python3 scripts/isa_lint.py isas/P1-5-worktree-cwd.md ; echo $?` | `0` |

## Git Plan

- **Branch**: `feat/codex-parallel-p1-5-worktree-cwd` off `fork/main`
  (post PR #37 merge at `2c20d0bb1`).
- **Single commit** — scope is small enough; separating contextvar +
  hook + tests would just create churn.
- **Push**: `git push fork feat/codex-parallel-p1-5-worktree-cwd`.
- **PR**: open against `fork/main` titled
  `feat(p1.5): per-thread worktree cwd for tool calls in tracked codex threads`.
- **Merge** when CI green (no operator gates — all probes are mechanical).

## Decisions

**D-1 (2026-05-25): contextvars over `register_task_env_overrides`.**
Considered using the existing `tools.terminal_tool.register_task_env_overrides`
infrastructure (already used by `batch_runner.py`). Rejected because
the override is keyed by `task_id`, and the discord adapter doesn't
know which Hermes session_id will be assigned at message-receive time
(it's computed lazily inside `_handle_message`). ContextVars sidestep
the matching problem — set at the discord boundary, automatically
inherited by every async call inside the turn. Tradeoff: one new
module instead of reusing an existing hook; cleanliness wins.

**D-2 (2026-05-25): Hook at `LocalEnvironment._run_bash`, not at higher
tool entry points.** Every code path that ends up running bash flows
through `_run_bash`'s `subprocess.Popen` call. Hooking there catches
the terminal tool, the code execution tool, AND any future tool that
spawns a shell. Hooking higher would require touching N entry points
and risk missing one.

## Changelog

2026-05-25 — P1 was conversational-only; tool calls not isolated
  conjectured:   the dispatcher allocating a per-thread worktree was
                 enough to give each Discord thread file-isolation
  refuted by:    P1 live testing showed `pwd` inside the thread
                 returned the live tree path, not the worktree —
                 because LocalEnvironment uses self.cwd which is set
                 at gateway init time, not per-message
  learned:       the worktree path needs to flow through the async
                 call chain from the message handler to the
                 subprocess.Popen call; contextvars are the
                 idiomatic Python primitive for per-task context
  criterion now: ISC-1..6 added; new `agent.codex_session_context`
                 module + hooks in discord.py and local.py;
                 contextvar is task-local by design so concurrent
                 threads stay isolated

## Verification

### ISC-1 — module exposes contextvar API

```
$ python -c "from agent.codex_session_context import set_active_worktree, get_active_worktree, reset_active_worktree; print('ok')"
ok
```

### ISC-2 — LocalEnvironment honors contextvar

```
$ pytest tests/agent/test_codex_session_context.py::TestLocalEnvironmentIntegration::test_run_bash_uses_contextvar_when_set -q
.                                                                        [100%]
1 passed
```

### ISC-3 — discord.py hooks the contextvar

```
$ grep -nE 'set_active_worktree' gateway/platforms/discord.py
913:                            from agent.codex_session_context import set_active_worktree  # noqa: PLC0415
923:                                set_active_worktree(_wt)
```

### ISC-4 — async task isolation

```
$ pytest tests/agent/test_codex_session_context.py::TestAsyncTaskIsolation -q
...                                                                      [100%]
3 passed
```

### ISC-5 — Anti: default path unchanged

```
$ pytest tests/agent/test_codex_session_context.py::TestLocalEnvironmentIntegration::test_run_bash_uses_self_cwd_when_contextvar_unset -q
.                                                                        [100%]
1 passed
```

### ISC-6 — Anti: missing dir falls back

```
$ pytest tests/agent/test_codex_session_context.py::TestLocalEnvironmentIntegration::test_run_bash_ignores_contextvar_when_dir_missing -q
.                                                                        [100%]
1 passed
```

### ISC-7 — isa_lint at phase: complete

```
$ python3 scripts/isa_lint.py isas/P1-5-worktree-cwd.md
PASS: isas/P1-5-worktree-cwd.md
$ echo $?
0
```
