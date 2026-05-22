"""Tests for scripts/isa_reconcile.py — ISC-15 through ISC-23.

Each test writes fixtures into tmp_path, calls isa_reconcile.main() or
isa_reconcile.reconcile() directly, and asserts on the resulting master
file / return code / slice files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


isa_common = _load("isa_common")
isa_reconcile = _load("isa_reconcile")


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

_FM_TEMPLATE = """\
---
isa:      20260522-1234_test-fixture
task:     "Test fixture ISA"
tier:     E1
phase:    scaffold
progress: {progress}
card:     "-"
board:    "-"
branch:   feat/test
hive:     "-"
owner:    claude
started:  2026-05-22T00:00:00Z
updated:  2026-05-22T00:00:00Z
---
"""


def _make_master(
    tmp_path: Path,
    iscs: list[tuple[str, str, str]],  # (id, state, text)
    verification_body: str = "",
    name: str = "master.md",
    total_override: int | None = None,
) -> Path:
    """Write a master ISA with the given ISCs and optional Verification body."""
    checked = sum(1 for _, s, _ in iscs if s == "x")
    total = total_override if total_override is not None else len(iscs)
    progress = f"{checked}/{total}"
    fm = _FM_TEMPLATE.format(progress=progress)

    criteria_lines = []
    for isc_id, state, text in iscs:
        criteria_lines.append(f"- [{state}] {isc_id}: {text}")
    criteria = "\n".join(criteria_lines)

    content = (
        fm
        + "\n## Goal\nTest fixture.\n\n"
        + "## Criteria\n"
        + criteria
        + "\n\n## Verification\n"
        + verification_body
    )

    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _make_slice(
    tmp_path: Path,
    iscs: list[tuple[str, str, str]],  # (id, state, text)
    verification_body: str = "",
    name: str = "slice.md",
) -> Path:
    """Write a slice ISA (same format, subset of ISCs)."""
    checked = sum(1 for _, s, _ in iscs if s == "x")
    total = len(iscs)
    progress = f"{checked}/{total}"
    fm = _FM_TEMPLATE.format(progress=progress)

    criteria_lines = []
    for isc_id, state, text in iscs:
        criteria_lines.append(f"- [{state}] {isc_id}: {text}")
    criteria = "\n".join(criteria_lines)

    content = (
        fm
        + "\n## Goal\nSlice fixture.\n\n"
        + "## Criteria\n"
        + criteria
        + "\n\n## Verification\n"
        + verification_body
    )

    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# ISC-15: copies checkbox state by ID
# ---------------------------------------------------------------------------

def test_isc_15_copies_checkbox_state_by_id(tmp_path):
    """Slice marks ISC-2 [x]; after reconcile the master's ISC-2 line is [x]."""
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first criterion"),
            ("ISC-2", " ", "second criterion"),
            ("ISC-3", " ", "Anti: must not happen"),
        ],
        name="master.md",
    )
    slice_f = _make_slice(
        tmp_path,
        iscs=[("ISC-2", "x", "second criterion")],
        name="slice.md",
    )

    rc = isa_reconcile.main([str(master), str(slice_f)])
    assert rc == 0

    result = master.read_text(encoding="utf-8")
    isa = isa_common.parse_isa_text(result)
    by_id = {i.id: i for i in isa.iscs}

    assert by_id["ISC-2"].state == "x", "ISC-2 should be checked"
    assert by_id["ISC-1"].state == " ", "ISC-1 should be unchanged (open)"
    assert by_id["ISC-3"].state == " ", "ISC-3 should be unchanged (open)"


# ---------------------------------------------------------------------------
# ISC-16: copies Verification block by ID
# ---------------------------------------------------------------------------

def test_isc_16_copies_verification_block_by_id(tmp_path):
    """Slice has a Verification block for ISC-2; master's ## Verification gets it."""
    slice_vblock = "ISC-2: probe output here\n  passed: yes\n"
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first"),
            ("ISC-2", " ", "second"),
        ],
        verification_body="",
        name="master.md",
    )
    slice_f = _make_slice(
        tmp_path,
        iscs=[("ISC-2", "x", "second")],
        verification_body=slice_vblock,
        name="slice.md",
    )

    rc = isa_reconcile.main([str(master), str(slice_f)])
    assert rc == 0

    result = master.read_text(encoding="utf-8")
    assert "ISC-2" in result
    # The block text should appear verbatim in the master's Verification section
    master_isa = isa_common.parse_isa_text(result)
    v_body = master_isa.section("Verification") or ""
    assert "probe output here" in v_body, (
        f"Expected slice verification text in master Verification, got:\n{v_body}"
    )


