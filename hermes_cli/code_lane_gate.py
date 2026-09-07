"""Deterministic test+lint gate for kanban CODE lanes (A3).

The kanban goal loop (``hermes_cli.goals.run_kanban_goal_loop``) decides
whether a worker is "done" with an LLM judge that reads only the worker's
narration. For code lanes that judge can be talked into "looks correct".
This module is the deterministic PRIMARY gate that runs BEFORE the judge:
a real ``pytest`` over the diff-mapped test files plus a ``ruff`` PLW1514
(``encoding=``) check over the changed Python files. The LLM judge is
demoted to a secondary semantic check that only runs once this gate is
green (or could not run).

Design invariants (all load-bearing):

- **FAIL-OPEN.** Any infra error — git/pytest/ruff failing to *run*, a
  timeout, an unparseable base state — returns ``GateResult(ran=False)``.
  The caller then falls through to the existing LLM-judge path. A buggy
  gate must NEVER wedge a lane; it degrades to today's behaviour.
- **FLAKE-SAFE BASE-VS-HEAD DELTA.** The fork carries ~7 known-flaky
  tests plus ordering pollution. A naive "all green or CONTINUE forever"
  gate would wedge every code lane into a sticky block. So for every
  mapped test that is RED at HEAD we also run it at the merge-base and
  SUBTRACT the pre-existing failures (set-difference on test node ids,
  mirroring ``scripts/lint_diff.py``). Only NEWLY-red tests set
  ``passed=False``.
- **PER-FILE pytest isolation.** Each test file runs in its own
  subprocess (mirrors ``scripts/run_tests_parallel.py``) — a single
  multi-file run leaks cross-file module state and re-introduces the
  ordering pollution the per-file runner exists to kill.
- **tests/ only.** ``pytest`` is invoked exclusively on paths under
  ``<worktree>/tests/`` so the hermetic browser guard in
  ``tests/conftest.py`` is always loaded; source modules are never
  executed directly (which would bypass the guard and could pop a real
  OAuth browser tab).
- **Subprocess injection.** All git/pytest/ruff calls go through an
  injected ``run`` callable (defaults to ``subprocess.run``) so unit
  tests use a fake and never spawn a real subprocess.

OFF by default — wired only when ``auxiliary.goal_judge.code_gate.enabled``
is true and the worker is running inside a git worktree.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

DEFAULT_WALL_CLOCK_CAP_SEC = 180
# Per-subprocess ceiling so one hung pytest file can't eat the whole cap.
_PER_CALL_CAP_SEC = 120
_GIT_CALL_CAP_SEC = 60
# pytest exit codes treated as "no failures": 0 = all passed, 5 = nothing
# collected (every test filtered by a marker, e.g. -m 'not integration').
_PYTEST_OK_CODES = frozenset((0, 5))
# pytest exit codes that mean "ran and at least one test failed" — the only
# states from which we can attribute clean per-test failures. 2 (interrupted),
# 3 (internal error), 4 (usage error) are unattributable → fail-open.
_PYTEST_RAN_CODES = frozenset((0, 1, 5))

# pytest -rfE prints lines like ``FAILED path::test - msg`` / ``ERROR path``.
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")

RunFn = Callable[..., "subprocess.CompletedProcess"]


# ──────────────────────────────────────────────────────────────────────
# Result
# ──────────────────────────────────────────────────────────────────────


@dataclass
class GateResult:
    """Outcome of one deterministic gate run.

    - ``ran``: the gate actually evaluated something. ``False`` means an
      infra error / fail-open — the caller must fall through to the LLM
      judge, NOT treat the lane as blocked.
    - ``passed``: only meaningful when ``ran`` is True. ``True`` = nothing
      to veto (still let the LLM judge have the secondary say); ``False``
      = a NEW test failure or a PLW1514 violation the lane introduced.
    - ``report``: human-readable summary pasted into the continuation
      prompt so the worker knows exactly what to fix.
    - ``tests_red``: newly-red test node ids (pre-existing flakes removed).
    - ``ruff_violations``: PLW1514 violations on the changed files.
    """

    passed: bool = True
    ran: bool = False
    report: str = ""
    tests_red: List[str] = field(default_factory=list)
    ruff_violations: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# Diff → changed files → mapped test files
# ──────────────────────────────────────────────────────────────────────


def changed_files(
    worktree: Path, base_ref: str, run: RunFn, *, timeout: float = _GIT_CALL_CAP_SEC
) -> Optional[List[str]]:
    """Return the UNION of repo-relative changed paths a worker may have left.

    Three sources are merged (de-duped, first-seen order):

    1. **Committed** — ``git diff --name-only <base_ref>...HEAD`` (three-dot,
       vs the merge-base). Same pattern as
       ``agent/merge_broker.py:classify_change`` (ISA D-codex-2026-05-26:
       diffing against ``origin/main`` over-classifies when origin is a
       divergent upstream fork). This is the AUTHORITATIVE source: if it
       cannot be computed the whole call fails open (returns ``None``).
    2. **Uncommitted tracked edits** — ``git diff --name-only <base_ref>``
       (working tree + index vs base). A worker may not have committed yet;
       the committed three-dot diff would then MISS its work and the gate
       would wave broken code through. Best-effort (treated as empty on
       failure so an enhancement can't regress the existing fail-open path).
    3. **Untracked** — ``git ls-files --others --exclude-standard`` (new,
       non-ignored files the worker added but never ``git add``-ed).
       Best-effort, as above.

    Returns ``None`` (fail-open) only when the authoritative committed diff
    cannot be computed.
    """
    wt = str(worktree)

    def _git(args: List[str]) -> Optional[List[str]]:
        try:
            proc = run(
                ["git", "-C", wt, *args],
                capture_output=True,
                encoding="utf-8",
                check=False,
                timeout=timeout,
            )
        except Exception as exc:  # subprocess error, timeout, OSError
            logger.debug("code gate: git %s failed (%s)", args, exc)
            return None
        if proc.returncode != 0:
            logger.debug("code gate: git %s rc=%s", args, proc.returncode)
            return None
        out = proc.stdout or ""
        return [line.strip() for line in out.splitlines() if line.strip()]

    committed = _git(["diff", "--name-only", f"{base_ref}...HEAD"])
    if committed is None:
        return None  # authoritative source unavailable → fail-open
    worktree_changes = _git(["diff", "--name-only", base_ref]) or []
    untracked = _git(["ls-files", "--others", "--exclude-standard"]) or []

    merged: List[str] = []
    for path in (*committed, *worktree_changes, *untracked):
        if path and path not in merged:
            merged.append(path)
    return merged


def map_tests(changed: Sequence[str], worktree: Path) -> List[str]:
    """Map changed paths to test files that EXIST under ``<worktree>/tests/``.

    Mirror convention ``<dir>/<mod>.py`` -> ``tests/<dir>/test_<mod>.py``
    (top-level ``cli.py`` -> ``tests/test_cli.py``). Changed paths already
    under ``tests/`` that look like ``test_*.py`` are included as-is. Only
    paths that exist on disk are returned (a mirror with no test file means
    "no mapped test" — ruff-only, do not fail). Result is de-duped and the
    returned paths are guaranteed to live under ``tests/``.
    """
    seen: List[str] = []

    def _add(rel: str) -> None:
        rel = rel.replace(os.sep, "/")
        if not rel.startswith("tests/"):
            return
        if rel in seen:
            return
        if (worktree / rel).is_file():
            seen.append(rel)

    for raw in changed:
        rel = (raw or "").strip().replace(os.sep, "/")
        if not rel or not rel.endswith(".py"):
            continue
        if rel.endswith("__init__.py"):
            continue
        if rel.startswith("tests/"):
            # A changed test file is its own target (only the test_*.py ones).
            if Path(rel).name.startswith("test_"):
                _add(rel)
            continue
        # Source file → mirror test path.
        parent = str(Path(rel).parent)
        stem = Path(rel).name
        if parent in ("", "."):
            mirror = f"tests/test_{stem}"
        else:
            mirror = f"tests/{parent}/test_{stem}"
        _add(mirror)

    return seen


def _changed_py_files(changed: Sequence[str], worktree: Path) -> List[str]:
    """Changed ``.py`` paths that still exist (for ruff PLW1514)."""
    out: List[str] = []
    for raw in changed:
        rel = (raw or "").strip().replace(os.sep, "/")
        if not rel or not rel.endswith(".py"):
            continue
        if (worktree / rel).is_file() and rel not in out:
            out.append(rel)
    return out


# ──────────────────────────────────────────────────────────────────────
# pytest (per file)
# ──────────────────────────────────────────────────────────────────────


def _pytest_cmd(testfile: str) -> List[str]:
    """Per-file pytest invocation. ``-rfE`` surfaces clean FAILED/ERROR
    node-id lines, ``--tb=no`` keeps output small. Project ``addopts``
    (``-m 'not integration' --timeout=30``) apply automatically via the
    worktree's ``pyproject.toml``. Uses ``sys.executable -m pytest`` so the
    right interpreter is used regardless of cwd (mirrors
    ``scripts/run_tests_parallel.py``). A literal ``--`` separates options
    from the path so a file named like an option can never be mis-parsed."""
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--no-header",
        "--tb=no",
        "-rfE",
        "--",
        testfile,
    ]


def run_pytest_file(
    testfile: str, cwd: Path, run: RunFn, *, timeout: float
) -> Tuple[int, str]:
    """Run one test file in its own subprocess. Returns ``(rc, output)``.

    ``rc=-1`` signals the subprocess could not be run (timeout / OSError) —
    an infra failure the caller treats as fail-open.
    """
    try:
        proc = run(
            _pytest_cmd(testfile),
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
            cwd=str(cwd),
        )
    except Exception as exc:
        logger.debug("code gate: pytest %s failed to run (%s)", testfile, exc)
        return -1, ""
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def _failed_ids(output: str) -> Set[str]:
    """Parse FAILED/ERROR test node ids from pytest ``-rfE`` output."""
    ids: Set[str] = set()
    for line in output.splitlines():
        m = _FAILED_LINE_RE.match(line.strip())
        if m:
            ids.add(m.group(1))
    return ids


# ──────────────────────────────────────────────────────────────────────
# ruff PLW1514
# ──────────────────────────────────────────────────────────────────────


def _ruff_bin() -> str:
    """Resolve the venv's ruff binary (sibling of the running interpreter),
    falling back to a bare ``ruff`` on PATH."""
    cand = Path(sys.executable).parent / "ruff"
    return str(cand) if cand.exists() else "ruff"


def run_ruff(
    py_files: Sequence[str], cwd: Path, run: RunFn, *, timeout: float
) -> Optional[List[str]]:
    """Run ``ruff check --select PLW1514`` on the changed Python files.

    Returns a list of ``path:line: message`` strings (empty = clean), or
    ``None`` (fail-open) when ruff could not be run / its JSON was
    unparseable. Parsed like ``scripts/lint_diff.py:_normalize_ruff``.

    No base-vs-head delta is needed here: CI blocks merge on PLW1514 for
    every non-exempt path, so the base branch is PLW1514-clean by
    construction and any violation in a changed file is diff-attributable.
    """
    if not py_files:
        return []
    try:
        proc = run(
            [
                _ruff_bin(),
                "check",
                "--select",
                "PLW1514",
                "--output-format",
                "json",
                "--",
                *py_files,
            ],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
            cwd=str(cwd),
        )
    except Exception as exc:
        logger.debug("code gate: ruff failed to run (%s); fail-open", exc)
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        # ruff prints "[]" on a clean run; a truly empty body with rc!=0
        # means ruff itself errored → fail-open.
        return [] if proc.returncode in (0, 1) else None
    try:
        import json

        entries = json.loads(raw)
    except Exception as exc:
        logger.debug("code gate: ruff JSON parse failed (%s); fail-open", exc)
        return None
    if not isinstance(entries, list):
        return None
    out: List[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if (e.get("code") or "") != "PLW1514":
            continue
        filename = e.get("filename", "")
        try:
            filename = os.path.relpath(filename, str(cwd))
        except ValueError:
            pass
        line = (e.get("location") or {}).get("row", 0)
        out.append(f"{filename}:{line}: [PLW1514] {e.get('message', '')}")
    return out


# ──────────────────────────────────────────────────────────────────────
# Base worktree (for the flake-safe delta)
# ──────────────────────────────────────────────────────────────────────


def _resolve_base_sha(
    worktree: Path, base_ref: str, run: RunFn, *, timeout: float
) -> str:
    """Resolve the MERGE-BASE of ``base_ref`` and ``HEAD``.

    The committed diff in :func:`changed_files` is three-dot (vs the
    merge-base), so the flake baseline must be evaluated at that same
    merge-base — not the ``base_ref`` TIP — or a commit that landed on the
    base branch after our fork point would skew which failures look
    "pre-existing". Falls back to ``base_ref`` when the merge-base cannot be
    resolved (better an approximate baseline than none).
    """
    try:
        proc = run(
            ["git", "-C", str(worktree), "merge-base", base_ref, "HEAD"],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except Exception as exc:
        logger.debug("code gate: merge-base resolution failed (%s); using base_ref tip", exc)
        return base_ref
    if proc.returncode != 0:
        logger.debug("code gate: merge-base rc=%s; using base_ref tip", proc.returncode)
        return base_ref
    sha = (proc.stdout or "").strip().splitlines()
    return sha[0].strip() if sha and sha[0].strip() else base_ref


def _provision_base_worktree(
    worktree: Path, base_ref: str, run: RunFn, *, timeout: float
) -> Optional[Path]:
    """Create a detached git worktree at the ``base_ref``↔``HEAD`` merge-base.

    Returns its path, or ``None`` on failure (caller fails open). Additive
    and reversible — adding/removing a detached worktree touches no branch
    and no working tree. The checkout SHA is the merge-base (see
    :func:`_resolve_base_sha`) so the flake baseline matches the three-dot
    committed diff used to pick changed files.
    """
    base_sha = _resolve_base_sha(worktree, base_ref, run, timeout=timeout)
    base_dir = Path(tempfile.mkdtemp(prefix="hermes-code-gate-base-"))
    try:
        proc = run(
            [
                "git",
                "-C",
                str(worktree),
                "worktree",
                "add",
                "--detach",
                str(base_dir),
                base_sha,
            ],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except Exception as exc:
        logger.debug("code gate: base worktree add failed (%s)", exc)
        _rmtree(base_dir)
        return None
    if proc.returncode != 0:
        logger.debug("code gate: base worktree add rc=%s", proc.returncode)
        _rmtree(base_dir)
        return None
    return base_dir


def _cleanup_base_worktree(worktree: Path, base_dir: Path, run: RunFn) -> None:
    """Remove the temporary base worktree + its directory. Best-effort."""
    try:
        run(
            ["git", "-C", str(worktree), "worktree", "remove", "--force", str(base_dir)],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=_GIT_CALL_CAP_SEC,
        )
    except Exception as exc:
        logger.debug("code gate: base worktree remove failed (%s)", exc)
    # Best-effort prune so a worktree leaked by an earlier SIGKILL'd run does
    # not accumulate stale administrative entries under .git/worktrees.
    try:
        run(
            ["git", "-C", str(worktree), "worktree", "prune"],
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=_GIT_CALL_CAP_SEC,
        )
    except Exception as exc:
        logger.debug("code gate: worktree prune failed (%s)", exc)
    _rmtree(base_dir)


def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────


def _build_report(tests_red: List[str], ruff_violations: List[str]) -> str:
    parts: List[str] = []
    if tests_red:
        parts.append("Newly-failing tests (pre-existing flakes excluded):")
        parts.extend(f"  - {t}" for t in tests_red)
    if ruff_violations:
        parts.append("PLW1514 (missing encoding=) violations on changed files:")
        parts.extend(f"  - {v}" for v in ruff_violations)
    return "\n".join(parts)


def run(
    worktree,
    base_ref: str,
    *,
    run: RunFn = subprocess.run,
    cap_sec: int = DEFAULT_WALL_CLOCK_CAP_SEC,
    require_mapped_tests: bool = False,
    map_strategy: str = "mirror",
) -> GateResult:
    """Run the deterministic code gate over a worktree's diff.

    Returns a :class:`GateResult`. NEVER raises — every failure path returns
    ``ran=False`` (fail-open) so the caller falls through to the LLM judge.

    ``cap_sec`` bounds the whole gate (per-file pytest inside a live loop
    turn must not run unbounded). ``require_mapped_tests`` (default False):
    when True and changed source files have NO mapped test, the gate fails
    (forcing the lane to add coverage). ``map_strategy`` accepts only
    ``"mirror"`` today; any other value falls open.
    """
    deadline = time.monotonic() + max(1, int(cap_sec or DEFAULT_WALL_CLOCK_CAP_SEC))

    def _remaining() -> float:
        return deadline - time.monotonic()

    def _call_timeout(ceiling: float) -> float:
        return max(1.0, min(_remaining(), ceiling))

    if map_strategy != "mirror":
        logger.debug("code gate: unsupported map_strategy=%r; fail-open", map_strategy)
        return GateResult(ran=False)

    wt = Path(worktree)
    if not wt.is_dir():
        logger.debug("code gate: worktree %s missing; fail-open", wt)
        return GateResult(ran=False)

    # 1. Diff.
    if _remaining() <= 0:
        return GateResult(ran=False)
    changed = changed_files(wt, base_ref, run, timeout=_call_timeout(_GIT_CALL_CAP_SEC))
    if changed is None:
        return GateResult(ran=False)

    # 2. Map to test files + collect ruff targets. These are pure path
    #    helpers but ``run()`` promises to NEVER raise, so any unexpected
    #    failure here fails open at the boundary rather than propagating.
    try:
        test_files = map_tests(changed, wt)
        py_files = _changed_py_files(changed, wt)
    except Exception as exc:
        logger.debug("code gate: mapping changed files failed (%s); fail-open", exc)
        return GateResult(ran=False)

    if not changed:
        # Nothing changed — committed, uncommitted, or untracked (see
        # changed_files' union) — so there is nothing to veto. An empty
        # COMMITTED diff over a DIRTY worktree still yields a non-empty
        # ``changed`` and gates normally. Let the judge decide here.
        return GateResult(passed=True, ran=True, report="code gate: no changed files")

    # 3. ruff PLW1514 on changed .py files.
    ruff_violations: List[str] = []
    if py_files:
        if _remaining() <= 0:
            return GateResult(ran=False)
        rv = run_ruff(py_files, wt, run, timeout=_call_timeout(_PER_CALL_CAP_SEC))
        if rv is None:
            return GateResult(ran=False)
        ruff_violations = rv

    # require_mapped_tests policy: changed source but no mapped test.
    has_source_change = any(
        f.endswith(".py") and not f.startswith("tests/") and not f.endswith("__init__.py")
        for f in changed
    )
    if require_mapped_tests and has_source_change and not test_files:
        report = "code gate: changed source has no mapped test file (require_mapped_tests)"
        return GateResult(passed=False, ran=True, report=report)

    # 4. Run mapped tests at HEAD (per file), collect reds.
    head_failures: dict[str, Set[str]] = {}
    for tf in test_files:
        if _remaining() <= 0:
            logger.debug("code gate: wall-clock cap hit during HEAD tests; fail-open")
            return GateResult(ran=False)
        rc, out = run_pytest_file(tf, wt, run, timeout=_call_timeout(_PER_CALL_CAP_SEC))
        if rc == -1:
            return GateResult(ran=False)
        if rc in _PYTEST_OK_CODES:
            continue
        ids = _failed_ids(out)
        if not ids or rc not in _PYTEST_RAN_CODES:
            # Collection / internal / usage error with no attributable test
            # ids — cannot tell flake from break → fail-open.
            logger.debug("code gate: %s rc=%s unattributable; fail-open", tf, rc)
            return GateResult(ran=False)
        head_failures[tf] = ids

    # 5. No reds at HEAD → nothing to veto.
    if not head_failures:
        report = (
            f"code gate: {len(test_files)} mapped test file(s) green, "
            f"{len(ruff_violations)} PLW1514 violation(s)"
        )
        if ruff_violations:
            return GateResult(
                passed=False,
                ran=True,
                report=_build_report([], ruff_violations),
                ruff_violations=ruff_violations,
            )
        return GateResult(passed=True, ran=True, report=report)

    # 6. Flake-safe delta: re-run the RED files at base_ref, subtract
    #    pre-existing failures. Provision a detached base worktree.
    if _remaining() <= 0:
        return GateResult(ran=False)
    base_dir = _provision_base_worktree(
        wt, base_ref, run, timeout=_call_timeout(_GIT_CALL_CAP_SEC)
    )
    if base_dir is None:
        return GateResult(ran=False)

    newly_red: List[str] = []
    try:
        for tf, head_ids in head_failures.items():
            # A test file that doesn't exist at base is new → all its
            # failures are new (no base version to compare against).
            if not (base_dir / tf).is_file():
                newly_red.extend(sorted(head_ids))
                continue
            if _remaining() <= 0:
                logger.debug("code gate: cap hit during base tests; fail-open")
                return GateResult(ran=False)
            rc, out = run_pytest_file(
                tf, base_dir, run, timeout=_call_timeout(_PER_CALL_CAP_SEC)
            )
            if rc == -1 or rc not in _PYTEST_RAN_CODES:
                # Base run itself broke — cannot establish a clean baseline
                # → fail-open rather than risk wedging on a flake.
                logger.debug("code gate: base run %s rc=%s; fail-open", tf, rc)
                return GateResult(ran=False)
            base_ids = _failed_ids(out)
            newly_red.extend(sorted(head_ids - base_ids))
    finally:
        _cleanup_base_worktree(wt, base_dir, run)

    passed = not newly_red and not ruff_violations
    report = _build_report(newly_red, ruff_violations) or (
        "code gate: all HEAD test failures are pre-existing flakes"
    )
    return GateResult(
        passed=passed,
        ran=True,
        report=report,
        tests_red=newly_red,
        ruff_violations=ruff_violations,
    )


__all__ = [
    "GateResult",
    "changed_files",
    "map_tests",
    "run_pytest_file",
    "run_ruff",
    "run",
    "DEFAULT_WALL_CLOCK_CAP_SEC",
]
