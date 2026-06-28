"""Dedicated bounded thread-pool for off-loop codex-lane execution.

Heavy lane runs (webhook-dispatched agent sessions) currently go to the
default ``asyncio`` ``ThreadPoolExecutor``.  Under many concurrent lanes
that shared pool can starve loop-side helpers — heartbeat ticks, semaphore
wait callbacks, etc. — which causes the watchdog to fire SIGABRT against an
otherwise healthy gateway.

This module provides ``LaneExecutorPool``: an isolated
``concurrent.futures.ThreadPoolExecutor`` backed by a ``threading.BoundedSemaphore``
for synchronous, non-blocking admission control.  When the pool is full,
``try_admit()`` returns ``False`` immediately (no ``await``, no scheduling
— admission is completely off the event loop).

Gated OFF by default (``offloop_lane_pool: false`` in config.yaml).
Set ``HERMES_OFFLOOP_LANE_POOL=1`` or ``offloop_lane_pool: true`` to opt in.

Usage
-----
The module exposes module-level singleton helpers that mirror the
``_get_agent_run_semaphore`` / ``_AGENT_RUN_SEMAPHORE`` pattern in
``gateway.platforms.webhook``.  Callers obtain the pool via
``get_lane_pool(max_workers)`` and check admission via ``pool.try_admit()``.
``reset_lane_pool()`` is a test-only hook that tears down the singleton.
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
from typing import Optional

_POOL: Optional["LaneExecutorPool"] = None
_POOL_CAP: Optional[int] = None
_POOL_LOCK = threading.Lock()


class LaneExecutorPool:
    """Isolated thread pool + admission semaphore for codex lane execution.

    Parameters
    ----------
    max_workers:
        Maximum number of concurrently executing lane threads.  Also the
        depth of the ``BoundedSemaphore``.
    """

    def __init__(self, max_workers: int) -> None:
        self._max_workers = max_workers
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="codex-lane",
        )
        self._gate = threading.BoundedSemaphore(max_workers)
        self._lock = threading.Lock()
        self._inflight: int = 0
        self._rejected: int = 0

    # ------------------------------------------------------------------
    # Admission

    def try_admit(self) -> bool:
        """Non-blocking admission check.

        Attempts to acquire the bounded semaphore without blocking.  Returns
        ``True`` (and increments *inflight*) on success; ``False`` (and
        increments *rejected*) when the pool is at capacity.  Never blocks
        the calling thread or coroutine.
        """
        acquired = self._gate.acquire(blocking=False)
        with self._lock:
            if acquired:
                self._inflight += 1
            else:
                self._rejected += 1
        return acquired

    def release(self) -> None:
        """Release one admission slot after a lane run finishes."""
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1
        self._gate.release()

    # ------------------------------------------------------------------
    # Executor access

    @property
    def executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """The underlying ``ThreadPoolExecutor`` for this pool."""
        return self._executor

    # ------------------------------------------------------------------
    # Observability

    def stats(self) -> dict:
        """Return a snapshot of pool counters suitable for health responses."""
        with self._lock:
            return {
                "inflight": self._inflight,
                "capacity": self._max_workers,
                "rejected": self._rejected,
            }

    def shutdown(self, wait: bool = False) -> None:
        """Shut down the underlying executor (graceful teardown)."""
        self._executor.shutdown(wait=wait)


# ------------------------------------------------------------------
# Module-level singleton helpers (mirror webhook._get_agent_run_semaphore)


def get_lane_pool(max_workers: int) -> LaneExecutorPool:
    """Return the process-global ``LaneExecutorPool``, recreating if cap changed."""
    global _POOL, _POOL_CAP
    with _POOL_LOCK:
        if _POOL is None or _POOL_CAP != max_workers:
            if _POOL is not None:
                _POOL.shutdown(wait=False)
            _POOL = LaneExecutorPool(max_workers)
            _POOL_CAP = max_workers
    return _POOL


def reset_lane_pool() -> None:
    """Destroy the singleton.  TEST HOOK ONLY — do not call in production."""
    global _POOL, _POOL_CAP
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.shutdown(wait=False)
        _POOL = None
        _POOL_CAP = None


def lane_pool_enabled(extra: dict) -> bool:
    """Return True when the off-loop lane pool is enabled.

    Resolution order (first truthy wins):

    1. ``HERMES_OFFLOOP_LANE_POOL`` environment variable (matches the
       ``HERMES_WEBHOOK_WORKTREE`` env-gate convention in webhook.py).
    2. ``offloop_lane_pool`` key in the platform ``extra`` dict (from
       config.yaml ``platforms.webhook.extra``).
    """
    env_val = os.environ.get("HERMES_OFFLOOP_LANE_POOL", "").strip().lower()
    if env_val in ("1", "true", "yes"):
        return True
    if env_val in ("0", "false", "no"):
        return False
    return bool(extra.get("offloop_lane_pool", False))
