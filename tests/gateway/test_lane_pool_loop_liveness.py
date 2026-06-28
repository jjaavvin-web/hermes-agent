"""Loop-liveness canary + starvation-contrast tests for the off-loop lane pool.

MINOR-5 fix: the original ``test_loop_stays_live_under_n_heavy_lanes`` was
tautological — ``loop.run_in_executor`` is non-blocking regardless of which
pool is passed, so the loop trivially stayed live.  This file replaces it with
``test_dedicated_pool_prevents_default_pool_starvation`` which *actually
distinguishes* the on-pool vs off-pool paths via a measurable contrast:

  (A) Route N heavy blocking sleeps to the DEFAULT pool (small, 2 workers)
      → the loop-helper (another run_in_executor on DEFAULT) is starved ≈1 s.
  (B) Route the same heavy sleeps to the DEDICATED lane pool (N workers)
      → the loop-helper on DEFAULT runs almost immediately (<50 ms).

Assertion: starved_A > 3 × time_B.  The test FAILS if heavy lane work is
accidentally routed to the default pool.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time

import pytest

from gateway.lane_executor import LaneExecutorPool, reset_lane_pool


@pytest.fixture(autouse=True)
def _reset_lane_pool_singleton():
    reset_lane_pool()
    yield
    reset_lane_pool()


@pytest.mark.asyncio
async def test_dedicated_pool_prevents_default_pool_starvation():
    """Starvation-contrast: heavy work on DEFAULT pool starves loop helpers;
    on DEDICATED pool it does not.

    Implementation
    --------------
    A constrained default executor (max_workers=2) is installed for the
    duration of the test.  N=10 heavy tasks (sleep 0.25 s each) are submitted
    to EITHER the default pool (Part A) or the lane pool (Part B).
    A lightweight no-op is then submitted to the DEFAULT pool and timed.

    Part A expected: no-op must wait for ~(N/2 - 1) * 0.25 s ≈ 1.0 s while
    the default pool is saturated.
    Part B expected: no-op completes almost immediately (<50 ms) because the
    default pool is free.

    Ratio assertion: starved_A > 3 * time_B ensures the test would FAIL if
    heavy work were routed to the default pool instead of the lane pool.
    """
    N = 10
    SMALL_CAP = 2          # constrained default pool
    SLEEP_DUR = 0.25       # each heavy task blocks this long (seconds)

    def _heavy():
        time.sleep(SLEEP_DUR)

    loop = asyncio.get_running_loop()
    small_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=SMALL_CAP, thread_name_prefix="test-default"
    )
    lane_pool = LaneExecutorPool(max_workers=N)

    # Replace the default executor for this test's event loop only.
    # pytest-asyncio strict mode gives each test a fresh loop, so this
    # modification does not leak to other tests.
    loop.set_default_executor(small_pool)

    try:
        # ---- Part A: saturate the DEFAULT (small) pool ----
        # Submit N heavy tasks; they queue up 2-at-a-time.
        futs_A = [loop.run_in_executor(None, _heavy) for _ in range(N)]

        t0 = time.monotonic()
        # This no-op also uses the default pool → queued behind the N heavy tasks.
        await loop.run_in_executor(None, lambda: None)
        starved_A = time.monotonic() - t0

        # Drain Part A (most tasks have already finished by now).
        await asyncio.gather(*futs_A, return_exceptions=True)

        # ---- Part B: route heavy work to the DEDICATED lane pool ----
        # Default pool is now free; heavy tasks go to lane_pool.
        futs_B = [loop.run_in_executor(lane_pool.executor, _heavy) for _ in range(N)]

        t0 = time.monotonic()
        # Same no-op on the DEFAULT pool, which is uncontested now.
        await loop.run_in_executor(None, lambda: None)
        time_B = time.monotonic() - t0

        await asyncio.gather(*futs_B, return_exceptions=True)

    finally:
        # No need to restore the default executor: pytest-asyncio strict mode
        # creates a fresh event loop for each test, so small_pool is isolated
        # to this test's loop and is safely discarded after shutdown.
        small_pool.shutdown(wait=False)
        lane_pool.shutdown(wait=False)

    # ---- Assertions ----
    # Expected: starved_A ≈ (N/SMALL_CAP - 1) * SLEEP_DUR ≈ 1.0 s
    #           time_B    ≈ 0 ms (default pool free)
    # Guard: use max(time_B, 0.005) to prevent divide-by-zero in the error msg.
    assert starved_A > 3 * max(time_B, 0.005), (
        "Starvation contrast too low: A={:.3f}s B={:.3f}s ratio={:.1f}x. "
        "If this fails, lane work is routing to the default pool and starving "
        "loop helpers.".format(
            starved_A, time_B, starved_A / max(time_B, 0.001)
        )
    )
    # Sanity: Part B baseline must be fast (less than one heavy-task duration).
    assert time_B < SLEEP_DUR, (
        "Baseline time_B={:.3f}s should be <{:.3f}s when default pool is "
        "free.".format(time_B, SLEEP_DUR)
    )


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
