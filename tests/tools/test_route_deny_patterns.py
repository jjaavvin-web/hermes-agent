"""Tests for server-side route-level terminal-command denial (E1).

Webhook/relay-dispatched agent sessions (loki lanes, relay workers) carry a
server-side deny list registered at dispatch time. Unlike the regular
DANGEROUS_PATTERNS path, a route deny is UNCONDITIONAL — it fires before the
yolo / mode=off / cron bypass, so a dispatched agent physically cannot run
``git push`` / ``gh pr create`` / ``gh pr merge`` even if its prompt guards are
stripped. This closes the deep-infra-audit P0 (prompt-only push/PR guards are
insufficient).
"""

import pytest

from gateway.config import Platform
from gateway.session import SessionSource, build_session_key
from gateway.platforms.webhook import DEFAULT_WEBHOOK_DENY_PATTERNS
from tools.approval import (
    check_all_command_guards,
    check_session_deny_patterns,
    clear_session,
    disable_session_yolo,
    enable_session_yolo,
    register_session_deny_patterns,
    reset_current_session_key,
    set_current_session_key,
)

_KEY = "agent:main:webhook:test-deny"

# Commands a dispatched session MUST be denied.
_DENY = [
    "git push",
    "git push origin main",
    "git push --force",
    "git push -f origin HEAD",
    "git -C /home/josep/repo push",
    "gh pr create --fill",
    "gh pr merge 123 --squash",
    "gh pr ready 45",
    "gh repo delete foo/bar --yes",
    "hub pull-request -m 'x'",
]

# Commands that must NOT be route-denied (benign git/gh ops a worker runs).
_ALLOW = [
    "git status",
    "git log --oneline -5",
    "git commit -m 'wip fix'",
    "git diff --stat",
    "git add -A",
    "git fetch origin",
    "gh pr view 12",
    "gh pr list",
    "cat git-push-notes.txt",
]


@pytest.fixture
def deny_session(monkeypatch):
    """A session with the default webhook deny patterns registered."""
    for var in (
        "HERMES_YOLO_MODE", "HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION",
        "HERMES_CRON_SESSION", "HERMES_EXEC_ASK",
    ):
        monkeypatch.delenv(var, raising=False)
    token = set_current_session_key(_KEY)
    register_session_deny_patterns(_KEY, DEFAULT_WEBHOOK_DENY_PATTERNS)
    try:
        disable_session_yolo(_KEY)
        yield
    finally:
        clear_session(_KEY)
        disable_session_yolo(_KEY)
        reset_current_session_key(token)


@pytest.mark.parametrize("command", _DENY)
def test_deny_patterns_match(deny_session, command):
    denied, pattern = check_session_deny_patterns(command)
    assert denied, f"expected route deny to match {command!r}"
    assert pattern


@pytest.mark.parametrize("command", _ALLOW)
def test_deny_patterns_allow_benign(deny_session, command):
    denied, _ = check_session_deny_patterns(command)
    assert not denied, f"route deny should NOT match benign {command!r}"


@pytest.mark.parametrize("command", _DENY)
def test_check_all_command_guards_blocks_denied(deny_session, command):
    result = check_all_command_guards(command, "local")
    assert result["approved"] is False, f"expected block on {command!r}"
    assert result.get("route_denied") is True
    assert "BLOCKED (route policy)" in result["message"]


def test_session_yolo_cannot_bypass_route_deny(deny_session):
    """The whole point: gateway /yolo bypasses DANGEROUS_PATTERNS but NOT a
    route deny — git push stays blocked for a dispatched session."""
    enable_session_yolo(_KEY)
    result = check_all_command_guards("git push origin main", "local")
    assert result["approved"] is False
    assert result.get("route_denied") is True


def test_yolo_env_cannot_bypass_route_deny(deny_session, monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    result = check_all_command_guards("gh pr merge 9", "local")
    assert result["approved"] is False
    assert result.get("route_denied") is True


def test_mode_off_cannot_bypass_route_deny(deny_session, monkeypatch):
    import tools.approval as approval_mod
    monkeypatch.setattr(approval_mod, "_get_approval_mode", lambda: "off")
    result = check_all_command_guards("git push", "local")
    assert result["approved"] is False
    assert result.get("route_denied") is True


def test_container_backends_still_bypass(deny_session):
    """Containerized backends can't touch the host repo — still bypass."""
    for env in ("docker", "singularity", "modal", "daytona"):
        result = check_all_command_guards("git push origin main", env)
        assert result["approved"] is True, f"container {env} should bypass"


def test_session_without_deny_is_unaffected(monkeypatch):
    """A normal (non-dispatched) session has no deny list — route deny must not
    fire. git push there still goes through the yolo-bypassable dangerous path."""
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    token = set_current_session_key("agent:main:local:plain")
    try:
        denied, _ = check_session_deny_patterns("git push origin main")
        assert not denied, "a session with no deny list must not be route-denied"
    finally:
        clear_session("agent:main:local:plain")
        reset_current_session_key(token)


def test_clear_session_removes_deny(deny_session):
    assert check_session_deny_patterns("git push")[0] is True
    clear_session(_KEY)
    assert check_session_deny_patterns("git push")[0] is False


def test_register_none_clears(deny_session):
    register_session_deny_patterns(_KEY, None)
    assert check_session_deny_patterns("git push")[0] is False


def test_webhook_key_matches_dispatcher_key():
    """The key webhook.py registers under must equal what build_session_key
    produces for the same source with the dispatcher's kwargs — otherwise the
    contextvar lookup at tool-exec time misses and the deny never fires.

    This mirrors gateway/platforms/webhook.py (registration) and
    gateway/platforms/base.py (dispatch) end-to-end.
    """
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:loki1:deliv-abc",
        chat_name="webhook/loki1",
        chat_type="webhook",
        user_id="webhook:loki1",
        user_name="loki1",
    )
    key = build_session_key(
        source, group_sessions_per_user=True, thread_sessions_per_user=False,
    )
    register_session_deny_patterns(key, DEFAULT_WEBHOOK_DENY_PATTERNS)
    token = set_current_session_key(key)
    try:
        result = check_all_command_guards("git push origin main", "local")
        assert result["approved"] is False
        assert result.get("route_denied") is True
    finally:
        clear_session(key)
        reset_current_session_key(token)
