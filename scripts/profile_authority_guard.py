#!/usr/bin/env python3
"""profile_authority_guard.py — read-only OPEN/CLOSED profile-authority tripwire.

Parses every ~/.hermes/profiles/<name>/config.yaml and asserts the live tool/exec
authority matches the governance-intended `assert` block in PROFILE-AUTHORITY-MATRIX.yaml
(the A2 / Card 48 keystone deliverable). Anti-rot companion to the matrix.

This script is INERT and read-only: it opens config files for reading only, never
writes, never restarts anything, never touches the network.

Modes
-----
  (default / "observe")  Print a per-profile authority report + any drift, then
                         exit 0 REGARDLESS of drift. Use for monitoring / dashboards.
  --check                Strict CI/tripwire mode: exit 1 if ANY asserted field drifts
                         (or a profile is missing / unknown). Exit 0 only when clean.
  --json                 Emit the full report as JSON (still honors --check exit code).

Why drift is EXPECTED on first run: the matrix encodes the *target* (post-hardening)
authority. Until the orchestrator applies the tier-(a) diffs in APPLY-PLAN.md, the
CLOSED rows intentionally differ from live -> observe mode shows that delta, --check
fails until apply completes.

NOT checked here (by design): codex-native sandbox mode (HERMES_TERMINAL_SECURITY_MODE).
It is read from the process-global env, not any profile config field, so there is
nothing in profiles/<name>/config.yaml to assert against. The matrix records its
target as `codex_sandbox.enforcement: pending-A2b`; this guard reports it as
informational only.

Dependencies: stdlib + pyyaml. Every text-mode open() passes encoding='utf-8'.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write("profile_authority_guard: PyYAML is required (pip install pyyaml)\n")
    sys.exit(2)

DEFAULT_PROFILES_DIR = Path.home() / ".hermes" / "profiles"
DEFAULT_MATRIX = (
    Path.home()
    / ".hermes"
    / "audits"
    / "20260626-governance-eval-burn"
    / "PROFILE-AUTHORITY-MATRIX.yaml"
)


# --------------------------------------------------------------------------- #
# config extraction helpers
# --------------------------------------------------------------------------- #
def _dig(cfg: dict, *path: str) -> Any:
    """Walk a nested dict by keys; return None if any key is missing."""
    node: Any = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _as_set(value: Any) -> Optional[frozenset]:
    """Normalize a toolset list to a comparable set; None stays None."""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return frozenset(str(v).strip() for v in value)
    return frozenset({str(value).strip()})


# Map each `assert`-block key -> (extractor, is_set_comparison).
# Adding a new assertable field is a one-line change here.
def _extract(cfg: dict, field: str) -> Any:
    if field == "provider":
        return _dig(cfg, "model", "provider")
    if field == "max_turns":
        return _dig(cfg, "agent", "max_turns")
    if field == "disabled_toolsets":
        return _dig(cfg, "agent", "disabled_toolsets")
    if field == "claude_cli_allowed_tools":
        return _dig(cfg, "claude_cli", "allowed_tools")
    if field == "claude_cli_permission_mode":
        return _dig(cfg, "claude_cli", "permission_mode")
    if field == "platform_toolsets_cli":
        return _dig(cfg, "platform_toolsets", "cli")
    if field == "memory_enabled":
        return _dig(cfg, "memory", "memory_enabled")
    return "<UNKNOWN-FIELD>"


_SET_FIELDS = {"disabled_toolsets", "platform_toolsets_cli"}


def _values_match(field: str, expected: Any, actual: Any) -> bool:
    if field in _SET_FIELDS:
        return _as_set(expected) == _as_set(actual)
    return expected == actual


def _fmt(field: str, value: Any) -> str:
    if field in _SET_FIELDS:
        s = _as_set(value)
        return "unset" if s is None else "[" + ", ".join(sorted(s)) + "]"
    if value is None:
        return "unset"
    return str(value)


# --------------------------------------------------------------------------- #
# core check
# --------------------------------------------------------------------------- #
def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def evaluate(profiles_dir: Path, matrix_path: Path) -> dict:
    """Return a structured report comparing live configs to the matrix."""
    matrix = load_yaml(matrix_path)
    rows = {row["name"]: row for row in matrix.get("profiles", []) if "name" in row}

    # Live profiles actually present on disk.
    live_dirs = {}
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir()):
            cfg = child / "config.yaml"
            if cfg.is_file():
                live_dirs[child.name] = cfg

    report: dict[str, Any] = {
        "matrix": str(matrix_path),
        "profiles_dir": str(profiles_dir),
        "profiles": [],
        "extra_live_profiles": [],   # on disk, not in matrix -> unaudited authority surface
        "missing_profiles": [],      # in matrix (scan=true) but no dir on disk
        "drift_count": 0,
        "fail_count": 0,             # drift + extra + missing == strict failures
    }

    # Profiles on disk but absent from the matrix == drift (someone added a lane).
    for name in live_dirs:
        if name not in rows:
            report["extra_live_profiles"].append(name)
            report["fail_count"] += 1

    for name in sorted(rows):
        row = rows[name]
        scan = row.get("scan", True)
        prof: dict[str, Any] = {
            "name": name,
            "class": row.get("class", "?"),
            "enforcement_model": row.get("enforcement_model", "?"),
            "tier": row.get("tier", "none"),
            "scanned": bool(scan),
            "fields": [],
            "drift": False,
            "notes": [],
        }

        # codex sandbox is informational only (not config-readable).
        sandbox = row.get("codex_sandbox")
        if isinstance(sandbox, dict):
            prof["codex_sandbox"] = sandbox
            if sandbox.get("enforcement") == "pending-A2b":
                prof["notes"].append(
                    "codex-native sandbox target=%s NOT enforceable per-profile today "
                    "(global env; pending-A2b) -> informational, not asserted"
                    % sandbox.get("target")
                )

        if not scan:
            prof["notes"].append("not scanned (root/default config is outside profiles/)")
            report["profiles"].append(prof)
            continue

        cfg_path = live_dirs.get(name)
        if cfg_path is None:
            prof["notes"].append("MISSING: matrix row has scan=true but no config.yaml on disk")
            report["missing_profiles"].append(name)
            report["fail_count"] += 1
            report["profiles"].append(prof)
            continue

        try:
            cfg = load_yaml(cfg_path)
        except yaml.YAMLError as exc:
            prof["notes"].append(f"PARSE-ERROR: {exc}")
            report["fail_count"] += 1
            prof["drift"] = True
            report["profiles"].append(prof)
            continue

        asserts = row.get("assert") or {}
        if not asserts:
            prof["notes"].append("no assert block (EXCLUDE/informational) -> parsed OK, nothing enforced")

        for field, expected in asserts.items():
            actual = _extract(cfg, field)
            ok = _values_match(field, expected, actual)
            prof["fields"].append(
                {
                    "field": field,
                    "expected": _fmt(field, expected),
                    "actual": _fmt(field, actual),
                    "ok": ok,
                }
            )
            if not ok:
                prof["drift"] = True

        if prof["drift"]:
            report["drift_count"] += 1
            report["fail_count"] += 1

        report["profiles"].append(prof)

    return report


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_text(report: dict) -> str:
    out: list[str] = []
    out.append("PROFILE AUTHORITY GUARD")
    out.append(f"  matrix:   {report['matrix']}")
    out.append(f"  profiles: {report['profiles_dir']}")
    out.append("")
    for prof in report["profiles"]:
        if not prof["scanned"]:
            out.append(f"  [SKIP] {prof['name']:<22} class={prof['class']} (not scanned)")
            for note in prof["notes"]:
                out.append(f"           - {note}")
            continue
        status = "DRIFT" if prof["drift"] else "OK"
        marker = "!!" if prof["drift"] else "ok"
        out.append(
            f"  [{marker}] {prof['name']:<22} class={prof['class']:<16} "
            f"model={prof['enforcement_model']:<11} tier={prof['tier']} -> {status}"
        )
        for f in prof["fields"]:
            flag = "  " if f["ok"] else "DRIFT->"
            out.append(
                f"           {flag}{f['field']:<26} expected={f['expected']:<40} actual={f['actual']}"
            )
        for note in prof["notes"]:
            out.append(f"           - {note}")
    out.append("")
    if report["extra_live_profiles"]:
        out.append(
            "  UNAUDITED live profiles (on disk, not in matrix): "
            + ", ".join(report["extra_live_profiles"])
        )
    if report["missing_profiles"]:
        out.append(
            "  MISSING profiles (in matrix, no config on disk): "
            + ", ".join(report["missing_profiles"])
        )
    out.append("")
    out.append(
        f"  SUMMARY: {report['drift_count']} profile(s) drift, "
        f"{len(report['extra_live_profiles'])} unaudited, "
        f"{len(report['missing_profiles'])} missing "
        f"(strict fail_count={report['fail_count']})"
    )
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only OPEN/CLOSED profile-authority tripwire (anti-rot for PROFILE-AUTHORITY-MATRIX.yaml).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Strict mode: exit 1 on any drift/extra/missing. Default is observe (exit 0).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON report.")
    parser.add_argument(
        "--profiles-dir",
        default=str(DEFAULT_PROFILES_DIR),
        help=f"Profiles root (default: {DEFAULT_PROFILES_DIR})",
    )
    parser.add_argument(
        "--matrix",
        default=str(DEFAULT_MATRIX),
        help=f"Authority matrix YAML (default: {DEFAULT_MATRIX})",
    )
    args = parser.parse_args(argv)

    matrix_path = Path(args.matrix)
    if not matrix_path.is_file():
        sys.stderr.write(f"profile_authority_guard: matrix not found: {matrix_path}\n")
        return 2

    report = evaluate(Path(args.profiles_dir), matrix_path)

    out = json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report)
    try:
        print(out)
        sys.stdout.flush()
    except BrokenPipeError:
        # A downstream consumer closed the pipe early (e.g. `guard --json | head`).
        # Redirect stdout to /dev/null so the interpreter-shutdown flush does not
        # re-raise "Exception ignored in ... BrokenPipeError", then exit cleanly (0).
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0

    if args.check and report["fail_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
