#!/usr/bin/env python3
"""Triage a test-run log against the documented pre-existing-failure baseline.

Full-suite reruns on a merge candidate branch routinely carry a handful of
failures that are pre-existing at the fork base (not regressions introduced
by the branch). Re-litigating those by eye on every rerun is exactly the
"more failures than forward progress" trap this repo has hit before — this
script makes the triage mechanical: it reads a test-run log, extracts the
failing targets, and buckets them against ``tests/known_test_debt.json``:

  * NEW failures      — failing and NOT in the baseline. These are the ones
                         that actually block: a regression this branch
                         introduced, or an undocumented flake.
  * KNOWN debt        — failing and covered by a baseline entry. Informational.
  * RESOLVED debt     — baseline entries whose target is NOT currently
                         failing. Informational; suggests pruning the entry
                         (it documents debt that no longer reproduces).

Only NEW failures make the process exit non-zero (see ``--update`` below for
the one exception: an UNREVIEWED entry re-triggers the gate on every run
until a human edits its reason).

Input log formats (auto-detected, first match wins):
  1. run_tests_parallel.py style — one or more ``╔╍ Failed: <path> ╍...``
     banner lines (see scripts/run_tests_parallel.py's
     ``_print_inline_failure``). Extracted targets are file-level.
  2. Plain pytest output — one or more ``FAILED <nodeid>`` short-summary
     lines (pytest's own ``-ra``/default failure summary). Extracted
     targets are test-id level (``path::Class::test``).

A baseline entry's ``target`` can be either a bare file path or a
``file::test`` node id; matching is granularity-tolerant in both
directions — a file-level baseline entry covers any failing test inside
that file, and a file-level *failing* target (from a run_tests_parallel
log, which only reports file granularity) is covered by any baseline entry
naming that file, whatever granularity the baseline entry was recorded at.

Usage:
    scripts/triage_against_baseline.py <log-file>
    scripts/triage_against_baseline.py <log-file> --baseline tests/known_test_debt.json
    scripts/triage_against_baseline.py <log-file> --update
    scripts/triage_against_baseline.py <log-file> --allow-unreviewed

Exit codes:
    0 — no NEW failures, and no UNREVIEWED baseline entries (or
        --allow-unreviewed was passed).
    1 — NEW failures exist, and/or UNREVIEWED baseline entries exist
        without --allow-unreviewed.
    2 — usage error (log file missing, baseline JSON malformed, etc.).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "tests" / "known_test_debt.json"

UNREVIEWED_PREFIX = "UNREVIEWED"

# run_tests_parallel.py's per-file failure banner, e.g.:
#   ╔╍ Failed: tests/agent/test_foo.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍
_MARKER_RE = re.compile(r"╔╍\s*Failed:\s*(\S+)\s*╍")

# pytest's own short-summary line, e.g.:
#   FAILED tests/foo.py::TestBar::test_baz - AssertionError: boom
# The optional " - <reason>" tail is stripped; the node id never contains
# " - " itself so splitting on the first occurrence is safe.
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(\S+)")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def extract_failing_targets(log_text: str) -> list[str]:
    """Extract failing test targets from a log, preserving first-seen order.

    Tries the run_tests_parallel.py marker format first; falls back to
    plain pytest ``FAILED <nodeid>`` lines if no markers are present, so a
    single function transparently handles either input log shape.
    """
    text = _strip_ansi(log_text)

    targets: list[str] = []
    seen: set[str] = set()

    for match in _MARKER_RE.finditer(text):
        target = match.group(1)
        if target not in seen:
            seen.add(target)
            targets.append(target)
    if targets:
        return targets

    for line in text.splitlines():
        match = _PYTEST_FAILED_RE.match(line.strip())
        if not match:
            continue
        target = match.group(1)
        if target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


def _file_of(target: str) -> str:
    return target.split("::", 1)[0]


def _covers(baseline_target: str, failing_target: str) -> bool:
    """Does a baseline entry's target cover a failing target?

    Exact match always covers. Otherwise the two must name the same file,
    and at least one side must be file-level (no ``::``) — a file-level
    target on either side is treated as covering/covered-by every test
    node inside that file, since that's the coarsest granularity either
    the log extractor or a human-authored baseline entry may use.
    """
    if baseline_target == failing_target:
        return True
    if _file_of(baseline_target) != _file_of(failing_target):
        return False
    return "::" not in baseline_target or "::" not in failing_target


def load_baseline(path: Path) -> list[dict[str, Any]]:
    """Load the baseline JSON. A missing file is treated as an empty
    baseline (every failure is then NEW) rather than an error, so a fresh
    checkout without a baseline file yet still runs the triage."""
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"baseline {path} must be a JSON array of entries")
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or "target" not in entry:
            raise ValueError(
                f"baseline {path} entry #{i} is malformed (must be an "
                f'object with at least a "target" key): {entry!r}'
            )
    return data


def save_baseline(path: Path, entries: list[dict[str, Any]]) -> None:
    ordered = sorted(entries, key=lambda e: str(e.get("target", "")))
    path.write_text(
        json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def bucket(
    failing_targets: list[str], baseline: list[dict[str, Any]]
) -> tuple[list[str], list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
    """Split failing targets into (new, known, resolved) buckets.

    ``known`` pairs each covered failing target with the baseline entry
    that covers it. ``resolved`` is every baseline entry that covers none
    of the failing targets.
    """
    new_failures: list[str] = []
    known: list[tuple[str, dict[str, Any]]] = []
    matched_ids: set[int] = set()

    for target in failing_targets:
        entry = next(
            (b for b in baseline if _covers(str(b["target"]), target)), None
        )
        if entry is None:
            new_failures.append(target)
        else:
            known.append((target, entry))
            matched_ids.add(id(entry))

    resolved = [b for b in baseline if id(b) not in matched_ids]
    return new_failures, known, resolved


def apply_update(
    baseline: list[dict[str, Any]],
    new_failures: list[str],
    log_path: Path,
    today: date,
) -> list[dict[str, Any]]:
    """Return a new baseline list with unseen ``new_failures`` appended as
    UNREVIEWED entries. Does not mutate the input list."""
    existing_targets = {str(e["target"]) for e in baseline}
    updated = list(baseline)
    for target in new_failures:
        if target in existing_targets:
            continue
        updated.append(
            {
                "target": target,
                "reason": f"{UNREVIEWED_PREFIX} — added by --update on "
                f"{today.isoformat()}",
                "recorded": today.isoformat(),
                "evidence": f"auto-added by scripts/triage_against_baseline.py "
                f"--update from {log_path}",
            }
        )
        existing_targets.add(target)
    return updated


def _print_report(
    baseline_path: Path,
    log_path: Path,
    failing_targets: list[str],
    new_failures: list[str],
    known: list[tuple[str, dict[str, Any]]],
    resolved: list[dict[str, Any]],
) -> None:
    print(f"=== Triage vs baseline: {baseline_path} ===")
    print(f"Source log: {log_path}")
    print(f"Failing targets extracted: {len(failing_targets)}")
    print()

    print(f"-- NEW failures ({len(new_failures)}) — BLOCK --")
    if new_failures:
        for target in new_failures:
            print(f"  {target}")
    else:
        print("  (none)")
    print()

    print(f"-- KNOWN debt ({len(known)}) — informational --")
    if known:
        for target, entry in known:
            print(f"  {target}")
            print(f"      baseline: {entry['target']} — {entry.get('reason', '')}")
    else:
        print("  (none)")
    print()

    print(f"-- RESOLVED debt ({len(resolved)}) — consider pruning --")
    if resolved:
        for entry in resolved:
            print(f"  {entry['target']} — {entry.get('reason', '')}")
    else:
        print("  (none)")
    print()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Triage a test-run log's failures against the pre-existing "
            "test-failure baseline (tests/known_test_debt.json)."
        )
    )
    parser.add_argument(
        "log",
        type=Path,
        help=(
            "Path to a run_tests_parallel.py-style log or plain pytest "
            "output file to triage."
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"Path to the baseline JSON (default: {DEFAULT_BASELINE}).",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "Append NEW failures to the baseline as UNREVIEWED entries "
            "(a human must then edit each entry's reason)."
        ),
    )
    parser.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help=(
            "Do not fail the gate solely because the baseline contains "
            "UNREVIEWED entries (genuine NEW failures still block)."
        ),
    )
    return parser


def run(argv: list[str] | None = None, *, today: date | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    today = today or datetime.now(timezone.utc).date()

    if not args.log.is_file():
        print(f"error: log file not found: {args.log}", file=sys.stderr)
        return 2

    log_text = args.log.read_text(encoding="utf-8", errors="replace")
    failing_targets = extract_failing_targets(log_text)

    try:
        baseline = load_baseline(args.baseline)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    new_failures, known, resolved = bucket(failing_targets, baseline)

    if args.update and new_failures:
        baseline = apply_update(baseline, new_failures, args.log, today)
        save_baseline(args.baseline, baseline)
        print(
            f"--update: appended {len(new_failures)} UNREVIEWED entr"
            f"{'y' if len(new_failures) == 1 else 'ies'} to {args.baseline}",
        )
        # Recompute against the updated baseline for reporting/gating below.
        new_failures, known, resolved = bucket(failing_targets, baseline)

    _print_report(args.baseline, args.log, failing_targets, new_failures, known, resolved)

    unreviewed = [
        e
        for e in baseline
        if str(e.get("reason", "")).startswith(UNREVIEWED_PREFIX)
    ]

    exit_code = 0
    if new_failures:
        print(f"BLOCK: {len(new_failures)} NEW failure(s) not covered by the baseline.")
        exit_code = 1
    if unreviewed and not args.allow_unreviewed:
        names = ", ".join(str(e["target"]) for e in unreviewed)
        print(
            f"BLOCK: {len(unreviewed)} UNREVIEWED baseline entr"
            f"{'y' if len(unreviewed) == 1 else 'ies'} require human review "
            f"before this gate can pass (edit the reason, or pass "
            f"--allow-unreviewed to bypass): {names}"
        )
        exit_code = 1

    if exit_code == 0:
        print("PASS: no new failures; baseline is fully reviewed.")

    return exit_code


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
