"""Focused tests for ``hermes_cli.voice``.

The voice wrapper owns process-wide recording/TTS state, but the tests here keep
all audio, transcription, TTS, filesystem, config, and backend boundaries mocked.
No microphone, speaker, network, or real ``~/.hermes`` state is touched.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest


class FakeRecorder:
    def __init__(self, wav_path: str | None = None) -> None:
        self.wav_path = wav_path
        self.is_recording = False
        self.start_calls = 0
        self.stop_calls = 0
        self.cancel_calls = 0
        self.last_callback = None
        self._silence_threshold = None
        self._silence_duration = None
        self._peak_rms = 123
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None

    def start(self, on_silence_stop=None) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.start_calls += 1
        self.last_callback = on_silence_stop
        self.is_recording = True

    def stop(self) -> str | None:
        if self.stop_error is not None:
            raise self.stop_error
        self.stop_calls += 1
        self.is_recording = False
        return self.wav_path

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.is_recording = False


@pytest.fixture
def voice_module(monkeypatch, tmp_path):
    """Import the wrapper with state and path/config dependencies isolated."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "hermes-home").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()

    import hermes_cli.voice as voice

    monkeypatch.setattr(voice, "_recorder", None)
    monkeypatch.setattr(voice, "_continuous_active", False)
    monkeypatch.setattr(voice, "_continuous_stopping", False)
    monkeypatch.setattr(voice, "_continuous_auto_restart", True)
    monkeypatch.setattr(voice, "_continuous_recorder", None)
    monkeypatch.setattr(voice, "_continuous_on_transcript", None)
    monkeypatch.setattr(voice, "_continuous_on_status", None)
    monkeypatch.setattr(voice, "_continuous_on_silent_limit", None)
    monkeypatch.setattr(voice, "_continuous_no_speech_count", 0)
    voice._tts_playing.set()

    monkeypatch.setattr(voice, "_play_beep", lambda *args, **kwargs: None)
    monkeypatch.setattr(voice, "play_audio_file", pytest.fail)
    monkeypatch.setattr(
        voice,
        "transcribe_recording",
        lambda *_args, **_kwargs: pytest.fail("transcribe_recording was not mocked"),
    )
    monkeypatch.setattr(voice, "is_whisper_hallucination", lambda _text: False)
    return voice


class ImmediateThread:
    def __init__(self, target, daemon=False):
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        self.target()


class TestVoiceRecordKeyConfiguration:
    @pytest.mark.parametrize(
        ("cfg", "expected"),
        [
            ({"voice": {"record_key": "ctrl+o"}}, "ctrl+o"),
            ({"voice": {"record_key": "option+space"}}, "option+space"),
            ({"voice": {"beep_enabled": False}}, None),
            ({"voice": True}, None),
            ({"voice": "cmd+b"}, None),
            (None, None),
            ([], None),
        ],
    )
    def test_voice_record_key_from_config_is_shape_safe(self, voice_module, cfg, expected):
        assert voice_module.voice_record_key_from_config(cfg) == expected

    @pytest.mark.parametrize(
        ("raw", "normalized", "status"),
        [
            ("ctrl+b", "c-b", "Ctrl+B"),
            ("control+return", "c-enter", "Ctrl+Enter"),
            ("option+space", "a-space", "Alt+Space"),
            ("opt+del", "a-delete", "Alt+Delete"),
            ("ctrl + esc", "c-escape", "Ctrl+Escape"),
            ("super+b", "c-b", "Ctrl+B"),
            ("win+o", "c-b", "Ctrl+B"),
            ("ctrl+c", "c-b", "Ctrl+B"),
            ("ctrl+alt+r", "c-b", "Ctrl+B"),
            ("ctrl+spcae", "c-b", "Ctrl+B"),
            ("", "c-b", "Ctrl+B"),
            (True, "c-b", "Ctrl+B"),
        ],
    )
    def test_record_key_normalization_and_status_validation(
        self, voice_module, raw, normalized, status
    ):
        assert voice_module.normalize_voice_record_key_for_prompt_toolkit(raw) == normalized
        assert voice_module.format_voice_record_key_for_status(raw) == status

    def test_alt_reserved_shortcuts_are_darwin_only(self, voice_module, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert voice_module.normalize_voice_record_key_for_prompt_toolkit("alt+c") == "c-b"
        assert voice_module.normalize_voice_record_key_for_prompt_toolkit("option+d") == "c-b"

        monkeypatch.setattr(sys, "platform", "linux")
        assert voice_module.normalize_voice_record_key_for_prompt_toolkit("alt+c") == "a-c"
        assert voice_module.normalize_voice_record_key_for_prompt_toolkit("option+d") == "a-d"

    @pytest.mark.parametrize(
        ("loaded_config", "expected"),
        [
            ({"voice": {"beep_enabled": False}}, False),
            ({"voice": {"beep_enabled": True}}, True),
            ({"voice": {}}, True),
            ({"voice": "malformed"}, True),
        ],
    )
    def test_beeps_enabled_reads_config_without_requiring_real_home(
        self, voice_module, monkeypatch, loaded_config, expected
    ):
        config_module = ModuleType("hermes_cli.config")
        setattr(config_module, "load_config", lambda: loaded_config)
        monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)

        assert voice_module._beeps_enabled() is expected

    def test_beeps_enabled_defaults_true_when_config_load_fails(self, voice_module, monkeypatch):
        config_module = ModuleType("hermes_cli.config")

        def fail_load_config():
            raise RuntimeError("config unavailable")

        setattr(config_module, "load_config", fail_load_config)
        monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)

        assert voice_module._beeps_enabled() is True


