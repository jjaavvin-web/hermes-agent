#!/usr/bin/env python3
"""Upstream-merge preflight — semantic-collision detectors, one command.

Bundles the ad-hoc probes that caught real P1s during the v0.20.1 upstream
merge (conflict-marker leftovers, an argparse subcommand collision, a
duplicate-method paste, a dropped config key) into ONE runnable gate, so the
next merge gets them for free instead of re-deriving them by hand.

Checks (run in order, table printed at the end, non-zero exit on any FAIL):

  1. conflict-marker scan   — ``git diff --check`` "leftover conflict marker"
                               hits plus a direct ``git grep`` for
                               ``^<<<<<<<``/``^>>>>>>>`` across tracked files.
  2. CLI-boot smoke          — imports/boots ``hermes_cli.main`` in a fresh
                               ``HERMES_HOME``; catches argparse subcommand
                               collisions (e.g. the pause/resume crash).
  3. AST duplicate-definition sweep — every ``.py`` file changed between
                               ``--base`` and ``HEAD``: flags a same-name
                               def/class introduced twice in one scope by the
                               merge (property/setter pairs and ``_`` are not
                               findings).
  4. config-schema key diff — ``DEFAULT_CONFIG`` dict-literal key paths in
                               ``hermes_cli/config_defaults.py``, ``--base``
                               vs ``HEAD``; a DROPPED key path is a FAIL,
                               an added one is INFO only.
  5. web route-manifest drift — the CI dashboard-route-drift gate, runnable
                               standalone: compares the live FastAPI route
                               table against
                               ``tests/fixtures/dashboard_route_manifest.json``.

Usage:
    python scripts/upstream_merge_preflight.py --base <rev>

``--base`` is always required — there is no implicit merge-base computation
here (a bare ``origin/main`` merge-base is not meaningful once the fork has
diverged; the caller must say what "before the merge" means).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

SCRIPT_REPO = Path(__file__).resolve().parents[1]

# Fields on ast.stmt nodes that hold nested statement lists belonging to the
# SAME enclosing scope (if/for/while/with/try do not introduce a new Python
# scope, unlike def/class). Used by ``find_duplicate_defs`` below to recurse
# into control-flow bodies without merging sibling branches together.
_NESTED_STMT_FIELDS = ("body", "orelse", "finalbody")


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    extra_lines: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _run_git(repo: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def rev_exists(repo: Path, rev: str) -> bool:
    proc = _run_git(repo, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"])
    return proc.returncode == 0


def git_show(repo: Path, rev: str, rel_path: str) -> str | None:
    """Return a tracked file's content at ``rev``, or None if it's absent there."""
    proc = _run_git(repo, ["show", f"{rev}:{rel_path}"])
    if proc.returncode != 0:
        return None
    return proc.stdout


def changed_python_files(repo: Path, base: str) -> list[str]:
    proc = _run_git(repo, ["diff", "--name-only", base, "HEAD"])
    return [line for line in proc.stdout.splitlines() if line.endswith(".py")]


# ---------------------------------------------------------------------------
# Check 1 — conflict-marker scan
# ---------------------------------------------------------------------------


def check_conflict_markers(repo: Path, base: str) -> CheckResult:
    name = "1. conflict-marker scan"

    diff_check = _run_git(repo, ["diff", "--check", base, "HEAD"])
    if diff_check.returncode not in (0, 1, 2):
        return CheckResult(
            name,
            False,
            f"git diff --check errored (exit {diff_check.returncode})",
            diff_check.stderr.splitlines(),
        )
    marker_hits = [
        line for line in diff_check.stdout.splitlines() if "conflict marker" in line.lower()
    ]

    grep = _run_git(
        repo,
        ["grep", "-I", "-n", "-E", r"^<<<<<<<|^>>>>>>>", "--", ".", ":(exclude)node_modules"],
    )
    if grep.returncode == 0:
        grep_hits = [line for line in grep.stdout.splitlines() if line.strip()]
    elif grep.returncode == 1:
        grep_hits = []
    else:
        return CheckResult(
            name,
            False,
            f"git grep errored (exit {grep.returncode})",
            grep.stderr.splitlines(),
        )

    hits = marker_hits + grep_hits
    passed = not hits
    detail = "no conflict markers found" if passed else f"{len(hits)} conflict-marker hit(s)"
    return CheckResult(name, passed, detail, hits)


# ---------------------------------------------------------------------------
# Check 2 — CLI-boot smoke
# ---------------------------------------------------------------------------


def has_preflight_parser_builder(repo: Path) -> bool:
    """True iff hermes_cli/main.py exposes an importable parser-builder.

    ``main()`` in this codebase builds every subparser inline inside the
    function body (no ``build_parser()``-style factory) as of the current
    merge; this checks the live file so the faster import-based smoke test
    is used automatically the day that changes.
    """
    main_py = repo / "hermes_cli" / "main.py"
    if not main_py.exists():
        return False
    text = main_py.read_text(encoding="utf-8")
    return "def build_parser_for_preflight(" in text


def check_cli_boot_smoke(repo: Path, python_exe: str) -> CheckResult:
    name = "2. CLI-boot smoke"
    use_builder = has_preflight_parser_builder(repo)
    if use_builder:
        cmd = [
            python_exe,
            "-c",
            "import hermes_cli.main; hermes_cli.main.build_parser_for_preflight()",
        ]
    else:
        cmd = [python_exe, "-m", "hermes_cli.main", "--help"]

    tmp_home = tempfile.mkdtemp(prefix="hermes-preflight-home-")
    try:
        env = os.environ.copy()
        env["HERMES_HOME"] = tmp_home
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name,
                False,
                f"timed out after 60s running: {' '.join(cmd)}",
                [],
            )
        elapsed = time.monotonic() - start
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)

    passed = proc.returncode == 0
    detail = f"`{' '.join(cmd)}` exited {proc.returncode} in {elapsed:.2f}s"
    extra = [] if passed else (proc.stdout.splitlines() + proc.stderr.splitlines())[-25:]
    return CheckResult(name, passed, detail, extra)


# ---------------------------------------------------------------------------
# Check 3 — AST duplicate-definition sweep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateDef:
    scope: str  # dotted enclosing def/class path, "" for module level
    name: str
    lines: tuple[int, ...]


def _decorator_matches_property_pattern(decorator: ast.expr, name: str) -> bool:
    if isinstance(decorator, ast.Name) and decorator.id == "property":
        return True
    if isinstance(decorator, ast.Attribute) and decorator.attr in ("setter", "deleter", "getter"):
        value = decorator.value
        if isinstance(value, ast.Name) and value.id == name:
            return True
        if isinstance(value, ast.Attribute) and value.attr == name:
            return True
    return False


def _is_property_pattern(name: str, defs: Sequence[ast.stmt]) -> bool:
    """True iff every def in the group is a legitimate @property/@x.setter member."""
    if any(isinstance(d, ast.ClassDef) for d in defs):
        return False
    for d in defs:
        decorators = getattr(d, "decorator_list", [])
        if not any(_decorator_matches_property_pattern(dec, name) for dec in decorators):
            return False
    return True


def find_duplicate_defs(source: str) -> list[DuplicateDef]:
    """Parse ``source`` and return same-name def/class collisions per scope.

    A collision is flagged only between def/class statements that are
    DIRECT siblings in the exact same literal statement list — this is the
    shape a bad merge-conflict resolution actually produces (two full
    definitions pasted back to back). If/else branches, try/except
    alternatives, and platform-conditional overrides live in different
    statement lists (``If.body`` vs ``If.orelse``, each ``except`` handler's
    own body, ...) and are deliberately never compared against each other,
    since conditionally-exclusive redefinition is a common, legitimate
    pattern (Windows/POSIX fallbacks, optional-import shims) and flagging it
    would bury the real signal in noise.

    Excludes ``_`` (conventional throwaway name) and property/setter/deleter
    groups. Raises ``SyntaxError`` for unparseable source — callers decide
    how to treat that (this sweep only makes sense on valid Python).
    """
    tree = ast.parse(source)
    results: list[DuplicateDef] = []

    def check_list(stmts: Sequence[ast.stmt], scope_path: str) -> None:
        direct_defs = [
            s for s in stmts if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        groups: dict[str, list[ast.stmt]] = {}
        for d in direct_defs:
            groups.setdefault(d.name, []).append(d)
        for def_name, group in groups.items():
            if def_name == "_" or len(group) < 2:
                continue
            if _is_property_pattern(def_name, group):
                continue
            results.append(
                DuplicateDef(
                    scope=scope_path,
                    name=def_name,
                    lines=tuple(getattr(d, "lineno", -1) for d in group),
                )
            )

        # Each def/class becomes its own new scope.
        for d in direct_defs:
            child_scope = f"{scope_path}.{d.name}" if scope_path else d.name
            check_list(d.body, child_scope)

        # Control-flow sub-lists stay in the SAME scope but are each checked
        # independently — never merged with a sibling branch.
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # body already recursed into above
            if isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    check_list(handler.body, scope_path)
            for field_name in _NESTED_STMT_FIELDS:
                value = getattr(stmt, field_name, None)
                if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                    check_list(value, scope_path)

    check_list(tree.body, "")
    return results


def find_merge_introduced_duplicates(
    head_source: str, base_source: str | None
) -> list[DuplicateDef]:
    """Duplicates in ``head_source`` that were NOT already duplicates at base.

    A duplicate present at both base and head is pre-existing debt, not
    something this merge introduced — it is filtered out here rather than
    reported. ``base_source`` of None (file didn't exist at base) means
    every head duplicate counts as merge-introduced. Unparseable base
    source is treated the same way: nothing to compare against, so nothing
    is filtered.
    """
    head_dupes = find_duplicate_defs(head_source)
    if not head_dupes:
        return []
    base_keys: set[tuple[str, str]] = set()
    if base_source is not None:
        try:
            base_keys = {(d.scope, d.name) for d in find_duplicate_defs(base_source)}
        except SyntaxError:
            base_keys = set()
    return [d for d in head_dupes if (d.scope, d.name) not in base_keys]


def check_duplicate_definitions(repo: Path, base: str) -> CheckResult:
    name = "3. AST duplicate-definition sweep"
    py_files = changed_python_files(repo, base)

    findings: list[str] = []
    scanned = 0
    for rel_path in py_files:
        head_source = git_show(repo, "HEAD", rel_path)
        if head_source is None:
            continue  # deleted at HEAD — nothing to sweep
        scanned += 1
        try:
            new_dupes = find_merge_introduced_duplicates(
                head_source, git_show(repo, base, rel_path)
            )
        except SyntaxError:
            continue

        for dupe in new_dupes:
            location = dupe.scope or "<module>"
            findings.append(
                f"{rel_path}: {location}.{dupe.name} defined at lines {list(dupe.lines)}"
            )

    passed = not findings
    detail = f"{scanned} changed .py file(s) scanned, {len(findings)} merge-introduced duplicate(s)"
    return CheckResult(name, passed, detail, findings)


# ---------------------------------------------------------------------------
# Check 4 — config-schema key diff
# ---------------------------------------------------------------------------


def _find_dict_literal_assignment(tree: ast.Module, var_name: str) -> ast.Dict | None:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Dict):
            if any(isinstance(t, ast.Name) and t.id == var_name for t in stmt.targets):
                return stmt.value
    return None


def _walk_dict_key_paths(dict_node: ast.Dict, prefix: str, paths: set[str]) -> None:
    for key_node, value_node in zip(dict_node.keys, dict_node.values):
        if key_node is None:
            continue  # a `**spread` entry — no static key path to record
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            key = key_node.value
        else:
            key = f"<non-str:{ast.dump(key_node)}>"
        path = f"{prefix}.{key}" if prefix else key
        paths.add(path)
        if isinstance(value_node, ast.Dict):
            _walk_dict_key_paths(value_node, path, paths)


def extract_config_key_paths(source: str, var_name: str = "DEFAULT_CONFIG") -> set[str]:
    """Return the set of dotted key paths in a ``{var_name} = {...}`` dict literal.

    Only descends into nested dict *literals*; list/tuple values are leaves.
    Raises ``ValueError`` if no top-level ``{var_name} = {...}`` assignment
    is found (a real structural change worth surfacing loudly, not silently
    treating as "zero keys").
    """
    tree = ast.parse(source)
    dict_node = _find_dict_literal_assignment(tree, var_name)
    if dict_node is None:
        raise ValueError(f"no top-level `{var_name} = {{...}}` dict literal found")
    paths: set[str] = set()
    _walk_dict_key_paths(dict_node, "", paths)
    return paths


def diff_config_key_paths(
    base_paths: set[str], head_paths: set[str]
) -> tuple[list[str], list[str]]:
    """Return (dropped, added) dotted key paths, both sorted.

    Dropped paths are a FAIL (a merge silently lost a config default);
    added paths are informational only.
    """
    dropped = sorted(base_paths - head_paths)
    added = sorted(head_paths - base_paths)
    return dropped, added


def check_config_schema_drift(repo: Path, base: str) -> CheckResult:
    name = "4. config-schema key diff"
    rel_path = "hermes_cli/config_defaults.py"

    head_source = git_show(repo, "HEAD", rel_path)
    if head_source is None:
        return CheckResult(name, False, f"{rel_path} missing at HEAD", [])
    try:
        head_paths = extract_config_key_paths(head_source)
    except (SyntaxError, ValueError) as exc:
        return CheckResult(name, False, f"could not parse DEFAULT_CONFIG at HEAD: {exc}", [])

    base_source = git_show(repo, base, rel_path)
    if base_source is None:
        base_paths: set[str] = set()
    else:
        try:
            base_paths = extract_config_key_paths(base_source)
        except (SyntaxError, ValueError) as exc:
            return CheckResult(name, False, f"could not parse DEFAULT_CONFIG at {base}: {exc}", [])

    dropped, added = diff_config_key_paths(base_paths, head_paths)
    passed = not dropped
    detail = f"{len(added)} key(s) added, {len(dropped)} key(s) dropped"
    extra = [f"DROPPED {k}" for k in dropped] + [f"INFO added {k}" for k in added]
    return CheckResult(name, passed, detail, extra)


# ---------------------------------------------------------------------------
# Check 5 — web route-manifest drift
# ---------------------------------------------------------------------------

_ROUTE_DRIFT_SNIPPET = """
import json
import sys

from hermes_cli import dashboard_smoke, web_server

expected = dashboard_smoke.load_route_manifest(sys.argv[1])
comparison = dashboard_smoke.compare_route_manifest(web_server.app, expected)
print(json.dumps(comparison))
"""


def check_route_manifest_drift(repo: Path, python_exe: str, manifest_path: Path) -> CheckResult:
    name = "5. web route-manifest drift"
    if not manifest_path.exists():
        return CheckResult(name, False, f"manifest not found: {manifest_path}", [])

    tmp_home = tempfile.mkdtemp(prefix="hermes-preflight-home-")
    try:
        env = os.environ.copy()
        env["HERMES_HOME"] = tmp_home
        try:
            proc = subprocess.run(
                [python_exe, "-c", _ROUTE_DRIFT_SNIPPET, str(manifest_path)],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(name, False, "timed out after 120s importing the dashboard app", [])
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)

    if proc.returncode != 0:
        tail = (proc.stdout.splitlines() + proc.stderr.splitlines())[-25:]
        return CheckResult(name, False, f"subprocess exited {proc.returncode}", tail)

    json_line = None
    for line in reversed(proc.stdout.splitlines()):
        if line.strip().startswith("{"):
            json_line = line.strip()
            break
    if json_line is None:
        return CheckResult(
            name, False, "no JSON result on stdout", proc.stdout.splitlines()[-25:]
        )

    comparison = json.loads(json_line)
    passed = bool(comparison.get("ok"))
    missing = comparison.get("missing", [])
    unexpected = comparison.get("unexpected", [])
    detail = (
        f"{comparison.get('actual_count')} live route(s); "
        f"missing={len(missing)} unexpected={len(unexpected)}"
    )
    extra = [f"MISSING {m}" for m in missing] + [f"UNEXPECTED {u}" for u in unexpected]
    return CheckResult(name, passed, detail, extra)


# ---------------------------------------------------------------------------
# Reporting + CLI
# ---------------------------------------------------------------------------


def _default_python(repo: Path) -> str:
    venv_python = repo / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    venv_python_win = repo / ".venv" / "Scripts" / "python.exe"
    if venv_python_win.exists():
        return str(venv_python_win)
    return sys.executable


def print_report(checks: Sequence[CheckResult]) -> None:
    name_width = max((len(c.name) for c in checks), default=4)
    name_width = max(name_width, len("CHECK"))
    header = f"{'CHECK':<{name_width}}  STATUS  DETAIL"
    print(header)
    print("-" * len(header))
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"{check.name:<{name_width}}  {status:<6}  {check.detail}")
    failing = [c for c in checks if not c.passed]
    if failing:
        print()
        print("FAIL detail:")
        for check in failing:
            print(f"  [{check.name}]")
            for line in check.extra_lines[:25]:
                print(f"    {line}")
            if len(check.extra_lines) > 25:
                print(f"    ... ({len(check.extra_lines) - 25} more line(s) omitted)")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the upstream-merge semantic-collision preflight checks.",
    )
    parser.add_argument(
        "--base",
        required=True,
        help=(
            "Revision to diff against (the pre-merge tip). Required explicitly — "
            "computing merge-base against origin/main is not meaningful once the "
            "fork has diverged from upstream."
        ),
    )
    parser.add_argument(
        "--repo",
        default=str(SCRIPT_REPO),
        help="Repo root to run checks against (default: this script's repo).",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Python executable for the subprocess checks (default: <repo>/.venv, else sys.executable).",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "Route manifest fixture path for check 5 "
            "(default: <repo>/tests/fixtures/dashboard_route_manifest.json)."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo = Path(args.repo).resolve()

    if not rev_exists(repo, args.base):
        print(f"error: --base {args.base!r} does not resolve to a commit in {repo}", file=sys.stderr)
        return 2

    python_exe = args.python or _default_python(repo)
    manifest_path = Path(args.manifest) if args.manifest else repo / "tests" / "fixtures" / "dashboard_route_manifest.json"

    checks = [
        check_conflict_markers(repo, args.base),
        check_cli_boot_smoke(repo, python_exe),
        check_duplicate_definitions(repo, args.base),
        check_config_schema_drift(repo, args.base),
        check_route_manifest_drift(repo, python_exe, manifest_path),
    ]

    print_report(checks)
    return 0 if all(c.passed for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
