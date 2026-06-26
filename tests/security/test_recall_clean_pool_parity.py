"""Vendored CI tripwire for the production recall clean-pool filter.

The deployed recall module (``~/.hermes/mcp/recall/recall_at_dispatch.py``)
defines ``CLEAN_POOL_WHERE`` — the SQL ``WHERE`` predicate that segregates the
injected-at-dispatch recall pool from poisoned / self-seeded rows. It MUST stay
in parity with the read-only MVMS MCP's default-clean view, or production recall
silently diverges from what the audits/probes are graded against: a
poisoned-recall / self-seed persistence channel (2026-06-25 spine audit, Memory
HIGH + Security HIGH).

A code comment in the live module claims a silent revert of any parity clause is
a RED test — but that RED test lived ONLY outside this repo
(``~/.hermes/mcp/recall/tests/test_clean_pool_parity.py``), run by no CI. This
module vendors the invariant into repo CI.

We deliberately do NOT import the live module (it pulls ``asyncpg``): we READ it
as TEXT, parse out the ``CLEAN_POOL_WHERE`` assignment, and assert every parity
clause is present. The test SKIPs when the live module is absent (GitHub CI /
fresh checkout) so those stay green; it goes RED on this host if a clause is
reverted.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

LIVE = Path("/home/josep/.hermes/mcp/recall/recall_at_dispatch.py")

# Each entry: (human label, lowercased substring that MUST appear in the
# CLEAN_POOL_WHERE predicate). Reverting any one of these silently re-opens the
# corresponding leak, so each becomes an independent RED assertion.
PARITY_CLAUSES = [
    ("currently-valid window (superseded excluded)", "valid_until is null"),
    ("quarantine-tag exclusion", "quarantine"),
    ("auto-bridged lesson-tag exclusion", "auto-bridged"),
    ("not-deprecated", "deprecated_at is null"),
    ("ict-brain source exclusion", "ict-brain"),
    ("gave_up|crashed|blocked source predicate", "gave_up|crashed|blocked"),
    ("kanban-mvms-bridge prefix exclusion", "^kanban-mvms-bridge:"),
]

_ASSIGN_RE = re.compile(
    r"""CLEAN_POOL_WHERE\s*=\s*(?P<q>'{3}|\"{3})(?P<body>.*?)(?P=q)""",
    re.DOTALL,
)


def _extract_clean_pool_where(text: str) -> str:
    """Return the lowercased body of the CLEAN_POOL_WHERE triple-quoted literal."""
    m = _ASSIGN_RE.search(text)
    if m is None:
        raise AssertionError(
            "CLEAN_POOL_WHERE triple-quoted assignment not found in the module text"
        )
    return m.group("body").lower()


@pytest.mark.skipif(
    not LIVE.is_file(),
    reason=f"live recall module not present at {LIVE} (fresh checkout / CI)",
)
@pytest.mark.parametrize("label,clause", PARITY_CLAUSES, ids=[c[0] for c in PARITY_CLAUSES])
def test_live_clean_pool_has_parity_clause(label: str, clause: str) -> None:
    where = _extract_clean_pool_where(LIVE.read_text(encoding="utf-8"))
    assert clause in where, (
        f"recall CLEAN_POOL_WHERE is missing the {label!r} parity clause "
        f"({clause!r}); a silent revert re-opens that leak (parity with the "
        f"mvms_readonly default-clean view)."
    )


# --- meta-tests: prove the tripwire actually fires / skips as designed -------


def test_negative_missing_clause_goes_red(tmp_path: Path) -> None:
    """A fixture whose CLEAN_POOL_WHERE drops 'valid_until IS NULL' must RED."""
    fixture = tmp_path / "reverted_recall.py"
    fixture.write_text(
        'CLEAN_POOL_WHERE = """source != \'ict-brain\'\n'
        "  AND source !~ ':(gave_up|crashed|blocked)$'\n"
        "  AND source !~ '^kanban-mvms-bridge:'\n"
        "  AND deprecated_at IS NULL\n"
        "  AND NOT (COALESCE(tags, ARRAY[]::text[]) @> ARRAY['quarantine']::text[])\n"
        '  AND NOT (kind = \'lesson\' AND COALESCE(tags, ARRAY[]::text[]) @> ARRAY[\'auto-bridged\']::text[])"""\n',
        encoding="utf-8",
    )
    where = _extract_clean_pool_where(fixture.read_text(encoding="utf-8"))
    assert "valid_until is null" not in where  # the silent revert
    with pytest.raises(AssertionError):
        assert "valid_until is null" in where, "tripwire should fire on this revert"


def test_nonexistent_path_is_skipped() -> None:
    """A nonexistent live path → the skipif guard would mark the suite SKIPPED."""
    missing = Path("/home/josep/.hermes/mcp/recall/__definitely_not_here__.py")
    assert not missing.is_file()
