#!/usr/bin/env python3
"""Doc/config-coverage lint (WF-08).

Guards against config keys that are read at runtime but never documented in
``AGENTS.md``.  The original trigger was INFRA-C-03's global dispatch-concurrency
cap ``kanban.max_spawn``: it is a *live* cap on how many dispatcher-spawned
workers may run at once, set via config but absent from the docs, so operators
had no canonical reference for the safety knob the global-concurrency story
rests on.

What it checks
--------------
For every ``kanban.*`` config key the lint considers "load-bearing", there must
be a ``kanban.<key>`` reference in ``AGENTS.md``.  The key set is *derived*
(not hand-copied from the docs) so the lint can't silently rot:

  * every key in ``DEFAULT_CONFIG["kanban"]`` (the canonical defaults), plus
  * the runtime-only concurrency caps that have no default entry but are read
    via ``kanban_cfg.get(...)`` — ``max_spawn`` (C-03), ``max_in_progress``,
    and ``global_max_running``.

Because the key list comes from real code, removing a doc line for any of these
keys turns the lint RED (failable, not vacuous), and adding a new kanban config
key without documenting it also turns it RED.

Usage
-----
    python scripts/lint_config_docs.py          # exits non-zero on any gap
    python scripts/lint_config_docs.py --list    # print the checked key set

Importable: ``from scripts.lint_config_docs import find_undocumented_keys``.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Iterable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"

# Runtime-only concurrency caps consulted via ``kanban_cfg.get(...)`` that have
# no entry in DEFAULT_CONFIG but are nonetheless real, documented behavior.
# ``max_spawn`` is the C-03 global dispatch-concurrency cap.
RUNTIME_ONLY_KANBAN_KEYS: tuple[str, ...] = (
    "max_spawn",
    "max_in_progress",
    "global_max_running",
)


def _default_kanban_keys() -> set[str]:
    """Keys defined in ``DEFAULT_CONFIG['kanban']`` (the canonical defaults)."""
    # Import lazily so a config import error surfaces with a clear message
    # rather than at module import time.
    from hermes_cli.config import DEFAULT_CONFIG

    kanban = DEFAULT_CONFIG.get("kanban", {})
    if not isinstance(kanban, dict):  # pragma: no cover — defensive
        return set()
    return set(kanban.keys())


def required_kanban_keys() -> set[str]:
    """The full set of ``kanban.*`` keys that must be documented."""
    return _default_kanban_keys() | set(RUNTIME_ONLY_KANBAN_KEYS)


def _documented_kanban_keys(agents_md_text: str) -> set[str]:
    """Keys that appear as ``kanban.<key>`` in the docs text."""
    return set(re.findall(r"kanban\.([a-z_]+)", agents_md_text))


def find_undocumented_keys(
    agents_md_path: pathlib.Path = AGENTS_MD,
    required: Iterable[str] | None = None,
) -> list[str]:
    """Return the sorted list of required kanban keys missing from the docs.

    Empty list == lint passes.
    """
    text = agents_md_path.read_text(encoding="utf-8")
    documented = _documented_kanban_keys(text)
    req = set(required) if required is not None else required_kanban_keys()
    return sorted(req - documented)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the set of kanban.* keys the lint requires, then exit",
    )
    args = parser.parse_args(argv)

    if args.list:
        for key in sorted(required_kanban_keys()):
            print(f"kanban.{key}")
        return 0

    missing = find_undocumented_keys()
    if missing:
        print(
            "doc/config lint FAILED: these kanban.* config keys are read at "
            "runtime but have no `kanban.<key>` reference in AGENTS.md:",
            file=sys.stderr,
        )
        for key in missing:
            print(f"  - kanban.{key}", file=sys.stderr)
        print(
            "\nDocument each key in the Kanban section of AGENTS.md "
            "(next to the other kanban.* keys).",
            file=sys.stderr,
        )
        return 1

    print(
        f"doc/config lint OK: all {len(required_kanban_keys())} kanban.* keys "
        "are documented in AGENTS.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
