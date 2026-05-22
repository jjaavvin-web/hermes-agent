"""Tests for the ISA completion gate wired into scripts/claude_kanban_bridge.py.

Covers ISC-24..29 of the isa-enforcement-layer ISA. The gate function
``_isa_gate(task_id)`` returns ``(allowed, reason)``; it is exercised directly.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


isa_common = _load("isa_common")
bridge = _load("claude_kanban_bridge")


def _e1_isa(card: str, phase: str, progress: str, criteria: str, verification: str) -> str:
    return f"""---
isa:      20260101-0000_gatefix
task:     "gate fixture"
tier:     E1
phase:    {phase}
progress: {progress}
card:     "{card}"
board:    "-"
branch:   b
hive:     "-"
owner:    claude
started:  2026-01-01T00:00:00Z
updated:  2026-01-01T00:00:00Z
---

## Goal
A real goal statement for the gate fixture.

## Criteria
{criteria}

## Verification
{verification}
"""


# A complete, isa_lint-clean E1 ISA.
_CLEAN_COMPLETE = _e1_isa(
    card="t_complete",
    phase="complete",
    progress="2/2",
    criteria="- [x] ISC-1: a real criterion\n- [x] ISC-2: Anti: a regression that did not happen",
    verification="ISC-1 verified — probe output ok.\nISC-2 verified — no regression observed.",
)


def _place(work_root: Path, slug: str, text: str) -> Path:
    """Write an ISA into <work_root>/<slug>/ISA.md."""
    d = work_root / slug
    d.mkdir(parents=True, exist_ok=True)
    isa = d / "ISA.md"
    isa.write_text(text, encoding="utf-8")
    return isa


def _work_root(tmp_path, monkeypatch) -> Path:
    """Point HERMES_HOME at tmp_path so find_isa_for_card scans tmp_path/work."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------
# ISC-24 — block when the linked ISA is not phase: complete
# --------------------------------------------------------------------------


def test_isc_24_blocks_when_isa_not_complete(tmp_path, monkeypatch):
    root = _work_root(tmp_path, monkeypatch)
    _place(
        root,
        "20260101-0000_incomplete",
        _e1_isa(
            card="t_incomplete",
            phase="execute",
            progress="0/2",
            criteria="- [ ] ISC-1: a criterion\n- [ ] ISC-2: Anti: a regression",
            verification="_(filled during verify)_",
        ),
    )
    allowed, reason = bridge._isa_gate("t_incomplete")
    assert allowed is False
    assert "execute" in reason and "complete" in reason


# --------------------------------------------------------------------------
# ISC-25 — block when phase: complete but isa_lint fails
# --------------------------------------------------------------------------


def test_isc_25_blocks_when_complete_but_lint_fails(tmp_path, monkeypatch):
    root = _work_root(tmp_path, monkeypatch)
    # phase: complete, yet ISC-2 is still open — isa_lint must reject it.
    _place(
        root,
        "20260101-0000_dishonest",
        _e1_isa(
            card="t_open",
            phase="complete",
            progress="1/2",
            criteria="- [x] ISC-1: a criterion\n- [ ] ISC-2: Anti: a regression",
            verification="ISC-1 verified — ok.",
        ),
    )
    allowed, reason = bridge._isa_gate("t_open")
    assert allowed is False
    assert "isa_lint" in reason


# --------------------------------------------------------------------------
# ISC-26 — allow when the linked ISA is complete and lint-clean
# --------------------------------------------------------------------------


def test_isc_26_allows_when_isa_complete_and_lint_clean(tmp_path, monkeypatch):
    root = _work_root(tmp_path, monkeypatch)
    _place(root, "20260101-0000_done", _CLEAN_COMPLETE)
    allowed, reason = bridge._isa_gate("t_complete")
    assert allowed is True
    assert reason == ""


# --------------------------------------------------------------------------
# ISC-27 — inert when the task has no linked ISA
# --------------------------------------------------------------------------


def test_isc_27_allows_when_no_isa_linked(tmp_path, monkeypatch):
    root = _work_root(tmp_path, monkeypatch)
    # An ISA exists, but it links a different card.
    _place(root, "20260101-0000_other", _CLEAN_COMPLETE)
    allowed, reason = bridge._isa_gate("t_a_card_with_no_isa")
    assert allowed is True
    assert reason == ""


# --------------------------------------------------------------------------
# ISC-28 — fail-open when ISA evaluation raises
# --------------------------------------------------------------------------


def test_isc_28_fails_open_on_evaluation_error(tmp_path, monkeypatch):
    _work_root(tmp_path, monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("simulated ISA tooling failure")

    # _isa_gate imports isa_common from sys.modules — patch the loaded module.
    monkeypatch.setattr(isa_common, "find_isa_for_card", _boom)
    allowed, reason = bridge._isa_gate("t_anything")
    assert allowed is True
    assert reason == ""


# --------------------------------------------------------------------------
# ISC-29 — the bridge module still imports and --help still works
# --------------------------------------------------------------------------


def test_isc_29_bridge_imports_and_help_works():
    # Import is proven by _load("claude_kanban_bridge") at module load above.
    assert callable(bridge._isa_gate)
    assert callable(bridge.main)
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS / "claude_kanban_bridge.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "task" in result.stdout.lower()
