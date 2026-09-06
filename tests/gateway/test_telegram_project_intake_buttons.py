"""Tests for Telegram /project intake inline buttons."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _query():
    query = AsyncMock()
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.message.chat.type = "private"
    query.from_user = MagicMock()
    query.from_user.id = "777"
    query.from_user.first_name = "Tester"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


@pytest.mark.asyncio
async def test_project_intake_prompt_stores_server_side_state_and_buttons():
    adapter = _make_adapter()
    mock_msg = MagicMock()
    mock_msg.message_id = 501
    bot = adapter._bot
    assert bot is not None
    bot.send_message = AsyncMock(return_value=mock_msg)

    async def _callback(payload):  # pragma: no cover - not invoked in this test
        return str(payload)

    result = await adapter.send_project_intake_prompt(
        chat_id="12345",
        title="Build intake flow",
        state={"description": "Build intake flow"},
        session_key="telegram:12345:777",
        on_intake_selected=_callback,
    )

    assert result.success is True
    assert result.message_id == "501"
    assert len(adapter._project_intake_state) == 1
    flow_id, state = next(iter(adapter._project_intake_state.items()))
    assert state["title"] == "Build intake flow"
    assert state["answers"] == {}

    kwargs = bot.send_message.call_args[1]
    assert kwargs["chat_id"] == 12345
    assert "Build intake flow" in kwargs["text"]
    assert "What is this?" in kwargs["text"]
    assert kwargs["reply_markup"] is not None
    assert flow_id


@pytest.mark.asyncio
async def test_project_intake_callback_reaches_create_payload():
    adapter = _make_adapter()
    payloads = []

    async def _callback(payload):
        payloads.append(payload)
        return "created-card"

    adapter._project_intake_state["1"] = {
        "session_key": "telegram:12345:777",
        "chat_id": "12345",
        "title": "Build intake flow",
        "description": "Build intake flow",
        "step": "kind",
        "answers": {},
        "metadata": {},
        "on_intake_selected": _callback,
    }

    query = _query()
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_project_intake_callback(query, "pi:1:kind:feature", chat_id=12345, chat_type="private")
        await adapter._handle_project_intake_callback(query, "pi:1:board:control", chat_id=12345, chat_type="private")
        await adapter._handle_project_intake_callback(query, "pi:1:scope:spec", chat_id=12345, chat_type="private")
        await adapter._handle_project_intake_callback(query, "pi:1:risk:manual", chat_id=12345, chat_type="private")
        await adapter._handle_project_intake_callback(query, "pi:1:confirm:create", chat_id=12345, chat_type="private")

    assert payloads == [
        {
            "title": "Build intake flow",
            "description": "Build intake flow",
            "answers": {
                "kind": "feature",
                "board": "control",
                "scope": "spec",
                "risk": "manual",
            },
            "source": {"platform": "telegram", "chat_id": "12345"},
        }
    ]
    assert "1" not in adapter._project_intake_state
    query.edit_message_text.assert_called_with(text="created-card", reply_markup=None)
