"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import subprocess

import pytest

_ORIGINAL_ACTIONS_SUBPROCESS_RUN = subprocess.run
_ORIGINAL_ACTIONS_SUBPROCESS_POPEN = subprocess.Popen


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "real_dispatch_integration: allow dashboard Nexus action tests to reach the real loki_send.py subprocess boundary")


@pytest.fixture(autouse=True)
def _guard_dashboard_nexus_actions_loki_send(request, monkeypatch):
    """Fail loudly if nexus-action tests hit loki_send.py without an explicit mock/marker."""
    if request.node.get_closest_marker("real_dispatch_integration"):
        return
    test_path = Path(str(request.node.fspath))
    if test_path.name != "test_dashboard_nexus_actions.py":
        return
    try:
        from hermes_cli import dashboard_nexus_actions as actions
    except Exception:
        return

    def blocked_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        command = " ".join(str(part) for part in argv) if isinstance(argv, (list, tuple)) else str(argv)
        if "loki_send.py" in command:
            raise AssertionError(
                "unmocked loki_send.py subprocess blocked by tests/hermes_cli/conftest.py; "
                "mock dashboard_nexus_actions.subprocess.run or mark real_dispatch_integration"
            )
        return _ORIGINAL_ACTIONS_SUBPROCESS_RUN(argv, *args, **kwargs)

    def blocked_popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        command = " ".join(str(part) for part in argv) if isinstance(argv, (list, tuple)) else str(argv)
        if "loki_send.py" in command:
            raise AssertionError(
                "unmocked loki_send.py Popen blocked by tests/hermes_cli/conftest.py; "
                "mock dashboard_nexus_actions.subprocess.Popen or mark real_dispatch_integration"
            )
        return _ORIGINAL_ACTIONS_SUBPROCESS_POPEN(argv, *args, **kwargs)

    monkeypatch.setattr(actions.subprocess, "run", blocked_run)
    monkeypatch.setattr(actions.subprocess, "Popen", blocked_popen)
