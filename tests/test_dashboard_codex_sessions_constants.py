from __future__ import annotations

from inspect import signature

import agent.peer_review as peer_review
from agent.worktree_broker import WorktreeBroker
from hermes_cli import dashboard_codex_sessions as dashboard


def test_dashboard_codex_sessions_constants_track_real_runtime_defaults() -> None:
    snapshot = dashboard._build_snapshot()
    review_pool = snapshot["review_pool"]

    assert review_pool["size"] == peer_review._DEFAULT_POOL_SIZE
    assert review_pool["daily_cap_per_sid"] == peer_review._DEFAULT_DAILY_CAP
    assert review_pool["iteration_cap"] == peer_review._DEFAULT_ITERATION_CAP

    broker_port_range = signature(WorktreeBroker.__init__).parameters["port_range"].default
    assert dashboard._CODEX_PORT_RANGE == broker_port_range
    assert dashboard._CODEX_PORT_POOL_SIZE == broker_port_range[1] - broker_port_range[0]
    assert snapshot["counts"]["ports_free"] == (
        dashboard._CODEX_PORT_POOL_SIZE - snapshot["counts"]["ports_claimed"]
    )
