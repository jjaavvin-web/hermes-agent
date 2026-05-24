#!/usr/bin/env python3
"""isa_common — shared parser and data model for ISA (Ideal State Artifact) files.

An ISA is the cross-agent work-spec defined in ``~/.hermes/ISA-SPEC.md``: one
markdown file with a YAML-ish frontmatter block and a fixed set of ``##``
sections. This module turns an ISA file into typed records so the policy tools
that sit on top of it — ``isa_lint`` (the CheckCompleteness gate, ISA-SPEC §9)
and ``isa_reconcile`` (the ephemeral-slice merge, §7) — can stay thin.

Design: a rich parser here, thin policy there. All structural parsing lives in
this module; ``isa_lint`` and ``isa_reconcile`` only inspect the records.

Standard library only — no PyYAML, no third-party dependency. The frontmatter
is a flat block of ``key: value`` scalars, so a line parser is enough.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "VALID_TIERS",
    "VALID_PHASES",
    "REQUIRED_FRONTMATTER",
    "TIER_SECTIONS",
    "CHANGELOG_PARTS",
    "Isc",
    "TestRow",
    "ChangelogEntry",
    "Isa",
    "parse_isa",
    "parse_isa_text",
    "find_isa_for_card",
    "default_work_root",
    "is_unfilled_placeholder",
]

# --------------------------------------------------------------------------
# Reference data from ISA-SPEC.md — kept here so every tool agrees on it.
# --------------------------------------------------------------------------

VALID_TIERS = ("E1", "E2", "E3", "E4")
VALID_PHASES = ("scaffold", "execute", "verify", "complete")

# Frontmatter keys every ISA must carry (ISA-SPEC §3 / §12 template). Extra
# keys (e.g. a pilot ISA's ``commit:``) are allowed; these are the minimum.
REQUIRED_FRONTMATTER = (
    "isa", "task", "tier", "phase", "progress",
    "card", "board", "branch", "hive", "owner", "started", "updated",
)

# Mandatory ``##`` sections per effort tier (ISA-SPEC §5), cumulative.
_SECTIONS_E1 = ("Goal", "Criteria", "Verification")
_SECTIONS_E2 = _SECTIONS_E1 + ("Out of Scope", "Constraints", "Test Strategy", "Git Plan")
_SECTIONS_E3 = (
    "Problem", "Goal", "Out of Scope", "Constraints", "Criteria",
    "Test Strategy", "Git Plan", "Decisions", "Changelog", "Verification", "Handback",
)
TIER_SECTIONS = {
    "E1": _SECTIONS_E1,
    "E2": _SECTIONS_E2,
    "E3": _SECTIONS_E3,
    "E4": _SECTIONS_E3,  # E4 = all 11; ephemeral slices are files, not sections
}

# The four mandatory parts of a Changelog entry (ISA-SPEC §8).
CHANGELOG_PARTS = ("conjectured", "refuted by", "learned", "criterion now")

# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

_FM_LINE_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*?)\s*$")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_ISC_RE = re.compile(r"^-\s*\[([ xX\-])\]\s*(ISC-[0-9][0-9.]*)\s*:\s*(.*)$")
_PROGRESS_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_CHANGELOG_PART_RE = re.compile(
    r"^\s+(" + "|".join(p.replace(" ", r"\s+") for p in CHANGELOG_PARTS) + r")\s*:\s*(.*)$"
)
_ITALIC_PLACEHOLDER_RE = re.compile(r"^_\(.*\)_$", re.DOTALL)
_ANGLE_ONLY_RE = re.compile(r"^<[^>\n]+>$")

# Markers that identify an unfilled section as a *template default* rather than
# a deliberate human note such as "_(none — <reason>)_". See ISA-SPEC §12.
_PLACEHOLDER_MARKERS = (
    "_(filled",                                  # Decisions / Changelog / Verification
    "completion record + lessons on complete",   # Handback
)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Isc:
    """One Ideal-State Criterion line from ``## Criteria``."""

    id: str            # "ISC-7", "ISC-7.1"
    state: str         # " " open · "x" verified · "-" tombstone
    text: str          # criterion text after the "ISC-N:" prefix
    is_anti: bool      # text begins with "Anti:"
    line_no: int = 0

    @property
    def is_checked(self) -> bool:
        return self.state == "x"

    @property
    def is_tombstone(self) -> bool:
        return self.state == "-"

    @property
    def is_open(self) -> bool:
        return self.state == " "


