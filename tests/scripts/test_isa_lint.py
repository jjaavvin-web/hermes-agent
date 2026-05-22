"""Tests for scripts/isa_lint.py — the ISA CheckCompleteness gate (ISA-SPEC §9).

ISC-2 through ISC-14 plus ISC-36 and ISC-37 of the isa-enforcement-layer ISA
are verified here. Run individual criteria with:  pytest -k isc_NN
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


isa_common = _load("isa_common")
isa_lint = _load("isa_lint")

parse_isa_text = isa_common.parse_isa_text
lint = isa_lint.lint

# --------------------------------------------------------------------------
# Baseline fixture — lints clean at phase: execute
# --------------------------------------------------------------------------

BASELINE = """\
---
isa:      20260101-0000_fixture
task:     "Fixture"
tier:     E3
phase:    execute
progress: 1/3
card:     "-"
board:    "-"
branch:   feat/fixture
hive:     "-"
owner:    claude
started:  2026-01-01T00:00:00Z
updated:  2026-01-01T00:00:00Z
---

## Problem
A fixture problem statement with real content.

## Goal
A fixture goal paragraph describing the end state.

## Out of Scope
- Nothing extra.

## Constraints
- Standard library only.

## Criteria
- [x] ISC-1: the first criterion
- [ ] ISC-2: Anti: a regression must not happen
- [ ] ISC-3: the third criterion

## Test Strategy
| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | run one | ok |
| ISC-2 | run two | ok |
| ISC-3 | run three | ok |

## Git Plan
- Branch feat/fixture; commit; push.

## Decisions
- 2026-01-01: a decision was made.

## Changelog
_(none — no corrections were needed.)_

## Verification
**ISC-1** — probe output: ok

