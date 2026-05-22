"""Git worktree/branch hygiene tooling — ``hermes git-health``.

Three subcommands dispatched through :func:`git_health_command`: ``janitor``
(classify worktrees ACTIVE/MERGED/STALE/ORPHANED; reap a class with
``--confirm``), ``merge-ready`` (ahead/behind, changed files, sibling
overlap, conflict prediction, kanban status), and ``install-hooks``.

Every git operation is read-only except the explicit ``--confirm`` reap
and ``install-hooks``. Worktree removal is guarded: a tree is removed
only when its merged/class check passes; on any doubt it is renamed to
``<path>.deleted.<ts>`` rather than deleted (never ``rm -rf``).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Branches that must never be reaped, regardless of classification.
PROTECTED_BRANCHES = {"main", "master", "dashboard-live"}
DEFAULT_BASE = "fork/main"
DEFAULT_STALE_DAYS = 7
# Classes a caller may pass to ``--confirm``. ``ACTIVE`` is excluded by
# design — an active worktree is never reapable.
REAP_CLASSES = ("MERGED", "STALE", "ORPHANED")


# ── Path / environment resolution ─────────────────────────────────────────

def _hermes_home() -> Path:
    """Hermes home dir — honours ``HERMES_HOME`` for test isolation."""
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _run_registry_dir() -> Path:
    """Directory of the Phase 1 run-registry ``*.lock`` files."""
    return _hermes_home() / "run-registry"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── Git plumbing ──────────────────────────────────────────────────────────

def _git(repo, *args: str) -> subprocess.CompletedProcess:
    """Run ``git -C <repo> <args>``, capturing output; never raises."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )


def _is_protected_branch(branch: Optional[str]) -> bool:
    return bool(branch) and branch in PROTECTED_BRANCHES


