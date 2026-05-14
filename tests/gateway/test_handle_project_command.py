"""Tests for _create_kanban_card_from_intake — the MUST #1 implementation.

These tests exercise the _create_kanban_card_from_intake closure that lives
inside _handle_project_command.  The strategy is:

1. Build a minimal GatewayRunner shell (object.__new__) with just enough mocks.
2. Give it a fake adapter whose send_project_intake_prompt captures the
   on_intake_selected kwarg.
3. Call runner._handle_project_command(event) to plant the closure.
4. Invoke the captured callback directly with a crafted payload.

All subprocess / kanban-CLI / DB boundaries are mocked — no live calls.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Minimal runner factory
# ---------------------------------------------------------------------------

def _make_source(
    platform: Platform = Platform.TELEGRAM,
    *,
    chat_id: str = "12345",
    thread_id: str | None = None,
) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id="u1",
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str = "/project My task", *, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(platform),
        message_id="m1",
    )


class _CapturingAdapter:
    """Adapter that captures the on_intake_selected callback for direct testing."""

    def __init__(self):
        self.captured_callback = None

    async def send_project_intake_prompt(self, *, chat_id, title, state, session_key,
                                         on_intake_selected, metadata=None):
        self.captured_callback = on_intake_selected
        return SimpleNamespace(success=True, message_id="77")


def _make_runner(adapter=None) -> object:
    """Build a minimal GatewayRunner shell without calling __init__."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = adapter or _CapturingAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_notifier_profile = None
    runner._session_key_for_source = lambda source: "sk:telegram:12345"
    runner._thread_metadata_for_source = lambda source, anchor=None: {}
    runner._reply_anchor_for_event = lambda event: None
    runner._active_profile_name = lambda: "default"
    return runner, adapter


# ---------------------------------------------------------------------------
# Helper to plant the closure via _handle_project_command
# ---------------------------------------------------------------------------

async def _get_intake_callback(runner, event=None):
    """Call _handle_project_command and return the captured on_intake_selected closure."""
    event = event or _make_event()
    adapter = runner.adapters[Platform.TELEGRAM]
    await runner._handle_project_command(event)
    return adapter.captured_callback


def _full_payload(**overrides):
    """Build a realistic intake payload with all fields populated."""
    base = {
        "title": "My shiny task",
        "description": "A full description.",
        "answers": {
            "kind": "feature",
            "scope": "impl_after_spec",
            "risk": "normal",
            "board": "control",
        },
        "source": {
            "platform": "telegram",
            "chat_id": "12345",
            "thread_id": None,
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Test 1 — success path: run_slash returns "Created t_abcd1234 ..."
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_kanban_card_from_intake_success():
    """run_slash returns a Created line → auto-subscribe called, result contains task id."""
    runner, adapter = _make_runner()

    mock_run_slash_output = "Created t_abcd1234 (todo, assignee=-)"
    mock_conn = MagicMock()

    with (
        patch("hermes_cli.kanban.run_slash", return_value=mock_run_slash_output) as mock_rs,
        patch("hermes_cli.kanban_db.connect", return_value=mock_conn) as mock_connect,
        patch("hermes_cli.kanban_db.add_notify_sub") as mock_add_sub,
    ):
        callback = await _get_intake_callback(runner)
        result = await callback(_full_payload())

    # run_slash was called with a command containing create + board + triage
    assert mock_rs.called
    cmd_arg = mock_rs.call_args[0][0]
    assert "create" in cmd_arg
    assert "hermes-kanban-control" in cmd_arg
    assert "--triage" in cmd_arg

    # auto-subscribe was called
    mock_add_sub.assert_called_once()
    sub_kwargs = mock_add_sub.call_args.kwargs
    assert sub_kwargs["task_id"] == "t_abcd1234"
    assert sub_kwargs["platform"] == "telegram"
    assert sub_kwargs["chat_id"] == "12345"

    # returned string contains task id and subscription line
    assert "t_abcd1234" in result
    assert "Telegram" in result or "telegram" in result.lower()


# ---------------------------------------------------------------------------
# Test 2 — run_slash returns non-matching output (error surface)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_kanban_card_from_intake_run_slash_error_returns_output_verbatim():
    """run_slash returns an error string → surfaced verbatim, no auto-subscribe."""
    runner, adapter = _make_runner()

    error_output = "ERROR: missing --title"

    with (
        patch("hermes_cli.kanban.run_slash", return_value=error_output),
        patch("hermes_cli.kanban_db.add_notify_sub") as mock_add_sub,
    ):
        callback = await _get_intake_callback(runner)
        result = await callback(_full_payload())

    assert result.strip() == error_output
    mock_add_sub.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — run_slash raises an exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_kanban_card_from_intake_run_slash_raises():
    """run_slash raises RuntimeError → returned string starts with failure prefix."""
    runner, adapter = _make_runner()

    with patch("hermes_cli.kanban.run_slash", side_effect=RuntimeError("boom")):
        callback = await _get_intake_callback(runner)
        result = await callback(_full_payload())

    assert result.startswith("❌ Project intake failed: boom")


# ---------------------------------------------------------------------------
# Test 4 — board token mapping (parametrized over 3 tokens)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("board_token,expected_board_slug,expected_prefix", [
    ("control", "hermes-kanban-control", None),
    ("triage", "default", "TRIAGE: "),
    ("new", "default", "BOARD-NEEDED: "),
], ids=[
    "control-hermes-kanban-control-None",
    "triage-default-TRIAGE",
    "new-default-BOARD-NEEDED",
])
async def test_create_kanban_card_from_intake_board_token_mapping(
    board_token, expected_board_slug, expected_prefix
):
    """Each board token maps to the correct slug and title prefix in run_slash command."""
    runner, adapter = _make_runner()

    captured_commands = []

    def fake_run_slash(cmd):
        captured_commands.append(cmd)
        return "Created t_aabbccdd (todo, assignee=-)"

    payload = _full_payload(answers={
        "kind": "feature",
        "scope": "spec",
        "risk": "safe",
        "board": board_token,
    })

    with (
        patch("hermes_cli.kanban.run_slash", side_effect=fake_run_slash),
        patch("hermes_cli.kanban_db.connect", return_value=MagicMock()),
        patch("hermes_cli.kanban_db.add_notify_sub"),
    ):
        callback = await _get_intake_callback(runner)
        result = await callback(payload)

    assert captured_commands, "run_slash was never called"
    cmd = captured_commands[0]

    # Board slug must appear in the command
    assert expected_board_slug in cmd

    # Title prefix must be present (or absent for None)
    if expected_prefix is not None:
        assert expected_prefix in cmd
    else:
        assert "TRIAGE: " not in cmd
        assert "BOARD-NEEDED: " not in cmd

    assert "t_aabbccdd" in result


