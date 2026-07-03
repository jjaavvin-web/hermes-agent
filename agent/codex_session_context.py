"""Per-Discord-thread session context for the Codex parallel workflow.

Set by the Discord adapter's ``on_message`` hook when a tracked codex
thread is the message source; consumed by tool/runtime surfaces to bind
work to a lane-specific worktree and, when configured, a lane-specific
executor.

`ContextVar` is async-safe — each `asyncio.Task` gets its own copy
inherited at creation time. That means concurrent Discord/webhook threads can
each set their own active worktree/executor without leaking across the event
loop's scheduling.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import os
from pathlib import Path
from typing import NamedTuple, Optional

_active_worktree_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hermes_codex_active_worktree",
    default=None,
)

_confinement_required_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hermes_codex_worktree_confinement_required",
    default=False,
)

_lane_executor_var: contextvars.ContextVar[
    Optional[concurrent.futures.Executor]
] = contextvars.ContextVar(
    "hermes_codex_lane_executor",
    default=None,
)

_run_execution_cwds_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "hermes_codex_runtime_execution_cwds",
    default=None,
)


class WorktreeConfinementError(RuntimeError):
    """Raised when a runtime/tool cwd cannot be confined to the active worktree."""


class _WorktreeContextToken(NamedTuple):
    """Reset tokens for the active worktree and confinement marker."""

    active_worktree: contextvars.Token[Optional[str]]
    confinement_required: contextvars.Token[bool]


def set_active_worktree(path: Optional[str]):
    """Set the active codex worktree path for the current async task.

    Returns a token that can be passed to ``reset_active_worktree`` to
    undo the change. Typically used at the boundary where a gateway message
    handler kicks off the agent turn.

    A truthy ``path`` marks this turn as requiring worktree confinement. The
    actual runtime/tool cwd resolver then validates the path at execution time
    and fails closed if it has disappeared or no longer points at a directory.
    """
    active_token = _active_worktree_var.set(path)
    confinement_token = _confinement_required_var.set(bool(path))
    return _WorktreeContextToken(active_token, confinement_token)


def get_active_worktree() -> Optional[str]:
    """Return the active codex worktree path, or ``None`` if unset."""
    return _active_worktree_var.get()


def is_worktree_confinement_required() -> bool:
    """Return whether this turn must fail closed to its bound worktree."""
    return _confinement_required_var.get()


def reset_active_worktree(token) -> None:
    """Reset the active worktree using the token returned by set_active_worktree.

    Backward-compatible with pre-F3 callers/tests that may still pass a raw
    ``ContextVar`` token for ``_active_worktree_var``.
    """
    if isinstance(token, _WorktreeContextToken):
        _confinement_required_var.reset(token.confinement_required)
        _active_worktree_var.reset(token.active_worktree)
    else:
        _active_worktree_var.reset(token)


def require_confinement_without_worktree():
    """Arm fail-closed worktree confinement WITHOUT a valid worktree — for an autonomous resume whose persisted worktree is GONE (isolation lost). is_worktree_confinement_required() -> True while get_active_worktree() -> None, so file_tools denies write_file/patch/relative-resolve (fail closed). Reset with reset_active_worktree(token). (t_0113eacc F5, escape vector V4)."""
    active_token = _active_worktree_var.set(None)
    confinement_token = _confinement_required_var.set(True)
    return _WorktreeContextToken(active_token, confinement_token)


def set_lane_executor(executor: Optional[concurrent.futures.Executor]):
    """Set the lane-dedicated executor for the current async task.

    Returns a token that can be passed to ``reset_lane_executor`` to undo
    the change. Set by the webhook adapter when the off-loop lane pool is
    enabled; consumed by ``GatewayRunner._run_in_executor_with_context`` to
    route heavy blocking work onto the isolated pool rather than the default
    asyncio thread pool.
    """
    return _lane_executor_var.set(executor)


def get_lane_executor() -> Optional[concurrent.futures.Executor]:
    """Return the lane executor for the current task, or ``None``.

    ``None`` means the caller should pass ``None`` to ``loop.run_in_executor``
    so asyncio uses its default thread pool, preserving behavior for non-lane
    callers.
    """
    return _lane_executor_var.get()


def reset_lane_executor(token) -> None:
    """Reset the lane executor using the token returned by set_lane_executor."""
    _lane_executor_var.reset(token)


def resolve_confined_cwd(candidate_cwd: Optional[str]) -> str:
    """Resolve a subprocess cwd through the active worktree confinement boundary.

    When confinement is not required, this intentionally returns ``candidate_cwd``
    unchanged to preserve singleton/live behavior. When confinement is required,
    the active worktree must exist and every subprocess cwd is forced to that
    exact worktree. There is no fallback to TERMINAL_CWD, ``os.getcwd()``, or
    temporary staging directories.
    """
    if not is_worktree_confinement_required():
        return candidate_cwd  # type: ignore[return-value]

    active = get_active_worktree()
    if not active:
        raise WorktreeConfinementError(
            "WORKTREE_CONFINEMENT: runtime cwd denied — confinement required but no active worktree is bound"
        )
    try:
        active_path = Path(active).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise WorktreeConfinementError(
            f"WORKTREE_CONFINEMENT: runtime cwd denied — active worktree {active!r} is invalid"
        ) from exc
    if not active_path.is_dir():
        raise WorktreeConfinementError(
            f"WORKTREE_CONFINEMENT: runtime cwd denied — active worktree {str(active_path)!r} is missing"
        )
    return str(active_path)


def record_runtime_execution_cwd(cwd: str) -> None:
    """Record an actually-resolved runtime/tool cwd for finalize-time audit."""
    resolved = str(Path(cwd).expanduser().resolve())
    current = _run_execution_cwds_var.get()
    if current is None:
        current = []
        _run_execution_cwds_var.set(current)
    current.append(resolved)


def get_runtime_execution_cwds() -> tuple[str, ...]:
    """Return cwd values recorded at subprocess execution boundaries."""
    current = _run_execution_cwds_var.get()
    return tuple(current or ())


def get_runtime_execution_cwd_recorder() -> list[str]:
    """Return the mutable run-scoped cwd recorder shared with copied contexts."""
    current = _run_execution_cwds_var.get()
    if current is None:
        current = []
        _run_execution_cwds_var.set(current)
    return current


def reset_runtime_execution_cwds() -> contextvars.Token[list[str] | None]:
    """Start a fresh shared run-scoped execution-cwd audit list."""
    return _run_execution_cwds_var.set([])


def restore_runtime_execution_cwds(token: contextvars.Token[list[str] | None]) -> None:
    """Restore the prior execution-cwd audit state."""
    _run_execution_cwds_var.reset(token)
