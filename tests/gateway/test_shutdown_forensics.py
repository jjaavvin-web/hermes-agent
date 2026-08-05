"""Tests for gateway.shutdown_forensics — fast snapshot + async diag spawn."""

from __future__ import annotations

import builtins
import io
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from gateway import shutdown_forensics as sf


# ---------------------------------------------------------------------------
# _signal_name
# ---------------------------------------------------------------------------

class TestSignalName:

    def test_unknown_int_returns_signal_num_token(self):
        # Pick an integer extremely unlikely to ever be a real signal alias
        assert sf._signal_name(9999) == "signal#9999"


# ---------------------------------------------------------------------------
# snapshot_shutdown_context
# ---------------------------------------------------------------------------

class TestSnapshotShutdownContext:

    def test_handles_none_signal(self):
        ctx = sf.snapshot_shutdown_context(None)
        assert ctx["signal"] == "UNKNOWN"
        assert ctx["signal_num"] is None

    def test_includes_timestamps(self):
        before = time.time()
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        after = time.time()
        assert before <= ctx["ts"] <= after
        assert isinstance(ctx["ts_monotonic"], float)


    def test_under_systemd_false_without_invocation_id_and_normal_ppid(
        self, monkeypatch
    ):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        # We can't actually change ppid; skip if we happen to be reaped
        # by init (e.g. running under tini).
        if os.getppid() == 1:
            pytest.skip("test process is reaped by init")
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        assert ctx["under_systemd"] is False


    def test_detects_takeover_marker_for_self(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        marker = tmp_path / ".gateway-takeover.json"
        marker.write_text(
            f'{{"target_pid": {os.getpid()}, "replacer_pid": 99999}}',
            encoding="utf-8",
        )
        ctx = sf.snapshot_shutdown_context(signal.SIGTERM)
        assert "takeover_marker" in ctx
        assert ctx["takeover_marker_for_self"] is True



# ---------------------------------------------------------------------------
# /proc helpers / _proc_summary
# ---------------------------------------------------------------------------

class TestProcSummary:
    @pytest.mark.parametrize("pid", [-1, 0])
    def test_non_positive_pid_returns_pid_only(self, pid):
        assert sf._proc_summary(pid) == {"pid": pid}

    def test_assembles_fields_from_proc_helpers(self, monkeypatch):
        long_cmdline = "python " + "x" * 500
        fields = {
            "Name": "hermes",
            "State": "S (sleeping)",
            "PPid": "123",
            "Uid": "1000 1000 1000 1000",
        }

        def fake_read_proc_field(pid, key):
            assert pid == 4242
            return fields.get(key)

        def fake_read_proc_cmdline(pid):
            assert pid == 4242
            return long_cmdline

        monkeypatch.setattr(sf, "_read_proc_field", fake_read_proc_field)
        monkeypatch.setattr(sf, "_read_proc_cmdline", fake_read_proc_cmdline)

        summary = sf._proc_summary(4242)

        assert summary == {
            "pid": 4242,
            "name": "hermes",
            "state": "S (sleeping)",
            "ppid": 123,
            "uid": "1000",
            "cmdline": long_cmdline[:300],
        }
        assert len(summary["cmdline"]) == 300

    @pytest.mark.skipif(sys.platform == "win32", reason="Linux /proc not present")
    def test_real_read_smoke_for_current_process_on_linux(self):
        summary = sf._proc_summary(os.getpid())

        assert summary["pid"] == os.getpid()
        assert isinstance(summary.get("cmdline"), str)
        assert summary["cmdline"]

    def test_read_proc_helpers_missing_pid_return_none(self):
        missing_pid = 2_000_000_000

        assert sf._read_proc_field(missing_pid, "Name") is None
        assert sf._read_proc_cmdline(missing_pid) is None


# ---------------------------------------------------------------------------
# format_context_for_log / context_as_json
# ---------------------------------------------------------------------------

class TestFormatters:


    def test_context_as_json_handles_unserialisable_values(self):
        ctx = {"signal": "SIGTERM", "weird": object()}
        payload = sf.context_as_json(ctx)
        # default=str means objects get repr'd, JSON stays valid
        decoded = json.loads(payload)
        assert decoded["signal"] == "SIGTERM"
        assert "weird" in decoded


# ---------------------------------------------------------------------------
# spawn_async_diagnostic
# ---------------------------------------------------------------------------

class TestSpawnAsyncDiagnostic:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only diagnostic")
    def test_spawns_subprocess_and_writes_output(self, tmp_path):
        log_path = tmp_path / "diag.log"
        pid = sf.spawn_async_diagnostic(log_path, "SIGTERM", timeout_seconds=3.0)
        assert pid is not None and pid > 0

        # Wait briefly for the subprocess to write — bounded by its own timeout.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if log_path.exists() and log_path.stat().st_size > 0:
                # Wait a touch longer for the script to finish writing
                time.sleep(0.2)
                break
            time.sleep(0.1)

        # Reap the subprocess so it doesn't show up as a zombie.
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass

        assert log_path.exists()
        contents = log_path.read_text(encoding="utf-8", errors="replace")
        assert "shutdown diagnostic" in contents
        assert "SIGTERM" in contents


# ---------------------------------------------------------------------------
# _parse_systemd_duration_to_us
# ---------------------------------------------------------------------------

class TestParseSystemdDuration:
    def test_seconds(self):
        assert sf._parse_systemd_duration_to_us("90s") == 90 * 1_000_000

    def test_minutes(self):
        assert sf._parse_systemd_duration_to_us("3min") == 180 * 1_000_000


# ---------------------------------------------------------------------------
# check_systemd_timing_alignment
# ---------------------------------------------------------------------------



class TestParseSystemdDurationEdges:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2us", 2),
            ("90", 90_000_000),
            ("1.5s", 1_500_000),
            ("2sec", 2_000_000),
            ("1hr", 3_600_000_000),
        ],
    )
    def test_edge_formats(self, raw, expected):
        assert sf._parse_systemd_duration_to_us(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "12x"])
    def test_garbage_returns_none(self, raw):
        assert sf._parse_systemd_duration_to_us(raw) is None


