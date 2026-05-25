"""Merge Broker — serialized fork/main merges for Codex sessions.

Implements ``isas/P3-merge-broker.md`` + ``module-specs/merge-broker.md``.

When a P2.5 verdict transitions a session to ``MERGING``, the dispatcher
calls :meth:`MergeBroker.merge`.  The broker acquires a global flock,
rebases the worktree onto ``origin/main``, runs ``isa_lint``, pushes the
feature branch, opens a PR via ``gh pr create``, classifies the change
as ``safe`` or ``sensitive``, and applies the matching label
(``auto-merge`` / ``needs-human``).  Mergify (or the alternate Actions
workflow) handles label-gated server-side auto-merge.

Constraint highlights:
- single global merge mutex via ``flock ~/.hermes/codex-merge.lock``
- never force-push to ``fork/main`` or any branch
- ``isa_lint`` exit 0 is non-negotiable before push
- classify_change deny-list is intentionally broad (over-flag > miss)
- worktree release is NOT done here — dispatcher polls for merged PR
"""

from __future__ import annotations

import fcntl
import json
import logging
import shlex
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

_DEFAULT_FLOCK_TIMEOUT_SEC = 30 * 60  # 30 min hard cap
_DEFAULT_GIT_TIMEOUT_SEC = 60
_DEFAULT_GH_TIMEOUT_SEC = 90

# Paths that, if touched, force the PR to needs-human review.  Deny-list is
# intentionally broad on first ship; operator can narrow it in P5 hardening.
_SENSITIVE_PREFIXES: tuple[str, ...] = (
    "agent/",
    "gateway/",
    "auth/",
    "migrations/",
    "pyproject.toml",
    "package",          # package.json, package-lock.json
    ".github/",
    "scripts/isa_",
    "hermes_state.py",
    "hermes_cli/web_server.py",
)


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass
class MergeResult:
    ok: bool
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    classification: Optional[str] = None  # "safe" | "sensitive"
    error: Optional[str] = None
    duration_sec: float = 0.0


class ConflictEscalation(RuntimeError):
    """Raised when ``git rebase origin/main`` produces conflicts."""


class MergeBrokerError(RuntimeError):
    """Base for non-conflict merge failures."""


# ── MergeBroker ──────────────────────────────────────────────────────────────


