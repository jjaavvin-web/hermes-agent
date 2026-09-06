"""Auto-PR for completed kanban CODE cards (task #12 — the code→PR layer).

A kanban code worker commits its change on a branch inside a git worktree.
``open_pr`` then pushes that branch and opens a ``needs-human`` PR via
``gh pr create``. The PR's label comes from the SAME deny-list classifier the
codex pipeline uses (``MergeBroker.classify_change``) — engine changes
(``hermes_cli/`` etc.) get ``needs-human`` and can never be auto-merged.

Deliberately decoupled from ``MergeBroker.merge()``, which is ISA-coupled
(``isa_lint`` + ISA-flavoured PR title/body) and codex-critical — we reuse only
the read-only ``classify_change``. Multi-repo aware: a remoteless target repo
(e.g. the local-only ict-brain repo) cannot open a PR, so we record a local
branch for human review instead of failing.

The merge gate stays HUMAN: PRs are opened, never merged here. Auto-merge is a
separate, gated decision (Mergify acting on the ``auto-merge`` label), and the
``auto-merge`` label only appears for ``safe`` changes by the same classifier.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

_GIT_TIMEOUT = 60
_GH_TIMEOUT = 90
# Push-remote preference: 'fork' is the writable jjaavvin-web fork; 'origin' is
# the read-only NousResearch upstream in this environment, so it is tried last.
_REMOTE_PREFERENCE = ("fork", "origin", "upstream")


def _git(run: Callable, worktree: Path, *args: str):
    return run(
        ["git", "-C", str(worktree), *args],
        capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT,
    )


def _detect_push_remote(run: Callable, worktree: Path) -> Optional[str]:
    """Return the preferred writable remote name, or None for a remoteless repo."""
    r = _git(run, worktree, "remote")
    if r.returncode != 0:
        return None
    remotes = r.stdout.split()
    for pref in _REMOTE_PREFERENCE:
        if pref in remotes:
            return pref
    return remotes[0] if remotes else None


def _commits_ahead(run: Callable, worktree: Path, base_ref: str, branch: str) -> Optional[int]:
    """Count commits on ``branch`` not in ``base_ref``. None if it can't be computed."""
    r = _git(run, worktree, "rev-list", "--count", f"{base_ref}..{branch}")
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except (ValueError, AttributeError):
        return None


def _gh_repo_slug(run: Callable, worktree: Path, remote: str) -> Optional[str]:
    """OWNER/REPO slug for ``remote`` (gh needs --repo; origin would mis-target upstream)."""
    r = _git(run, worktree, "remote", "get-url", remote)
    if r.returncode != 0:
        return None
    url = (r.stdout or "").strip()
    slug: Optional[str] = None
    if url.startswith("git@") and ":" in url:
        slug = url.split(":", 1)[1]
    elif "github.com/" in url:
        slug = url.split("github.com/", 1)[1]
    if not slug:
        return None
    return slug.removesuffix(".git").strip("/") or None


def _default_classify(worktree: Path, base_remote: str, base_branch: str,
                      hermes_home: Optional[Path], run: Callable) -> str:
    """Reuse the codex deny-list classifier (read-only) for the PR label."""
    try:
        from agent.merge_broker import MergeBroker
        mb = MergeBroker(
            hermes_home=hermes_home or Path.home() / ".hermes",
            base_branch=base_branch,
            base_remote=base_remote,
            subprocess_run=run,
        )
        return mb.classify_change(worktree)
    except Exception:
        # Fail closed: if we can't classify, treat as sensitive (needs-human).
        return "sensitive"


def open_pr(
    *,
    worktree: Any,
    branch: str,
    title: str,
    body: str,
    base_branch: str = "main",
    hermes_home: Optional[Path] = None,
    run: Optional[Callable] = None,
    classify: Optional[Callable] = None,
) -> dict:
    """Push ``branch`` and open a PR for a completed code card's ``worktree``.

    Returns a dict: always has ``ok``; ``mode`` ∈ {pr, local-branch, noop, error}.
    Never raises — failures land in ``error``. The merge gate stays human: this
    opens a PR, it never merges.
    """
    run = run or subprocess.run
    worktree = Path(worktree)

    remote = _detect_push_remote(run, worktree)
    if remote is None:
        # Remoteless repo (e.g. local-only ict-brain): can't PR. The branch is
        # already committed locally; surface it for human review.
        return {
            "ok": True, "mode": "local-branch", "pr_url": None, "branch": branch,
            "note": "remoteless repo — change is on a local branch; review locally (no PR possible)",
        }

    base_ref = f"{remote}/{base_branch}"
    ahead = _commits_ahead(run, worktree, base_ref, branch)
    if ahead == 0:
        return {"ok": True, "mode": "noop", "pr_url": None,
                "note": f"no commits ahead of {base_ref}; nothing to PR"}

    push = _git(run, worktree, "push", remote, branch)
    if push.returncode != 0:
        return {"ok": False, "mode": "error", "error": f"git push failed: {push.stderr.strip()}"}

    slug = _gh_repo_slug(run, worktree, remote)
    repo_args = ["--repo", slug] if slug else []

    # Idempotency: reuse an existing open PR for this branch.
    existing = run(
        ["gh", "pr", "list", *repo_args, "--head", branch, "--state", "open",
         "--json", "number,url", "--limit", "1"],
        capture_output=True, text=True, check=False, timeout=_GH_TIMEOUT,
    )
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    if existing.returncode == 0 and existing.stdout.strip():
        try:
            rows = json.loads(existing.stdout)
            if rows:
                pr_number, pr_url = rows[0].get("number"), rows[0].get("url")
        except json.JSONDecodeError:
            pass

    if pr_url is None:
        create = run(
            ["gh", "pr", "create", *repo_args, "--base", base_branch, "--head", branch,
             "--title", title, "--body", body],
            capture_output=True, text=True, check=False, timeout=_GH_TIMEOUT,
        )
        if create.returncode != 0:
            return {"ok": False, "mode": "error",
                    "error": f"gh pr create failed: {create.stderr.strip()}"}
        out = (create.stdout or "").strip()
        pr_url = out.splitlines()[-1] if out else None
        try:
            pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1]) if pr_url else None
        except ValueError:
            pr_number = None

    classifier = classify or _default_classify
    classification = classifier(worktree, remote, base_branch, hermes_home, run)
    # 'auto-merge' label is inert until Mergify is installed; 'needs-human' (the
    # default for any sensitive change incl. all of hermes_cli/) keeps the gate.
    label = "auto-merge" if classification == "safe" else "needs-human"
    if pr_number is not None:
        run(
            ["gh", "pr", "edit", *repo_args, str(pr_number), "--add-label", label],
            capture_output=True, text=True, check=False, timeout=_GH_TIMEOUT,
        )

    return {"ok": True, "mode": "pr", "pr_url": pr_url, "pr_number": pr_number,
            "classification": classification, "label": label}
