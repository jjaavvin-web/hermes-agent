"""Regression matrix for Codex-native exec/apply_patch approval routing.

These tests intentionally pin two different contracts:

* GREEN: the current adapter fails closed when approval is not explicitly wired.
* XFAIL: the desired KEYSTONE wiring should run the same approval guard floor
  used by the terminal tool before any Codex-native accept path.

Production modules are not modified by this file.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from agent.transports.codex_app_server_session import (
    CodexAppServerSession,
    _ServerRequestRouting,
)
from tools import approval as approval_guards

KEYSTONE_XFAIL_REASON = "pending KEYSTONE wiring (#1b), Claude-hands-on"
HARDLINE_COMMAND = "rm -rf /"


class CallbackRecorder:
    """Small approval callback double that records Codex approval prompts."""

    def __init__(self, choice: str = "once") -> None:
        self.choice = choice
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(
        self,
        command: str,
        description: str,
        *,
        allow_permanent: bool = False,
    ) -> str:
        self.calls.append((command, description, allow_permanent))
        return self.choice


def make_session(
    *,
    approval_callback: Callable[..., str] | None = None,
    auto_approve_exec: bool = False,
    auto_approve_apply_patch: bool = False,
) -> CodexAppServerSession:
    return CodexAppServerSession(
        cwd="/tmp/hermes-codex-approval-matrix",
        approval_callback=approval_callback,
        request_routing=_ServerRequestRouting(
            auto_approve_exec=auto_approve_exec,
            auto_approve_apply_patch=auto_approve_apply_patch,
        ),
        client_factory=lambda **_: None,  # Constructor-only tests never start Codex.
    )


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"command": "echo safe", "cwd": "/tmp"},
        {"command": HARDLINE_COMMAND, "cwd": "/"},
    ],
)
def test_exec_approval_declines_when_no_callback_is_wired(params: dict[str, Any]) -> None:
    session = make_session()

    assert session._decide_exec_approval(params) == "decline"


def test_exec_approval_declines_without_auto_approve_even_for_missing_command() -> None:
    session = make_session(auto_approve_exec=False)

    assert session._decide_exec_approval({}) == "decline"


def test_exec_approval_declines_without_auto_approve_even_for_hardline_command() -> None:
    blocked, description = approval_guards.detect_hardline_command(HARDLINE_COMMAND)
    assert blocked is True
    assert description

    session = make_session(auto_approve_exec=False)

    assert session._decide_exec_approval({"command": HARDLINE_COMMAND, "cwd": "/"}) == "decline"


def test_exec_approval_declines_when_callback_raises() -> None:
    def raising_callback(*_: Any, **__: Any) -> str:
        raise RuntimeError("approval UI unavailable")

    session = make_session(approval_callback=raising_callback)

    assert session._decide_exec_approval({"command": "echo safe"}) == "decline"


def test_apply_patch_approval_declines_when_no_callback_is_wired() -> None:
    session = make_session()

    assert session._decide_apply_patch_approval({"reason": "edit requested"}) == "decline"


def test_apply_patch_approval_declines_without_auto_approve_when_callback_missing() -> None:
    session = make_session(auto_approve_apply_patch=False)

    assert session._decide_apply_patch_approval({"reason": "edit requested"}) == "decline"


def test_codex_exec_auto_accept_must_route_through_check_all_command_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str]] = []

    def fake_check_all_command_guards(command_arg: str, env_type: str, **_: Any) -> dict[str, Any]:
        observed.append((command_arg, env_type))
        return {"approved": False, "hardline": True, "message": "blocked by fake guard"}

    monkeypatch.setattr(approval_guards, "check_all_command_guards", fake_check_all_command_guards)
    session = make_session(auto_approve_exec=True)

    assert session._decide_exec_approval({"command": HARDLINE_COMMAND, "cwd": "/"}) == "decline"
    assert observed == [(HARDLINE_COMMAND, "local")]


def test_codex_exec_callback_accept_must_route_through_detect_hardline_before_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = CallbackRecorder(choice="once")
    observed: list[str] = []

    def fake_detect_hardline_command(command_arg: str) -> tuple[bool, str]:
        observed.append(command_arg)
        return (True, "blocked by fake hardline")

    monkeypatch.setattr(approval_guards, "detect_hardline_command", fake_detect_hardline_command)
    session = make_session(approval_callback=recorder)

    assert session._decide_exec_approval({"command": HARDLINE_COMMAND, "cwd": "/"}) == "decline"
    assert observed == [HARDLINE_COMMAND]
    assert recorder.calls == []


def test_codex_apply_patch_auto_accept_must_route_through_check_all_command_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_check_all_command_guards(command_arg: str, env_type: str, **_: Any) -> dict[str, Any]:
        observed.append(command_arg)
        return {"approved": False, "hardline": True, "message": "blocked by fake guard"}

    monkeypatch.setattr(approval_guards, "check_all_command_guards", fake_check_all_command_guards)
    session = make_session(auto_approve_apply_patch=True)

    assert session._decide_apply_patch_approval({"reason": "apply test patch"}) == "decline"
    assert observed == ["apply_patch"]


def test_codex_apply_patch_callback_accept_must_route_through_check_all_command_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = CallbackRecorder(choice="once")
    observed: list[str] = []

    def fake_check_all_command_guards(command_arg: str, env_type: str, **_: Any) -> dict[str, Any]:
        observed.append(command_arg)
        return {"approved": False, "hardline": True, "message": "blocked by fake guard"}

    monkeypatch.setattr(approval_guards, "check_all_command_guards", fake_check_all_command_guards)
    session = make_session(approval_callback=recorder)

    assert session._decide_apply_patch_approval({"reason": "apply test patch"}) == "decline"
    assert observed == ["apply_patch: apply test patch"]
    assert recorder.calls == []
