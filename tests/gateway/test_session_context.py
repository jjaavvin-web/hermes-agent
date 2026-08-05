"""Tests for gateway.session_context and its _UNSET / empty string / os.environ three-state contract."""

from __future__ import annotations

import contextvars
import os

import pytest

from gateway import session_context
from gateway.session_context import (
    _UNSET,
    clear_session_vars,
    get_session_env,
    set_current_session_id,
    set_session_vars,
)


def test_get_session_env_falls_back_to_os_environ_when_contextvar_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")

    def read_platform() -> str:
        return get_session_env("HERMES_SESSION_PLATFORM")

    assert contextvars.copy_context().run(read_platform) == "telegram"


def test_get_session_env_returns_default_when_contextvar_and_environ_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

    def read_platform() -> str:
        return get_session_env("HERMES_SESSION_PLATFORM", "fallbackdef")

    assert contextvars.copy_context().run(read_platform) == "fallbackdef"


def test_set_session_vars_context_value_wins_over_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "ENV_SHOULD_BE_IGNORED")

    def read_chat_id_after_context_set() -> str:
        set_session_vars(chat_id="ctx-chat-42")
        return get_session_env("HERMES_SESSION_CHAT_ID")

    assert contextvars.copy_context().run(read_chat_id_after_context_set) == "ctx-chat-42"


def test_clear_session_vars_explicit_empty_suppresses_os_environ_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "STALE_ENV_THREAD")

    def read_thread_id_after_clear() -> str:
        tokens = set_session_vars(thread_id="t1")
        clear_session_vars(tokens)
        return get_session_env("HERMES_SESSION_THREAD_ID")

    assert contextvars.copy_context().run(read_thread_id_after_clear) == ""


def test_session_contextvars_are_task_local_between_copied_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    def read_user_a_after_context_set() -> str:
        set_session_vars(user_id="user-A")
        return get_session_env("HERMES_SESSION_USER_ID")

    def read_user_b_without_context_set() -> str:
        return get_session_env("HERMES_SESSION_USER_ID", "default-B")

    assert ctx_a.run(read_user_a_after_context_set) == "user-A"
    assert ctx_b.run(read_user_b_without_context_set) == "default-B"


def test_set_current_session_id_writes_os_environ_and_contextvar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    def set_and_read_session_id() -> tuple[str, str]:
        set_current_session_id("sess-xyz")
        return os.environ["HERMES_SESSION_ID"], get_session_env("HERMES_SESSION_ID")

    assert contextvars.copy_context().run(set_and_read_session_id) == (
        "sess-xyz",
        "sess-xyz",
    )


def test_set_session_vars_returns_fifteen_reset_tokens() -> None:
    def set_and_return_tokens() -> list[object]:
        return set_session_vars()

    result = contextvars.copy_context().run(set_and_return_tokens)

    assert isinstance(result, list)
    assert len(result) == 15


def test_unknown_names_use_default_and_cron_names_route_to_context_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown_name = "HERMES_SESSION_NOT_A_REAL_VAR"
    monkeypatch.delenv(unknown_name, raising=False)
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "c9")

    def read_unknown_and_cron() -> tuple[str, str]:
        return (
            get_session_env(unknown_name),
            get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID"),
        )

    assert _UNSET is session_context._UNSET
    assert unknown_name not in session_context._VAR_MAP
    assert "HERMES_CRON_AUTO_DELIVER_CHAT_ID" in session_context._VAR_MAP
    assert contextvars.copy_context().run(read_unknown_and_cron) == ("", "c9")
