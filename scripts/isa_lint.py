#!/usr/bin/env python3
"""isa_lint — the ISA CheckCompleteness gate (ISA-SPEC §9)."""
from __future__ import annotations
import argparse, json, re, sys
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isa_common


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class LintResult:
    isa_path: str
    tier: str
    phase: str
    ok: bool
    failures: list  # list[str], human-readable, one per problem

    def to_dict(self) -> dict:
        return {
            "isa": self.isa_path,
            "tier": self.tier,
            "phase": self.phase,
            "ok": self.ok,
            "failures": self.failures,
        }


# --------------------------------------------------------------------------
# Core lint function
# --------------------------------------------------------------------------


def lint(isa_or_path) -> LintResult:
    """Run CheckCompleteness on an ISA.

    Accepts either an ``isa_common.Isa`` object or a path (str/Path).
    All checks are run; failures are accumulated (never stop-at-first).
    Returns a LintResult with ok=True iff no failures were found.
    """
    if isinstance(isa_or_path, isa_common.Isa):
        isa = isa_or_path
        isa_path = str(isa.path) if isa.path is not None else "<text>"
    else:
        isa = isa_common.parse_isa(isa_or_path)
        isa_path = str(isa_or_path)

    failures: list[str] = []

    # ------------------------------------------------------------------
    # STRUCTURAL CHECKS — run at every phase
    # ------------------------------------------------------------------

    # Check 1: required frontmatter keys present
    for key in isa_common.REQUIRED_FRONTMATTER:
        if key not in isa.frontmatter:
            failures.append(f"frontmatter missing required key: '{key}'")

    # Check 2: tier is valid
    tier = isa.frontmatter.get("tier", "")
    if tier not in isa_common.VALID_TIERS:
        failures.append(
            f"frontmatter 'tier' value '{tier}' is not one of {list(isa_common.VALID_TIERS)}"
        )

    # Check 3: phase is valid
    phase = isa.frontmatter.get("phase", "")
    if phase not in isa_common.VALID_PHASES:
        failures.append(
            f"frontmatter 'phase' value '{phase}' is not one of {list(isa_common.VALID_PHASES)}"
        )

    # Check 4: progress parses as N/M
    progress_pair = isa.progress_pair()
    if progress_pair is None:
        failures.append(
            f"frontmatter 'progress' value '{isa.frontmatter.get('progress', '')}' "
            f"does not parse as N/M"
        )

    # Check 5: mandatory sections for tier are present (skip if tier invalid)
    if tier in isa_common.VALID_TIERS:
        for section_name in isa_common.TIER_SECTIONS[tier]:
            if not isa.has_section(section_name):
                failures.append(
                    f"mandatory section '## {section_name}' is missing (required for tier {tier})"
                )

    # Check 6: at least one ISC in Criteria
    if not isa.iscs:
        failures.append("## Criteria section contains zero ISCs (malformed)")

    # Check 7: every non-tombstone ISC has a Test Strategy row (E2/E3/E4 only, not E1)
    if tier in ("E2", "E3", "E4"):
        for isc in isa.iscs:
            if not isc.is_tombstone and isa.test_row_for(isc.id) is None:
                failures.append(
                    f"{isc.id}: non-tombstone ISC has no matching Test Strategy row"
                )

    # Check 8: at least one non-tombstone Anti: ISC exists
    if not isa.anti_iscs():
        failures.append(
            "## Criteria has no non-tombstone 'Anti:' ISC (at least one required per ISA-SPEC §6)"
        )

    # Check 9: every [x] ISC has a Verification block mentioning its id
    verification_body = isa.section("Verification") or ""
    for isc in isa.iscs:
        if isc.is_checked:
            if not re.search(r"\b" + re.escape(isc.id) + r"\b", verification_body):
                failures.append(
                    f"{isc.id}: checked [x] ISC has no mention in ## Verification"
                )

    # Check 10: progress consistency (only if check 4 passed)
    if progress_pair is not None:
        n, m = progress_pair
        actual_checked = isa.checked_count()
        actual_total = len(isa.iscs)
        if n != actual_checked:
            failures.append(
                f"progress N={n} does not match actual checked ISC count={actual_checked}"
            )
        if m != actual_total:
            failures.append(
                f"progress M={m} does not match total ISC count={actual_total} "
                f"(open + checked + tombstone)"
            )

    # Check 11: every Changelog entry has all required parts
    for entry in isa.changelog:
        missing = entry.missing_parts()
        if missing:
            failures.append(
                f"Changelog entry '{entry.header[:60]}' is missing required part(s): "
                f"{missing}"
            )

    # ------------------------------------------------------------------
    # COMPLETION-ONLY CHECKS — run only when phase == "complete"
    # ------------------------------------------------------------------

    if isa.phase == "complete":

        # Check 12: no open [ ] ISCs remain
        open_iscs = isa.open_iscs()
        if open_iscs:
            open_ids = [i.id for i in open_iscs]
            failures.append(
                f"phase is 'complete' but {len(open_ids)} open ISC(s) remain: {open_ids}"
            )

        # Check 13: every mandatory section that is present is non-thin
        if tier in isa_common.VALID_TIERS:
            for section_name in isa_common.TIER_SECTIONS[tier]:
                if isa.has_section(section_name):
                    body = isa.section(section_name)
                    if isa_common.is_unfilled_placeholder(body):
                        failures.append(
                            f"phase is 'complete' but '## {section_name}' is still an "
                            f"unfilled placeholder (thin section)"
                        )

    return LintResult(
        isa_path=isa_path,
        tier=tier,
        phase=phase,
        ok=(len(failures) == 0),
        failures=failures,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="isa_lint — ISA CheckCompleteness gate (ISA-SPEC §9)"
    )
    parser.add_argument("path", help="Path to the ISA.md file to lint")
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit machine-parseable JSON to stdout instead of human text",
    )
    args = parser.parse_args(argv)

    isa_path = Path(args.path)
    if not isa_path.exists():
        print(f"error: file not found: {args.path}", file=sys.stderr)
        return 2

    result = lint(isa_path)

    if args.json_output:
        print(json.dumps(result.to_dict()))
    else:
        if result.ok:
            print(f"PASS: {args.path}")
        else:
            print(f"FAIL: {args.path} ({len(result.failures)} failure(s))")
            for f in result.failures:
                print(f"  - {f}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
