"""Tests for scripts/isa_common.py — the shared ISA parser/data-model.

ISC-1 of the isa-enforcement-layer ISA is verified here; the remaining tests
are parser-coverage hygiene (they back ISC-35, the no-regression criterion).
"""

from __future__ import annotations

import importlib.util
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


def _write_isa(dir_path: Path, card: str) -> Path:
    """Write a minimal valid ISA into dir_path; return the ISA.md path."""
    dir_path.mkdir(parents=True, exist_ok=True)
    isa = dir_path / "ISA.md"
    isa.write_text(
        f"""---
isa:      {dir_path.name}
task:     "fixture"
tier:     E1
phase:    scaffold
progress: 0/1
card:     "{card}"
board:    "-"
branch:   b
hive:     "-"
owner:    claude
started:  2026-05-22T00:00:00Z
updated:  2026-05-22T00:00:00Z
---

## Goal
A fixture ISA.

## Criteria
- [ ] ISC-1: a thing

## Verification
_(filled during verify)_
""",
        encoding="utf-8",
    )
    return isa


# --------------------------------------------------------------------------
# ISC-1 — find_isa_for_card
# --------------------------------------------------------------------------


def test_isc_01_find_isa_for_card_matches_by_card_field(tmp_path):
    root = tmp_path / "work"
    _write_isa(root / "20260101-0000_alpha", "t_aaa111")
    beta = _write_isa(root / "20260101-0001_beta", "t_bbb222")
    _write_isa(root / "20260101-0002_nocard", "-")

    assert isa_common.find_isa_for_card("t_bbb222", work_root=root) == beta


def test_isc_01_find_isa_for_card_returns_none_when_unlinked(tmp_path):
    root = tmp_path / "work"
    _write_isa(root / "20260101-0000_alpha", "t_aaa111")

    assert isa_common.find_isa_for_card("t_no_such_card", work_root=root) is None
    assert isa_common.find_isa_for_card("-", work_root=root) is None
    assert isa_common.find_isa_for_card("", work_root=root) is None
    # A missing work root is not an error.
    assert isa_common.find_isa_for_card("t_x", work_root=tmp_path / "absent") is None


def test_isc_01_find_isa_for_card_skips_unreadable_isa(tmp_path):
    """A malformed/binary ISA.md in the work tree is skipped, not fatal."""
    root = tmp_path / "work"
    good = _write_isa(root / "20260101-0000_alpha", "t_aaa111")
    bad_dir = root / "20260101-0009_corrupt"
    bad_dir.mkdir(parents=True)
    (bad_dir / "ISA.md").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")

    assert isa_common.find_isa_for_card("t_aaa111", work_root=root) == good


# --------------------------------------------------------------------------
# Parser-coverage hygiene
# --------------------------------------------------------------------------

_SAMPLE = """---
isa:      20260522-1603_sample
task:     "Sample"
tier:     E3
phase:    execute
progress: 1/3
card:     "-"
board:    claude-arch
branch:   feat/x
hive:     "-"
owner:    claude
started:  2026-05-22T16:03:56Z
updated:  2026-05-22T16:03:56Z
---

## Criteria
- [x] ISC-1: the first criterion
- [ ] ISC-2: Anti: something must not happen
- [-] ISC-3: (dropped — see Decisions)

## Test Strategy
| ISC | Probe | Pass |
|-----|-------|------|
| ISC-1 | `run it` | ok |
| ISC-2 | `run that` | ok |

## Changelog
2026-05-22 — a conjecture failed
  conjectured:   X would work
  refuted by:    it did not
  learned:       Y is the cause
  criterion now: ISC-2 added
"""


def test_parse_frontmatter_and_sections():
    isa = isa_common.parse_isa_text(_SAMPLE)
    assert isa.tier == "E3"
    assert isa.phase == "execute"
    assert isa.frontmatter["isa"] == "20260522-1603_sample"
    assert isa.progress_pair() == (1, 3)
    assert isa.has_section("Criteria")
    assert isa.has_section("Changelog")


def test_parse_iscs_state_and_anti():
    isa = isa_common.parse_isa_text(_SAMPLE)
    by_id = {i.id: i for i in isa.iscs}
    assert by_id["ISC-1"].is_checked and not by_id["ISC-1"].is_anti
    assert by_id["ISC-2"].is_open and by_id["ISC-2"].is_anti
    assert by_id["ISC-3"].is_tombstone
    assert isa.checked_count() == 1
    assert isa.tombstone_count() == 1
    assert [i.id for i in isa.anti_iscs()] == ["ISC-2"]


def test_parse_test_rows_keyed_by_isc_id():
    isa = isa_common.parse_isa_text(_SAMPLE)
    assert {r.isc_id for r in isa.test_rows} == {"ISC-1", "ISC-2"}
    assert isa.test_row_for("ISC-1").probe == "`run it`"
    assert isa.test_row_for("ISC-3") is None


def test_parse_changelog_four_tuple():
    isa = isa_common.parse_isa_text(_SAMPLE)
    assert len(isa.changelog) == 1
    assert isa.changelog[0].missing_parts() == []


def test_parse_changelog_placeholder_is_not_an_entry():
    text = _SAMPLE.replace(
        "## Changelog\n2026-05-22 — a conjecture failed\n"
        "  conjectured:   X would work\n"
        "  refuted by:    it did not\n"
        "  learned:       Y is the cause\n"
        "  criterion now: ISC-2 added\n",
        "## Changelog\n_(none yet — filled on each correction)_\n",
    )
    isa = isa_common.parse_isa_text(text)
    assert isa.changelog == []


def test_parse_changelog_detects_missing_part():
    text = _SAMPLE.replace("  learned:       Y is the cause\n", "")
    isa = isa_common.parse_isa_text(text)
    assert isa.changelog[0].missing_parts() == ["learned"]


def test_is_unfilled_placeholder():
    assert isa_common.is_unfilled_placeholder("") is True
    assert isa_common.is_unfilled_placeholder("   \n  ") is True
    assert isa_common.is_unfilled_placeholder("_(filled during verify)_") is True
    assert isa_common.is_unfilled_placeholder("<one paragraph: the end state>") is True
    assert (
        isa_common.is_unfilled_placeholder(
            "_(MVMS completion record + lessons on complete — E3+)_"
        )
        is True
    )
    # A deliberate human closure note is NOT a placeholder.
    assert isa_common.is_unfilled_placeholder("_(none — no corrections were needed)_") is False
    assert isa_common.is_unfilled_placeholder("Real prose content here.") is False
