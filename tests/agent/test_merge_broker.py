"""Tests for agent.merge_broker (P3)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent.merge_broker import (
    ConflictEscalation,
    MergeBroker,
    MergeResult,
)


@dataclass
class _FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _Script:
    """Drives the subprocess_run mock — pop a response per command."""
    by_cmd: dict = field(default_factory=dict)  # first arg of cmd → response
    default: _FakeProc = field(default_factory=lambda: _FakeProc())
    log: list = field(default_factory=list)

    def __call__(self, cmd, **kwargs):
        self.log.append(cmd)
        # Match by joined command for clearer dispatch, falling back to cmd[0].
        for key in (" ".join(cmd[:3]), cmd[0]):
            if key in self.by_cmd:
                resp = self.by_cmd[key]
                if isinstance(resp, list):
                    if not resp:
                        return self.default
                    return resp.pop(0)
                return resp
        return self.default


def _make_broker(tmp_path, script: _Script) -> MergeBroker:
    return MergeBroker(
        hermes_home=tmp_path,
        subprocess_run=script,
    )


# ── classify_change ─────────────────────────────────────────────────────


class TestClassifyChange:
    def test_safe_when_only_docs(self, tmp_path):
        script = _Script(by_cmd={
            "git": _FakeProc(returncode=0, stdout="README.md\ndocs/notes.md\n"),
        })
        broker = _make_broker(tmp_path, script)
        assert broker.classify_change(tmp_path / "wt") == "safe"

    def test_sensitive_on_agent_dir(self, tmp_path):
        script = _Script(by_cmd={
            "git": _FakeProc(returncode=0, stdout="README.md\nagent/foo.py\n"),
        })
        broker = _make_broker(tmp_path, script)
        assert broker.classify_change(tmp_path / "wt") == "sensitive"

    def test_sensitive_on_package_lock(self, tmp_path):
        script = _Script(by_cmd={
            "git": _FakeProc(returncode=0, stdout="package-lock.json\n"),
        })
        broker = _make_broker(tmp_path, script)
        assert broker.classify_change(tmp_path / "wt") == "sensitive"

    def test_sensitive_on_workflow_change(self, tmp_path):
        script = _Script(by_cmd={
            "git": _FakeProc(returncode=0, stdout=".github/workflows/test.yml\n"),
        })
        broker = _make_broker(tmp_path, script)
        assert broker.classify_change(tmp_path / "wt") == "sensitive"

    def test_diff_failure_defaults_to_sensitive(self, tmp_path):
        script = _Script(by_cmd={
            "git": _FakeProc(returncode=1, stderr="not a git repo"),
        })
        broker = _make_broker(tmp_path, script)
        assert broker.classify_change(tmp_path / "wt") == "sensitive"


# ── merge happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_safe_change_opens_pr_with_auto_merge_label(tmp_path):
    isa = tmp_path / "isa.md"
    isa.write_text(
        "---\nphase: complete\nprogress: 5/5\n---\n## Problem\nx",
        encoding="utf-8",
    )
    script = _Script(by_cmd={
        # git fetch / rebase / push / diff
        "git": [
            _FakeProc(returncode=0),  # fetch
            _FakeProc(returncode=0),  # rebase
            _FakeProc(returncode=0),  # push
            _FakeProc(returncode=0, stdout="README.md\n"),  # diff --name-only
        ],
        # isa_lint
        "python3": _FakeProc(returncode=0, stdout="PASS: isa.md\n"),
        # gh pr list (no existing PR), gh pr create, gh pr edit
        "gh": [
            _FakeProc(returncode=0, stdout="[]"),  # list
            _FakeProc(returncode=0,
                       stdout="https://github.com/jjaavvin-web/hermes-agent/pull/123\n"),
            _FakeProc(returncode=0),  # edit add-label
        ],
    })
    broker = _make_broker(tmp_path, script)
    result = await broker.merge(
        session_id="sid-abc",
        worktree=tmp_path / "wt",
        branch="codex/sid-abc/test",
        isa_path=isa,
        summary="looks good",
    )
    assert result.ok is True
    assert result.pr_number == 123
    assert result.pr_url.endswith("/123")
    assert result.classification == "safe"
    # Verify the label call was made with auto-merge.
    label_calls = [c for c in script.log if c[0] == "gh" and "edit" in c and "--add-label" in c]
    assert label_calls
    assert "auto-merge" in label_calls[-1]


@pytest.mark.asyncio
async def test_merge_sensitive_change_labels_needs_human(tmp_path):
    isa = tmp_path / "isa.md"
    isa.write_text(
        "---\nphase: complete\nprogress: 5/5\n---\n## Problem\nx",
        encoding="utf-8",
    )
    script = _Script(by_cmd={
        "git": [
            _FakeProc(returncode=0),  # fetch
            _FakeProc(returncode=0),  # rebase
            _FakeProc(returncode=0),  # push
            _FakeProc(returncode=0, stdout="agent/runtime.py\n"),  # diff
        ],
        "python3": _FakeProc(returncode=0),
        "gh": [
            _FakeProc(returncode=0, stdout="[]"),
            _FakeProc(returncode=0,
                       stdout="https://github.com/jjaavvin-web/hermes-agent/pull/124\n"),
            _FakeProc(returncode=0),
        ],
    })
    broker = _make_broker(tmp_path, script)
    result = await broker.merge(
        session_id="sid-x", worktree=tmp_path / "wt",
        branch="codex/sid-x/test", isa_path=isa, summary="ok",
    )
    assert result.ok is True
    assert result.classification == "sensitive"
    label_calls = [c for c in script.log if c[0] == "gh" and "edit" in c]
    assert any("needs-human" in c for c in label_calls)


# ── failure modes ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rebase_conflict_returns_conflict_error(tmp_path):
    isa = tmp_path / "isa.md"
    isa.write_text("---\nphase: complete\n---\n## Problem\nx", encoding="utf-8")
    script = _Script(by_cmd={
        "git": [
            _FakeProc(returncode=0),  # fetch
            _FakeProc(returncode=1, stderr="CONFLICT (content): merge conflict"),  # rebase
            _FakeProc(returncode=0),  # rebase --abort
        ],
    })
    broker = _make_broker(tmp_path, script)
    result = await broker.merge(
        session_id="sid-c", worktree=tmp_path / "wt",
        branch="codex/sid-c/test", isa_path=isa, summary="x",
    )
    assert result.ok is False
    assert "conflict" in result.error.lower()


@pytest.mark.asyncio
async def test_isa_lint_failure_returns_lint_error(tmp_path):
    isa = tmp_path / "isa.md"
    isa.write_text("---\nphase: execute\n---\n", encoding="utf-8")
    script = _Script(by_cmd={
        "git": [
            _FakeProc(returncode=0),  # fetch
            _FakeProc(returncode=0),  # rebase
        ],
        "python3": _FakeProc(returncode=1, stdout="FAIL: missing section"),
    })
    broker = _make_broker(tmp_path, script)
    result = await broker.merge(
        session_id="sid-l", worktree=tmp_path / "wt",
        branch="codex/sid-l/test", isa_path=isa, summary="x",
    )
    assert result.ok is False
    assert "isa_lint" in result.error.lower()


@pytest.mark.asyncio
async def test_existing_pr_is_not_recreated(tmp_path):
    """gh pr list returns an existing PR — broker reuses it."""
    isa = tmp_path / "isa.md"
    isa.write_text("---\nphase: complete\n---\n## Problem\nx", encoding="utf-8")
    script = _Script(by_cmd={
        "git": [
            _FakeProc(returncode=0),  # fetch
            _FakeProc(returncode=0),  # rebase
            _FakeProc(returncode=0),  # push
            _FakeProc(returncode=0, stdout="README.md\n"),  # diff
        ],
        "python3": _FakeProc(returncode=0),
        "gh": [
            _FakeProc(returncode=0,
                       stdout='[{"number": 99, "url": "https://github.com/x/y/pull/99"}]'),
            _FakeProc(returncode=0),  # edit add-label
        ],
    })
    broker = _make_broker(tmp_path, script)
    result = await broker.merge(
        session_id="sid-i", worktree=tmp_path / "wt",
        branch="codex/sid-i/test", isa_path=isa, summary="x",
    )
    assert result.ok is True
    assert result.pr_number == 99
    # Only ONE gh call before the label edit — no `gh pr create`.
    create_calls = [c for c in script.log if c[0] == "gh" and "create" in c]
    assert create_calls == []


# ── anti probes ────────────────────────────────────────────────────────


class TestAntiProbes:
    def test_no_force_push_in_module(self):
        import agent.merge_broker as _module
        src = Path(_module.__file__).read_text(encoding="utf-8")
        import re
        cleaned = re.sub(r'"""[\s\S]*?"""', '', src)
        for forbidden in (
            "push --force",
            "--force-with-lease",
            "push -f",
        ):
            assert forbidden not in cleaned, f"forbidden token {forbidden!r} in module"

    def test_no_no_verify_in_module(self):
        import agent.merge_broker as _module
        src = Path(_module.__file__).read_text(encoding="utf-8")
        import re
        cleaned = re.sub(r'"""[\s\S]*?"""', '', src)
        for forbidden in ("--no-verify", "--no-gpg-sign"):
            assert forbidden not in cleaned
