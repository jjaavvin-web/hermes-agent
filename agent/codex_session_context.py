"""Per-Discord-thread session context for the Codex parallel workflow.

Set by the Discord adapter's ``on_message`` hook when a tracked codex
thread is the message source; consumed by ``tools/environments/local.py``
to override the default cwd for subprocess calls during that turn.

`ContextVar` is async-safe — each `asyncio.Task` gets its own copy
inherited at creation time. That means concurrent Discord threads can
each set their own active worktree without leaking across the event
loop's scheduling.

Design background: P1's dispatcher allocates a per-thread git worktree
(see ``agent.worktree_broker``) but P1 itself stops short of using it
as the cwd for tool calls — Hermes still cwds in the live tree.  P1.5
closes that gap by threading the worktree path through this contextvar
to ``LocalEnvironment._run_bash``.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
from typing import Optional

_active_worktree_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hermes_codex_active_worktree",
    default=None,
)

_lane_executor_var: contextvars.ContextVar[
    Optional[concurrent.futures.Executor]
] = contextvars.ContextVar(
    "hermes_codex_lane_executor",
    default=None,
)


def set_active_worktree(path: Optional[str]):
    """Set the active codex worktree path for the current async task.

    Returns a token that can be passed to ``reset_active_worktree`` to
    undo the change.  Typically used at the boundary where a Discord
    message handler kicks off the agent turn.
    """
    return _active_worktree_var.set(path)


def get_active_worktree() -> Optional[str]:
    """Return the active codex worktree path, or ``None`` if not inside
    a codex-tracked thread context."""
    return _active_worktree_var.get()


def reset_active_worktree(token) -> None:
    """Reset the active worktree to its prior value using the token
    returned by ``set_active_worktree``."""
    _active_worktree_var.reset(token)


def set_lane_executor(executor: Optional[concurrent.futures.Executor]):
    """Set the lane-dedicated executor for the current async task.

    Returns a token that can be passed to ``reset_lane_executor`` to undo
    the change.  Set by the webhook adapter when the off-loop lane pool is
    enabled; consumed by ``GatewayRunner._run_in_executor_with_context`` to
    route heavy blocking work onto the isolated pool rather than the default
    asyncio thread pool.
    """
    return _lane_executor_var.set(executor)


def get_lane_executor() -> Optional[concurrent.futures.Executor]:
    """Return the lane executor for the current task, or ``None``.

    ``None`` means the caller should pass ``None`` to ``loop.run_in_executor``
    so asyncio uses its default thread pool — preserving byte-for-byte
    identical behavior for all non-lane callers.
    """
    return _lane_executor_var.get()


def reset_lane_executor(token) -> None:
    """Reset the lane executor to its prior value using the token returned
    by ``set_lane_executor``."""
    _lane_executor_var.reset(token)