## Handback
- Record a completion note.
"""


def valid_isa(**kw) -> str:
    """Return BASELINE with frontmatter/body substitutions applied.

    Keyword arguments may replace a frontmatter value (by matching
    'key:     <old>' patterns) or a body substring.  Pass the key name
    as keyword and the replacement string as value.

    For targeted replacements where the auto-match is ambiguous, callers
    can pass a two-tuple (old, new) as the value to do a literal replace.
    """
    text = BASELINE
    for key, val in kw.items():
        if isinstance(val, tuple):
            old, new = val
            text = text.replace(old, new, 1)
        else:
            # Replace frontmatter line: "key:   <anything>\n"
            import re
            text = re.sub(
                r"(?m)^" + re.escape(key) + r":[ \t]+.*$",
                f"{key}:     {val}",
                text,
            )
    return text


# --------------------------------------------------------------------------
# Sanity guard — baseline must lint clean before any mutation tests run
# --------------------------------------------------------------------------


def test_baseline_lints_clean():
    result = lint(parse_isa_text(BASELINE))
    assert result.ok is True, f"Baseline should lint clean; failures: {result.failures}"


# --------------------------------------------------------------------------
# ISC-2 — valid complete real ISA lints clean
# --------------------------------------------------------------------------

_PILOT_ISA = Path("/home/josep/.hermes/work/20260522-1412_dashboard-token-persist/ISA.md")


@pytest.mark.skipif(not _PILOT_ISA.exists(), reason="pilot ISA not present")
def test_isc_02_valid_complete_real_isa():
    result = lint(_PILOT_ISA)
    assert result.ok is True, f"Pilot ISA should lint clean; failures: {result.failures}"


# --------------------------------------------------------------------------
# ISC-3 — missing mandatory section
# --------------------------------------------------------------------------


def test_isc_03_missing_mandatory_section():
    # Remove the ## Git Plan section entirely.
    text = BASELINE.replace("## Git Plan\n- Branch feat/fixture; commit; push.\n", "")
    result = lint(parse_isa_text(text))
    assert result.ok is False
    assert any("Git Plan" in f for f in result.failures), (
        f"Expected a failure naming 'Git Plan'; got: {result.failures}"
    )


# --------------------------------------------------------------------------
# ISC-4 — ISC without Test Strategy row (E2+)
# --------------------------------------------------------------------------


def test_isc_04_isc_without_test_strategy_row():
    # Add ISC-4 to Criteria but no row for it in Test Strategy.
    text = BASELINE.replace(
        "- [ ] ISC-3: the third criterion",
        "- [ ] ISC-3: the third criterion\n- [ ] ISC-4: another criterion",
    )
    result = lint(parse_isa_text(text))
    assert result.ok is False
    assert any("ISC-4" in f for f in result.failures), (
        f"Expected a failure mentioning ISC-4; got: {result.failures}"
    )


# --------------------------------------------------------------------------
# ISC-5 — zero Anti: ISCs
# --------------------------------------------------------------------------


def test_isc_05_zero_anti_iscs():
    # ISC-2's text normally starts with "Anti:".  Remove that prefix.
    text = BASELINE.replace(
        "- [ ] ISC-2: Anti: a regression must not happen",
        "- [ ] ISC-2: a regression must not happen",
    )
    result = lint(parse_isa_text(text))
    assert result.ok is False
    assert any("Anti" in f for f in result.failures), (
        f"Expected a failure about missing Anti ISC; got: {result.failures}"
    )


# --------------------------------------------------------------------------
# ISC-6 — checked ISC without Verification mention
# --------------------------------------------------------------------------


def test_isc_06_checked_isc_without_verification():
    # Mark ISC-3 as checked and bump progress to 2/3; add no ISC-3 mention.
    text = BASELINE.replace(
        "- [ ] ISC-3: the third criterion",
        "- [x] ISC-3: the third criterion",
    )
    text = text.replace("progress:     1/3", "progress:     2/3")
    result = lint(parse_isa_text(text))
    assert result.ok is False
    # Confirm the failure specifically names ISC-3 + verification.
    verification_failures = [
        f for f in result.failures
        if "ISC-3" in f and "Verification" in f
    ]
    assert verification_failures, (
        f"Expected a failure for ISC-3 missing Verification; got: {result.failures}"
    )


# --------------------------------------------------------------------------
# ISC-7 — progress count mismatch
# --------------------------------------------------------------------------


def test_isc_07_progress_count_mismatch():
    # Claim progress 2/3 but only ISC-1 is [x] (count=1).
    text = BASELINE.replace("progress: 1/3", "progress: 2/3")
    result = lint(parse_isa_text(text))
    assert result.ok is False
    assert any("progress" in f.lower() or "checked" in f.lower() for f in result.failures), (
        f"Expected a progress-mismatch failure; got: {result.failures}"
    )


# --------------------------------------------------------------------------
# ISC-8 — Changelog entry missing a required part
# --------------------------------------------------------------------------


def test_isc_08_changelog_entry_missing_part():
    # Replace the placeholder Changelog with a real entry missing 'learned:'.
    text = BASELINE.replace(
        "## Changelog\n_(none — no corrections were needed.)_",
        "## Changelog\n2026-01-01 — something went wrong\n"
        "  conjectured:   X would work\n"
        "  refuted by:    it did not\n"
        "  criterion now: ISC-2 added\n",
    )
    result = lint(parse_isa_text(text))
    assert result.ok is False
    assert any("Changelog" in f for f in result.failures), (
        f"Expected a Changelog failure; got: {result.failures}"
    )


# --------------------------------------------------------------------------
# ISC-9 — invalid frontmatter (missing key; bad phase)
# --------------------------------------------------------------------------


def test_isc_09_invalid_frontmatter():
    # (a) Remove the 'owner:' line.
    text_a = BASELINE.replace("owner:    claude\n", "")
    result_a = lint(parse_isa_text(text_a))
    assert result_a.ok is False, "Should fail when 'owner' is missing"
    assert any("owner" in f for f in result_a.failures), (
        f"Expected a failure for missing 'owner'; got: {result_a.failures}"
    )

    # (b) Set phase to a bogus value.
    text_b = BASELINE.replace("phase:    execute", "phase:    bogus")
    result_b = lint(parse_isa_text(text_b))
    assert result_b.ok is False, "Should fail when phase is invalid"
    assert any("phase" in f and "bogus" in f for f in result_b.failures), (
        f"Expected a failure for invalid phase 'bogus'; got: {result_b.failures}"
    )


# --------------------------------------------------------------------------
# ISC-10 — all failures reported in a single run
# --------------------------------------------------------------------------


def test_isc_10_reports_all_failures():
    # Three distinct defects:
    #   1. Remove ## Git Plan (missing mandatory section)
    #   2. Remove Anti: prefix from ISC-2 (zero Anti ISCs)
    #   3. Claim progress 3/3 while only ISC-1 is [x] (mismatch)
    text = BASELINE
    text = text.replace("## Git Plan\n- Branch feat/fixture; commit; push.\n", "")
    text = text.replace(
        "- [ ] ISC-2: Anti: a regression must not happen",
        "- [ ] ISC-2: a regression must not happen",
    )
    text = text.replace("progress: 1/3", "progress: 3/3")

    result = lint(parse_isa_text(text))
    assert result.ok is False
    assert len(result.failures) >= 3, (
        f"Expected at least 3 failures; got {len(result.failures)}: {result.failures}"
    )
    # Confirm each of the three defects produced a failure.
    assert any("Git Plan" in f for f in result.failures), "Missing 'Git Plan' failure"
    assert any("Anti" in f for f in result.failures), "Missing Anti ISC failure"
    assert any(
        "progress" in f.lower() or "checked" in f.lower() or "N=" in f
        for f in result.failures
    ), "Missing progress-mismatch failure"


# --------------------------------------------------------------------------
# ISC-11 — minimal valid E1 ISA lints clean
# --------------------------------------------------------------------------

_E1_BASELINE = """\
---
isa:      20260101-0001_e1fixture
task:     "E1 Fixture"
tier:     E1
phase:    execute
progress: 1/2
card:     "-"
board:    "-"
branch:   feat/e1fixture
hive:     "-"
owner:    claude
started:  2026-01-01T00:00:00Z
updated:  2026-01-01T00:00:00Z
---