# ---------------------------------------------------------------------------
# Test 5 — risk token → priority mapping (parametrized)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("risk_token,expected_priority", [
    ("safe", 50),
    ("normal", 60),
    ("manual", 70),
])
async def test_create_kanban_card_from_intake_risk_priority_mapping(risk_token, expected_priority):
    """Each risk token maps to the correct --priority value in the run_slash command."""
    runner, adapter = _make_runner()

    captured_commands = []

    def fake_run_slash(cmd):
        captured_commands.append(cmd)
        return "Created t_risk1234 (todo, assignee=-)"

    payload = _full_payload(answers={
        "kind": "ops",
        "scope": "triage_only",
        "risk": risk_token,
        "board": "control",
    })

    with (
        patch("hermes_cli.kanban.run_slash", side_effect=fake_run_slash),
        patch("hermes_cli.kanban_db.connect", return_value=MagicMock()),
        patch("hermes_cli.kanban_db.add_notify_sub"),
    ):
        callback = await _get_intake_callback(runner)
        await callback(payload)

    assert captured_commands, "run_slash was never called"
    cmd = captured_commands[0]
    assert f"--priority {expected_priority}" in cmd or f"'{expected_priority}'" in cmd or str(expected_priority) in cmd


# ---------------------------------------------------------------------------
# Test 6 — body rendering includes all expected fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_kanban_card_from_intake_body_rendering_includes_all_answers():
    """Full payload → body passed to run_slash includes kind, scope, risk, board, source."""
    runner, adapter = _make_runner()

    captured_commands = []

    def fake_run_slash(cmd):
        captured_commands.append(cmd)
        return "Created t_body5678 (todo, assignee=-)"

    payload = {
        "title": "Body rendering test",
        "description": "My description text.",
        "answers": {
            "kind": "bug",
            "scope": "impl_after_spec",
            "risk": "manual",
            "board": "triage",
        },
        "source": {
            "platform": "telegram",
            "chat_id": "99887",
            "thread_id": "42",
        },
    }

    with (
        patch("hermes_cli.kanban.run_slash", side_effect=fake_run_slash),
        patch("hermes_cli.kanban_db.connect", return_value=MagicMock()),
        patch("hermes_cli.kanban_db.add_notify_sub"),
    ):
        callback = await _get_intake_callback(runner)
        await callback(payload)

    assert captured_commands, "run_slash was never called"
    cmd = captured_commands[0]

    # The --body argument is shlex-quoted; unescape for assertion
    import shlex
    tokens = shlex.split(cmd)
    body_idx = tokens.index("--body")
    body = tokens[body_idx + 1]

    assert "My description text." in body
    assert "kind: bug" in body
    assert "scope: impl_after_spec" in body
    assert "risk: manual" in body
    assert "board: triage" in body
    assert "telegram chat 99887" in body
    assert "thread 42" in body
