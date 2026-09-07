"""Dedicated unit tests for GatewayEventDispatcher branch behavior."""

from __future__ import annotations

from gateway.stream_dispatch import GatewayEventDispatcher
from gateway.stream_events import (
    Commentary,
    GatewayNotice,
    LongToolHint,
    MessageChunk,
    MessageStop,
    ToolCallChunk,
    ToolCallFinished,
)


class RecordingAdapter:
    def __init__(self, *, tool_returns=None, raise_on_message=False, raise_on_tool=False):
        self.message_calls = []
        self.tool_calls = []
        self.tool_returns = list(tool_returns or [])
        self.raise_on_message = raise_on_message
        self.raise_on_tool = raise_on_tool

    def render_message_event(self, event, sink):
        if self.raise_on_message:
            raise RuntimeError("message render broke")
        self.message_calls.append((event, sink))

    def format_tool_event(self, event, *, mode, preview_max_len):
        if self.raise_on_tool:
            raise RuntimeError("tool render broke")
        self.tool_calls.append((event, mode, preview_max_len))
        if self.tool_returns:
            return self.tool_returns.pop(0)
        return f"tool-line:{event.tool_name}:{mode}:{preview_max_len}"


class RecordingSink:
    pass


class RecordingCallback:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


def test_message_chunk_and_commentary_are_rendered_to_sink():
    adapter = RecordingAdapter()
    sink = RecordingSink()
    dispatcher = GatewayEventDispatcher(adapter, sink)
    chunk = MessageChunk("hello")
    commentary = Commentary("working on it")

    dispatcher.dispatch(chunk)
    dispatcher.dispatch(commentary)

    assert adapter.message_calls == [(chunk, sink), (commentary, sink)]


def test_message_events_are_dropped_when_sink_is_none():
    adapter = RecordingAdapter()
    dispatcher = GatewayEventDispatcher(adapter, sink=None)

    dispatcher.dispatch(MessageChunk("invisible"))

    assert adapter.message_calls == []


def test_tool_mode_off_drops_tool_chunk_without_formatting_or_enqueueing():
    adapter = RecordingAdapter()
    enqueued = []
    dispatcher = GatewayEventDispatcher(
        adapter,
        RecordingSink(),
        enqueue_tool_line=enqueued.append,
        tool_mode="off",
    )

    dispatcher.dispatch(ToolCallChunk("browser_snapshot", preview="{}"))

    assert adapter.tool_calls == []
    assert enqueued == []


def test_tool_mode_all_formats_tool_chunk_and_enqueues_rendered_line():
    adapter = RecordingAdapter(tool_returns=["rendered tool line"])
    enqueued = []
    dispatcher = GatewayEventDispatcher(
        adapter,
        RecordingSink(),
        enqueue_tool_line=enqueued.append,
        preview_max_len=17,
    )
    event = ToolCallChunk("terminal", preview="ls", args={"command": "ls"}, index=3)

    dispatcher.dispatch(event)

    assert adapter.tool_calls == [(event, "all", 17)]
    assert enqueued == ["rendered tool line"]


def test_adapter_can_eat_tool_event_with_falsy_rendered_line():
    adapter = RecordingAdapter(tool_returns=[None, ""])
    enqueued = []
    dispatcher = GatewayEventDispatcher(
        adapter,
        RecordingSink(),
        enqueue_tool_line=enqueued.append,
    )
    none_event = ToolCallChunk("read_file", index=1)
    empty_event = ToolCallChunk("search_files", index=2)

    dispatcher.dispatch(none_event)
    dispatcher.dispatch(empty_event)

    assert adapter.tool_calls == [(none_event, "all", 40), (empty_event, "all", 40)]
    assert enqueued == []


def test_new_mode_deduplicates_consecutive_tool_names():
    adapter = RecordingAdapter()
    enqueued = []
    dispatcher = GatewayEventDispatcher(
        adapter,
        RecordingSink(),
        enqueue_tool_line=enqueued.append,
        tool_mode="new",
        preview_max_len=9,
    )
    first_a = ToolCallChunk("toolA", index=1)
    second_a = ToolCallChunk("toolA", index=2)
    first_b = ToolCallChunk("toolB", index=3)

    dispatcher.dispatch(first_a)
    dispatcher.dispatch(second_a)
    dispatcher.dispatch(first_b)

    assert adapter.tool_calls == [(first_a, "new", 9), (first_b, "new", 9)]
    assert enqueued == ["tool-line:toolA:new:9", "tool-line:toolB:new:9"]


def test_tool_call_finished_is_noop():
    adapter = RecordingAdapter()
    enqueued = []
    dispatcher = GatewayEventDispatcher(
        adapter,
        RecordingSink(),
        enqueue_tool_line=enqueued.append,
    )

    dispatcher.dispatch(ToolCallFinished("terminal", duration=1.2, ok=True, index=4))

    assert adapter.message_calls == []
    assert adapter.tool_calls == []
    assert enqueued == []


def test_long_tool_hint_and_gateway_notice_call_optional_hooks_and_are_safe_without_hooks():
    adapter = RecordingAdapter()
    long_tool_callback = RecordingCallback()
    notice_callback = RecordingCallback()
    dispatcher = GatewayEventDispatcher(
        adapter,
        RecordingSink(),
        on_long_tool=long_tool_callback,
        on_notice=notice_callback,
    )
    long_tool = LongToolHint("terminal", duration=12.5)
    notice = GatewayNotice("restart", "gateway restarted", {"source": "test"})

    dispatcher.dispatch(long_tool)
    dispatcher.dispatch(notice)

    assert long_tool_callback.events == [long_tool]
    assert notice_callback.events == [notice]

    dispatcher_without_hooks = GatewayEventDispatcher(adapter, RecordingSink())
    dispatcher_without_hooks.dispatch(LongToolHint())
    dispatcher_without_hooks.dispatch(GatewayNotice("online"))


def test_dispatch_swallows_adapter_render_and_format_exceptions():
    message_adapter = RecordingAdapter(raise_on_message=True)
    message_dispatcher = GatewayEventDispatcher(message_adapter, RecordingSink())
    tool_adapter = RecordingAdapter(raise_on_tool=True)
    enqueued = []
    tool_dispatcher = GatewayEventDispatcher(
        tool_adapter,
        RecordingSink(),
        enqueue_tool_line=enqueued.append,
    )

    message_dispatcher.dispatch(MessageChunk("x"))
    tool_dispatcher.dispatch(ToolCallChunk("terminal"))

    assert message_adapter.message_calls == []
    assert tool_adapter.tool_calls == []
    assert enqueued == []


def test_message_stop_is_routed_through_message_adapter_hook():
    adapter = RecordingAdapter()
    sink = RecordingSink()
    dispatcher = GatewayEventDispatcher(adapter, sink)
    event = MessageStop(final=True)

    dispatcher.dispatch(event)

    assert adapter.message_calls == [(event, sink)]