## Goal
A minimal E1 goal paragraph.

## Criteria
- [x] ISC-1: the primary criterion
- [ ] ISC-2: Anti: a regression must not happen

## Verification
**ISC-1** — probe output: ok
"""


def test_isc_11_minimal_e1_isa_passes():
    result = lint(parse_isa_text(_E1_BASELINE))
    assert result.ok is True, (
        f"Minimal E1 ISA should lint clean; failures: {result.failures}"
    )


# --------------------------------------------------------------------------
# ISC-12 — scaffold ISA lints clean (completion checks deferred)
# --------------------------------------------------------------------------


def test_isc_12_scaffold_isa_passes():
    text = (
        BASELINE
        .replace("phase:    execute", "phase:    scaffold")
        .replace("progress: 1/3", "progress: 0/3")
        .replace("- [x] ISC-1: the first criterion", "- [ ] ISC-1: the first criterion")
        .replace(
            "## Verification\n**ISC-1** — probe output: ok",
            "## Verification\n_(filled during verify)_",
        )
    )
    result = lint(parse_isa_text(text))
    assert result.ok is True, (
        f"Scaffold ISA with placeholder Verification should lint clean; "
        f"failures: {result.failures}"
    )


# --------------------------------------------------------------------------
# ISC-13 — --json output is machine-parseable
# --------------------------------------------------------------------------


def test_isc_13_json_output(tmp_path):
    # Write a passing fixture file.
    passing_file = tmp_path / "passing_ISA.md"
    passing_file.write_text(BASELINE, encoding="utf-8")

    # Run the CLI with --json and parse stdout.
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "isa_lint.py"), "--json", str(passing_file)],
        capture_output=True,
        text=True,
    )
    data = json.loads(proc.stdout)
    assert set(data.keys()) >= {"ok", "failures", "tier", "phase"}, (
        f"Missing keys in JSON output: {data.keys()}"
    )
    assert isinstance(data["ok"], bool)
    assert data["ok"] is True

    # Write a failing fixture file (missing Git Plan section).
    failing_text = BASELINE.replace(
        "## Git Plan\n- Branch feat/fixture; commit; push.\n", ""
    )
    failing_file = tmp_path / "failing_ISA.md"
    failing_file.write_text(failing_text, encoding="utf-8")

    proc2 = subprocess.run(
        [sys.executable, str(_SCRIPTS / "isa_lint.py"), "--json", str(failing_file)],
        capture_output=True,
        text=True,
    )
    data2 = json.loads(proc2.stdout)
    assert isinstance(data2["ok"], bool)
    assert data2["ok"] is False
    assert isinstance(data2["failures"], list)
    assert len(data2["failures"]) >= 1


# --------------------------------------------------------------------------
# ISC-14 — stdlib only (no third-party imports)
# --------------------------------------------------------------------------


def test_isc_14_stdlib_only():
    import re as _re

    _ALLOWED_NON_STDLIB = {"isa_common", "isa_lint", "isa_reconcile"}

    def _top_level_imports(source: str) -> set[str]:
        """Extract top-level module names from 'import X' and 'from X import' lines."""
        modules: set[str] = set()
        for line in source.splitlines():
            line = line.strip()
            # Match: import X[, Y, ...]  or  import X as Y
            m = _re.match(r"^import\s+([\w, ]+)", line)
            if m:
                for token in m.group(1).split(","):
                    token = token.strip().split()[0]  # strip "as alias"
                    if token:
                        modules.add(token.split(".")[0])
                continue
            # Match: from X import ...
            m = _re.match(r"^from\s+([\w.]+)\s+import", line)
            if m:
                modules.add(m.group(1).split(".")[0])
        return modules

    stdlib = sys.stdlib_module_names

    for script_name in ("isa_lint", "isa_common"):
        source = (_SCRIPTS / f"{script_name}.py").read_text(encoding="utf-8")
        imported = _top_level_imports(source)
        for mod in imported:
            assert mod in stdlib or mod in _ALLOWED_NON_STDLIB, (
                f"{script_name}.py imports non-stdlib, non-allowed module: '{mod}'"
            )


# --------------------------------------------------------------------------
# ISC-36 / ISC-37 — completion-gate checks
# --------------------------------------------------------------------------

# Build a clean phase: complete baseline (all ISCs [x], progress 3/3,
# every ISC mentioned in Verification, all sections non-thin).
_COMPLETE_BASELINE = """\
---
isa:      20260101-0000_fixture
task:     "Fixture"
tier:     E3
phase:    complete
progress: 3/3
card:     "-"
board:    "-"
branch:   feat/fixture
hive:     "-"
owner:    claude
started:  2026-01-01T00:00:00Z
updated:  2026-01-01T00:00:00Z
---

