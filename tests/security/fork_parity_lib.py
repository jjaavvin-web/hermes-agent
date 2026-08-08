"""Shared evaluation logic for the executable fork-parity guard.

Single source of truth for BOTH ``tests/security/test_fork_parity_guard.py``
(the in-suite pytest guard) and ``scripts/fork_parity_guard.py`` (the
machine-readable evidence runner), so the pytest checks and the verdict
emission cannot drift apart.

The guard's data is ``tests/security/fork_parity_manifest.json``: the 71-item
fork-parity custody docket (audit epoch ``20260807T173333Z-fable-v020-custody``)
bound item-by-item via SHA-256 of the exact docket text, with a stable ID,
verdict classification, phase/lineage provenance, structural anchors, and
executable proof tests per item. The docket is provenance input, not
self-proving truth: every eligible security invariant must resolve to real
anchors in THIS checkout and to behavioral tests that execute in the same run.

Item verdicts are three-valued and the distinction is load-bearing:

* ``PASS``        — item eligible for this tree and every anchor/proof holds.
* ``FAIL``        — item eligible and at least one anchor/proof is broken.
* ``WRONG_PHASE`` — the item's introducing lineage is not in this tree's
  ancestry (pre-introduction or foreign checkout). Never a regression
  verdict, and never counts as covering proof either.

Not a test module (no ``test_`` prefix); pytest does not collect it.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import warnings
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

REPO = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).resolve().parent / "fork_parity_manifest.json"

SCHEMA = "hermes-fork-parity-manifest/2"
VERDICT_CLASSES = ("PRESERVED_EQUIVALENT", "RELOCATED", "SUPERSEDED_LEGIT")
KINDS = ("security_invariant", "structural", "superseded", "meta")
PHASES = ("in_repo", "wrong_phase")
ANCHOR_MODES = ("exists", "contains", "call_edge", "absent_or_repurposed")

ITEM_PASS = "PASS"
ITEM_FAIL = "FAIL"
ITEM_WRONG_PHASE = "WRONG_PHASE"

# Sealed disposable mutation fixtures are exported without .git; only the
# guard runner sets this mode, and every emitted verdict records it so a
# fixture run can never masquerade as a lineage-bound assurance run.
ANCESTRY_MODES = ("git", "assume-eligible")


def load_manifest(path: Path | None = None) -> Dict[str, Any]:
    manifest_path = path or MANIFEST_PATH
    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def manifest_integrity_errors(manifest: Mapping[str, Any]) -> List[str]:
    """Structural validation of the manifest itself. Empty list == sound."""
    errors: List[str] = []
    if manifest.get("schema") != SCHEMA:
        errors.append(f"schema is {manifest.get('schema')!r}, expected {SCHEMA!r}")
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        return errors + ["items missing or empty"]

    min_collected = manifest.get("security_suite_min_collected")
    if not isinstance(min_collected, int) or min_collected < 1:
        errors.append(
            "security_suite_min_collected must be a positive collected-count pin "
            f"(got {min_collected!r}); 0 disables the count-drift gate"
        )
    if not manifest.get("closed_stale_tests"):
        errors.append("closed_stale_tests missing/empty (differential gate has no input)")
    if not manifest.get("repo_owned_top_level"):
        errors.append(
            "repo_owned_top_level missing/empty (provenance owned-name universe "
            "would silently shrink when a whole package is deleted)"
        )

    counts = manifest.get("counts") or {}
    recount: Dict[str, int] = {}
    seen_ids = set()
    for item in items:
        item_id = item.get("id")
        if item_id in seen_ids:
            errors.append(f"duplicate id {item_id!r}")
        seen_ids.add(item_id)
        verdict = item.get("verdict")
        if verdict not in VERDICT_CLASSES:
            errors.append(f"{item_id}: bad verdict {verdict!r}")
        recount[verdict] = recount.get(verdict, 0) + 1
        if item.get("kind") not in KINDS:
            errors.append(f"{item_id}: bad kind {item.get('kind')!r}")
        if item.get("phase") not in PHASES:
            errors.append(f"{item_id}: bad phase {item.get('phase')!r}")
        digest = hashlib.sha256(
            str(item.get("docket_item")).encode("utf-8")
        ).hexdigest()
        if digest[:16] != item.get("docket_item_sha256_16"):
            errors.append(f"{item_id}: docket_item hash mismatch (docket binding broken)")
        anchors = item.get("anchors", [])
        for anchor in anchors:
            if anchor.get("mode") not in ANCHOR_MODES:
                errors.append(f"{item_id}: bad anchor mode {anchor.get('mode')!r}")
        if item.get("phase") == "in_repo" and not anchors:
            errors.append(f"{item_id}: in_repo item without anchors proves nothing")
        if item.get("kind") == "security_invariant" and item.get("phase") == "in_repo":
            if not item.get("proofs"):
                errors.append(f"{item_id}: eligible security invariant without executable proof")

    if counts.get("total") != len(items):
        errors.append(f"counts.total={counts.get('total')} but {len(items)} items present")
    for verdict_class in VERDICT_CLASSES:
        if counts.get(verdict_class) != recount.get(verdict_class, 0):
            errors.append(
                f"counts[{verdict_class}]={counts.get(verdict_class)} but recount "
                f"is {recount.get(verdict_class, 0)} (classification drift)"
            )
    return errors


# ── Import provenance ───────────────────────────────────────────────────────


def repo_top_level_names(repo: Path) -> set:
    """Top-level importable names owned by this checkout.

    A directory counts only as a regular package with ``__init__.py``
    (``packaging/`` here is data, not a package, and must not shadow the
    PyPI ``packaging`` dist).
    """
    names = {p.stem for p in repo.glob("*.py")}
    names |= {
        entry.name
        for entry in repo.iterdir()
        if entry.is_dir() and (entry / "__init__.py").exists()
    }
    return names


def manifest_owned_names(manifest: Mapping[str, Any], repo: Path) -> set:
    """Owned-name universe for provenance checks: the manifest's declared
    top-level names UNION the live scan.

    The declared list is load-bearing: a deleted repo-owned package would
    otherwise leave the live-scan universe entirely, making its foreign
    resolution (the editable finder serving deployment bytes) invisible to
    the provenance gate (cockpit mutation class m1)."""
    declared = set(manifest.get("repo_owned_top_level") or [])
    return declared | repo_top_level_names(repo)


def module_provenance_rows(modules: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """name/__file__/__spec__.origin/__path__/realpath rows for loaded modules."""
    rows: List[Dict[str, Any]] = []
    for name in sorted(modules):
        module = modules.get(name)
        if module is None:
            continue
        module_file = getattr(module, "__file__", None)
        spec = getattr(module, "__spec__", None)
        origin = getattr(spec, "origin", None) if spec is not None else None
        pkg_path = getattr(module, "__path__", None)
        if module_file is None and pkg_path is None:
            continue
        rows.append({
            "module": name,
            "file": module_file,
            "spec_origin": origin,
            "path": [str(entry) for entry in pkg_path] if pkg_path is not None else None,
            "realpath": os.path.realpath(module_file) if module_file else None,
        })
    return rows


def foreign_module_rows(
    rows: Iterable[Mapping[str, Any]],
    repo: Path,
    owned: set | None = None,
) -> List[Dict[str, Any]]:
    """Rows for repo-owned module names that resolved OUTSIDE ``repo``.

    Fail-closed: this venv's editable-install finder serves DEPLOYMENT copies
    of repo-owned top-level names whenever sys.path misses one, so a deleted
    or shadowed module does not ImportError — it silently runs foreign bytes.
    Any repo-owned row whose ``__file__`` realpath or any package ``__path__``
    entry resolves outside the checkout under test is contamination.

    Callers with a manifest should pass ``owned=manifest_owned_names(...)`` so
    a wholesale-deleted package stays in the universe.
    """
    owned = owned if owned is not None else repo_top_level_names(repo)
    prefix = os.path.realpath(str(repo)).rstrip(os.sep) + os.sep
    foreign: List[Dict[str, Any]] = []
    for row in rows:
        top_level = str(row.get("module", "")).partition(".")[0]
        if top_level not in owned:
            continue
        bad = False
        realpath = row.get("realpath")
        if realpath and not str(realpath).startswith(prefix):
            bad = True
        for path_entry in row.get("path") or []:
            if not os.path.realpath(str(path_entry)).startswith(prefix):
                bad = True
        if bad:
            foreign.append(dict(row))
    return foreign


def split_package_rows(
    rows: Iterable[Mapping[str, Any]],
    repo: Path,
    owned: set | None = None,
) -> List[Dict[str, Any]]:
    """Repo-owned packages whose ``__path__`` spans more than one real root."""
    owned = owned if owned is not None else repo_top_level_names(repo)
    split: List[Dict[str, Any]] = []
    for row in rows:
        top_level = str(row.get("module", "")).partition(".")[0]
        if top_level not in owned:
            continue
        entries = row.get("path") or []
        if len({os.path.realpath(str(entry)) for entry in entries}) > 1:
            split.append(dict(row))
    return split


# ── Anchor evaluation ───────────────────────────────────────────────────────


def _module_defines_symbol(source: str, symbol: str) -> bool:
    """True when ``symbol`` is a module-level def/class/assignment (AST)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol:
                return True
    return False