class TestPushToTalkRecordingAndTranscription:
    def test_start_recording_creates_and_starts_recorder_once(self, voice_module, monkeypatch):
        recorder = FakeRecorder()
        monkeypatch.setattr(voice_module, "create_audio_recorder", lambda: recorder)

        voice_module.start_recording()
        voice_module.start_recording()

        assert recorder.start_calls == 1
        assert voice_module._recorder is recorder

    def test_stop_without_active_recording_is_noop(self, voice_module):
        assert voice_module.stop_and_transcribe() is None

    def test_stop_returns_none_when_recorder_produces_no_audio_file(self, voice_module, monkeypatch):
        recorder = FakeRecorder(wav_path=None)
        monkeypatch.setattr(voice_module, "_recorder", recorder)

        assert voice_module.stop_and_transcribe() is None
        assert recorder.stop_calls == 1

    def test_successful_transcription_strips_text_and_deletes_tmp_wav(
        self, voice_module, monkeypatch, tmp_path
    ):
        wav = tmp_path / "recording.wav"
        wav.write_bytes(b"fake wav bytes")
        recorder = FakeRecorder(wav_path=str(wav))
        monkeypatch.setattr(voice_module, "_recorder", recorder)
        calls = []
        monkeypatch.setattr(
            voice_module,
            "transcribe_recording",
            lambda path: calls.append(path) or {"success": True, "transcript": "  hello world  "},
        )

        assert voice_module.stop_and_transcribe() == "hello world"
        assert calls == [str(wav)]
        assert not wav.exists()
        assert voice_module._recorder is None

    @pytest.mark.parametrize(
        "result",
        [
            {"success": False, "error": "missing ffmpeg"},
            {"success": True, "transcript": ""},
            {"success": True},
            {"unexpected": "malformed output"},
        ],
    )
    def test_malformed_or_failed_transcription_results_return_none_and_cleanup(
        self, voice_module, monkeypatch, tmp_path, result
    ):
        wav = tmp_path / "bad-output.wav"
        wav.write_bytes(b"fake wav bytes")
        monkeypatch.setattr(voice_module, "_recorder", FakeRecorder(wav_path=str(wav)))
        monkeypatch.setattr(voice_module, "transcribe_recording", lambda _path: result)

        assert voice_module.stop_and_transcribe() is None
        assert not wav.exists()

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("backend failed"), TimeoutError("backend timeout")],
    )
    def test_backend_exception_or_timeout_returns_none_and_deletes_tmp_wav(
        self, voice_module, monkeypatch, tmp_path, caplog, exc
    ):
        wav = tmp_path / "timeout.wav"
        wav.write_bytes(b"fake wav bytes")
        monkeypatch.setattr(voice_module, "_recorder", FakeRecorder(wav_path=str(wav)))

        def fail_transcribe(_path):
            raise exc

        monkeypatch.setattr(voice_module, "transcribe_recording", fail_transcribe)

        assert voice_module.stop_and_transcribe() is None
        assert not wav.exists()
        assert "voice transcription failed" in caplog.text

    def test_hallucinated_whisper_text_is_suppressed_and_cleaned_up(
        self, voice_module, monkeypatch, tmp_path
    ):
        wav = tmp_path / "hallucination.wav"
        wav.write_bytes(b"fake wav bytes")
        monkeypatch.setattr(voice_module, "_recorder", FakeRecorder(wav_path=str(wav)))
        monkeypatch.setattr(
            voice_module,
            "transcribe_recording",
            lambda _path: {"success": True, "transcript": "thank you for watching"},
        )
        monkeypatch.setattr(voice_module, "is_whisper_hallucination", lambda _text: True)

        assert voice_module.stop_and_transcribe() is None
        assert not wav.exists()