class MergeBroker:
    """Serialized merge orchestrator for APPROVE'd Codex sessions."""

    def __init__(
        self,
        *,
        hermes_home: Path,
        base_branch: str = "main",
        base_remote: str = "fork",
        flock_timeout_sec: int = _DEFAULT_FLOCK_TIMEOUT_SEC,
        # Indirection for tests — production passes the real callables.
        subprocess_run=None,
    ) -> None:
        self._hermes_home = Path(hermes_home)
        self._base_branch = base_branch
        self._base_remote = base_remote
        self._flock_timeout_sec = flock_timeout_sec
        self._run = subprocess_run or subprocess.run
        self._lock_path = self._hermes_home / "codex-merge.lock"

    async def merge(
        self,
        *,
        session_id: str,
        worktree: Path,
        branch: str,
        isa_path: Path,
        summary: str,
    ) -> MergeResult:
        """Merge a session's branch via flock + rebase + lint + push + PR.

        Returns :class:`MergeResult`.  Never raises — failures land in
        ``MergeResult.error``.  Conflicts are encoded as a special
        ``error="conflict: <details>"`` so the caller can post the
        escalation to Discord without catching an exception.
        """
        start = time.monotonic()
        worktree = Path(worktree)
        isa_path = Path(isa_path)

        with self._merge_flock():
            # Step 1: rebase onto latest origin/main
            try:
                self._fetch_origin(worktree)
                self._rebase_onto_origin_main(worktree)
            except ConflictEscalation as exc:
                return MergeResult(
                    ok=False,
                    error=f"conflict: {exc}",
                    duration_sec=time.monotonic() - start,
                )
            except MergeBrokerError as exc:
                return MergeResult(
                    ok=False,
                    error=str(exc),
                    duration_sec=time.monotonic() - start,
                )

            # Step 2: isa_lint must exit 0
            lint = self._run_isa_lint(isa_path)
            if lint.returncode != 0:
                return MergeResult(
                    ok=False,
                    error=f"isa_lint failed: {lint.stdout.strip() or lint.stderr.strip()}",
                    duration_sec=time.monotonic() - start,
                )

            # Step 3: push feature branch to fork remote
            push = self._run(
                ["git", "-C", str(worktree), "push", self._base_remote, branch],
                capture_output=True, text=True, check=False,
                timeout=_DEFAULT_GIT_TIMEOUT_SEC,
            )
            if push.returncode != 0:
                return MergeResult(
                    ok=False,
                    error=f"git push failed: {push.stderr.strip()}",
                    duration_sec=time.monotonic() - start,
                )

        # Step 4: open PR (no longer under flock — gh + Discord I/O slow paths)
        pr_number, pr_url, pr_err = self._ensure_pr(
            worktree=worktree,
            branch=branch,
            isa_path=isa_path,
            session_id=session_id,
            summary=summary,
        )
        if pr_number is None:
            return MergeResult(
                ok=False,
                error=pr_err or "gh pr create failed",
                duration_sec=time.monotonic() - start,
            )

        # Step 5: classify + label
        classification = self.classify_change(worktree)
        label = "auto-merge" if classification == "safe" else "needs-human"
        self._run(
            ["gh", "pr", "edit", str(pr_number), "--add-label", label],
            capture_output=True, text=True, check=False,
            timeout=_DEFAULT_GH_TIMEOUT_SEC,
        )

        return MergeResult(
            ok=True,
            pr_number=pr_number,
            pr_url=pr_url,
            classification=classification,
            duration_sec=time.monotonic() - start,
        )

    # ── classification ──────────────────────────────────────────────────

    def classify_change(self, worktree: Path) -> str:
        """Return 'safe' if NO changed file matches the deny-list, else 'sensitive'."""
        diff = self._run(
            ["git", "-C", str(worktree), "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True, text=True, check=False,
            timeout=_DEFAULT_GIT_TIMEOUT_SEC,
        )
        if diff.returncode != 0:
            log.warning("classify_change: diff failed, defaulting to sensitive: %s", diff.stderr.strip())
            return "sensitive"
        for line in diff.stdout.splitlines():
            file = line.strip()
            if not file:
                continue
            if any(file.startswith(p) for p in _SENSITIVE_PREFIXES):
                return "sensitive"
        return "safe"

    # ── git helpers ─────────────────────────────────────────────────────

    def _fetch_origin(self, worktree: Path) -> None:
        r = self._run(
            ["git", "-C", str(worktree), "fetch", "origin"],
            capture_output=True, text=True, check=False,
            timeout=_DEFAULT_GIT_TIMEOUT_SEC,
        )
        if r.returncode != 0:
            raise MergeBrokerError(f"git fetch origin failed: {r.stderr.strip()}")

    def _rebase_onto_origin_main(self, worktree: Path) -> None:
        r = self._run(
            ["git", "-C", str(worktree), "rebase", "origin/main"],
            capture_output=True, text=True, check=False,
            timeout=_DEFAULT_GIT_TIMEOUT_SEC,
        )
        if r.returncode != 0:
            # Abort the in-progress rebase so the worktree is clean again.
            self._run(
                ["git", "-C", str(worktree), "rebase", "--abort"],
                capture_output=True, text=True, check=False,
                timeout=_DEFAULT_GIT_TIMEOUT_SEC,
            )
            raise ConflictEscalation(r.stderr.strip() or r.stdout.strip())

    def _run_isa_lint(self, isa_path: Path) -> subprocess.CompletedProcess:
        return self._run(
            ["python3", "scripts/isa_lint.py", str(isa_path)],
            capture_output=True, text=True, check=False,
            timeout=_DEFAULT_GIT_TIMEOUT_SEC,
        )

    def _ensure_pr(
        self,
        *,
        worktree: Path,
        branch: str,
        isa_path: Path,
        session_id: str,
        summary: str,
    ) -> tuple[Optional[int], Optional[str], Optional[str]]:
        """Open a PR if one doesn't exist for the branch; return (number, url, err)."""
        # Check if a PR already exists (idempotency).
        existing = self._run(
            ["gh", "pr", "list", "--head", branch, "--state", "open",
             "--json", "number,url", "--limit", "1"],
            capture_output=True, text=True, check=False,
            timeout=_DEFAULT_GH_TIMEOUT_SEC,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            try:
                rows = json.loads(existing.stdout)
                if rows:
                    return rows[0].get("number"), rows[0].get("url"), None
            except json.JSONDecodeError:
                pass

        # Compose PR body.
        body = self._render_pr_body(isa_path, session_id, summary)
        create = self._run(
            ["gh", "pr", "create",
             "--base", self._base_branch,
             "--head", branch,
             "--title", f"feat(codex): {Path(isa_path).stem} — peer-reviewed by Opus",
             "--body", body],
            capture_output=True, text=True, check=False,
            timeout=_DEFAULT_GH_TIMEOUT_SEC,
        )
        if create.returncode != 0:
            return None, None, f"gh pr create failed: {create.stderr.strip()}"

        # gh prints the URL on stdout; extract number from the URL tail.
        url = create.stdout.strip().splitlines()[-1] if create.stdout.strip() else ""
        try:
            number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url else None
        except ValueError:
            number = None
        return number, url or None, None

    def _render_pr_body(
        self,
        isa_path: Path,
        session_id: str,
        summary: str,
    ) -> str:
        try:
            isa_text = Path(isa_path).read_text(encoding="utf-8") if isa_path.exists() else ""
        except OSError:
            isa_text = "<isa unreadable>"
        progress = ""
        for line in isa_text.splitlines():
            if line.strip().startswith("progress:"):
                progress = line.strip()
                break

        return (
            f"## Summary\n\n"
            f"Codex session `{session_id[:8]}` — peer-reviewed by Opus (APPROVE).\n\n"
            f"**ISA path:** `{isa_path}`\n"
            f"**ISC progress:** {progress or '<unknown>'}\n\n"
            f"## Opus verdict\n\n"
            f"{summary}\n\n"
            f"## Verification\n\n"
            f"Per the ISA above — see `## Verification` block for verbatim probe outputs.\n\n"
            f"🤖 Generated by the codex parallel workflow merge broker (P3).\n"
        )

    # ── flock helper ────────────────────────────────────────────────────

    @contextmanager
    def _merge_flock(self):
        """Acquire the global merge lock; release on exit (always)."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(self._lock_path, "w", encoding="utf-8")
        deadline = time.monotonic() + self._flock_timeout_sec
        try:
            while True:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise MergeBrokerError(
                            f"could not acquire {self._lock_path} within "
                            f"{self._flock_timeout_sec}s"
                        )
                    time.sleep(0.5)
            fd.write(f"pid={Path('/proc/self').resolve().name} ts={time.time()}\n")
            fd.flush()
            yield
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fd.close()