# ---------------------------------------------------------------------------
# ISC-17: aborts on slice ID absent from master
# ---------------------------------------------------------------------------

def test_isc_17_aborts_on_slice_id_absent_from_master(tmp_path):
    """Slice contains ISC-99 which is absent from master → rc != 0, master unchanged."""
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first"),
            ("ISC-2", " ", "second"),
        ],
        name="master.md",
    )
    before_bytes = master.read_bytes()

    slice_f = _make_slice(
        tmp_path,
        iscs=[
            ("ISC-1", "x", "first"),
            ("ISC-99", "x", "drifted criterion that master does not have"),
        ],
        name="slice.md",
    )

    rc = isa_reconcile.main([str(master), str(slice_f)])
    assert rc != 0, "Should return non-zero on drift"

    after_bytes = master.read_bytes()
    assert before_bytes == after_bytes, "Master must be byte-identical after abort"


# ---------------------------------------------------------------------------
# ISC-18: matches by ID not by position
# ---------------------------------------------------------------------------

def test_isc_18_matches_by_id_not_position(tmp_path):
    """Slice lists ISCs in different order than master; each master ISC gets
    the state of the slice ISC with its OWN id, not the one at the same position."""
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first"),
            ("ISC-2", " ", "second"),
            ("ISC-3", " ", "third"),
        ],
        name="master.md",
    )
    # Slice has ISC-3 then ISC-1 (different order), ISC-3 is [x], ISC-1 is [ ]
    slice_f = _make_slice(
        tmp_path,
        iscs=[
            ("ISC-3", "x", "third"),
            ("ISC-1", " ", "first"),
        ],
        name="slice.md",
    )

    rc = isa_reconcile.main([str(master), str(slice_f)])
    assert rc == 0

    result = master.read_text(encoding="utf-8")
    isa = isa_common.parse_isa_text(result)
    by_id = {i.id: i for i in isa.iscs}

    # ISC-3 (slice position 0, master position 2) should be [x]
    assert by_id["ISC-3"].state == "x", "ISC-3 should be [x] (matched by ID)"
    # ISC-1 (slice position 1, master position 0) should stay [ ]
    assert by_id["ISC-1"].state == " ", "ISC-1 should be [ ] (matched by ID)"
    # ISC-2 (absent from slice) should be unchanged [ ]
    assert by_id["ISC-2"].state == " ", "ISC-2 should be unchanged"


# ---------------------------------------------------------------------------
# ISC-19: idempotent
# ---------------------------------------------------------------------------

def test_isc_19_idempotent(tmp_path):
    """Running reconcile twice produces a byte-identical master after run 2."""
    slice_vblock = "ISC-1: all good\n  result: pass\n"
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first"),
            ("ISC-2", " ", "second"),
        ],
        verification_body="",
        name="master.md",
    )
    slice_f = _make_slice(
        tmp_path,
        iscs=[("ISC-1", "x", "first")],
        verification_body=slice_vblock,
        name="slice.md",
    )

    rc1 = isa_reconcile.main([str(master), str(slice_f)])
    assert rc1 == 0
    after_run1 = master.read_bytes()

    rc2 = isa_reconcile.main([str(master), str(slice_f)])
    assert rc2 == 0
    after_run2 = master.read_bytes()

    assert after_run1 == after_run2, (
        "Second reconcile run must produce byte-identical master file\n"
        f"Run 1 result:\n{after_run1.decode()}\n\nRun 2 result:\n{after_run2.decode()}"
    )


# ---------------------------------------------------------------------------
# ISC-20: recomputes progress
# ---------------------------------------------------------------------------