def _call_leaf_names(call: ast.Call) -> set:
    """Terminal callable names reachable from a Call node's func expression."""
    names = set()
    node = call.func
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, ast.Attribute):
        names.add(node.attr)
    return names


def _function_calls_callee(source: str, caller: str, callee: str) -> Tuple[bool, str]:
    """AST call-edge check: some def named ``caller`` (any nesting) contains a
    call whose terminal name is ``callee``."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"does not parse ({exc})"
    found_caller = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == caller:
            found_caller = True
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and callee in _call_leaf_names(inner):
                    return True, f"{caller} -> {callee} call edge present"
    if not found_caller:
        return False, f"caller {caller!r} not defined"
    return False, f"{caller} no longer calls {callee} (call edge dropped)"


def check_anchor(anchor: Mapping[str, Any], repo: Path) -> Tuple[bool, str]:
    """Evaluate one anchor against ``repo``. Returns (ok, detail)."""
    rel = anchor.get("path", "")
    mode = anchor.get("mode")
    target = repo / rel
    if mode == "absent_or_repurposed":
        if not target.exists():
            return True, f"{rel}: absent as expected"
        marker = anchor.get("retired_marker")
        if marker:
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return False, f"{rel}: unreadable ({exc})"
            if marker in text:
                return False, f"{rel}: retired marker {marker!r} still present"
            return True, f"{rel}: present but repurposed (retired marker gone)"
        return True, f"{rel}: present (superseded item, presence tolerated)"
    if not target.exists():
        return False, f"{rel}: missing"
    if mode == "exists":
        return True, f"{rel}: present"
    if mode in ("contains", "call_edge"):
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"{rel}: unreadable ({exc})"
    if mode == "call_edge":
        ok, detail = _function_calls_callee(text, anchor.get("caller", ""), anchor.get("callee", ""))
        return ok, f"{rel}: {detail}"
    if mode == "contains":
        symbol = anchor.get("symbol")
        if symbol:
            if rel.endswith(".py") and _module_defines_symbol(text, symbol):
                return True, f"{rel}: defines {symbol}"
            if symbol in text:
                # Class attributes / nested names miss the module-level AST
                # pass; accept the textual hit but record the weaker strength.
                return True, f"{rel}: contains {symbol} (textual)"
            return False, f"{rel}: symbol {symbol} not found"
        needle = anchor.get("text")
        if needle is not None:
            if needle in text:
                return True, f"{rel}: contains pinned text"
            return False, f"{rel}: pinned text not found"
        return False, f"{rel}: contains anchor without symbol/text"
    return False, f"{rel}: unknown anchor mode {mode!r}"


# ── Executable proof references ─────────────────────────────────────────────

_SKIP_MARK_NAMES = {"skip", "skipif", "xfail"}


def _decorator_mark_names(decorators: Iterable[ast.expr]) -> set:
    names = set()
    for decorator in decorators:
        node = decorator
        if isinstance(node, ast.Call):
            node = node.func
        parts: List[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        dotted = ".".join(reversed(parts))
        if dotted.startswith("pytest.mark.") or dotted.startswith("mark."):
            names.add(parts[0])
    return names


def proof_test_status(repo: Path, proof: Mapping[str, Any]) -> Tuple[bool, str]:
    """Statically verify one executable proof reference (proof-node drift).

    The node id must name a real test function (or parametrized family) in a
    real file, and the function must not be statically skip/skipif/xfail
    marked — a required proof that cannot run is non-PASS.
    """
    node_id = proof.get("test", "")
    rel, _, name = node_id.partition("::")
    test_path = repo / rel
    if not test_path.exists():
        return False, f"{node_id}: file missing"
    if not name:
        return False, f"{node_id}: no test name in node id"
    base_name = name.split("::")[-1].split("[", 1)[0]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(test_path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return False, f"{node_id}: file does not parse ({exc})"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == base_name:
            blocked = _decorator_mark_names(node.decorator_list) & _SKIP_MARK_NAMES
            if blocked:
                return False, f"{node_id}: statically marked {sorted(blocked)}"
            return True, f"{node_id}: defined and unskipped"
    return False, f"{node_id}: test function {base_name!r} not found"


# ── Phase / lineage eligibility ─────────────────────────────────────────────


def commit_is_ancestor(repo: Path, commit: str) -> bool | None:
    """True/False from git; None when git cannot answer (no repo/timeout)."""
    try:
        probe = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            return False
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode in (0, 1):
            return result.returncode == 0
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def item_eligibility(
    item: Mapping[str, Any],
    repo: Path,
    ancestry_mode: str = "git",
) -> Tuple[bool, str]:
    """(eligible, reason). Ineligible items classify WRONG_PHASE.

    In ``git`` mode an item with recorded introducing commits is eligible
    only when at least one of them is an ancestor of the target HEAD; a tree
    that predates the introduction (or comes from a foreign lineage) must be
    classified WRONG_PHASE, never regression PASS/FAIL.
    """
    if item.get("phase") == "wrong_phase":
        return False, "manifest phase: wrong_phase"
    introduced = item.get("introduced_by") or []
    if not introduced:
        return True, "eligible (no introducing provenance recorded)"
    if ancestry_mode == "assume-eligible":
        return True, "eligible (assumed: sealed non-git fixture mode)"
    for commit in introduced:
        if commit_is_ancestor(repo, commit) is True:
            return True, f"introducing commit {commit[:12]} is an ancestor of HEAD"
    return False, "no introducing commit is an ancestor of HEAD (pre-introduction/foreign lineage)"


def evaluate_item(
    item: Mapping[str, Any],
    repo: Path,
    ancestry_mode: str = "git",
) -> Dict[str, Any]:
    """Full static disposition for one docket item."""
    eligible, reason = item_eligibility(item, repo, ancestry_mode)
    record: Dict[str, Any] = {
        "id": item.get("id"),
        "verdict_class": item.get("verdict"),
        "kind": item.get("kind"),
        "phase": item.get("phase"),
        "eligibility_reason": reason,
        "details": [],
    }
    if not eligible:
        record["verdict"] = ITEM_WRONG_PHASE
        return record

    ok = True
    for anchor in item.get("anchors", []):
        anchor_ok, detail = check_anchor(anchor, repo)
        record["details"].append(detail)
        ok = ok and anchor_ok
    for proof in item.get("proofs", []):
        proof_ok, detail = proof_test_status(repo, proof)
        record["details"].append(detail)
        ok = ok and proof_ok
    if item.get("kind") == "security_invariant" and not item.get("proofs"):
        ok = False
        record["details"].append("eligible security invariant has no executable proof")
    record["verdict"] = ITEM_PASS if ok else ITEM_FAIL
    return record
