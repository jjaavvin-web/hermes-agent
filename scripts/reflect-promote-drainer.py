#!/usr/bin/env python3
"""reflect-promote-drainer — MEM-11 drainer CLI.

List the reflect-promote candidates awaiting promotion and, WHEN the
``reflect.promotion_enabled`` flag is on in ``~/.hermes/config.yaml``, promote
each one to MVMS as a lesson via the constrained mvms-writer MCP path
(``agent.reflect_promote_mvms.MvmsWriterRecorder``).

DEFAULT-OFF SAFETY: with the flag off (the default) this prints the candidates
and exits without writing anything — identical to today's behaviour. The
dashboard Approve button stays disabled and the web approve endpoint keeps
returning 501 until the operator flips the flag AND un-disables the button.

Usage:
  reflect-promote-drainer.py --list           # show awaiting candidates (never writes)
  reflect-promote-drainer.py --drain           # promote when flag on; no-op when off
  reflect-promote-drainer.py --drain --json     # machine-readable report

Idempotent: re-running --drain skips already-promoted rows and relies on the
MVMS 24h source-keyed dedup as a second layer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the repo importable when run as a standalone script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.reflect_promote import (  # noqa: E402
    default_queue_path,
    drain_approved_queue,
    list_approved,
)


def _cmd_list(queue_path: Path, as_json: bool) -> int:
    candidates = list_approved(queue_path=queue_path)
    if as_json:
        print(json.dumps({"awaiting": [c.to_row() for c in candidates]}, indent=2))
    else:
        if not candidates:
            print("No reflect-promote candidates awaiting promotion.")
        for c in candidates:
            print(f"  {c.id:<28} [{c.project}] {c.situation[:70]}")
        print(f"\n{len(candidates)} candidate(s) awaiting promotion.")
    return 0


def _cmd_drain(queue_path: Path, as_json: bool) -> int:
    report = drain_approved_queue(queue_path=queue_path)
    payload = report.to_dict()
    if as_json:
        print(json.dumps(payload, indent=2))
        return 0 if not report.errors else 1

    if not report.enabled:
        print(
            "reflect.promotion_enabled is OFF — no candidates promoted. "
            "Flip the flag in ~/.hermes/config.yaml (and un-disable the dashboard "
            "Approve button) to enable MVMS promotion."
        )
        return 0
    print(f"Promoted:    {len(report.promoted)}  {report.promoted}")
    print(f"Deduplicated: {len(report.deduplicated)}  {report.deduplicated}")
    print(f"Already done: {len(report.skipped_already_promoted)}")
    if report.errors:
        print(f"Errors:      {len(report.errors)}")
        for cid, err in report.errors:
            print(f"  {cid}: {err}", file=sys.stderr)
    return 0 if not report.errors else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true",
                       help="list awaiting candidates without promoting (never writes)")
    group.add_argument("--drain", action="store_true",
                       help="promote awaiting candidates (no-op unless flag on)")
    parser.add_argument("--queue-path", type=Path, default=None,
                        help=f"override queue path (default: {default_queue_path()})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    queue_path = args.queue_path or default_queue_path()
    if args.list:
        return _cmd_list(queue_path, args.json)
    return _cmd_drain(queue_path, args.json)


if __name__ == "__main__":
    sys.exit(main())