def test_isc_20_recomputes_progress(tmp_path):
    """Master starts at progress 0/4; slice marks 2 ISCs [x]; result is 2/4."""
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first"),
            ("ISC-2", " ", "second"),
            ("ISC-3", " ", "third"),
            ("ISC-4", " ", "Anti: fourth"),
        ],
        name="master.md",
    )
    # Verify initial progress
    raw = master.read_text(encoding="utf-8")
    assert "progress: 0/4" in raw, f"Expected progress: 0/4 in:\n{raw}"

    slice_f = _make_slice(
        tmp_path,
        iscs=[
            ("ISC-1", "x", "first"),
            ("ISC-3", "x", "third"),
        ],
        name="slice.md",
    )

    rc = isa_reconcile.main([str(master), str(slice_f)])
    assert rc == 0

    result = master.read_text(encoding="utf-8")
    assert "progress: 2/4" in result, (
        f"Expected 'progress: 2/4' in master after reconcile, got:\n{result}"
    )


# ---------------------------------------------------------------------------
# ISC-21: slice files are unchanged
# ---------------------------------------------------------------------------

def test_isc_21_slice_files_unchanged(tmp_path):
    """Each slice file must be byte-identical before and after reconcile."""
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first"),
            ("ISC-2", " ", "second"),
        ],
        name="master.md",
    )
    slice1 = _make_slice(
        tmp_path,
        iscs=[("ISC-1", "x", "first")],
        verification_body="ISC-1: verified\n",
        name="slice1.md",
    )
    slice2 = _make_slice(
        tmp_path,
        iscs=[("ISC-2", "x", "second")],
        verification_body="ISC-2: verified\n",
        name="slice2.md",
    )

    slice1_before = slice1.read_bytes()
    slice2_before = slice2.read_bytes()

    rc = isa_reconcile.main([str(master), str(slice1), str(slice2)])
    assert rc == 0

    assert slice1.read_bytes() == slice1_before, "slice1 must be byte-unchanged"
    assert slice2.read_bytes() == slice2_before, "slice2 must be byte-unchanged"


# ---------------------------------------------------------------------------
# ISC-22: master-only ISC is untouched
# ---------------------------------------------------------------------------

def test_isc_22_master_only_isc_untouched(tmp_path):
    """A master ISC absent from every slice is left byte-unchanged (state and text)."""
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first"),
            ("ISC-2", " ", "second"),
            ("ISC-3", " ", "third"),
            ("ISC-4", " ", "Anti: fourth — master only"),
        ],
        name="master.md",
    )

    # Capture the ISC-4 line before reconcile
    raw_before = master.read_text(encoding="utf-8")
    isc4_line_before = next(
        line for line in raw_before.splitlines() if "ISC-4" in line
    )

    # Slice only touches ISC-1 and ISC-2 — ISC-4 is absent from slice
    slice_f = _make_slice(
        tmp_path,
        iscs=[
            ("ISC-1", "x", "first"),
            ("ISC-2", "x", "second"),
        ],
        name="slice.md",
    )

    rc = isa_reconcile.main([str(master), str(slice_f)])
    assert rc == 0

    raw_after = master.read_text(encoding="utf-8")
    isc4_line_after = next(
        line for line in raw_after.splitlines() if "ISC-4" in line
    )

    assert isc4_line_before == isc4_line_after, (
        f"ISC-4 line must be byte-unchanged.\n"
        f"Before: {isc4_line_before!r}\n"
        f"After:  {isc4_line_after!r}"
    )


# ---------------------------------------------------------------------------
# ISC-23: --dry-run writes nothing
# ---------------------------------------------------------------------------

def test_isc_23_dry_run_writes_nothing(tmp_path, capsys):
    """--dry-run: master is byte-identical after, command prints a plan, rc=0."""
    master = _make_master(
        tmp_path,
        iscs=[
            ("ISC-1", " ", "first"),
            ("ISC-2", " ", "second"),
        ],
        verification_body="",
        name="master.md",
    )
    slice_f = _make_slice(
        tmp_path,
        iscs=[("ISC-1", "x", "first")],
        verification_body="ISC-1: all good\n",
        name="slice.md",
    )

    before_bytes = master.read_bytes()

    rc = isa_reconcile.main(["--dry-run", str(master), str(slice_f)])
    assert rc == 0, f"dry-run should return 0, got {rc}"

    after_bytes = master.read_bytes()
    assert before_bytes == after_bytes, "dry-run must not modify the master file"

    captured = capsys.readouterr()
    plan_output = captured.out
    assert plan_output.strip(), "dry-run must print a merge plan"
    # The plan should mention ISC ids
    assert "ISC-1" in plan_output, "merge plan should mention ISC-1"
