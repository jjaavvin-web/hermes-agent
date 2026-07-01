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


class _WorktreeContextToken(NamedTuple):
    """Reset tokens for the active worktree and confinement marker."""

    active_worktree: contextvars.Token[Optional[str]]
    confinement_required: contextvars.Token[bool]


def set_active_worktree(path: Optional[str]):
    """Set the active codex worktree path for the current async task.

    Returns a token that can be passed to ``reset_active_worktree`` to
    undo the change. Typically used at the boundary where a gateway message
    handler kicks off the agent turn.

    A truthy existing ``path`` also marks this turn as requiring worktree
    confinement: file/terminal/codex writes must stay in that worktree; if the
    worktree later goes missing/invalid, downstream write surfaces fail closed.
    """
    active_token = _active_worktree_var.set(path)
    confinement_token = _confinement_required_var.set(bool(path and os.path.isdir(path)))
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
