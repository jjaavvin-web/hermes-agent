"""Loop-liveness canary tests for the off-loop codex-lane executor pool.

These tests verify that routing heavy blocking work through a DEDICATED
``ThreadPoolExecutor`` does NOT starve asyncio's event loop.

Design contract under test
--------------------------
- ``LaneExecutorPool.try_admit()`` is synchronous (never awaits).
- With N=10 heavy lanes (sleep 0.3-0.5s) active simultaneously, the event
  loop still makes measurable progress: liveness-gate tick fires, and the
  measured loop-lag stays below 0.5 s.
- With the pool saturated, ``try_admit()`` returns False immediately
  (no yield point, no scheduling cost to the caller).

The tests use a real ``_LoopLivenessGate`` at a short tick interval so
they finish well within the 30 s per-test timeout.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import random
import time

import pytest

from gateway.lane_executor import LaneExecutorPool, reset_lane_pool
from hermes_cli.gateway import _LoopLivenessGate


@pytest.fixture(autouse=True)
def _reset_lane_pool_singleton():
    reset_lane_pool()
    yield
    reset_lane_pool()


@pytest.mark.asyncio
async def test_loop_stays_live_under_n_heavy_lanes():
    """Start 10 heavy lane tasks; the event loop keeps making progress.

    Contract:
    - _LoopLivenessGate.loop_is_live() stays True throughout the run
    - Maximum observed loop lag < 0.5 s
    - All 10 tasks complete before the test exits
    """
    N = 10
    pool = LaneExecutorPool(max_workers=N)
    gate = _LoopLivenessGate(wedge_budget_sec=2.0, tick_interval_sec=0.05)
    gate.start()

    max_lag = 0.0
    lag_samples: list = []
    stop_sampling = asyncio.Event()

    async def _lag_probe():
        """Sample event-loop lag every 50 ms."""
        nonlocal max_lag
        while not stop_sampling.is_set():
            t0 = time.monotonic()
            await asyncio.sleep(0.05)
            gap = time.monotonic() - t0 - 0.05
            lag_samples.append(max(0.0, gap))
            if lag_samples:
                max_lag = max(lag_samples)

    probe_task = asyncio.create_task(_lag_probe())

    def _heavy_work():
        """Simulate a blocking lane run (0.3-0.5 s)."""
        time.sleep(random.uniform(0.3, 0.5))

    loop = asyncio.get_running_loop()
    futures = []
    for _ in range(N):
        admitted = pool.try_admit()
        assert admitted, "Pool should admit all N initial requests"
        fut = loop.run_in_executor(pool.executor, _heavy_work)
        futures.append(fut)

    # Wait for all heavy work to finish
    await asyncio.gather(*futures)

    # Release slots (would normally happen in _run_with_backpressure finally)
    for _ in range(N):
        pool.release()

    stop_sampling.set()
    await probe_task

    await gate.stop()

    # Assertions
    assert gate.loop_is_live(), (
        "Loop liveness gate reports stale tick; last age: {:.3f}s".format(gate.age())
    )
    assert max_lag < 0.5, (
        "Max event-loop lag {:.3f}s exceeds 0.5 s threshold — "
        "lane pool may be blocking the event loop".format(max_lag)
    )
    # All slots released
    stats = pool.stats()
    assert stats["inflight"] == 0
    assert stats["rejected"] == 0

    pool.shutdown(wait=False)


def test_admission_is_offloop():
    """try_admit() / release() must be synchronous - no event-loop involvement.

    This test runs OUTSIDE an async context deliberately to prove that
    admission control does not require the event loop.
    """
    cap = 4
    pool = LaneExecutorPool(max_workers=cap)

    # Fill pool to capacity
    for i in range(cap):
        assert pool.try_admit(), "Should admit slot {}".format(i)

    stats = pool.stats()
    assert stats["inflight"] == cap
    assert stats["capacity"] == cap

    # Over-capacity requests are rejected synchronously
    for _ in range(3):
        assert not pool.try_admit(), "Should reject when at capacity"

    assert pool.stats()["rejected"] == 3

    # Release one slot; next admit succeeds
    pool.release()
    assert pool.stats()["inflight"] == cap - 1
    assert pool.try_admit(), "Should admit after a release"
    assert pool.stats()["inflight"] == cap

    # Cleanup
    for _ in range(cap):
        pool.release()
    assert pool.stats()["inflight"] == 0

    pool.shutdown(wait=False)