class TestContinuousRecordingLoop:
    def test_start_continuous_sets_thresholds_and_reports_listening(
        self, voice_module, monkeypatch
    ):
        recorder = FakeRecorder(wav_path="/no/real/file.wav")
        monkeypatch.setattr(voice_module, "create_audio_recorder", lambda: recorder)
        statuses = []

        assert voice_module.start_continuous(
            on_transcript=lambda _text: None,
            on_status=statuses.append,
            silence_threshold=111,
            silence_duration=1.5,
        ) is True

        assert recorder.start_calls == 1
        assert recorder._silence_threshold == 111
        assert recorder._silence_duration == 1.5
        assert callable(recorder.last_callback)
        assert statuses == ["listening"]
        assert voice_module.is_continuous_active() is True

    def test_start_continuous_returns_false_while_stop_cleanup_in_progress(
        self, voice_module, monkeypatch
    ):
        monkeypatch.setattr(voice_module, "_continuous_stopping", True)
        monkeypatch.setattr(voice_module, "create_audio_recorder", pytest.fail)

        assert voice_module.start_continuous(on_transcript=lambda _text: None) is False

    def test_continuous_transcribes_on_silence_restarts_and_cleans_tmp_wav(
        self, voice_module, monkeypatch, tmp_path
    ):
        wav = tmp_path / "vad.wav"
        wav.write_bytes(b"fake wav bytes")
        recorder = FakeRecorder(wav_path=str(wav))
        monkeypatch.setattr(voice_module, "create_audio_recorder", lambda: recorder)
        monkeypatch.setattr(
            voice_module,
            "transcribe_recording",
            lambda path: {"success": path == str(wav), "transcript": "  vad text  "},
        )
        transcripts = []
        statuses = []

        voice_module.start_continuous(
            on_transcript=transcripts.append,
            on_status=statuses.append,
        )
        callback = recorder.last_callback
        assert callback is not None
        callback()

        assert transcripts == ["vad text"]
        assert statuses == ["listening", "transcribing", "listening"]
        assert recorder.start_calls == 2
        assert not wav.exists()
        assert voice_module.is_continuous_active() is True

    @pytest.mark.parametrize(
        "transcribe_result",
        [
            {"success": False, "error": "backend failed"},
            {"success": True, "transcript": ""},
            {"success": True},
            {"nonsense": "malformed"},
        ],
    )
    def test_continuous_malformed_or_failed_output_counts_as_no_speech(
        self, voice_module, monkeypatch, tmp_path, transcribe_result
    ):
        wav = tmp_path / "empty.wav"
        wav.write_bytes(b"fake wav bytes")
        recorder = FakeRecorder(wav_path=str(wav))
        monkeypatch.setattr(voice_module, "create_audio_recorder", lambda: recorder)
        monkeypatch.setattr(voice_module, "transcribe_recording", lambda _path: transcribe_result)
        transcripts = []

        voice_module.start_continuous(on_transcript=transcripts.append)
        callback = recorder.last_callback
        assert callback is not None
        callback()

        assert transcripts == []
        assert voice_module._continuous_no_speech_count == 1
        assert not wav.exists()
        assert voice_module.is_continuous_active() is True

    def test_continuous_backend_timeout_is_logged_and_loop_restarts(
        self, voice_module, monkeypatch, tmp_path, caplog
    ):
        wav = tmp_path / "timeout.wav"
        wav.write_bytes(b"fake wav bytes")
        recorder = FakeRecorder(wav_path=str(wav))
        monkeypatch.setattr(voice_module, "create_audio_recorder", lambda: recorder)

        def timeout(_path):
            raise TimeoutError("slow backend")

        monkeypatch.setattr(voice_module, "transcribe_recording", timeout)

        voice_module.start_continuous(on_transcript=lambda _text: None)
        callback = recorder.last_callback
        assert callback is not None
        callback()

        assert "continuous transcription failed" in caplog.text
        assert recorder.start_calls == 2
        assert not wav.exists()

    def test_three_silent_cycles_stop_loop_and_fire_silent_limit(
        self, voice_module, monkeypatch, tmp_path
    ):
        wavs = []
        for i in range(3):
            wav = tmp_path / f"silent-{i}.wav"
            wav.write_bytes(b"fake wav bytes")
            wavs.append(wav)
        recorder = FakeRecorder(wav_path=str(wavs[0]))
        monkeypatch.setattr(voice_module, "create_audio_recorder", lambda: recorder)
        monkeypatch.setattr(
            voice_module,
            "transcribe_recording",
            lambda _path: {"success": True, "transcript": ""},
        )
        silent_limit = []

        voice_module.start_continuous(
            on_transcript=lambda _text: None,
            on_silent_limit=lambda: silent_limit.append("hit"),
        )
        for wav in wavs:
            recorder.wav_path = str(wav)
            callback = recorder.last_callback
            assert callback is not None
            callback()

        assert silent_limit == ["hit"]
        assert voice_module.is_continuous_active() is False
        assert recorder.cancel_calls == 1
        assert all(not wav.exists() for wav in wavs)

    def test_force_transcribe_stop_runs_cleanup_thread_and_delivers_transcript(
        self, voice_module, monkeypatch, tmp_path
    ):
        wav = tmp_path / "force.wav"
        wav.write_bytes(b"fake wav bytes")
        recorder = FakeRecorder(wav_path=str(wav))
        monkeypatch.setattr(voice_module, "create_audio_recorder", lambda: recorder)
        monkeypatch.setattr(voice_module.threading, "Thread", ImmediateThread)
        monkeypatch.setattr(
            voice_module,
            "transcribe_recording",
            lambda _path: {"success": True, "transcript": "manual stop"},
        )
        transcripts = []
        statuses = []

        voice_module.start_continuous(
            on_transcript=transcripts.append,
            on_status=statuses.append,
            auto_restart=False,
        )
        voice_module.stop_continuous(force_transcribe=True)

        assert transcripts == ["manual stop"]
        assert statuses == ["listening", "transcribing", "idle"]
        assert recorder.stop_calls == 1
        assert not wav.exists()
        assert voice_module._continuous_stopping is False

    def test_force_transcribe_stop_failure_cancels_and_reports_idle(
        self, voice_module, monkeypatch
    ):
        recorder = FakeRecorder(wav_path="/no/real/file.wav")
        recorder.stop_error = RuntimeError("stop failed")
        monkeypatch.setattr(voice_module, "create_audio_recorder", lambda: recorder)
        monkeypatch.setattr(voice_module.threading, "Thread", ImmediateThread)
        statuses = []

        voice_module.start_continuous(
            on_transcript=lambda _text: None,
            on_status=statuses.append,
        )
        voice_module.stop_continuous(force_transcribe=True)

        assert recorder.cancel_calls == 1
        assert statuses == ["listening", "transcribing", "idle"]
        assert voice_module._continuous_stopping is False
        assert voice_module.is_continuous_active() is False


