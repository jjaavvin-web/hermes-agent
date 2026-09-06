from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class _Runner(GatewaySlashCommandsMixin):
    adapters = {}

    def _active_profile_name(self) -> str:
        return "gateway-profile"


def _event(
    text: str,
    *,
    message_id: str = "m-1",
    update_id: int | None = None,
    delivery_id: str | None = None,
    chat_id: str = "channel-1",
    thread_id: str | None = "thread-1",
    user_id: str = "user-1",
) -> MessageEvent:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type="channel",
        user_id=user_id,
        thread_id=thread_id,
        scope_id="guild-1",
        message_id=message_id,
    )
    return MessageEvent(
        text=text,
        source=source,
        message_id=message_id,
        delivery_id=delivery_id or message_id,
        platform_update_id=update_id,
        raw_message=SimpleNamespace(content="ambient text must not be stored"),
    )


def _created_task_id(output: str) -> str:
    match = re.search(r"Created\s+(t_[0-9a-f]+)\b", output)
    assert match, output
    return match.group(1)


def _created_event_payload(conn, task_id: str) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'",
        (task_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["payload"])


@pytest.mark.asyncio
async def test_sol_intake_forces_sol_board_triage_and_subscribes(kanban_home):
    kb.create_board("other")
    kb.set_current_board("other")

    output = await _Runner()._handle_sol_command(
        _event('/sol "Intake title" --body "Explicit body"')
    )
    task_id = _created_task_id(output)

    with kb.connect(board="sol") as conn:
        task = kb.get_task(conn, task_id)
        subs = kb.list_notify_subs(conn, task_id)
    with kb.connect(board="other") as other:
        assert kb.list_tasks(other) == []

    assert task is not None
    assert task.title == "Intake title"
    assert task.body == "Explicit body"
    assert task.status == "triage"
    assert task.assignee is None
    assert subs == [
        {
            "task_id": task_id,
            "platform": "discord",
            "chat_id": "channel-1",
            "thread_id": "thread-1",
            "user_id": "user-1",
            # /sol's add_notify_sub call doesn't pass user_id_alt/chat_type/
            # delivery_mode, so upstream's active-wake delivery-mode columns
            # (kanban_db.add_notify_sub) land on their defaults: chat_type
            # falls back to "dm", delivery_mode to "notify" (non-api_server
            # platform), user_id_alt stays unset.
            "user_id_alt": None,
            "chat_type": "dm",
            "notifier_profile": "gateway-profile",
            "delivery_mode": "notify",
            "delivery_metadata": {},
            "created_at": subs[0]["created_at"],
            "last_event_id": 1,
        }
    ]


@pytest.mark.asyncio
async def test_sol_intake_persists_sanitized_provenance_without_ambient_text(kanban_home):
    output = await _Runner()._handle_sol_command(
        _event('/sol "Needs intake" --body "Only explicit body"')
    )
    task_id = _created_task_id(output)

    with kb.connect(board="sol") as conn:
        payload = _created_event_payload(conn, task_id)
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.body == "Only explicit body"
    assert payload["provenance"] == {
        "platform": "discord",
        "chat_id": "channel-1",
        "thread_id": "thread-1",
        "user_id": "user-1",
        "message_id": "m-1",
        "delivery_id": "m-1",
    }
    serialized = json.dumps(payload, sort_keys=True)
    assert "ambient text must not be stored" not in serialized


@pytest.mark.asyncio
async def test_sol_intake_same_message_retry_reuses_task(kanban_home):
    runner = _Runner()
    first = await runner._handle_sol_command(
        _event('/sol "Same" --body "Body"', message_id="m-same", update_id=10)
    )
    second = await runner._handle_sol_command(
        _event('/sol "Same" --body "Body"', message_id="m-same", update_id=10)
    )

    assert _created_task_id(first) == _created_task_id(second)
    with kb.connect(board="sol") as conn:
        rows = conn.execute("SELECT id FROM tasks").fetchall()
        subs = kb.list_notify_subs(conn, _created_task_id(first))
    assert len(rows) == 1
    assert len(subs) == 1


@pytest.mark.asyncio
async def test_sol_intake_retry_after_archive_reuses_original_row(kanban_home):
    runner = _Runner()
    event = _event('/sol "Immutable delivery" --body "Body"', message_id="m-archive")
    first = await runner._handle_sol_command(event)
    task_id = _created_task_id(first)

    with kb.connect(board="sol") as conn:
        assert kb.archive_task(conn, task_id)

    second = await runner._handle_sol_command(event)

    assert _created_task_id(second) == task_id
    with kb.connect(board="sol") as conn:
        rows = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key IS NOT NULL"
        ).fetchall()
    assert [(row["id"], row["status"]) for row in rows] == [(task_id, "archived")]


