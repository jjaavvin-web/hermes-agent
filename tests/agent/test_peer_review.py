"""Tests for agent.peer_review (P2 — Opus pane pool orchestrator)."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from agent.peer_review import (
    PanePoolFailedToStart,
    PeerReviewOrchestrator,
    Verdict,
)


# ── fakes / helpers ──────────────────────────────────────────────────────


@dataclass
class _FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


# Regex to extract the verdict_path from the review invocation send-keys text.
# The invocation contains a path like /tmp/review-<session_id>.verdict.json.
_VERDICT_PATH_RE = re.compile(r"(/tmp/review-\S+\.verdict\.json)")


@dataclass
class _TmuxState:
    """Tracks the in-memory tmux session set + scripted capture-pane outputs.

    verdict_script maps pane_id -> verdict dict to write when the review
    invocation send-keys is detected for that pane.  Default (None) means
    write an APPROVE verdict.
    """
    sessions: set = field(default_factory=set)
    # capture_script is still used by _dialog_clear (startup "Enter to confirm").
    capture_script: dict = field(default_factory=dict)  # session -> [outputs]
    send_keys_log: list = field(default_factory=list)
    new_session_will_fail: set = field(default_factory=set)
    has_session_dead: set = field(default_factory=set)
    # pane_id -> verdict dict; None entry means use default APPROVE.
    verdict_script: dict = field(default_factory=dict)


def _make_fake_subprocess(tmux_state: _TmuxState):
    def fake_run(args, **kwargs):
        if not args or args[0] != "tmux":
            return _FakeProc(returncode=0)
        cmd = args[1] if len(args) > 1 else ""
        if cmd == "new-session":
            session = args[args.index("-s") + 1]
            if session in tmux_state.new_session_will_fail:
                return _FakeProc(returncode=1, stderr="forced fail")
            tmux_state.sessions.add(session)
            return _FakeProc(returncode=0)
        if cmd == "has-session":
            session = args[args.index("-t") + 1]
            if session in tmux_state.has_session_dead:
                return _FakeProc(returncode=1)
            return _FakeProc(returncode=0 if session in tmux_state.sessions else 1)
        if cmd == "capture-pane":
            session = args[args.index("-t") + 1]
            script = tmux_state.capture_script.get(session, [""])
            out = script.pop(0) if len(script) > 1 else script[0]
            return _FakeProc(returncode=0, stdout=out)
        if cmd == "send-keys":
            session = args[args.index("-t") + 1]
            # args after -t <session> are the key text(s).
            key_args = args[args.index("-t") + 2:]
            tmux_state.send_keys_log.append((session, key_args))
            # When the review invocation arrives, write the verdict file.
            # We detect it by looking for the verdict_path pattern in the text.
            text = " ".join(str(a) for a in key_args)
            m = _VERDICT_PATH_RE.search(text)
            if m:
                verdict_path = Path(m.group(1))
                # Use the pane-specific script or fall back to APPROVE.
                script_entry = tmux_state.verdict_script.get(session)
                if script_entry is None:
                    verdict_dict = {
                        "verdict": "APPROVE",
                        "summary": "looks good",
                        "comments": [],
                    }
                elif isinstance(script_entry, list):
                    # Pop the first entry if multiple rounds are scripted.
                    if len(script_entry) > 1:
                        verdict_dict = script_entry.pop(0)
                    else:
                        verdict_dict = script_entry[0]
                else:
                    verdict_dict = script_entry
                verdict_path.write_text(json.dumps(verdict_dict), encoding="utf-8")
            return _FakeProc(returncode=0)
        if cmd == "kill-session":
            session = args[args.index("-t") + 1]
            tmux_state.sessions.discard(session)
            return _FakeProc(returncode=0)
        if cmd == "pipe-pane":
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=0)
    return fake_run


async def _no_sleep(_seconds: float) -> None:
    return None


def _make_orchestrator(
    tmp_path: Path,
    tmux_state: _TmuxState,
    *,
    pool_size: int = 2,
    iteration_cap: int = 3,
    daily_cap: int = 10,
    review_timeout_sec: int = 300,
    idle_threshold_sec: int = 15,
    pane_recycle_after: int = 50,
) -> PeerReviewOrchestrator:
    return PeerReviewOrchestrator(
        hermes_home=tmp_path,
        pool_size=pool_size,
        iteration_cap=iteration_cap,
        daily_cap=daily_cap,
        review_timeout_sec=review_timeout_sec,
        idle_threshold_sec=idle_threshold_sec,
        pane_recycle_after=pane_recycle_after,
        subprocess_run=_make_fake_subprocess(tmux_state),
        sleep_async=_no_sleep,
    )


# ── start / stop ─────────────────────────────────────────────────────────


class TestStart:
    @pytest.mark.asyncio
    async def test_spawns_pool_size_panes(self, tmp_path):
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", "", ""],
        }
        orch = _make_orchestrator(tmp_path, tmux, pool_size=2)
        await orch.start()
        assert "codex-review-0" in tmux.sessions
        assert "codex-review-1" in tmux.sessions

    @pytest.mark.asyncio
    async def test_raises_if_all_panes_fail(self, tmp_path):
        tmux = _TmuxState()
        tmux.new_session_will_fail = {"codex-review-0", "codex-review-1"}
        orch = _make_orchestrator(tmp_path, tmux, pool_size=2)
        with pytest.raises(PanePoolFailedToStart):
            await orch.start()


# ── verdict delivery via file handoff ────────────────────────────────────


class TestVerdictFileHandoff:
    @pytest.mark.asyncio
    async def test_approve_picked_up(self, tmp_path):
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        tmux.verdict_script = {
            "codex-review-0": {
                "verdict": "APPROVE",
                "summary": "looks good",
                "comments": [],
            },
        }
        orch = _make_orchestrator(tmp_path, tmux, idle_threshold_sec=0)
        await orch.start()
        v = await orch.review(
            session_id="sid-a",
            isa_path=tmp_path / "missing.md",
            diff="trivial diff",
        )
        assert v.kind == "APPROVE"
        assert "looks good" in v.rationale

    @pytest.mark.asyncio
    async def test_revise_picked_up(self, tmp_path):
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        tmux.verdict_script = {
            "codex-review-0": {
                "verdict": "REVISE",
                "summary": "needs work",
                "comments": ["Missing input validation on line 42."],
            },
        }
        orch = _make_orchestrator(tmp_path, tmux, idle_threshold_sec=0)
        await orch.start()
        v = await orch.review(
            session_id="sid-b",
            isa_path=tmp_path / "missing.md",
            diff="diff body",
        )
        assert v.kind == "REVISE"
        assert "line 42" in v.rationale

    @pytest.mark.asyncio
    async def test_escalate_picked_up(self, tmp_path):
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        tmux.verdict_script = {
            "codex-review-0": {
                "verdict": "ESCALATE",
                "summary": "fundamental scope drift",
                "comments": ["completely wrong approach"],
            },
        }
        orch = _make_orchestrator(tmp_path, tmux, idle_threshold_sec=0)
        await orch.start()
        v = await orch.review(
            session_id="sid-esc",
            isa_path=tmp_path / "missing.md",
            diff="diff",
        )
        assert v.kind == "ESCALATE"
        assert "fundamental scope drift" in v.rationale

    @pytest.mark.asyncio
    async def test_verdict_path_extracted_from_send_keys(self, tmp_path):
        """Verify the fake correctly extracts verdict_path from the invocation text."""
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        # No explicit verdict_script — defaults to APPROVE.
        orch = _make_orchestrator(tmp_path, tmux, idle_threshold_sec=0)
        await orch.start()
        v = await orch.review(
            session_id="sid-extract",
            isa_path=tmp_path / "missing.md",
            diff="d",
        )
        assert v.kind == "APPROVE"
        # Confirm the invocation send-keys contained the verdict path pattern.
        invocation_calls = [
            (sess, keys) for sess, keys in tmux.send_keys_log
            if any(_VERDICT_PATH_RE.search(str(k)) for k in keys)
        ]
        assert invocation_calls, "No send-keys with verdict path found"


# ── _read_verdict_file unit tests ─────────────────────────────────────────


class TestReadVerdictFile:
    """Direct unit tests for PeerReviewOrchestrator._read_verdict_file."""

    def _make_pane_and_state(self):
        from agent.peer_review import _Pane, _ReviewState
        pane = _Pane(pane_id="codex-review-0", state="BUSY")
        state = _ReviewState(iterations=1)
        return pane, state

    def test_valid_approve_json(self, tmp_path):
        orch = _make_orchestrator(tmp_path, _TmuxState())
        pane, state = self._make_pane_and_state()
        vf = tmp_path / "verdict.json"
        vf.write_text(json.dumps({
            "verdict": "APPROVE",
            "summary": "all good",
            "comments": [],
        }))
        import time
        result = orch._read_verdict_file(vf, pane, state, time.monotonic())
        assert result is not None
        assert result.kind == "APPROVE"
        assert "all good" in result.rationale
        assert result.iteration == 1

    def test_valid_revise_with_comments(self, tmp_path):
        orch = _make_orchestrator(tmp_path, _TmuxState())
        pane, state = self._make_pane_and_state()
        vf = tmp_path / "verdict.json"
        vf.write_text(json.dumps({
            "verdict": "REVISE",
            "summary": "issues found",
            "comments": ["fix import", "add tests"],
        }))
        import time
        result = orch._read_verdict_file(vf, pane, state, time.monotonic())
        assert result is not None
        assert result.kind == "REVISE"
        assert "fix import" in result.rationale
        assert "add tests" in result.rationale

    def test_empty_file_returns_none(self, tmp_path):
        orch = _make_orchestrator(tmp_path, _TmuxState())
        pane, state = self._make_pane_and_state()
        vf = tmp_path / "verdict.json"
        vf.write_text("")
        import time
        result = orch._read_verdict_file(vf, pane, state, time.monotonic())
        assert result is None

    def test_malformed_json_returns_none(self, tmp_path):
        orch = _make_orchestrator(tmp_path, _TmuxState())
        pane, state = self._make_pane_and_state()
        vf = tmp_path / "verdict.json"
        vf.write_text('{"verdict": "APPROVE"')  # truncated — mid-write
        import time
        result = orch._read_verdict_file(vf, pane, state, time.monotonic())
        assert result is None

    def test_unrecognised_verdict_value_returns_escalate(self, tmp_path):
        orch = _make_orchestrator(tmp_path, _TmuxState())
        pane, state = self._make_pane_and_state()
        vf = tmp_path / "verdict.json"
        vf.write_text(json.dumps({
            "verdict": "YOLO",
            "summary": "bad value",
            "comments": [],
        }))
        import time
        result = orch._read_verdict_file(vf, pane, state, time.monotonic())
        assert result is not None
        assert result.kind == "ESCALATE"
        assert "YOLO" in result.rationale

    def test_missing_file_returns_none(self, tmp_path):
        orch = _make_orchestrator(tmp_path, _TmuxState())
        pane, state = self._make_pane_and_state()
        vf = tmp_path / "nonexistent.json"
        import time
        result = orch._read_verdict_file(vf, pane, state, time.monotonic())
        assert result is None


# ── caps ────────────────────────────────────────────────────────────────


class TestIterationCap:
    @pytest.mark.asyncio
    async def test_4th_verify_after_3_revises_auto_escalates_without_pane(self, tmp_path):
        """ISC-10: 4th verify for same sid after 3 REVISE → auto ESCALATE without claiming pane."""
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        orch = _make_orchestrator(tmp_path, tmux, iteration_cap=3, idle_threshold_sec=0)
        await orch.start()
        for i in range(3):
            tmux.verdict_script["codex-review-0"] = {
                "verdict": "REVISE",
                "summary": f"round {i}",
                "comments": [],
            }
            tmux.verdict_script["codex-review-1"] = {
                "verdict": "REVISE",
                "summary": f"round {i}",
                "comments": [],
            }
            v = await orch.review(
                session_id="sid-loop",
                isa_path=tmp_path / "missing.md",
                diff="d",
            )
            assert v.kind == "REVISE"

        send_calls_before = len(tmux.send_keys_log)
        v = await orch.review(
            session_id="sid-loop",
            isa_path=tmp_path / "missing.md",
            diff="d",
        )
        assert v.kind == "ESCALATE"
        assert "iteration cap" in v.rationale.lower()
        assert v.pane_id == "(no pane)"
        # No new send-keys — pane was not claimed for the auto-escalate.
        assert len(tmux.send_keys_log) == send_calls_before


class TestDailyCap:
    @pytest.mark.asyncio
    async def test_over_daily_cap_escalates_without_pane(self, tmp_path):
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        orch = _make_orchestrator(tmp_path, tmux, daily_cap=2, idle_threshold_sec=0)
        await orch.start()
        # Two reviews under the cap.
        for _ in range(2):
            tmux.verdict_script["codex-review-0"] = {
                "verdict": "APPROVE",
                "summary": "ok",
                "comments": [],
            }
            tmux.verdict_script["codex-review-1"] = {
                "verdict": "APPROVE",
                "summary": "ok",
                "comments": [],
            }
            v = await orch.review(
                session_id="sid-cap",
                isa_path=tmp_path / "missing.md",
                diff="d",
            )
            assert v.kind == "APPROVE"

        v = await orch.review(
            session_id="sid-cap",
            isa_path=tmp_path / "missing.md",
            diff="d",
        )
        assert v.kind == "ESCALATE"
        assert "daily review cap" in v.rationale.lower()

    @pytest.mark.asyncio
    async def test_day_rollover_resets_counter(self, tmp_path):
        # Seed state file with yesterday's counter at the cap.
        state_path = tmp_path / "codex-review-state.json"
        state_path.write_text(json.dumps({
            "version": 1,
            "sessions": {
                "sid-roll": {
                    "iterations": 0,
                    "reviews_today": 10,
                    "day_started": "1999-01-01",
                    "last_verdict": "APPROVE",
                    "last_review_at": "1999-01-01T00:00:00+00:00",
                },
            },
        }))

        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        tmux.verdict_script = {
            "codex-review-0": {
                "verdict": "APPROVE",
                "summary": "fresh day",
                "comments": [],
            },
        }
        orch = _make_orchestrator(tmp_path, tmux, daily_cap=10, idle_threshold_sec=0)
        await orch.start()
        v = await orch.review(
            session_id="sid-roll",
            isa_path=tmp_path / "missing.md",
            diff="d",
        )
        # Should NOT escalate — day rolled over, counter reset.
        assert v.kind == "APPROVE"


# ── state persistence ──────────────────────────────────────────────────


class TestStatePersistence:
    @pytest.mark.asyncio
    async def test_review_writes_counter(self, tmp_path):
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        tmux.verdict_script = {
            "codex-review-0": {
                "verdict": "REVISE",
                "summary": "feedback",
                "comments": [],
            },
        }
        orch = _make_orchestrator(tmp_path, tmux, idle_threshold_sec=0)
        await orch.start()
        await orch.review(
            session_id="sid-write",
            isa_path=tmp_path / "missing.md",
            diff="d",
        )

        state = json.loads((tmp_path / "codex-review-state.json").read_text())
        entry = state["sessions"]["sid-write"]
        assert entry["iterations"] == 1
        assert entry["reviews_today"] == 1
        assert entry["last_verdict"] == "REVISE"

    @pytest.mark.asyncio
    async def test_approve_resets_iterations(self, tmp_path):
        """APPROVE clears the REVISE counter so a session can re-enter the loop."""
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        orch = _make_orchestrator(tmp_path, tmux, idle_threshold_sec=0)
        await orch.start()

        tmux.verdict_script["codex-review-0"] = {
            "verdict": "REVISE",
            "summary": "try again",
            "comments": [],
        }
        await orch.review(session_id="sid-r", isa_path=tmp_path / "x.md", diff="d")
        tmux.verdict_script["codex-review-1"] = {
            "verdict": "APPROVE",
            "summary": "ok",
            "comments": [],
        }
        await orch.review(session_id="sid-r", isa_path=tmp_path / "x.md", diff="d")

        state = json.loads((tmp_path / "codex-review-state.json").read_text())
        assert state["sessions"]["sid-r"]["iterations"] == 0


# ── pane death ─────────────────────────────────────────────────────────


class TestPaneDeathMidReview:
    @pytest.mark.asyncio
    async def test_pane_dying_mid_review_escalates(self, tmp_path):
        """ISC-13: pane death mid-review → ESCALATE, pane marked DEAD."""
        tmux = _TmuxState()
        tmux.capture_script = {
            "codex-review-0": ["Enter to confirm", "", "", ""],
            "codex-review-1": ["Enter to confirm", "", "", ""],
        }
        orch = _make_orchestrator(tmp_path, tmux, idle_threshold_sec=0)
        await orch.start()
        # The pane will have its has-session probe FAIL on the next poll.
        # Do NOT write a verdict file — the poll loop should detect the dead pane.
        tmux.has_session_dead.add("codex-review-0")
        # Override the fake so it does NOT write a verdict file for this pane.
        # We achieve this by removing codex-review-0 from verdict_script and
        # relying on the has_session_dead check firing before has-session returns 0.
        # The default fake_run writes on send-keys invocation, but has_session_dead
        # will cause the poll loop to ESCALATE before any file appears.
        # We prevent file write by patching verdict_script to a sentinel that
        # tells the fake not to write.
        tmux.verdict_script["codex-review-0"] = None
        # Restore the default to None so no file is written — but wait,
        # the default (None) currently writes APPROVE. We need to intercept.
        # Use a special sentinel value: set to an empty dict to avoid writing.
        # Actually: the send-keys fake writes on receipt of the invocation,
        # which happens BEFORE the poll loop starts. By the time the poll
        # loop runs, the pane is dead (has_session_dead) so it should ESCALATE
        # even if a file was written.
        # The pane-death check comes BEFORE the file check in the poll loop,
        # so has_session_dead fires first.

        v = await orch.review(
            session_id="sid-dead",
            isa_path=tmp_path / "missing.md",
            diff="d",
        )
        # Either pre-dispatch or mid-poll detection — both ESCALATE.
        assert v.kind == "ESCALATE"
        assert "dead" in v.rationale.lower() or "died" in v.rationale.lower()


# ── diff summarization ────────────────────────────────────────────────


class TestDiffSummarization:
    def test_small_diff_is_passed_through(self, tmp_path):
        orch = _make_orchestrator(tmp_path, _TmuxState())
        small = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new"
        out, truncated = orch._maybe_summarize_diff(small)
        assert out == small
        assert not truncated

    def test_large_diff_is_summarized(self, tmp_path):
        orch = _make_orchestrator(tmp_path, _TmuxState())
        # Realistic multi-line diff: 8 file hunks, 400 context lines each.
        chunks: list[str] = []
        for f in range(8):
            chunks.append(f"diff --git a/file{f}.py b/file{f}.py")
            chunks.append(f"--- a/file{f}.py")
            chunks.append(f"+++ b/file{f}.py")
            chunks.append("@@ -1,400 +1,400 @@")
            chunks.extend([f"context line {i}" for i in range(400)])
        big = "\n".join(chunks)
        assert len(big) > 20480

        out, truncated = orch._maybe_summarize_diff(big)
        assert truncated
        assert len(out) < len(big)
        # Must keep diff headers + first few context lines per hunk.
        assert "diff --git a/file0.py" in out
        assert "diff --git a/file4.py" in out
        assert "@@ -1,400 +1,400 @@" in out


# ── anti probes ───────────────────────────────────────────────────────


class TestAntiProbes:
    def test_no_claude_p_or_agent_sdk_in_module(self):
        """ISC-12: NO `claude -p`, `--print`, `--non-interactive`, Agent SDK
        invocation in agent/peer_review.py."""
        import agent.peer_review as _module
        src = Path(_module.__file__).read_text(encoding="utf-8")
        # Strip docstrings / comments referencing the forbidden APIs by context.
        # The actual check is that no actual invocation exists.
        # Acceptable: doctring mentions for explanation. We check that the
        # forbidden strings do not appear as code.
        import re as _re
        # Remove docstrings.
        cleaned = _re.sub(r'"""[\s\S]*?"""', '', src)
        # Forbidden tokens (treated as identifiers that would only appear in real calls):
        for forbidden in (
            "claude_code_sdk",
            "anthropic.AsyncAnthropic",
            "anthropic.Anthropic(",
        ):
            assert forbidden not in cleaned, f"forbidden token {forbidden!r} in module"
        # `--print` would only appear in subprocess args — we use only tmux,
        # so its absence is the assertion.
        assert "--print" not in cleaned
        assert "--non-interactive" not in cleaned

    def test_no_dead_regex_machinery(self):
        """Confirm the dead sentinel/regex globals are gone from the module."""
        import agent.peer_review as _module
        assert not hasattr(_module, "_VERDICT_SENTINEL"), \
            "_VERDICT_SENTINEL should be deleted"
        assert not hasattr(_module, "_VERDICT_PATTERN"), \
            "_VERDICT_PATTERN should be deleted"
        assert not hasattr(_module, "_FUZZY_VERDICT_PATTERN"), \
            "_FUZZY_VERDICT_PATTERN should be deleted"
        assert not hasattr(_module, "_canonicalize_fuzzy_verdict"), \
            "_canonicalize_fuzzy_verdict should be deleted"

    def test_opus_model_in_spawn_command(self):
        """Verify --model opus is present in _spawn_pane command."""
        import agent.peer_review as _module
        src = Path(_module.__file__).read_text(encoding="utf-8")
        assert '"--model", "opus"' in src or "'--model', 'opus'" in src, \
            "--model opus not found in _spawn_pane"

    def test_write_tool_in_allowed_tools(self):
        """Verify Write is in --allowed-tools in _spawn_pane command."""
        import agent.peer_review as _module
        src = Path(_module.__file__).read_text(encoding="utf-8")
        assert "Read,Bash,Write" in src, \
            "Write not found in --allowed-tools in _spawn_pane"