@pytest.mark.real_audio_playback
class TestSpeakTextTtsHandling:
    """Exercise the real ``speak_text`` synth/playback pipeline.

    Every test in this file runs under the autouse ``_audio_playback_guard``
    (tests/conftest.py), which stubs ``voice.speak_text`` to a no-op for
    every OTHER test class here so no test ever opens real speakers. This
    class is specifically testing that pipeline's own behavior, so it opts
    back into the real function with the documented escape hatch.
    """

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_empty_text_is_noop(self, voice_module, text):
        assert voice_module.speak_text(text) is None

    def test_speak_text_sanitizes_markdown_writes_tmp_mp3_plays_and_cleans(
        self, voice_module, monkeypatch, tmp_path
    ):
        fake_tmp = tmp_path / "voice-tmp"
        fake_tmp.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))
        monkeypatch.setattr(time, "strftime", lambda _fmt: "20260102_030405")

        calls: list[tuple[str, str]] = []
        played: list[str] = []

        fake_tts_module = ModuleType("tools.tts_tool")

        def fake_text_to_speech_tool(*, text: str, output_path: str):
            calls.append((text, output_path))
            Path(output_path).write_bytes(b"mp3")
            Path(output_path).with_suffix(".ogg").write_bytes(b"ogg")
            return '{"success": true}'

        setattr(fake_tts_module, "text_to_speech_tool", fake_text_to_speech_tool)
        monkeypatch.setitem(sys.modules, "tools.tts_tool", fake_tts_module)
        monkeypatch.setattr(voice_module, "play_audio_file", lambda path: played.append(path))

        voice_module.speak_text(
            "# Title\n**bold** [link](https://example.com) `code`\n"
            "```python\nprint('hidden')\n```\nhttps://example.com/raw"
        )

        expected_path = fake_tmp / "hermes_voice" / "tts_20260102_030405.mp3"
        # Sanitization now runs through the shared tools.tts_text_normalize
        # cleaner (prepare_spoken_text) instead of speak_text's old inline
        # regex pipeline — it also normalizes line breaks into
        # comma-joined, period-terminated speakable sentences rather than
        # preserving raw newlines (#58930).
        assert calls == [
            ("Title, bold link code.", str(expected_path)),
        ]
        assert played == [str(expected_path)]
        assert not expected_path.exists()
        assert not expected_path.with_suffix(".ogg").exists()
        assert voice_module._tts_playing.is_set()

    def test_speak_text_forwards_long_text_unsliced_to_the_tts_tool(
        self, voice_module, monkeypatch, tmp_path
    ):
        """speak_text no longer truncates long text itself (#58930).

        The old cap silently dropped everything past 4000 chars before
        synthesis. That responsibility moved downstream to
        ``text_to_speech_tool``, which normalizes with ``max_chars=None``
        and instead splits long-form text into provider-safe chunks "without
        silent truncation" (its own docstring) — no content is lost either
        way, so speak_text's job is just to hand the full cleaned text
        through untouched.
        """
        fake_tmp = tmp_path / "voice-tmp"
        fake_tmp.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))
        monkeypatch.setattr(time, "strftime", lambda _fmt: "20260102_030405")
        captured = {}
        fake_tts_module = ModuleType("tools.tts_tool")

        def fake_text_to_speech_tool(*, text: str, output_path: str):
            captured["text"] = text
            Path(output_path).write_bytes(b"mp3")
            return '{"success": true}'

        setattr(fake_tts_module, "text_to_speech_tool", fake_text_to_speech_tool)
        monkeypatch.setitem(sys.modules, "tools.tts_tool", fake_tts_module)
        monkeypatch.setattr(voice_module, "play_audio_file", lambda _path: None)

        voice_module.speak_text("x" * 4100)

        assert len(captured["text"]) == 4100

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("missing tts binary"), TimeoutError("tts timeout")],
    )
    def test_speak_text_backend_failure_or_timeout_is_logged_and_state_restored(
        self, voice_module, monkeypatch, tmp_path, caplog, exc
    ):
        fake_tmp = tmp_path / "voice-tmp"
        fake_tmp.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))
        fake_tts_module = ModuleType("tools.tts_tool")

        def fake_text_to_speech_tool(*, text: str, output_path: str):
            raise exc

        setattr(fake_tts_module, "text_to_speech_tool", fake_text_to_speech_tool)
        monkeypatch.setitem(sys.modules, "tools.tts_tool", fake_tts_module)

        voice_module.speak_text("hello")

        assert "Voice TTS playback failed" in caplog.text
        assert voice_module._tts_playing.is_set()

    def test_speak_text_no_audio_file_does_not_try_playback(
        self, voice_module, monkeypatch, tmp_path
    ):
        fake_tmp = tmp_path / "voice-tmp"
        fake_tmp.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))
        fake_tts_module = ModuleType("tools.tts_tool")
        setattr(
            fake_tts_module,
            "text_to_speech_tool",
            lambda *, text, output_path: '{"success": true}',
        )
        monkeypatch.setitem(sys.modules, "tools.tts_tool", fake_tts_module)
        play_calls = []
        monkeypatch.setattr(voice_module, "play_audio_file", lambda path: play_calls.append(path))

        voice_module.speak_text("hello")

        assert play_calls == []
        assert voice_module._tts_playing.is_set()

    def test_speak_text_pauses_and_resumes_active_continuous_recorder(
        self, voice_module, monkeypatch, tmp_path
    ):
        fake_tmp = tmp_path / "voice-tmp"
        fake_tmp.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
        recorder = FakeRecorder(wav_path="/no/real/file.wav")
        recorder.is_recording = True
        monkeypatch.setattr(voice_module, "_continuous_active", True)
        monkeypatch.setattr(voice_module, "_continuous_recorder", recorder)
        fake_tts_module = ModuleType("tools.tts_tool")

        def fake_text_to_speech_tool(*, text: str, output_path: str):
            Path(output_path).write_bytes(b"mp3")
            return '{"success": true}'

        setattr(fake_tts_module, "text_to_speech_tool", fake_text_to_speech_tool)
        monkeypatch.setitem(sys.modules, "tools.tts_tool", fake_tts_module)
        monkeypatch.setattr(voice_module, "play_audio_file", lambda _path: None)

        voice_module.speak_text("agent reply")

        assert recorder.cancel_calls == 1
        assert recorder.start_calls == 1
        assert recorder.last_callback is voice_module._continuous_on_silence
        assert voice_module._tts_playing.is_set()