def inventory_worktrees(repo) -> list[dict]:
    """Parse ``git worktree list --porcelain`` into a list of dicts.

    Each entry: ``{path, head, branch, bare, detached, locked, prunable}``;
    ``branch`` is the short name (``refs/heads/`` stripped) or ``None``.
    """
    cp = _git(repo, "worktree", "list", "--porcelain")
    if cp.returncode != 0:
        return []
    worktrees: list[dict] = []
    cur: dict = {}
    for line in cp.stdout.splitlines():
        if not line.strip():
            if cur:
                worktrees.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur = {
                "path": line[len("worktree "):],
                "head": None, "branch": None,
                "bare": False, "detached": False,
                "locked": False, "prunable": False,
            }
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].replace("refs/heads/", "", 1)
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
        elif line.startswith("locked"):
            cur["locked"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
    if cur:
        worktrees.append(cur)
    return worktrees


def _is_ancestor(repo, head: Optional[str], base: str) -> bool:
    """True when ``head`` is an ancestor of ``base`` (already merged)."""
    if not head:
        return False
    return _git(repo, "merge-base", "--is-ancestor", head, base).returncode == 0


def _commit_age_days(repo, head: Optional[str]) -> Optional[float]:
    """Age in days of ``head``'s commit, or ``None`` if unreadable."""
    if not head:
        return None
    cp = _git(repo, "log", "-1", "--format=%ct", head)
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    try:
        committed = int(cp.stdout.strip())
    except ValueError:
        return None
    return (datetime.now(timezone.utc).timestamp() - committed) / 86400.0


# ── Run-registry + kanban linkage ─────────────────────────────────────────

def _read_run_registry() -> list[dict]:
    """Every ``*.lock`` in the run-registry as a dict (``_path`` added).

    Unreadable or malformed locks are skipped.
    """
    registry = _run_registry_dir()
    if not registry.is_dir():
        return []
    locks: list[dict] = []
    for lock in sorted(registry.glob("*.lock")):
        try:
            data = json.loads(lock.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["_path"] = str(lock)
            locks.append(data)
    return locks


def _lock_for_branch(locks: list[dict], branch: Optional[str]) -> Optional[dict]:
    """First registry lock whose ``branch`` field matches, else ``None``."""
    if not branch:
        return None
    for lock in locks:
        if lock.get("branch") == branch:
            return lock
    return None


def _tmux_alive(session: Optional[str]) -> bool:
    """True when a tmux session named ``session`` currently exists."""
    if not session:
        return False
    try:
        return subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, check=False,
        ).returncode == 0
    except (OSError, ValueError):
        return False


def _card_status(card_id: Optional[str]) -> Optional[str]:
    """Kanban status for ``card_id``, or ``None`` on any failure.

    Degrades gracefully so the janitor keeps working when the kanban DB
    is missing, locked, or on an incompatible schema.
    """
    if not card_id:
        return None
    try:
        from hermes_cli import kanban_db
        with kanban_db.connect() as conn:
            task = kanban_db.get_task(conn, card_id)
        return getattr(task, "status", None) if task else None
    except Exception:
        return None


# ── Worktree classification (pure) ────────────────────────────────────────

def classify_worktree(
    wt: dict,
    *,
    lock: Optional[dict],
    is_merged: bool,
    card_status: Optional[str],
    tmux_alive: bool,
    age_days: Optional[float],
    stale_days: int = DEFAULT_STALE_DAYS,
) -> str:
    """Classify a worktree ``ACTIVE | MERGED | STALE | ORPHANED``.

    Pure — every I/O-derived input is passed in, so the decision is unit
    testable. Deliberately conservative: anything ambiguous resolves to
    ``ACTIVE`` so the reaper never touches it. ACTIVE = protected branch,
    live tmux session, or a running/blocked card; MERGED = head is an
    ancestor of the base; STALE = dead-session registry entry with an
    archived/done card older than ``stale_days``; ORPHANED = no lock and
    no card.
    """
    branch = wt.get("branch")
    if not branch or _is_protected_branch(branch):
        return "ACTIVE"
    # No registry lock => no branch->card link is discoverable.
    if lock is None:
        return "MERGED" if is_merged else "ORPHANED"
    # A live session or an in-flight card means ACTIVE.
    if tmux_alive or card_status in ("running", "blocked"):
        return "ACTIVE"
    if is_merged:
        return "MERGED"
    # Dead session + terminal card + an old commit => safe to reap.
    if card_status in ("archived", "done") and (
        age_days is None or age_days > stale_days
    ):
        return "STALE"
    # A registry entry we cannot confidently age out — never auto-reap.
    return "ACTIVE"


def select_reapable(
    classified: list[tuple[dict, str]], confirm_class: str
) -> list[tuple[dict, str]]:
    """Filter ``(worktree, class)`` pairs to those safe to reap.

    Survivors match ``confirm_class`` and are never ``ACTIVE`` nor a
    protected branch — the guard the ``--confirm`` path relies on.
    """
    out: list[tuple[dict, str]] = []
    for wt, klass in classified:
        if klass != confirm_class or klass == "ACTIVE":
            continue
        if _is_protected_branch(wt.get("branch")):
            continue
        out.append((wt, klass))
    return out


def reap_worktree(repo, wt: dict, klass: str) -> tuple[str, str]:
    """Reap one worktree. Returns ``(action, detail)``.

    ``MERGED`` worktrees are removed after a fresh ancestor re-check (the
    work is safely in the base ref). ``STALE``/``ORPHANED`` worktrees may
    carry unmerged work, so they are *renamed* to ``<path>.deleted.<ts>``
    and pruned — never force-removed, never ``rm -rf``.
    """
    if _is_protected_branch(wt.get("branch")):
        return ("skipped", "protected branch")
    path = Path(wt["path"])
    # The primary checkout's ``.git`` is a real directory; linked
    # worktrees use a ``.git`` *file* pointing into it. Never reap the
    # main working tree — removing/renaming it would break the repo.
    if (path / ".git").is_dir():
        return ("skipped", "main working tree")
    if klass == "MERGED":
        if not _is_ancestor(repo, wt.get("head"), DEFAULT_BASE):
            return ("skipped", "merge re-check failed — leaving in place")
        if _git(repo, "worktree", "remove", str(path)).returncode == 0:
            return ("removed", "git worktree remove")
        # Removal refused (dirty/untracked) — fall through to soft delete.
    dest = path.with_name(f"{path.name}.deleted.{_utc_stamp()}")
    try:
        path.rename(dest)
    except OSError as exc:
        return ("error", f"rename failed: {exc}")
    _git(repo, "worktree", "prune")
    return ("renamed", str(dest))


# ── merge-ready ───────────────────────────────────────────────────────────

def _rev_count(repo, rev_range: str) -> Optional[int]:
    cp = _git(repo, "rev-list", "--count", rev_range)
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    try:
        return int(cp.stdout.strip())
    except ValueError:
        return None


def _diff_name_only(repo, rev_range: str) -> list[str]:
    cp = _git(repo, "diff", "--name-only", rev_range)
    return [ln for ln in cp.stdout.splitlines() if ln.strip()] if cp.returncode == 0 else []


def _predict_conflict(repo, base: str, branch: str) -> str:
    """Predict a merge outcome: ``CLEAN | CONFLICT | UNKNOWN``."""
    # Modern git (>=2.38): --write-tree exits 0 clean, 1 on conflict.
    cp = _git(repo, "merge-tree", "--write-tree", base, branch)
    if cp.returncode == 0:
        return "CLEAN"
    if cp.returncode == 1:
        return "CONFLICT"
    # Legacy 3-arg form: exits 0, emits conflict markers inline.
    mb = _git(repo, "merge-base", base, branch)
    if mb.returncode == 0 and mb.stdout.strip():
        legacy = _git(repo, "merge-tree", mb.stdout.strip(), base, branch)
        if legacy.returncode == 0:
            if "<<<<<<<" in legacy.stdout or "changed in both" in legacy.stdout:
                return "CONFLICT"
            return "CLEAN"
    return "UNKNOWN"


def merge_ready_report(branch: str, repo, *, base: str = DEFAULT_BASE) -> dict:
    """Build the merge-readiness report for ``branch`` against ``base``."""
    if _git(repo, "rev-parse", "--verify", branch).returncode != 0:
        return {"branch": branch, "base": base, "error": f"unknown branch '{branch}'"}

    changed = _diff_name_only(repo, f"{base}...{branch}")
    changed_set = set(changed)
    overlaps: dict[str, list[str]] = {}
    for other in inventory_worktrees(repo):
        ob = other.get("branch")
        if not ob or ob == branch or _is_protected_branch(ob):
            continue
        common = sorted(changed_set & set(_diff_name_only(repo, f"{base}...{ob}")))
        if common:
            overlaps[ob] = common

    lock = _lock_for_branch(_read_run_registry(), branch)
    card_id = lock.get("tracking_card") if lock else None
    return {
        "branch": branch,
        "base": base,
        "ahead": _rev_count(repo, f"{base}..{branch}"),
        "behind": _rev_count(repo, f"{branch}..{base}"),
        "changed_files": changed,
        "overlaps": overlaps,
        "conflict_prediction": _predict_conflict(repo, base, branch),
        "kanban_card": card_id,
        "kanban_status": _card_status(card_id),
    }


# ── install-hooks ─────────────────────────────────────────────────────────

_HOOK_MARKER = "hermes-git-health-hook"

# Thin fallback hook — used when ``pre-commit`` is unavailable. Refuses
# commits to a protected branch and blocks unresolved conflict markers.
_FALLBACK_HOOK = """#!/usr/bin/env bash
# hermes-git-health-hook — installed by `hermes git-health install-hooks`.
# Thin pre-commit gate: no direct commits to protected branches, and no
# unresolved merge-conflict markers in the staged diff.
set -u
branch=$(git symbolic-ref --short -q HEAD || echo "")
case "$branch" in
  main|master|dashboard-live)
    echo "pre-commit: direct commits to '$branch' are blocked." >&2
    echo "  create a feature branch first." >&2
    exit 1 ;;
esac
markers=$(git diff --cached -U0 | grep -E '^\\+(<{7}|={7}|>{7})' || true)
if [ -n "$markers" ]; then
  echo "pre-commit: unresolved merge-conflict markers in staged changes." >&2
  exit 1
fi
exit 0
"""


def _git_hooks_dir(repo) -> Optional[Path]:
    """Resolve the effective git hooks directory for ``repo``."""
    cp = _git(repo, "rev-parse", "--git-path", "hooks")
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    hooks = Path(cp.stdout.strip())
    return hooks if hooks.is_absolute() else Path(repo) / hooks


def _install_hook_into(repo) -> dict:
    """Install hooks into a single repo. Returns a result dict."""
    import shutil
    repo = str(repo)

    config = Path(repo) / ".pre-commit-config.yaml"
    if shutil.which("pre-commit") and config.exists():
        cp = subprocess.run(
            ["pre-commit", "install"], cwd=repo,
            capture_output=True, text=True, check=False,
        )
        if cp.returncode == 0:
            return {"repo": repo, "mode": "pre-commit", "detail": "pre-commit install"}
        # else fall through to the thin fallback hook.

    hooks_dir = _git_hooks_dir(repo)
    if hooks_dir is None:
        return {"repo": repo, "mode": "error", "detail": "no git hooks dir"}
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-commit"
        if hook.exists() and _HOOK_MARKER not in hook.read_text(errors="ignore"):
            # Preserve a pre-existing third-party hook before overwriting.
            hook.rename(hook.with_name(f"pre-commit.pre-hermes.{_utc_stamp()}"))
        hook.write_text(_FALLBACK_HOOK)
        hook.chmod(0o755)
    except OSError as exc:
        return {"repo": repo, "mode": "error", "detail": str(exc)}
    return {"repo": repo, "mode": "fallback", "detail": str(hook)}


def install_hooks(repo, *, all_worktrees: bool = False) -> list[dict]:
    """Install pre-commit hooks into ``repo`` (and active worktrees)."""
    if not all_worktrees:
        return [_install_hook_into(repo)]
    # Hooks are shared via the common git dir; dedupe by resolved dir.
    seen: set[str] = set()
    targets: list[str] = []
    for wt in inventory_worktrees(repo):
        if wt.get("bare") or not wt.get("branch"):
            continue
        hd = _git_hooks_dir(wt["path"])
        key = str(hd.resolve()) if hd else wt["path"]
        if key not in seen:
            seen.add(key)
            targets.append(wt["path"])
    return [_install_hook_into(t) for t in (targets or [str(repo)])]


# ── janitor orchestration + CLI dispatch ──────────────────────────────────

def gather_classified(
    repo, *, stale_days: int = DEFAULT_STALE_DAYS
) -> list[tuple[dict, str]]:
    """Inventory and classify every worktree of ``repo``."""
    locks = _read_run_registry()
    classified: list[tuple[dict, str]] = []
    for wt in inventory_worktrees(repo):
        lock = _lock_for_branch(locks, wt.get("branch"))
        card_id = lock.get("tracking_card") if lock else None
        klass = classify_worktree(
            wt,
            lock=lock,
            is_merged=_is_ancestor(repo, wt.get("head"), DEFAULT_BASE),
            card_status=_card_status(card_id),
            tmux_alive=_tmux_alive(lock.get("tmux_session") if lock else None),
            age_days=_commit_age_days(repo, wt.get("head")),
            stale_days=stale_days,
        )
        classified.append((wt, klass))
    return classified


def _resolve_repo(arg: Optional[str]) -> str:
    """Resolve ``--repo`` to a usable path (default: cwd)."""
    return str(Path(arg).expanduser()) if arg else os.getcwd()


def run_janitor(
    repo, *, stale_days: int = DEFAULT_STALE_DAYS, confirm: Optional[str] = None
) -> int:
    """Execute ``hermes git-health janitor``. Returns a process exit code."""
    classified = gather_classified(repo, stale_days=stale_days)
    if not classified:
        print("git-health janitor: no worktrees found (not a git repo?)")
        return 1

    counts: dict[str, int] = {}
    for _wt, klass in classified:
        counts[klass] = counts.get(klass, 0) + 1
    print(f"git-health janitor — {len(classified)} worktree(s) "
          f"[stale-days={stale_days}]")
    print("  " + "  ".join(f"{k}={counts.get(k, 0)}"
                           for k in ("ACTIVE", "MERGED", "STALE", "ORPHANED")))
    print(f"\n{'CLASS':<9} {'BRANCH':<46} PATH")
    print("-" * 100)
    for wt, klass in classified:
        branch = wt.get("branch") or ("(detached)" if wt.get("detached") else "(bare)")
        print(f"{klass:<9} {branch:<46} {wt['path']}")

    if not confirm:
        print("\n(dry-run — no mutations. Pass --confirm "
              "MERGED|STALE|ORPHANED to reap a class.)")
        return 0

    confirm = confirm.upper()
    if confirm not in REAP_CLASSES:
        print(f"\n--confirm must be one of {REAP_CLASSES}")
        return 2
    reapable = select_reapable(classified, confirm)
    if not reapable:
        print(f"\nNothing classified {confirm} to reap.")
        return 0
    print(f"\nReaping {len(reapable)} worktree(s) classified {confirm}:")
    for wt, klass in reapable:
        action, detail = reap_worktree(repo, wt, klass)
        print(f"  [{action}] {wt.get('branch')}  ({wt['path']}) — {detail}")
    return 0


def _print_merge_ready(report: dict) -> int:
    if report.get("error"):
        print(f"merge-ready: {report['error']}")
        return 1
    print(f"merge-ready — {report['branch']}  (base {report['base']})")
    print(f"  ahead:  {report['ahead']}    behind: {report['behind']}")
    print(f"  conflict prediction: {report['conflict_prediction']}")
    print(f"  kanban card: {report['kanban_card'] or '-'}  "
          f"status: {report['kanban_status'] or '-'}")
    changed = report["changed_files"]
    print(f"  changed files ({len(changed)}):")
    for path in changed[:40]:
        print(f"    {path}")
    if len(changed) > 40:
        print(f"    ... and {len(changed) - 40} more")
    if report["overlaps"]:
        print("  file overlap with sibling worktree branches:")
        for other, files in report["overlaps"].items():
            print(f"    {other}: {len(files)} file(s) — {', '.join(files[:6])}")
    else:
        print("  file overlap with sibling worktree branches: none")
    return 0


def git_health_command(args) -> int:
    """Dispatch ``hermes git-health <subcommand>``."""
    sub = getattr(args, "git_health_command", None)
    if not sub:
        print("usage: hermes git-health <janitor|merge-ready|install-hooks>")
        return 1

    if sub == "janitor":
        return run_janitor(
            _resolve_repo(getattr(args, "repo", None)),
            stale_days=getattr(args, "stale_days", DEFAULT_STALE_DAYS),
            confirm=getattr(args, "confirm", None),
        )

    if sub == "merge-ready":
        return _print_merge_ready(merge_ready_report(
            args.branch,
            _resolve_repo(getattr(args, "repo", None)),
            base=getattr(args, "base", DEFAULT_BASE),
        ))

    if sub == "install-hooks":
        results = install_hooks(
            _resolve_repo(getattr(args, "repo", None)),
            all_worktrees=getattr(args, "all_worktrees", False),
        )
        rc = 0
        for res in results:
            print(f"  [{res['mode']}] {res['repo']} — {res['detail']}")
            if res["mode"] == "error":
                rc = 1
        return rc

    print(f"unknown git-health subcommand: {sub}")
    return 1
