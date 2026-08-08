#!/usr/bin/env python3
"""Fork-parity assurance runner — machine-readable verdicts, fail closed.

Evaluates the 71-item fork-parity docket manifest against a target checkout,
runs the decisive security suites in FRESH pytest subprocesses (cwd pinned to
the target), collects per-subprocess import provenance via the
``FORK_PARITY_PROVENANCE_OUT`` hook in ``tests/conftest.py``, and emits one
JSON verdict document with the gates separated:

  * ``absolute_suite``      — full tests/security/ green: any failure, error,
                              skip, xfail, or xpass is non-PASS; junit report
                              missing (collection failure) is non-PASS;
                              collected-count drift below the manifest pin is
                              non-PASS.
  * ``differential``        — every corrected previously-stale test family in
                              ``closed_stale_tests`` was collected and passed.
  * ``provenance``          — every repo-owned module imported by the decisive
                              subprocesses resolved from the target checkout
                              (no editable/deployment fallback, no foreign
                              checkout, no split packages); missing provenance
                              evidence is non-PASS.
  * ``phase_eligibility``   — runtime lineage agrees with each item's manifest
                              phase; ineligible items classify WRONG_PHASE,
                              never regression PASS/FAIL.
  * ``parity_completeness`` — manifest integrity holds, all 71 items are
                              represented with exact classification counts,
                              zero item FAILs, and every eligible item's proof
                              tests ran green in this run.
  * ``merge_integration_gate`` — conjunction of the above, bound to the exact
                              target realpath + HEAD. Explicitly does NOT
                              claim deployment or merge readiness
                              (``claims.deployment_ready`` /
                              ``claims.merge_ready`` are always false).

Exit code 0 only when every gate passes.

``--ancestry-mode assume-eligible`` (+ ``--skip-full-suite``) exists ONLY for
sealed git-less mutation fixtures; the emitted verdict records the mode and
the reduced suite so a fixture run can never masquerade as assurance.

Usage:
  python scripts/fork_parity_guard.py --out-dir DIR [--repo PATH]
      [--manifest PATH] [--python PATH]
      [--ancestry-mode git|assume-eligible] [--skip-full-suite]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT_REPO = Path(__file__).resolve().parents[1]

# In --skip-full-suite (fixture) mode the decisive suite shrinks to the guard
# + the corrected previously-stale files so the differential gate keeps its
# input while the fixture run stays fast.
FIXTURE_DECISIVE_FILES = [
    "tests/security/test_fork_parity_guard.py",
    "tests/security/test_credential_persistence.py",
    "tests/security/test_dashboard_auth_boundary.py",
]


def _load_lib():
    lib_path = SCRIPT_REPO / "tests" / "security" / "fork_parity_lib.py"
    spec = importlib.util.spec_from_file_location("fork_parity_lib_runner", lib_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_suite(
    *,
    name: str,
    pytest_args: list,
    repo: Path,
    python: str,
    out_dir: Path,
    extra_env: dict | None = None,
) -> dict:
    junit = out_dir / f"junit-{name}.xml"
    provenance = out_dir / f"provenance-{name}.jsonl"
    log_path = out_dir / f"suite-{name}.log"
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["FORK_PARITY_PROVENANCE_OUT"] = str(provenance)
    # Ambient PYTHONPATH must never decide module resolution for a decisive
    # run; the provenance gate would catch the fallout, but don't invite it.
    env.pop("PYTHONPATH", None)
    env.update(extra_env or {})
    cmd = [
        python, "-m", "pytest", *pytest_args,
        "-q",
        # Non-strict XPASS is recorded as a plain pass in xunit1 junit, so the
        # absolute gate cannot see it; forcing strict turns any XPASS into a
        # visible failure (cockpit mutation class m10 proves the seam).
        "-o", "xfail_strict=true",
        "-o", "junit_family=xunit1",
        f"--junitxml={junit}",
        "-o", f"cache_dir={out_dir / f'pytest-cache-{name}'}",
    ]
    started = time.time()
    result = subprocess.run(
        cmd, cwd=str(repo), env=env, capture_output=True, text=True, timeout=3600,
    )
    log_path.write_text(
        result.stdout + "\n--- STDERR ---\n" + result.stderr, encoding="utf-8"
    )
    return {
        "name": name,
        "cmd": cmd,
        "cwd": str(repo),
        "exit_code": result.returncode,
        "duration_s": round(time.time() - started, 2),
        "junit_xml": str(junit),
        "provenance_jsonl": str(provenance),
        "log": str(log_path),
        "log_sha256": _sha256(log_path) if log_path.exists() else None,
    }


def _xpassed_from_log(log_path: Path) -> int:
    """XPASS count from pytest's terminal summary line.

    xunit1 junit records a NON-STRICT xpass as a plain pass, and an explicit
    ``strict=False`` marker overrides the ``xfail_strict`` ini default, so the
    junit report alone cannot see this outcome (cockpit mutation class m10).
    The ``-q`` summary line ("… N xpassed …") reports it reliably."""
    import re as _re

    if not log_path.exists():
        return 0
    text = log_path.read_text(encoding="utf-8", errors="replace")
    total = 0
    for match in _re.finditer(r"(\d+) xpassed", text):
        total = max(total, int(match.group(1)))
    return total


def _parse_junit(junit_path: Path) -> dict:
    """Outcome table from a junit_family=xunit1 report.

    node_id is ``file::name`` (rootdir-relative ``file`` attr); outcome is one
    of passed/failed/error/skipped/xfailed/xpassed. A missing report means the
    subprocess died before writing — callers treat that as collection failure.
    """
    cases: dict = {}
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}
    if not junit_path.exists():
        return {"cases": cases, "counts": counts, "collected": 0, "missing_report": True}
    tree = ET.parse(junit_path)
    for case in tree.iter("testcase"):
        file_attr = case.get("file") or ""
        name = case.get("name") or ""
        node_id = f"{file_attr}::{name}" if file_attr else name
        outcome = "passed"
        message = ""
        for child in case:
            if child.tag == "failure":
                outcome = "failed"
                message = child.get("message") or ""
            elif child.tag == "error":
                outcome = "error"
            elif child.tag == "skipped":
                if (child.get("type") or "") == "pytest.xfail":
                    outcome = "xfailed"
                else:
                    outcome = "skipped"
        if outcome in ("passed", "failed") and "XPASS" in message:
            outcome = "xpassed"
        cases[node_id] = outcome
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"cases": cases, "counts": counts, "collected": len(cases), "missing_report": False}


def _family_outcomes(cases: dict, node_family: str) -> list:
    """Outcomes for a node id or its parametrized family.

    junit(xunit1) keys are ``file::name`` (class qualifiers dropped), so match
    on file + final name segment; parametrized cases match ``name[`` prefix.
    """
    file_part = node_family.partition("::")[0]
    last = node_family.split("::")[-1]
    outcomes = []
    for node, outcome in cases.items():
        if node.partition("::")[0] != file_part:
            continue
        node_last = node.split("::")[-1]
        if node_last == last or node_last.startswith(last + "["):
            outcomes.append(outcome)
    return outcomes


def _read_provenance_rows(paths: list) -> list:
    rows = []
    for path in paths:
        candidate = Path(path)
        if not candidate.exists():
            continue
        with open(candidate, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=SCRIPT_REPO)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--ancestry-mode", choices=("git", "assume-eligible"), default="git",
        help="assume-eligible is for sealed git-less mutation fixtures ONLY",
    )
    parser.add_argument(
        "--skip-full-suite", action="store_true",
        help="fixture speed knob: guard + corrected files instead of full tests/security/",
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    lib = _load_lib()
    manifest_path = (args.manifest or (repo / "tests" / "security" / "fork_parity_manifest.json")).resolve()
    manifest = lib.load_manifest(manifest_path)
    items = manifest.get("items", [])

    head = _git(repo, "rev-parse", "HEAD")
    dirty = _git(repo, "status", "--porcelain")
    base_commit = manifest.get("target_base_commit", "")
    base_is_ancestor = lib.commit_is_ancestor(repo, base_commit) if base_commit else None

    verdict: dict = {
        "schema": "hermes-fork-parity-verdict/2",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": {
            "repo_realpath": str(repo),
            "head": head or None,
            "worktree_dirty": bool(dirty),
            "target_base_commit": base_commit,
            "base_is_ancestor_of_head": base_is_ancestor,
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "python": args.python,
            "ancestry_mode": args.ancestry_mode,
            "skip_full_suite": bool(args.skip_full_suite),
        },
        "claims": {"deployment_ready": False, "merge_ready": False},
        "gates": {},
        "suites": [],
    }

    # ── static evaluation (before suites, so a dead tree still classifies) ──
    integrity_errors = lib.manifest_integrity_errors(manifest)
    item_records = [
        lib.evaluate_item(item, repo, ancestry_mode=args.ancestry_mode)
        for item in items
    ]
    (out_dir / "item-records.json").write_text(
        json.dumps(item_records, indent=1), encoding="utf-8"
    )

    phase_mismatches = []
    for item, record in zip(items, item_records):
        runtime_wrong_phase = record["verdict"] == lib.ITEM_WRONG_PHASE
        manifest_wrong_phase = item.get("phase") == "wrong_phase"
        if runtime_wrong_phase != manifest_wrong_phase:
            phase_mismatches.append({
                "id": item.get("id"),
                "manifest_phase": item.get("phase"),
                "runtime_verdict": record["verdict"],
                "reason": record.get("eligibility_reason"),
            })

    # ── decisive suites in fresh subprocesses ──────────────────────────────
    fixture_env = (
        {"FORK_PARITY_SEALED_FIXTURE": "1"}
        if args.ancestry_mode == "assume-eligible" else {}
    )
    suites = [
        _run_suite(
            name="merge-invariants",
            pytest_args=["tests/security/test_merge_invariants.py"],
            repo=repo, python=args.python, out_dir=out_dir, extra_env=fixture_env,
        ),
        _run_suite(
            name="security-fixture" if args.skip_full_suite else "security-full",
            pytest_args=(
                list(FIXTURE_DECISIVE_FILES) if args.skip_full_suite
                else ["tests/security/"]
            ),
            repo=repo, python=args.python, out_dir=out_dir, extra_env=fixture_env,
        ),
    ]
    security_suite = suites[-1]

    # Proof nodes outside tests/security/ run as WHOLE FILES in one fresh
    # subprocess each — the repo's own isolation model is per-file
    # (scripts/run_tests_parallel.py), and intra-file ordering is a supported
    # contract there, so node-selection would manufacture failures the real
    # gate never sees. Skipped in fixture mode (recorded); static proof-node
    # checks still apply there.
    proof_suites = []
    if not args.skip_full_suite:
        external_files = set()
        for item in items:
            for proof in item.get("proofs", []):
                file_part = proof.get("test", "").partition("::")[0]
                if file_part and not file_part.startswith("tests/security/"):
                    external_files.add(file_part)
        for index, file_part in enumerate(sorted(external_files)):
            proof_suites.append(_run_suite(
                name=f"proof-{index:02d}-{Path(file_part).stem}",
                pytest_args=[file_part],
                repo=repo, python=args.python, out_dir=out_dir, extra_env=fixture_env,
            ))
    suites.extend(proof_suites)
    verdict["suites"] = suites

    decisive = _parse_junit(Path(security_suite["junit_xml"]))
    invariants = _parse_junit(Path(suites[0]["junit_xml"]))
    proof_cases: dict = {}
    proof_suite_failures = []
    for suite in proof_suites:
        parsed = _parse_junit(Path(suite["junit_xml"]))
        proof_cases.update(parsed["cases"])
        if suite["exit_code"] != 0 or parsed.get("missing_report"):
            proof_suite_failures.append(suite["name"])

    # ── gate: absolute suite ───────────────────────────────────────────────
    counts = decisive["counts"]
    log_xpassed = _xpassed_from_log(Path(security_suite["log"]))
    if log_xpassed > counts.get("xpassed", 0):
        counts["xpassed"] = log_xpassed
    min_collected = int(manifest.get("security_suite_min_collected", 0) or 0)
    absolute_reasons = []
    if security_suite["exit_code"] != 0:
        absolute_reasons.append(f"suite exit code {security_suite['exit_code']}")
    for bad in ("failed", "error", "skipped", "xfailed", "xpassed"):
        if counts.get(bad):
            absolute_reasons.append(f"{counts[bad]} {bad} case(s)")
    if decisive.get("missing_report"):
        absolute_reasons.append("junit report missing (collection failure)")
    if not args.skip_full_suite and decisive["collected"] < min_collected:
        absolute_reasons.append(
            f"count drift: collected {decisive['collected']} < pinned minimum {min_collected}"
        )
    if (
        invariants["counts"].get("failed")
        or invariants["counts"].get("error")
        or invariants.get("missing_report")
    ):
        absolute_reasons.append("merge-invariant suite not green")
    verdict["gates"]["absolute_suite"] = {
        "pass": not absolute_reasons,
        "counts": counts,
        "collected": decisive["collected"],
        "min_collected_pin": min_collected,
        "merge_invariants_counts": invariants["counts"],
        "reasons": absolute_reasons,
    }

    # ── gate: differential (corrected previously-stale tests) ──────────────
    differential_reasons = []
    for node_family in manifest.get("closed_stale_tests", []):
        outcomes = _family_outcomes(decisive["cases"], node_family)
        if not outcomes:
            differential_reasons.append(f"{node_family}: not collected")
        elif any(outcome != "passed" for outcome in outcomes):
            differential_reasons.append(f"{node_family}: {outcomes}")
    verdict["gates"]["differential"] = {
        "pass": not differential_reasons,
        "checked": manifest.get("closed_stale_tests", []),
        "reasons": differential_reasons,
    }

    # ── gate: provenance ───────────────────────────────────────────────────
    provenance_rows = _read_provenance_rows([s["provenance_jsonl"] for s in suites])
    owned = lib.manifest_owned_names(manifest, repo)
    foreign = lib.foreign_module_rows(provenance_rows, repo, owned=owned)
    split = lib.split_package_rows(provenance_rows, repo, owned=owned)
    provenance_reasons = []
    if not provenance_rows:
        provenance_reasons.append(
            "no provenance evidence collected (conftest hook missing?) — fail closed"
        )
    if foreign:
        provenance_reasons.append(
            f"{len(foreign)} repo-owned module row(s) resolved from foreign paths"
        )
    if split:
        provenance_reasons.append(f"{len(split)} split-package row(s)")
    repo_owned_rows = [
        row for row in provenance_rows
        if str(row.get("module", "")).partition(".")[0] in owned
    ]
    verdict["gates"]["provenance"] = {
        "pass": not provenance_reasons,
        "module_rows_total": len(provenance_rows),
        "repo_owned_rows": len(repo_owned_rows),
        "foreign_rows": foreign,
        "split_package_rows": split,
        "reasons": provenance_reasons,
    }

    # ── gate: phase/eligibility ────────────────────────────────────────────
    verdict["gates"]["phase_eligibility"] = {
        "pass": not phase_mismatches,
        "wrong_phase_items": [
            record["id"] for record in item_records
            if record["verdict"] == lib.ITEM_WRONG_PHASE
        ],
        "mismatches": phase_mismatches,
        "note": "ineligible items classify WRONG_PHASE, never regression PASS/FAIL",
    }

    # ── gate: parity/completeness ──────────────────────────────────────────
    parity_reasons = list(integrity_errors)
    failed_items = [
        record["id"] for record in item_records if record["verdict"] == lib.ITEM_FAIL
    ]
    if failed_items:
        parity_reasons.append(f"item FAIL: {failed_items}")
    if proof_suite_failures:
        parity_reasons.append(f"external proof suite(s) not green: {proof_suite_failures}")
    proof_runs = {}
    for item, record in zip(items, item_records):
        if record["verdict"] != lib.ITEM_PASS:
            continue
        for proof in item.get("proofs", []):
            node_family = proof.get("test", "")
            outcomes = (
                _family_outcomes(decisive["cases"], node_family)
                or _family_outcomes(invariants["cases"], node_family)
                or _family_outcomes(proof_cases, node_family)
            )
            proof_runs[node_family] = outcomes
            if not outcomes:
                if not args.skip_full_suite:
                    parity_reasons.append(f"{item['id']}: proof {node_family} did not run")
            elif any(outcome != "passed" for outcome in outcomes):
                parity_reasons.append(f"{item['id']}: proof {node_family} -> {outcomes}")
    verdict["gates"]["parity_completeness"] = {
        "pass": not parity_reasons,
        "items_total": len(items),
        "items_pass": sum(1 for r in item_records if r["verdict"] == lib.ITEM_PASS),
        "items_wrong_phase": sum(
            1 for r in item_records if r["verdict"] == lib.ITEM_WRONG_PHASE
        ),
        "items_fail": failed_items,
        "proof_families_checked": len(proof_runs),
        "reasons": parity_reasons,
    }

    # ── gate: merge/integration (aggregate; claims nothing beyond) ─────────
    gate_names = (
        "absolute_suite", "differential", "provenance",
        "phase_eligibility", "parity_completeness",
    )
    failing = [name for name in gate_names if not verdict["gates"][name]["pass"]]
    verdict["gates"]["merge_integration_gate"] = {
        "pass": not failing,
        "failing_gates": failing,
        "bound_to_head": head or None,
        "worktree_dirty": bool(dirty),
        "note": (
            "PASS means the fork-parity assurance gates held for this exact "
            "checkout; it does NOT claim deployment or merge readiness."
        ),
    }

    verdict_path = out_dir / "fork-parity-verdict.json"
    verdict_path.write_text(json.dumps(verdict, indent=1), encoding="utf-8")
    print(json.dumps({
        "verdict": "PASS" if not failing else "FAIL",
        "failing_gates": failing,
        "items_fail": failed_items,
        "items_wrong_phase": verdict["gates"]["phase_eligibility"]["wrong_phase_items"],
        "out": str(verdict_path),
    }))
    return 0 if not failing else 1


if __name__ == "__main__":
    sys.exit(main())