class TestCheckSystemdTimingAlignment:

    def test_returns_none_when_unit_undeterminable(self, monkeypatch):
        monkeypatch.setenv("INVOCATION_ID", "abc")
        # /proc/self/cgroup likely doesn't end in .service for the test runner
        result = sf.check_systemd_timing_alignment(180.0)
        # Either None (we couldn't find a unit) or a dict with mismatch info
        # for whatever unit pytest IS in.  Both are valid; we just ensure
        # the function doesn't raise.
        assert result is None or isinstance(result, dict)

class TestTimingAlignmentBranches:
    @staticmethod
    def _patch_systemd_probe(monkeypatch, stdout):
        original_open = builtins.open
        calls = []

        def fake_open(path, *args, **kwargs):
            if path == "/proc/self/cgroup":
                return io.StringIO(
                    "0::/user.slice/user-1000.slice/user@1000.service/"
                    "app.slice/hermes-gateway.service\n"
                )
            return original_open(path, *args, **kwargs)

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": stdout},
            )()

        monkeypatch.setenv("INVOCATION_ID", "test-invocation")
        monkeypatch.setattr(builtins, "open", fake_open)
        monkeypatch.setattr(sf.subprocess, "run", fake_run)
        return calls

    def test_aligned_timeout_reports_mismatch_false(self, monkeypatch):
        calls = self._patch_systemd_probe(
            monkeypatch, "TimeoutStopUSec=90000000\n"
        )

        result = sf.check_systemd_timing_alignment(30.0)

        assert result is not None
        assert list(result) == [
            "unit",
            "timeout_stop_sec",
            "drain_timeout",
            "expected_min",
            "mismatch",
        ]
        assert result == {
            "unit": "hermes-gateway.service",
            "timeout_stop_sec": 90.0,
            "drain_timeout": 30.0,
            "expected_min": 60.0,
            "mismatch": False,
        }
        assert calls == [
            (
                [
                    "systemctl",
                    "--user",
                    "show",
                    "hermes-gateway.service",
                    "--property=TimeoutStopUSec",
                ],
                {"capture_output": True, "text": True, "timeout": 2.0},
            )
        ]

    def test_misaligned_timeout_reports_mismatch_true(self, monkeypatch):
        calls = self._patch_systemd_probe(
            monkeypatch, "TimeoutStopUSec=1min 30s\n"
        )

        result = sf.check_systemd_timing_alignment(120.0)

        assert result is not None
        assert list(result) == [
            "unit",
            "timeout_stop_sec",
            "drain_timeout",
            "expected_min",
            "mismatch",
        ]
        assert result == {
            "unit": "hermes-gateway.service",
            "timeout_stop_sec": 90.0,
            "drain_timeout": 120.0,
            "expected_min": 150.0,
            "mismatch": True,
        }
        assert len(calls) == 1
        assert calls[0][0][0] == "systemctl"
