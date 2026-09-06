#!/usr/bin/env python3
"""Compute the test footprint (blast radius) of a change set.

Lesson (2026-07-03, F4-L4 fake-green): a "full suite" run must be COMPUTED
from the touched files' test footprint, never taken from a curated list a
packet happens to carry.  A curated list can be green while the uncovered
surface is broken.

Usage:
    scripts/blast_radius_tests.py path/to/changed.py [more paths ...]
    scripts/blast_radius_tests.py --diff            # vs HEAD~1
    scripts/blast_radius_tests.py --diff origin/main

Output: sorted test paths (one per line) on stdout; a ready-to-run
venv/bin/pytest command on stderr.  tests/security/ is ALWAYS included.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
ALWAYS_INCLUDE = "tests/security"


def changed_files_from_diff(ref: str) -> list[str]:
    """Return files changed between *ref* and the working tree."""
    out = subprocess.run(
        ["git", "diff", "--name-only", ref],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def search_tokens_for(path: str) -> set[str]:
    """Tokens whose presence in a test file marks it as covering *path*."""
    p = Path(path)
    tokens = {p.name}  # e.g. backup.py
    if p.suffix == ".py":
        stem = p.stem  # e.g. backup
        tokens.add(stem)
        # dotted module path, e.g. hermes_cli.backup
        parts = [part for part in p.with_suffix("").parts if part not in (".", "")]
        if len(parts) > 1:
            tokens.add(".".join(parts))
    return tokens


def find_covering_tests(changed: list[str]) -> set[str]:
    """grep -l style scan: test files referencing any changed module."""
    token_sets = {f: search_tokens_for(f) for f in changed}
    hits: set[str] = set()
    for test_file in TESTS_DIR.rglob("test_*.py"):
        try:
            text = test_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = test_file.relative_to(REPO_ROOT).as_posix()
        for tokens in token_sets.values():
            if any(tok in text for tok in tokens):
                hits.add(rel)
                break
    # A changed test file is part of its own blast radius.
    for f in changed:
        if f.startswith("tests/") and f.endswith(".py") and (REPO_ROOT / f).exists():
            hits.add(Path(f).as_posix())
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute the pytest blast radius of a change set."
    )
    parser.add_argument("paths", nargs="*", help="Changed source files")
    parser.add_argument(
        "--diff",
        nargs="?",
        const="HEAD~1",
        default=None,
        metavar="REF",
        help="Derive changed files via git diff --name-only REF (default HEAD~1)",
    )
    args = parser.parse_args()

    changed: list[str] = list(args.paths)
    if args.diff is not None:
        changed.extend(changed_files_from_diff(args.diff))
    if not changed:
        parser.error("no input: pass file paths or --diff [REF]")

    selected = find_covering_tests(changed)
    # tests/security/ is always in scope; drop members subsumed by the dir.
    selected = {p for p in selected if not p.startswith(ALWAYS_INCLUDE + "/")}
    selected.add(ALWAYS_INCLUDE)

    ordered = sorted(selected)
    for path in ordered:
        print(path)

    cmd = "venv/bin/pytest " + " ".join(ordered)
    print(f"\n# run with:\n{cmd}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
