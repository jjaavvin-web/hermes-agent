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


def _git_env() -> dict:
    """Environment for git probes with ambient redirection scrubbed.

    Inherited GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE could make ``git -C repo``
    report an approved HEAD from a DIFFERENT repository while the guard tests
    other filesystem bytes (external-review negative control)."""
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(key, None)
    return env


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60, env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _concealed_byte_drift(repo: Path) -> dict:
    """Byte-verify the worktree against the HEAD tree (fail closed).

    ``git status`` trusts index stat flags, so tracked bytes altered under
    ``update-index --assume-unchanged`` / ``skip-worktree`` present as a clean
    tree at the approved SHA while different code actually runs — the exact
    false-green the release gate exists to prevent (external-review P1).
    Every tracked blob is re-hashed from the filesystem and compared to the
    HEAD object; index hiding flags are reported alongside.
    """
    flags = [
        line for line in _git(repo, "ls-files", "-v").splitlines()
        if line[:1].islower() or line.startswith("S ")
    ]
    ls_tree = _git(repo, "ls-tree", "-r", "HEAD")
    expected: dict = {}
    for line in ls_tree.splitlines():
        try:
            meta, path = line.split("\t", 1)
            mode, otype, sha = meta.split()
        except ValueError:
            continue
        if otype == "blob" and mode != "120000":  # symlink bytes differ by design
            expected[path] = sha
    paths = sorted(expected)
    hashed: dict = {}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "--stdin-paths"],
            input="\n".join(paths) + "\n", capture_output=True, text=True,
            timeout=300, env=_git_env(),
        )
        if result.returncode == 0:
            hashed = dict(zip(paths, result.stdout.split()))
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not hashed:
        return {"checked": 0, "mismatches": ["byte verification failed to run — fail closed"], "index_hiding_flags": flags}
    mismatches = [
        path for path in paths
        if hashed.get(path) != expected[path]
    ]
    return {
        "checked": len(paths),
        "mismatches": mismatches[:20],
        "mismatch_total": len(mismatches),
        "index_hiding_flags": flags,
    }


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
    # An ambient FORK_PARITY_SEALED_FIXTURE (leftover shell export, CI copy)
    # would silently soften the in-suite lineage checks to assume-eligible;
    # only an explicit fixture-mode extra_env may set it (review P1 finding).
    env.pop("FORK_PARITY_SEALED_FIXTURE", None)
    env.update(extra_env or {})
    # Provenance evidence is append-mode in the conftest hook; a reused
    # out-dir must not leak prior runs' rows into this run's gate.
    provenance.unlink(missing_ok=True)
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
    try:
        result = subprocess.run(
            cmd, cwd=str(repo), env=env, capture_output=True, text=True, timeout=3600,
        )
        stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        # A hung suite must produce a clean non-PASS gate, not an unhandled
        # traceback (review P1 finding). junit stays missing -> the gates
        # already treat that as collection failure.
        stdout = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = f"TIMEOUT after {exc.timeout}s"
        exit_code = 124
    log_path.write_text(
        stdout + "\n--- STDERR ---\n" + stderr, encoding="utf-8"
    )
    return {
        "name": name,
        "cmd": cmd,
        "cwd": str(repo),
        "exit_code": exit_code,
        "duration_s": round(time.time() - started, 2),
        "junit_xml": str(junit),
        "provenance_jsonl": str(provenance),
        "log": str(log_path),
        "log_sha256": _sha256(log_path) if log_path.exists() else None,
    }