@dataclass
class TestRow:
    """One row of the ``## Test Strategy`` table."""

    isc_id: str
    probe: str
    pass_threshold: str


@dataclass
class ChangelogEntry:
    """One ``## Changelog`` entry (ISA-SPEC §8)."""

    header: str
    parts: dict = field(default_factory=dict)   # CHANGELOG_PARTS subset -> value
    raw: str = ""

    def missing_parts(self) -> list[str]:
        """Return the mandatory parts that are absent or empty for this entry."""
        return [p for p in CHANGELOG_PARTS if not self.parts.get(p, "").strip()]


@dataclass
class Isa:
    """A parsed ISA file. Lenient: malformed input yields empty fields, never
    an exception — strictness is ``isa_lint``'s job, not the parser's."""

    path: Path | None
    raw: str
    frontmatter: dict
    sections: dict          # section name -> body text
    iscs: list              # list[Isc] from ## Criteria
    test_rows: list         # list[TestRow] from ## Test Strategy
    changelog: list         # list[ChangelogEntry] from ## Changelog

    def section(self, name: str) -> str | None:
        return self.sections.get(name)

    def has_section(self, name: str) -> bool:
        return name in self.sections

    @property
    def tier(self) -> str:
        return self.frontmatter.get("tier", "")

    @property
    def phase(self) -> str:
        return self.frontmatter.get("phase", "")

    def progress_pair(self) -> tuple[int, int] | None:
        """Return (N, M) from the frontmatter ``progress`` field, or None."""
        m = _PROGRESS_RE.match(self.frontmatter.get("progress", ""))
        return (int(m.group(1)), int(m.group(2))) if m else None

    def checked_count(self) -> int:
        return sum(1 for i in self.iscs if i.is_checked)

    def tombstone_count(self) -> int:
        return sum(1 for i in self.iscs if i.is_tombstone)

    def open_iscs(self) -> list:
        return [i for i in self.iscs if i.is_open]

    def anti_iscs(self) -> list:
        return [i for i in self.iscs if i.is_anti and not i.is_tombstone]

    def test_row_for(self, isc_id: str) -> TestRow | None:
        for r in self.test_rows:
            if r.isc_id == isc_id:
                return r
        return None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading ``---`` ... ``---`` block. Returns (frontmatter, body)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm: dict = {}
    for ln in lines[1:end]:
        m = _FM_LINE_RE.match(ln)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        fm[key] = val
    return fm, "\n".join(lines[end + 1:])


def _split_sections(body: str) -> dict:
    """Map each ``## Heading`` to its body text (everything up to the next ##)."""
    sections: dict = {}
    current: str | None = None
    buf: list[str] = []
    for ln in body.splitlines():
        m = _HEADING_RE.match(ln)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip("\n")
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(ln)
    if current is not None:
        sections[current] = "\n".join(buf).strip("\n")
    return sections


def _parse_iscs(criteria_body: str) -> list:
    """Parse ``- [ ] ISC-N: text`` lines from the Criteria section body."""
    iscs: list = []
    for idx, ln in enumerate(criteria_body.splitlines(), start=1):
        m = _ISC_RE.match(ln)
        if not m:
            continue
        state = m.group(1).lower()
        if state == "":
            state = " "
        text = m.group(3).strip()
        iscs.append(
            Isc(
                id=m.group(2),
                state=state,
                text=text,
                is_anti=text.lstrip().startswith("Anti:"),
                line_no=idx,
            )
        )
    return iscs


def _parse_test_rows(ts_body: str) -> list:
    """Parse the ``## Test Strategy`` markdown table into TestRow records."""
    rows: list = []
    for ln in ts_body.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        first = cells[0]
        # Skip the header row and the |---|---| separator row.
        if first.lower() == "isc" or (first and set(first) <= set("-: ")):
            continue
        m = re.search(r"ISC-[0-9][0-9.]*", first)
        if not m:
            continue
        rows.append(
            TestRow(
                isc_id=m.group(0),
                probe=cells[1] if len(cells) > 1 else "",
                pass_threshold=cells[2] if len(cells) > 2 else "",
            )
        )
    return rows