@pytest.mark.asyncio
async def test_sol_intake_same_message_retry_ignores_changed_delivery_id(kanban_home):
    runner = _Runner()
    first = await runner._handle_sol_command(
        _event('/sol "Same" --body "Body"', message_id="m-stable", delivery_id="delivery-10")
    )
    second = await runner._handle_sol_command(
        _event('/sol "Same" --body "Body"', message_id="m-stable", delivery_id="delivery-11")
    )

    assert _created_task_id(first) == _created_task_id(second)
    with kb.connect(board="sol") as conn:
        rows = conn.execute("SELECT id FROM tasks").fetchall()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_sol_intake_rejects_non_discord_gateway_sources(kanban_home):
    event = _event('/sol "Wrong platform" --body "Body"')
    event.source.platform = Platform.TELEGRAM

    output = await _Runner()._handle_sol_command(event)

    assert "restricted to Discord" in output
    with kb.connect(board="sol") as conn:
        assert kb.list_tasks(conn) == []


@pytest.mark.asyncio
async def test_sol_intake_requires_stable_discord_message_id(kanban_home):
    event = _event('/sol "No stable id" --body "Body"')
    event.message_id = None
    event.source.message_id = None
    event.reply_to_message_id = "m-parent-must-not-be-used-as-identity"

    output = await _Runner()._handle_sol_command(event)

    assert "stable Discord message identity" in output
    with kb.connect(board="sol") as conn:
        assert kb.list_tasks(conn) == []


@pytest.mark.asyncio
async def test_sol_intake_rejects_oversized_explicit_fields(kanban_home):
    runner = _Runner()
    too_long_title = "t" * 241
    too_long_body = "b" * 20_001

    title_output = await runner._handle_sol_command(
        _event(f'/sol "{too_long_title}" --body "Body"', message_id="m-title")
    )
    body_output = await runner._handle_sol_command(
        _event(f'/sol "Title" --body "{too_long_body}"', message_id="m-body")
    )

    assert "240 characters" in title_output
    assert "20,000 characters" in body_output
    with kb.connect(board="sol") as conn:
        assert kb.list_tasks(conn) == []


@pytest.mark.asyncio
async def test_sol_parser_does_not_strip_sol_prefix_from_title(kanban_home):
    output = await _Runner()._handle_sol_command(
        _event('/sol "solve carefully" --body "Body"')
    )
    task_id = _created_task_id(output)

    with kb.connect(board="sol") as conn:
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.title == "solve carefully"


@pytest.mark.asyncio
async def test_sol_intake_runs_blocking_db_work_off_event_loop(kanban_home, monkeypatch):
    called = False
    import asyncio
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(func, /, *args, **kwargs):
        nonlocal called
        called = True
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("gateway.slash_commands.asyncio.to_thread", recording_to_thread)

    output = await _Runner()._handle_sol_command(
        _event('/sol "Off loop" --body "Body"')
    )

    assert called is True
    assert _created_task_id(output)


@pytest.mark.asyncio
async def test_sol_intake_same_text_different_message_ids_create_distinct_cards(kanban_home):
    runner = _Runner()
    first = await runner._handle_sol_command(
        _event('/sol "Same" --body "Body"', message_id="m-1", update_id=10)
    )
    second = await runner._handle_sol_command(
        _event('/sol "Same" --body "Body"', message_id="m-2", update_id=10)
    )

    assert _created_task_id(first) != _created_task_id(second)
    with kb.connect(board="sol") as conn:
        rows = conn.execute("SELECT id FROM tasks").fetchall()
    assert len(rows) == 2


def test_generic_kanban_create_still_uses_current_board_and_ready_status(kanban_home):
    from hermes_cli.kanban import run_slash

    kb.create_board("other")
    kb.set_current_board("other")

    output = run_slash('create "Generic card" --body "Generic body"')
    task_id = _created_task_id(output)

    with kb.connect(board="other") as conn:
        task = kb.get_task(conn, task_id)
    with kb.connect(board="sol") as sol:
        assert kb.list_tasks(sol) == []

    assert task is not None
    assert task.status == "ready"
    assert task.assignee is None


def test_sol_command_is_registered_for_slash_access():
    from hermes_cli.commands import (
        GATEWAY_KNOWN_COMMANDS,
        resolve_command,
        slack_native_slashes,
        telegram_bot_commands,
    )

    cmd = resolve_command("sol")
    assert cmd is not None
    assert cmd.gateway_only
    assert "sol" in GATEWAY_KNOWN_COMMANDS
    assert "sol" not in {name for name, _description in telegram_bot_commands()}
    assert "sol" not in {name for name, _description, _hint in slack_native_slashes()}
