"""Regression tests for runtime cwd recorder propagation across real thread hops."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agent.codex_session_context import (
    get_runtime_execution_cwd_recorder,
    get_runtime_execution_cwds,
    record_runtime_execution_cwd,
    reset_runtime_execution_cwds,
    restore_runtime_execution_cwds,
)
from tools.thread_context import propagate_context_to_thread


def test_runtime_cwd_recorder_survives_real_threadpool_context_propagation(tmp_path):
    """A parent-created recorder list must be mutated by the real worker path.

    ``contextvars.copy_context()`` copies bindings, not values. This pins the
    intended design: the recorder is a shared mutable list, so subprocess cwd
    entries made through ``propagate_context_to_thread`` are visible to the
    finalize-time parent audit.
    """
    worker_cwd = tmp_path / "worker-cwd"
    worker_cwd.mkdir()
    audit_token = reset_runtime_execution_cwds()
    try:
        parent_recorder = get_runtime_execution_cwd_recorder()
        assert parent_recorder == []

        def worker() -> list[str]:
            assert get_runtime_execution_cwd_recorder() is parent_recorder
            record_runtime_execution_cwd(str(worker_cwd))
            return get_runtime_execution_cwd_recorder()

        with ThreadPoolExecutor(max_workers=1) as pool:
            worker_recorder = pool.submit(propagate_context_to_thread(worker)).result(timeout=5)

        assert worker_recorder is parent_recorder
        assert parent_recorder == [str(worker_cwd.resolve())]
        assert get_runtime_execution_cwds() == (str(worker_cwd.resolve()),)
    finally:
        restore_runtime_execution_cwds(audit_token)