def _volatile_route_cases(cases: dict, repo: Path) -> list:
    """Cases parametrized on routes of OPERATOR-LOCAL dashboard plugins.

    The auth-boundary tests enumerate registered ``/api`` routes at import
    time, so their param list tracks live plugin registration. Plugins shipped
    in the repo (``plugins/<name>``) are stable; operator-local plugins come
    and go with machine state — observed live during this mission: the
    mothership-nexus and trt route params vanished from fresh processes while
    the live dashboard kept serving both. Their presence must not decide the
    count-drift gate, in either direction."""
    plugins_dir = repo / "plugins"
    repo_plugins = (
        {p.name for p in plugins_dir.iterdir() if p.is_dir()}
        if plugins_dir.is_dir() else set()
    )
    marker = "[/api/plugins/"
    volatile = []
    for node in cases:
        idx = node.find(marker)
        if idx == -1:
            continue
        plugin = node[idx + len(marker):].split("/", 1)[0]
        if plugin not in repo_plugins:
            volatile.append(node)
    return volatile


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
    parser.add_argument(
        "--expect-head", default="",
        help="external verification: hard-fail the aggregate gate unless the "
             "target's discovered HEAD equals this exact commit",
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    lib = _load_lib()
    manifest_path = (args.manifest or (repo / "tests" / "security" / "fork_parity_manifest.json")).resolve()
    manifest = lib.load_manifest(manifest_path)
    items = manifest.get("items", [])

    # A git-less target under a PARENT repository (e.g. a sealed fixture in an
    # audit dir inside a repo-managed home) must not inherit that parent's
    # HEAD/status/bytes: git discovery walks upward, so require the target to
    # be its own toplevel before treating it as lineage-bound.
    toplevel = _git(repo, "rev-parse", "--show-toplevel")
    repo_is_git_root = bool(toplevel) and os.path.realpath(toplevel) == os.path.realpath(str(repo))
    head = _git(repo, "rev-parse", "HEAD") if repo_is_git_root else ""
    dirty = _git(repo, "status", "--porcelain") if repo_is_git_root else ""
    # Byte binding applies to lineage-bound targets; git-less fixtures already
    # record reduced custody via ancestry_mode.
    concealed = _concealed_byte_drift(repo) if head else None
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
    # Docket binding: enforce when the custody docket is reachable; a manifest
    # re-carved in lockstep with its own count pins would otherwise only be
    # caught by human review (review P1 finding). On portable checkouts the
    # docket audit dir may be absent — recorded, not failed.
    docket_source = str(manifest.get("docket_source") or "")
    docket_expected = str(manifest.get("docket_sha256") or "")
    docket_binding = "not_declared"
    if docket_source and docket_expected:
        # docket_source is a machine-portable label: either an absolute path
        # or "audit <dir>/<file>" relative to the local hermes audits root.
        rel = docket_source.removeprefix("audit ").strip()
        candidates = [
            Path(docket_source),
            Path.home() / ".hermes" / "audits" / rel,
        ]
        docket_path = next((c for c in candidates if c.is_file()), candidates[0])
        if docket_path.exists():
            if _sha256(docket_path) == docket_expected:
                docket_binding = "verified"
            else:
                docket_binding = "MISMATCH"
                integrity_errors.append(
                    f"custody docket {docket_source} does not match manifest "
                    "docket_sha256 (docket re-carve or tamper)"
                )
        else:
            docket_binding = "absent (portable run; recount protection is a review control)"
    verdict["target"]["docket_binding"] = docket_binding
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
            proof_env = dict(fixture_env)
            if "test_dashboard_smoke" in file_part:
                # The smoke gate HANGS when real provider keys are present;
                # CI (tests.yml) forces exactly these empty. Reproduce that
                # required shaping instead of depending on a keyless dev box.
                proof_env.update({
                    "OPENROUTER_API_KEY": "",
                    "OPENAI_API_KEY": "",
                    "NOUS_API_KEY": "",
                })
            proof_suites.append(_run_suite(
                name=f"proof-{index:02d}-{Path(file_part).stem}",
                pytest_args=[file_part],
                repo=repo, python=args.python, out_dir=out_dir, extra_env=proof_env,
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
    volatile_cases = _volatile_route_cases(decisive["cases"], repo)
    stable_collected = decisive["collected"] - len(volatile_cases)
    if not args.skip_full_suite and stable_collected < min_collected:
        absolute_reasons.append(
            f"count drift: stable collected {stable_collected} "
            f"(={decisive['collected']} - {len(volatile_cases)} operator-local "
            f"plugin-route cases) < pinned minimum {min_collected}"
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
        "stable_collected": stable_collected,
        "volatile_plugin_route_cases": volatile_cases,
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
    binding_reasons = []
    if args.expect_head and head != args.expect_head:
        binding_reasons.append(
            f"HEAD identity mismatch: discovered {head or 'NONE'!r} != "
            f"expected {args.expect_head!r}"
        )
    if concealed is not None:
        if concealed.get("mismatch_total") or concealed.get("mismatches"):
            binding_reasons.append(
                f"concealed byte drift: {concealed.get('mismatch_total', len(concealed['mismatches']))} "
                f"tracked file(s) differ from HEAD blobs (index-hiding defeated): "
                f"{concealed['mismatches'][:5]}"
            )
        if concealed.get("index_hiding_flags"):
            binding_reasons.append(
                f"index hiding flags present (assume-unchanged/skip-worktree): "
                f"{concealed['index_hiding_flags'][:5]}"
            )
    if binding_reasons:
        failing.append("commit_byte_binding")
    verdict["gates"]["merge_integration_gate"] = {
        "pass": not failing,
        "failing_gates": failing,
        "bound_to_head": head or None,
        "worktree_dirty": bool(dirty),
        "byte_binding": (
            {
                "tracked_blobs_verified": concealed.get("checked", 0),
                "reasons": binding_reasons,
            }
            if concealed is not None
            else {"skipped": "no git HEAD (sealed fixture; ancestry_mode recorded)"}
        ),
        "note": (
            "PASS means the fork-parity assurance gates held for this exact "
            "checkout, with every tracked blob re-hashed against HEAD; it does "
            "NOT claim deployment or merge readiness."
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