## Problem
A fixture problem statement with real content.

## Goal
A fixture goal paragraph describing the end state.

## Out of Scope
- Nothing extra.

## Constraints
- Standard library only.

## Criteria
- [x] ISC-1: the first criterion
- [x] ISC-2: Anti: a regression must not happen
- [x] ISC-3: the third criterion

## Test Strategy
| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | run one | ok |
| ISC-2 | run two | ok |
| ISC-3 | run three | ok |

## Git Plan
- Branch feat/fixture; commit; push.

## Decisions
- 2026-01-01: a decision was made.

## Changelog
_(none — no corrections were needed.)_

## Verification
**ISC-1** — probe output: ok
**ISC-2** — probe output: ok
**ISC-3** — probe output: ok

## Handback
- STATUS: DONE. Completion note recorded.
"""


def test_complete_baseline_lints_clean():
    """Guard: the complete-phase baseline must lint clean before mutation tests."""
    result = lint(parse_isa_text(_COMPLETE_BASELINE))
    assert result.ok is True, (
        f"Complete-phase baseline should lint clean; failures: {result.failures}"
    )


def test_isc_36_complete_isa_with_open_isc():
    # Change ISC-3 back to open [ ].
    text = _COMPLETE_BASELINE.replace(
        "- [x] ISC-3: the third criterion",
        "- [ ] ISC-3: the third criterion",
    )
    # Adjust progress to still-wrong-but-consistent-for-this-test (keep [x] count at 2).
    text = text.replace("progress:     3/3", "progress:     2/3")
    result = lint(parse_isa_text(text))
    assert result.ok is False
    assert any("open" in f.lower() or "complete" in f.lower() for f in result.failures), (
        f"Expected a failure about open ISC in complete phase; got: {result.failures}"
    )


def test_isc_37_complete_isa_with_thin_section():
    # Make the Verification section an unfilled placeholder.
    text = _COMPLETE_BASELINE.replace(
        "## Verification\n**ISC-1** — probe output: ok\n"
        "**ISC-2** — probe output: ok\n"
        "**ISC-3** — probe output: ok",
        "## Verification\n_(filled during verify)_",
    )
    result = lint(parse_isa_text(text))
    assert result.ok is False
    assert any("Verification" in f and "placeholder" in f.lower() for f in result.failures), (
        f"Expected a thin-section failure for Verification; got: {result.failures}"
    )
