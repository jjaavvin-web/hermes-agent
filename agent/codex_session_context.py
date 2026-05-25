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

import contextvars
from typing import Optional

_active_worktree_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "hermes_codex_active_worktree",
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
