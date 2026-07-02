"""Unit tests for tools/thread_context.py security context propagation into worker threads."""

from __future__ import annotations

import threading

import pytest

from tools.approval import (
    get_current_session_key,
    reset_current_session_key,
    set_current_session_key,
)
from tools.terminal_tool import (
    _get_approval_callback,
    _get_sudo_password_callback,
    set_approval_callback,
    set_sudo_password_callback,
)
from tools.thread_context import propagate_context_to_thread


@pytest.fixture(autouse=True)
def reset_terminal_tool_callbacks():
    """Keep thread-local callback state independent across test orderings."""
    set_approval_callback(None)
    set_sudo_password_callback(None)
    yield
    set_approval_callback(None)
    set_sudo_password_callback(None)


class CallbackProbeThread(threading.Thread):
    """Run a callable, then record callback TLS state before the thread exits."""

    def __init__(self, target, *args, **kwargs):
        super().__init__()
        self._target_callable = target
        self._args = args
        self._kwargs = kwargs
        self.result = None
        self.exception = None
        self.after_callbacks = None

    def run(self):
        try:
            self.result = self._target_callable(*self._args, **self._kwargs)
        except BaseException as exc:  # noqa: BLE001 - deliberately capture worker failure
            self.exception = exc
        finally:
            self.after_callbacks = (
                _get_approval_callback(),
                _get_sudo_password_callback(),
            )


def join_and_assert_finished(thread: threading.Thread) -> None:
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_return_value_and_arguments_are_forwarded():
    def target(prefix: str, value: int, *, suffix: str) -> str:
        return f"{prefix}-{value}-{suffix}"

    wrapper = propagate_context_to_thread(target)

    assert wrapper("arg", 7, suffix="kw") == "arg-7-kw"


def test_contextvar_propagates_to_wrapped_worker_thread_with_bare_thread_negative_control(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    token = set_current_session_key("sess-XYZ")
    try:
        bare_seen = []
        wrapped_seen = []

        def bare_worker():
            bare_seen.append(get_current_session_key("missing"))

        bare_thread = threading.Thread(target=bare_worker)
        bare_thread.start()
        join_and_assert_finished(bare_thread)

        def wrapped_worker():
            wrapped_seen.append(get_current_session_key("missing"))

        wrapped_thread = threading.Thread(
            target=propagate_context_to_thread(wrapped_worker),
        )
        wrapped_thread.start()
        join_and_assert_finished(wrapped_thread)

        assert bare_seen == ["missing"]
        assert wrapped_seen == ["sess-XYZ"]
    finally:
        reset_current_session_key(token)


def test_contextvar_mutation_inside_worker_isolated_from_parent():
    token = set_current_session_key("parent-original")
    try:
        worker_seen = []

        def target():
            worker_seen.append(get_current_session_key("missing"))
            set_current_session_key("mutated-in-worker")
            worker_seen.append(get_current_session_key("missing"))

        thread = threading.Thread(target=propagate_context_to_thread(target))
        thread.start()
        join_and_assert_finished(thread)

        assert worker_seen == ["parent-original", "mutated-in-worker"]
        assert get_current_session_key("missing") == "parent-original"
    finally:
        reset_current_session_key(token)


def test_parent_approval_and_sudo_callbacks_are_installed_in_worker_thread():
    def approval_cb(*_args, **_kwargs):
        return "approved"

    def sudo_cb():
        return "sudo-password"

    set_approval_callback(approval_cb)
    set_sudo_password_callback(sudo_cb)
    seen = []

    def target():
        seen.append(
            (
                _get_approval_callback(),
                _get_sudo_password_callback(),
            )
        )

    thread = threading.Thread(target=propagate_context_to_thread(target))
    thread.start()
    join_and_assert_finished(thread)

    assert seen == [(approval_cb, sudo_cb)]


def test_callbacks_are_cleared_on_success_before_worker_thread_exits():
    def approval_cb(*_args, **_kwargs):
        return "approved"

    def sudo_cb():
        return "sudo-password"

    set_approval_callback(approval_cb)
    set_sudo_password_callback(sudo_cb)
    seen_during_target = []

    def target():
        seen_during_target.append(
            (
                _get_approval_callback(),
                _get_sudo_password_callback(),
            )
        )
        return "done"

    thread = CallbackProbeThread(propagate_context_to_thread(target))
    thread.start()
    join_and_assert_finished(thread)

    assert thread.exception is None
    assert thread.result == "done"
    assert seen_during_target == [(approval_cb, sudo_cb)]
    assert thread.after_callbacks == (None, None)


def test_callbacks_are_cleared_when_target_raises_and_exception_is_preserved():
    def approval_cb(*_args, **_kwargs):
        return "approved"

    def sudo_cb():
        return "sudo-password"

    set_approval_callback(approval_cb)
    set_sudo_password_callback(sudo_cb)
    seen_during_target = []

    def target():
        seen_during_target.append(
            (
                _get_approval_callback(),
                _get_sudo_password_callback(),
            )
        )
        raise RuntimeError("target exploded")

    thread = CallbackProbeThread(propagate_context_to_thread(target))
    thread.start()
    join_and_assert_finished(thread)

    assert seen_during_target == [(approval_cb, sudo_cb)]
    assert isinstance(thread.exception, RuntimeError)
    with pytest.raises(RuntimeError, match="target exploded"):
        raise thread.exception
    assert thread.after_callbacks == (None, None)


def test_no_parent_callbacks_path_runs_without_installing_callbacks():
    seen = []

    def target():
        seen.append(
            (
                _get_approval_callback(),
                _get_sudo_password_callback(),
            )
        )
        return "no-callback-result"

    thread = CallbackProbeThread(propagate_context_to_thread(target))
    thread.start()
    join_and_assert_finished(thread)

    assert thread.exception is None
    assert thread.result == "no-callback-result"
    assert seen == [(None, None)]
    assert thread.after_callbacks == (None, None)


def test_propagate_context_returns_new_callable_and_direct_target_is_unchanged():
    calls = []

    def target():
        calls.append("direct")
        return "target-result"

    wrapper = propagate_context_to_thread(target)

    assert wrapper is not target
    assert target() == "target-result"
    assert calls == ["direct"]
