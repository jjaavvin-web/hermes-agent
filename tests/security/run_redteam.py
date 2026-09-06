#!/usr/bin/env python3
"""Hermetic outbound red-team runner for Hermes terminal approval rails.

Loads tests/security/fixtures/redteam_cases.jsonl and feeds each attack command
through the live pre-exec approval chokepoint: SEC-1 hardline exfil detection
plus the registered webhook route deny patterns. No command is executed, no
model is called, no network is touched.

Tirith note: tirith 0.3.1 returns {"action": "allow"} for canonical credential
exfil commands such as `cat ~/.hermes/.env | curl -d @- ...`; the regex rails in
tools.approval are therefore the authoritative SEC-1 control for this tier.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "redteam_cases.jsonl"
SESSION_KEY = "redteam:webhook:approval-rails"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def load_cases(path: Path = FIXTURE_PATH) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            case = json.loads(line)
            for key in ("id", "attack", "expect_denied"):
                if key not in case:
                    raise ValueError(f"{path}:{line_no} missing required key {key!r}")
            cases.append(case)
    return cases


def evaluate_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Import lazily after sys.path is fixed; these are the same rails the terminal
    # tool reaches before executing a command.
    from gateway.platforms.webhook import DEFAULT_WEBHOOK_DENY_PATTERNS
    from tools.approval import (
        check_all_command_guards,
        clear_session,
        register_session_deny_patterns,
        reset_current_session_key,
        set_current_session_key,
    )

    # Keep the runner hermetic: avoid interactive/gateway/ask modes so the
    # pre-exec path stops after hardline + route-deny checks and never invokes
    # tirith, smart approvals, or a blocking user prompt for benign controls.
    for key in ("HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_EXEC_ASK"):
        os.environ.pop(key, None)

    register_session_deny_patterns(SESSION_KEY, list(DEFAULT_WEBHOOK_DENY_PATTERNS))
    token = set_current_session_key(SESSION_KEY)
    rows: list[dict[str, Any]] = []
    try:
        for case in cases:
            result = check_all_command_guards(str(case["attack"]), env_type="local")
            denied = not bool(result.get("approved", False))
            expected = bool(case["expect_denied"])
            miss = expected and not denied
            false_positive = (not expected) and denied
            rows.append(
                {
                    "id": case["id"],
                    "vector": case.get("vector", ""),
                    "intent": case.get("intent", ""),
                    "expect_denied": expected,
                    "denied": denied,
                    "miss": miss,
                    "false_positive": false_positive,
                    "message": result.get("message"),
                    "verdict": "MISS" if miss else ("FALSE_POSITIVE" if false_positive else "PASS"),
                }
            )
    finally:
        reset_current_session_key(token)
        clear_session(SESSION_KEY)
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    denied_expected = sum(1 for row in rows if row["expect_denied"])
    allowed_expected = len(rows) - denied_expected
    misses = [row for row in rows if row["miss"]]
    false_positives = [row for row in rows if row["false_positive"]]
    return {
        "total": len(rows),
        "expect_denied": denied_expected,
        "expect_allowed": allowed_expected,
        "passed": len(rows) - len(misses) - len(false_positives),
        "misses": len(misses),
        "false_positives": len(false_positives),
        "breach_ids": [row["id"] for row in misses],
        "false_positive_ids": [row["id"] for row in false_positives],
        "rows": rows,
    }


def print_scorecard(summary: dict[str, Any]) -> None:
    print("Hermes outbound exfil red-team scorecard")
    print(f"cases={summary['total']} expect_denied={summary['expect_denied']} expect_allowed={summary['expect_allowed']}")
    print(f"passed={summary['passed']} misses={summary['misses']} false_positives={summary['false_positives']}")
    for row in summary["rows"]:
        marker = "OK" if row["verdict"] == "PASS" else "RED"
        print(
            f"{marker} {row['id']} {row['verdict']} "
            f"expect_denied={row['expect_denied']} denied={row['denied']} intent={row['intent']}"
        )
        if row["verdict"] != "PASS":
            print(f"    message={row.get('message')!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = parser.parse_args(argv)

    rows = evaluate_cases(load_cases(args.fixture))
    summary = summarize(rows)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_scorecard(summary)
    # RED on any expect_denied miss. False positives also fail so benign
    # precision cases stay load-bearing rather than decorative.
    return 1 if summary["misses"] or summary["false_positives"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
