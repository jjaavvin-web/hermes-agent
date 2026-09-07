"""Wiring regression tests for the browser extension router.

These guard the *registry wiring* — that every ``browser_*`` handler routes
through :func:`tools.browser_extension_router.routed_browser_handler` with
the tool's action name, its raw args, and its identity kwargs, instead of
calling the legacy backend directly. The routing contract itself is tested
by ``test_browser_extension_router.py``; here we only pin the plumbing.
"""

import pytest

from tools.registry import registry


@pytest.fixture(autouse=True)
def _route_spy(monkeypatch):
    """Replace the wrapper with a spy that records the route and then runs
    the legacy fallback, so each test proves the handler is wired without
    exercising real routing or a real browser backend."""
    calls = []

    # Missing browser dependencies are a fixture condition; never invoke the
    # real Node/browser installer while exercising the legacy fallback.
    monkeypatch.setattr(
        "hermes_cli.dep_ensure.ensure_dependency", lambda *args, **kwargs: False
    )

    def spy(action, args, *, fallback, task_id=None, session_id=None, tool_call_id=None):
        calls.append(
            {
                "action": action,
                "args": dict(args),
                "task_id": task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
            }
        )
        return fallback()

    import tools.browser_tool as browser_tool
    import tools.browser_cdp_tool as browser_cdp_tool

    # Every legacy ``browser_*`` fallback bottoms out in ``_run_browser_command``
    # (directly, or via ``_current_page_private_url`` / ``_browser_eval`` /
    # ``_run_chrome_fallback_command``, which all call it first). On a host
    # with Chrome on PATH that helper spawns ``npx agent-browser`` and blocks
    # in ``proc.wait(timeout=30)`` — the same 30 s as the pytest ceiling.
    # Fail fast instead: no fallback may ever launch a subprocess here.
    monkeypatch.setattr(
        browser_tool,
        "_run_browser_command",
        lambda *args, **kwargs: {
            "success": False,
            "error": "browser backend disabled by test fixture",
        },
    )

    monkeypatch.setattr(browser_tool, "routed_browser_handler", spy)
    monkeypatch.setattr(browser_cdp_tool, "routed_browser_handler", spy)
    monkeypatch.setattr(
        browser_tool,
        "browser_navigate",
        lambda url="", task_id=None, local_browser=False: "legacy-nav",
    )
    monkeypatch.setattr(browser_cdp_tool, "browser_cdp", lambda *a, **k: "legacy-cdp")
    return calls


BROWSER_ACTIONS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_back",
    "browser_press",
    "browser_get_images",
    "browser_vision",
    "browser_console",
]


def test_every_browser_registry_handler_routes_through_wrapper(_route_spy):
    for name in BROWSER_ACTIONS:
        _route_spy.clear()
        handler = registry.get_entry(name).handler
        args = {"url": "https://example.test", "ref": "@e1", "text": "hi"}
        result = handler(dict(args), task_id="task-fixture", session_id="session-fixture")
        assert result is not None
        assert len(_route_spy) == 1, f"{name} did not route through the wrapper"
        route = _route_spy[0]
        assert route["action"] == name
        assert route["task_id"] == "task-fixture"
        assert route["session_id"] == "session-fixture"


def test_browser_navigate_forwards_raw_args_and_identity(_route_spy):
    handler = registry.get_entry("browser_navigate").handler
    args = {"url": "https://example.test"}
    result = handler(dict(args), task_id="task-fixture", session_id="session-fixture")
    assert result == "legacy-nav"
    route = _route_spy[0]
    assert route["args"] == args
    # The router must not mutate the args dict.
    assert args == {"url": "https://example.test"}


def test_browser_cdp_handler_routes_through_wrapper(_route_spy):
    handler = registry.get_entry("browser_cdp").handler
    args = {"method": "Target.getTargets", "params": {"filter": []}}
    result = handler(dict(args), task_id="task-fixture", session_id="session-fixture")
    assert result == "legacy-cdp"
    route = _route_spy[0]
    assert route["action"] == "browser_cdp"
    assert route["args"] == args
    assert route["task_id"] == "task-fixture"
    assert route["session_id"] == "session-fixture"