def _parse_changelog(cl_body: str) -> list:
    """Parse ``## Changelog`` into ChangelogEntry records (ISA-SPEC §8).

    An entry header is a non-indented, non-blank line that is not an italic
    ``_(...)_`` placeholder. Its four parts are indented ``key: value`` lines.
    """
    entries: list = []
    header: str | None = None
    body_lines: list[str] = []

    def _flush() -> None:
        nonlocal header, body_lines
        if header is None:
            return
        parts: dict = {}
        for cl in body_lines:
            pm = _CHANGELOG_PART_RE.match(cl)
            if pm:
                key = re.sub(r"\s+", " ", pm.group(1)).strip().lower()
                parts[key] = pm.group(2).strip()
        entries.append(
            ChangelogEntry(
                header=header.strip(),
                parts=parts,
                raw="\n".join([header, *body_lines]),
            )
        )
        header, body_lines = None, []

    for ln in cl_body.splitlines():
        if not ln.strip():
            if header is not None:
                body_lines.append(ln)
            continue
        if _ITALIC_PLACEHOLDER_RE.match(ln.strip()):
            continue  # "_(none — ...)_" is a note, not an entry
        if ln[:1].isspace():
            if header is not None:
                body_lines.append(ln)
            continue
        # A non-indented, non-blank, non-placeholder line begins a new entry.
        _flush()
        header = ln
    _flush()
    return entries


def parse_isa_text(text: str, path: str | os.PathLike | None = None) -> Isa:
    """Parse ISA markdown text into an Isa record. Never raises on bad input."""
    frontmatter, body = _split_frontmatter(text)
    sections = _split_sections(body)
    return Isa(
        path=Path(path) if path is not None else None,
        raw=text,
        frontmatter=frontmatter,
        sections=sections,
        iscs=_parse_iscs(sections.get("Criteria", "")),
        test_rows=_parse_test_rows(sections.get("Test Strategy", "")),
        changelog=_parse_changelog(sections.get("Changelog", "")),
    )


def parse_isa(path: str | os.PathLike) -> Isa:
    """Parse an ISA file from disk."""
    p = Path(path)
    return parse_isa_text(p.read_text(encoding="utf-8"), path=p)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def is_unfilled_placeholder(body: str | None) -> bool:
    """True if a section body is empty or still carries template placeholder text.

    Catches an empty body, an italic ``_(filled ...)_`` template default, the
    Handback template default, and a body that is only a ``<template token>``.
    Does NOT flag a deliberate human note such as ``_(none — <reason>)_``.
    """
    s = (body or "").strip()
    if not s:
        return True
    low = s.lower()
    if any(marker in low for marker in _PLACEHOLDER_MARKERS):
        return True
    if _ANGLE_ONLY_RE.match(s):
        return True
    return False


def default_work_root() -> Path:
    """The canonical out-of-repo ISA store: ``$HERMES_HOME/work`` (ISA-SPEC §2).

    Honours ``HERMES_HOME`` so test harnesses that redirect it stay isolated;
    falls back to ``~/.hermes/work``.
    """
    home = os.environ.get("HERMES_HOME")
    base = Path(home) if home else (Path.home() / ".hermes")
    return base / "work"


def find_isa_for_card(card_id: str, work_root: str | os.PathLike | None = None) -> Path | None:
    """Return the path of the ISA whose frontmatter ``card:`` equals ``card_id``.

    Scans ``<work_root>/*/ISA.md`` (default: :func:`default_work_root`). Returns
    None when no ISA links the card. Files that cannot be read are skipped, not
    fatal. Matching is deterministic — globbed paths are sorted.
    """
    if not card_id or card_id == "-":
        return None
    root = Path(work_root) if work_root is not None else default_work_root()
    if not root.is_dir():
        return None
    for isa_path in sorted(root.glob("*/ISA.md")):
        try:
            frontmatter, _ = _split_frontmatter(isa_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if frontmatter.get("card", "").strip() == card_id:
            return isa_path
    return None
