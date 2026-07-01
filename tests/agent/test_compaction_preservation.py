"""Regression tests for memory-provider preservation during compaction."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor, PRESERVATION_CONTEXT_LABEL
from agent.conversation_compression import compress_context


def _response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    return resp


def _compressor() -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        return ContextCompressor(
            model="test/model",
            protect_first_n=1,
            protect_last_n=2,
            quiet_mode=True,
        )


def _messages(prefix: str = "turn") -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": f"{prefix} user 1"},
        {"role": "assistant", "content": f"{prefix} assistant 1"},
        {"role": "user", "content": f"{prefix} user 2"},
        {"role": "assistant", "content": f"{prefix} assistant 2"},
        {"role": "user", "content": f"{prefix} protected tail user"},
        {"role": "assistant", "content": f"{prefix} protected tail assistant"},
    ]


def _agent(memory_manager: object, compressor: ContextCompressor) -> SimpleNamespace:
    return SimpleNamespace(
        _compression_feasibility_checked=True,
        session_id="session-1",
        model="test/model",
        _memory_manager=memory_manager,
        context_compressor=compressor,
        _session_db=None,
        _cached_system_prompt=None,
        _todo_store=SimpleNamespace(format_for_injection=lambda: None),
        tools=None,
        platform="cli",
        event_callback=None,
        _session_init_model_config={},
        _emit_status=lambda *_args, **_kwargs: None,
        _emit_warning=lambda *_args, **_kwargs: None,
        _invalidate_system_prompt=lambda: None,
        _build_system_prompt=lambda system_message: system_message,
        _vprint=lambda *_args, **_kwargs: None,
        log_prefix="",
    )


def test_on_pre_compress_return_is_injected_verbatim_into_summarizer_prompt() -> None:
    preservation = "MEMORY FACT: preserve OPUSHANDS review-only routing exactly."
    prompts: list[str] = []
    compressor = _compressor()
    memory_manager = SimpleNamespace(on_pre_compress=lambda messages: preservation)
    agent = _agent(memory_manager, compressor)

    def fake_call_llm(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        prompts.append(prompt)
        return _response(f"## Critical Context\n{preservation}")

    with (
        patch.object(compressor, "_find_tail_cut_by_tokens", return_value=5),
        patch("agent.context_compressor.call_llm", fake_call_llm),
    ):
        compressed, _ = compress_context(agent, _messages(), "System prompt", approx_tokens=90_000)

    assert prompts, "summarizer prompt was not captured"
    assert PRESERVATION_CONTEXT_LABEL in prompts[0]
    assert preservation in prompts[0]
    assert preservation in "\n".join(str(message.get("content", "")) for message in compressed)


def test_preservation_context_survives_iterative_second_compaction() -> None:
    first_preservation = "ITERATIVE MEMORY FACT: user approved Loki packets only, not direct execution."
    second_preservation = "SECOND MEMORY FACT: keep merge and restart as owner gates."
    prompts: list[str] = []
    responses = iter([
        f"## Critical Context\n{first_preservation}",
        f"## Critical Context\n{first_preservation}\n{second_preservation}",
    ])
    compressor = _compressor()

    def fake_call_llm(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _response(next(responses))

    with patch("agent.context_compressor.call_llm", fake_call_llm):
        first = compressor._generate_summary(
            _messages("first")[1:5],
            preservation_context=first_preservation,
        )
        second = compressor._generate_summary(
            _messages("second")[1:5],
            preservation_context=second_preservation,
        )

    assert first_preservation in first
    assert first_preservation in prompts[1]
    assert second_preservation in prompts[1]
    assert first_preservation in second
    assert second_preservation in second


def test_opushands_preservation_context_present_in_compressed_output() -> None:
    opushands_fact = (
        "OPUSHANDS thread is review-only; no direct execution except audits; "
        "execution via Loki goal packets"
    )
    compressor = _compressor()

    def fake_call_llm(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        assert opushands_fact in prompt
        return _response(f"## Critical Context\n{opushands_fact}")

    with (
        patch.object(compressor, "_find_tail_cut_by_tokens", return_value=5),
        patch("agent.context_compressor.call_llm", fake_call_llm),
    ):
        compressed = compressor.compress(
            _messages("opushands"),
            current_tokens=90_000,
            preservation_context=opushands_fact,
        )

    combined = "\n".join(str(message.get("content", "")) for message in compressed)
    assert opushands_fact in combined
