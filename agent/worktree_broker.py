"""Worktree Broker — per-session git worktree lifecycle and port allocation.

Implements DESIGN.md §6.2.  Phase P1 covers allocate/release; gc() is P5.

The broker is the single module responsible for creating, tracking, and
destroying per-session git worktrees under ~/.hermes/codex-wt/<sid>/ and
for owning the port side-table at ~/.hermes/codex-ports.json.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Tuple

log = logging.getLogger(__name__)

# ── Public error classes ──────────────────────────────────────────────────────


class DiskPressureError(OSError):
    """Raised when ~/.hermes free space is below 4 GB before allocate."""


class BranchCollisionError(ValueError):
    """Raised when the branch codex/<sid>/<isa-slug> already exists.

    Structurally impossible with UUID4 sids, but handled explicitly.
    """


class LeaseCapacityError(RuntimeError):
    """Raised when the configured active worktree lease cap is exhausted."""


class RepoStateError(RuntimeError):
    """Raised when git worktree add fails due to dirty parent repo state.

    Broker does NOT stash — that would silently discard operator work.
    """


class WorktreeReleaseRefused(RuntimeError):
    """Raised when a NON-FORCE release could not complete (C7 / Gate 7).

    Signals that git declined to remove the worktree — or removed its index
    entry yet left the directory behind — and that the broker consequently
    refused to escalate.  Callers (notably
    :class:`gateway.codex_session_reaper.CodexSessionReaper`) treat this as
    "quarantine, do not delete": the session row is downgraded to ORPHANED
    with the refusal reason and a human decides.
    """


# ── Data classes ──────────────────────────────────────────────────────────────

_LOCK_TYPE_FILES = [
    ("pnpm", "pnpm-lock.yaml"),
    ("npm", "package-lock.json"),
    ("yarn", "yarn.lock"),
]

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_REPEAT_DASH_RE = re.compile(r"-+")
_IDENTITY_PATH_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify_ref(value: str, *, fallback: str = "task", max_len: int = 40) -> str:
    """Coerce arbitrary text into a git-ref-safe slug.

    Discord thread titles, user-supplied ISA names, etc. routinely contain
    spaces, capitals, and punctuation that `git worktree add -b` rejects.
    """
    lowered = (value or "").lower().strip()
    replaced = _SLUG_INVALID_RE.sub("-", lowered)
    collapsed = _SLUG_REPEAT_DASH_RE.sub("-", replaced).strip("-")
    truncated = collapsed[:max_len].rstrip("-")
    return truncated or fallback


@dataclass
class Worktree:
    session_id: str        # UUID4 assigned by dispatcher
    path: Path             # ~/.hermes/codex-wt/<sid>/
    branch: str            # codex/<sid>/<isa-slug>
    port: int | None       # None if no port was available
    created_at: datetime
    lock_type: str | None = field(default=None)  # pnpm | npm | yarn | None
    base_sha: str | None = None
    identity: str | None = None


@dataclass
class WorktreeStatus:
    session_id: str
    path: Path
    branch: str
    port: int | None
    created_at: datetime
    path_exists: bool      # False if worktree was removed externally
    tmux_alive: bool       # result of `tmux has-session -t codex-sess-<sid>`


# ── GcAction stub (P5) ────────────────────────────────────────────────────────

@dataclass
class GcAction:
    """Describes one gc() rename action.  Defined here so P5 can import it."""
    sid: str
    old_path: Path
    new_path: Path
    reason: str


# ── Broker ────────────────────────────────────────────────────────────────────

_GB = 1024 ** 3
_DISK_HARD_FLOOR = 4 * _GB
_DISK_SOFT_FLOOR = 8 * _GB


class WorktreeBroker:
    def __init__(
        self,
        *,
        repo_root: Path,
        hermes_home: Path,
        port_range: Tuple[int, int] = (50000, 50008),
        existing_sessions: Mapping[str, str | Mapping[str, Any]] | None = None,
        wt_dir_name: str = "codex-wt",
        branch_prefix: str = "codex",
        ports_enabled: bool = True,
        max_active_leases: int | None = None,
    ) -> None:
        """Initialise the broker.

        repo_root         — absolute path to the hermes-agent git repo.
        hermes_home       — typically Path("~/.hermes").expanduser()
        port_range        — half-open [lo, hi); default covers 50000-50007.
        existing_sessions — optional {session_id: worktree_path} mapping read
                            from codex_sessions.json by the caller on restart.
        wt_dir_name       — directory below hermes_home for worktrees.
        branch_prefix     — first git-ref component for new branches.
        ports_enabled     — when false, never create/recover/touch codex-ports.json.
        max_active_leases — optional cap for active registry entries.
        """
        self.repo_root = Path(repo_root)
        self.hermes_home = Path(hermes_home)
        self.port_range = port_range
        self.wt_dir_name = wt_dir_name
        self.branch_prefix = branch_prefix
        self.ports_enabled = ports_enabled
        self.max_active_leases = max_active_leases
        self._wt_root = self.hermes_home / wt_dir_name

        # In-memory registry keyed by session_id → Worktree
        self._registry: dict[str, Worktree] = {}

        # Pre-populate from existing_sessions (M7 amendment — bot-restart path)
        if existing_sessions:
            for sid, entry in existing_sessions.items():
                if isinstance(entry, Mapping):
                    wt_path = Path(str(entry.get("path", self._wt_root / sid)))
                    branch = str(entry.get("branch") or f"{self.branch_prefix}/{sid}/unknown")
                    base_sha = entry.get("base_sha") or entry.get("base")
                    identity = entry.get("identity")
                else:
                    wt_path = Path(entry)
                    branch = f"{self.branch_prefix}/{sid}/unknown"
                    base_sha = None
                    identity = None
                if identity is None:
                    identity = self._read_identity(session_id=sid)
                self._registry[sid] = Worktree(
                    session_id=sid,
                    path=wt_path,
                    branch=branch,
                    port=None,
                    created_at=datetime.now(timezone.utc),
                    base_sha=str(base_sha) if base_sha else None,
                    identity=str(identity) if identity is not None else None,
                )

        # Ensure the worktree root exists (no-op if already present).
        self._wt_root.mkdir(parents=True, exist_ok=True)

        # Initialise ports file if absent; run recovery if present. Webhook
        # brokers pass ports_enabled=False and must not touch codex-ports.json.
        if self.ports_enabled:
            ports_path = self._ports_path()
            if not ports_path.exists():
                self._init_ports_file()
            else:
                self._recover_ports()

    # ── Public API ────────────────────────────────────────────────────────────

    def allocate(
        self,
        session_id: str,
        *,
        isa_slug: str,
        base_branch: str = "origin/main",
        branch_name: str | None = None,
        base_sha: str | None = None,
        identity: str | None = None,
    ) -> Worktree:
        """Create a git worktree and claim a port for the given session."""
        # Step 1: disk-pressure check
        free = self._disk_free_bytes()
        if free < _DISK_HARD_FLOOR:
            raise DiskPressureError(
                f"Cannot allocate worktree: {free / _GB:.1f} GB free on "
                f"{self.hermes_home} (need 4 GB). Free space and retry."
            )
        if free < _DISK_SOFT_FLOOR:
            log.warning(
                "disk pressure (%.1f GB free); allocating anyway. "
                "Consider running gc.",
                free / _GB,
            )

        # Step 2: idempotency check
        if session_id in self._registry:
            wt = self._registry[session_id]
            if identity is not None and wt.identity != identity:
                raise BranchCollisionError(
                    f"Session {session_id} already has a lease for a different identity. "
                    "Refusing to share a worktree."
                )
            return wt

        if (
            self.max_active_leases is not None
            and len(self._registry) >= self.max_active_leases
        ):
            raise LeaseCapacityError(
                f"Active worktree lease capacity exhausted "
                f"({self.max_active_leases}). Retry after another run completes."
            )

        # Step 3: git worktree add (slugify isa_slug — git refs disallow
        # spaces / capitals / punctuation that Discord thread titles freely use)
        isa_slug = slugify_ref(isa_slug)
        branch = branch_name or f"{self.branch_prefix}/{session_id}/{isa_slug}"
        wt_path = self._wt_root / session_id
        if wt_path.is_dir() and identity is None:
            raise BranchCollisionError(
                f"Worktree path {wt_path} already exists. Operator intervention required."
            )
        if wt_path.is_dir() and identity is not None:
            stored_identity = self._read_identity(session_id=session_id)
            if stored_identity != identity or not self._is_registered_worktree_path(wt_path):
                raise BranchCollisionError(
                    f"Worktree {wt_path} already exists but is not a registered broker-owned "
                    "worktree for the requested identity. Refusing to share a worktree."
                )
            wt = Worktree(
                session_id=session_id,
                path=wt_path,
                branch=branch,
                port=None,
                created_at=datetime.now(timezone.utc),
                lock_type=self._detect_lock_type(wt_path),
                base_sha=base_sha,
                identity=stored_identity,
            )
            self._registry[session_id] = wt
            return wt
        result = self._git(
            "worktree", "add",
            str(wt_path), "-b", branch, base_branch,
        )
        if result.returncode != 0:
            stderr = result.stderr or ""
            if "already exists" in stderr or "not a git repository" in stderr:
                raise BranchCollisionError(
                    f"Worktree path or branch already exists for {branch}. "
                    "Operator intervention required."
                )
            if "modified files" in stderr or "untracked files" in stderr:
                raise RepoStateError(
                    f"git worktree add failed: repo has uncommitted changes "
                    f"in {self.repo_root}. Clean or stash them and retry."
                )
            raise RuntimeError(
                f"git worktree add failed (rc={result.returncode}): {stderr}"
            )

        # Step 4: port allocation
        port = self._allocate_port(session_id) if self.ports_enabled else None
        if self.ports_enabled and port is None:
            log.warning(
                "All ports in range %s-%s occupied; session %s has no port.",
                self.port_range[0],
                self.port_range[1] - 1,
                session_id,
            )

        # Step 5: JS lock-type detection (install deferred to on-demand)
        lock_type = self._detect_lock_type(wt_path)
        if lock_type == "pnpm":
            workspace_yaml = wt_path / "pnpm-workspace.yaml"
            if workspace_yaml.exists():
                content = workspace_yaml.read_text(encoding="utf-8", errors="replace")
                if "enableGlobalVirtualStore" not in content:
                    log.info(
                        "Project uses pnpm. Adding enableGlobalVirtualStore: "
                        "true to pnpm-workspace.yaml would reduce per-worktree "
                        "disk from ~500 MB to near-zero. Operator can apply "
                        "this; broker will not modify the file automatically."
                    )
            else:
                log.info(
                    "Project uses pnpm. Adding enableGlobalVirtualStore: true "
                    "to pnpm-workspace.yaml would reduce per-worktree disk "
                    "from ~500 MB to near-zero. Operator can apply this; "
                    "broker will not modify the file automatically."
                )

        # Step 6 & 7: register and return
        wt = Worktree(
            session_id=session_id,
            path=wt_path,
            branch=branch,
            port=port,
            created_at=datetime.now(timezone.utc),
            lock_type=lock_type,
            base_sha=base_sha,
            identity=identity,
        )
        self._registry[session_id] = wt
        self._write_identity(session_id=session_id, identity=identity)
        return wt

    def release(self, session_id: str) -> None:
        """Tear down the worktree for the given session (idempotent).

        Works in two cases:
          - sid is in the in-memory registry (normal: same-process allocate)
          - sid was allocated by a prior gateway run (registry empty after
            restart) but the worktree dir is still on disk under the
            canonical path. Without the fallback, post-restart releases
            (most notably the closeout fired by ``on_pr_merged`` after the
            MergeWatcher catches a merge) would silently no-op and leave
            the worktree checked out forever. Discovered 2026-05-26.
        """
        wt = self._registry.get(session_id)
        wt_path = wt.path if wt else (self._wt_root / session_id)
        if not wt and not wt_path.exists():
            return

        # Step 1: kill tmux session
        tmux_result = subprocess.run(
            ["tmux", "kill-session", "-t", f"codex-sess-{session_id}"],
            capture_output=True, text=True, check=False,
        )
        if tmux_result.returncode != 0:
            log.debug(
                "tmux kill-session codex-sess-%s: %s (ignored)",
                session_id,
                tmux_result.stderr.strip(),
            )

        # Step 2: remove git worktree (git-level — clears worktree index
        # entry + removes the dir when there's no uncommitted work).
        rm_result = self._git(
            "worktree", "remove", "--force", str(wt_path)
        )
        if rm_result.returncode != 0:
            log.warning(
                "git worktree remove failed for %s: %s",
                wt_path,
                rm_result.stderr.strip(),
            )

        # Step 3: filesystem cleanup — ``git worktree remove`` leaves the
        # directory behind in edge cases (e.g. the worktree was never
        # registered with git, or rm failed). Fall back to a direct
        # direct recursive cleanup so the closeout actually frees the disk.
        if wt_path.exists():
            try:
                import shutil  # noqa: PLC0415
                shutil.rmtree(wt_path, ignore_errors=True)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("worktree dir cleanup failed for %s: %s", wt_path, exc)

        # Step 4: free port
        if self.ports_enabled:
            self._free_port(session_id)
        self._remove_identity(session_id=session_id)

        # Step 5: remove from registry (if it was there)
        self._registry.pop(session_id, None)

    def release_nonforce(self, session_id: str) -> None:
        """Release a worktree WITHOUT ``--force`` and WITHOUT any rmtree fallback.

        The automated release path for C7 / Gate 7.  :meth:`release` is the
        operator/closeout path and is deliberately destructive: it force-removes
        the git worktree and then ``shutil.rmtree``s whatever survives, which is
        correct when a human (or a merged PR) has already decided the work is
        finished.  An unattended hourly reaper must not have that power — under
        it, one wrong gate reading permanently destroys uncommitted work.

        So this variant is *refusal-first*:

        * ``git worktree remove`` with **no** ``--force`` — git itself declines
          if the tree is dirty, has submodules in use, or is locked;
        * **no** ``shutil.rmtree`` fallback of any kind;
        * **no** ``tmux kill-session`` — killing a live process is exactly what
          the reaper's process-owner gate exists to make unnecessary, and a
          non-force release that murders the tenant is not non-force.

        Any refusal raises :class:`WorktreeReleaseRefused` and leaves the
        worktree, the port lease, and the identity file completely untouched,
        so the caller can quarantine the session instead of losing it.

        Idempotent: releasing a session whose directory is already gone and
        which is not in the in-memory registry is a no-op, matching
        :meth:`release`.

        Raises
        ------
        WorktreeReleaseRefused
            git refused the removal, or the directory outlived a "successful"
            removal (which would need an rmtree to finish — precisely what this
            method will not do).
        """
        # C7 LOW-1: an empty session_id makes ``self._wt_root / session_id``
        # resolve to the worktree ROOT — i.e. a "release one session" call that
        # aims at every session at once.  Refuse before any path is built.
        if not session_id:
            raise WorktreeReleaseRefused(
                "release_nonforce() requires a session_id; an empty one resolves "
                f"to the worktree root {self._wt_root}"
            )

        wt = self._registry.get(session_id)
        wt_path = wt.path if wt else (self._wt_root / session_id)
        if not wt and not wt_path.exists():
            return

        if wt_path.exists():
            rm_result = self._git("worktree", "remove", str(wt_path))
            if rm_result.returncode != 0:
                stderr = (rm_result.stderr or "").strip()
                log.warning(
                    "release_nonforce: git declined to remove %s: %s", wt_path, stderr,
                )
                raise WorktreeReleaseRefused(
                    f"git worktree remove (non-force) refused {wt_path}: "
                    f"exit {rm_result.returncode}: {stderr}"
                )

        if wt_path.exists():
            # git reported success but the directory survived.  Finishing the
            # job would require the rmtree this method exists to avoid.
            log.warning(
                "release_nonforce: %s survived a successful git worktree remove "
                "— refusing to rmtree", wt_path,
            )
            raise WorktreeReleaseRefused(
                f"worktree dir {wt_path} still present after non-force removal; "
                "refusing rmtree fallback"
            )

        # Only now that the disk is genuinely reclaimed do we drop the leases.
        if self.ports_enabled:
            self._free_port(session_id)
        self._remove_identity(session_id=session_id)
        self._registry.pop(session_id, None)

    def complete_lease(self, session_id: str, *, base_sha: str | None = None) -> str:
        """Complete an active lease, removing only evidence-free worktrees.

        Returns ``"removed"`` when the tree had no commits past ``base_sha`` and
        no dirty/untracked state, otherwise ``"awaiting-harvest"``. Retained
        worktrees are intentionally left on disk for operator harvest, but the
        active lease registry entry is cleared so completed runs do not consume
        live capacity.
        """
        wt = self._registry.get(session_id)
        wt_path = wt.path if wt else (self._wt_root / session_id)
        effective_base = base_sha or (wt.base_sha if wt else None)
        outcome = "awaiting-harvest"

        if not wt_path.exists():
            outcome = "removed"
        elif self._worktree_is_clean_for_removal(wt_path, effective_base):
            rm_result = self._git("worktree", "remove", str(wt_path))
            if rm_result.returncode == 0 or not wt_path.exists():
                outcome = "removed"
            else:
                log.warning(
                    "git worktree remove failed for completed lease %s: %s",
                    wt_path,
                    rm_result.stderr.strip(),
                )
                outcome = "awaiting-harvest"

        if self.ports_enabled:
            self._free_port(session_id)
        self._remove_identity(session_id=session_id)
        self._registry.pop(session_id, None)
        return outcome

    def gc(
        self,
        *,
        tracked_sids: set[str],
        live_branches: set[str] | None = None,
    ) -> list[GcAction]:
        """Sweep orphan worktrees out of ``~/.hermes/codex-wt/``.

        An orphan is a worktree directory under ``codex-wt/`` whose sid
        is NOT in ``tracked_sids`` (the dispatcher's ``codex_sessions.json``
        rows) AND whose branch is NOT in ``live_branches`` (open PRs on
        fork — caller passes ``None`` to skip that check).  Each orphan
        is RENAMED to ``codex-wt/.deleted-<ts>/<sid>/`` so the
        operator can recover; the reaper purges entries older than 7 days.

        Per WORKFLOW-LESSONS §3 rule 5: no direct recursive cleanup; renames are the
        safe deletion pattern.
        """
        actions: list[GcAction] = []
        wt_root = getattr(self, "_wt_root", self.hermes_home / "codex-wt")
        if not wt_root.is_dir():
            return actions
        live_branches = live_branches or set()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        deleted_root = wt_root / f".deleted-{ts}"
        for entry in sorted(wt_root.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            # Skip our own .deleted-* dirs and any other dotfiles.
            if name.startswith("."):
                continue
            if name in tracked_sids:
                continue
            # If the sid's branch is in the live-branches set (open PR),
            # leave the worktree alone — operator hasn't merged yet.
            branch_match = any(b.endswith(f"/{name}/") or f"/{name}/" in b for b in live_branches)
            if branch_match:
                continue
            # Rename into the deleted-<ts> bucket.
            deleted_root.mkdir(parents=True, exist_ok=True)
            new_path = deleted_root / name
            try:
                entry.rename(new_path)
                # Also tell git the worktree is gone (cheap; prune later
                # the next time `git worktree list` is called).
                self._git("worktree", "prune")
            except OSError as exc:
                log.warning("gc: rename %s -> %s failed: %s", entry, new_path, exc)
                continue
            actions.append(GcAction(
                sid=name,
                old_path=entry,
                new_path=new_path,
                reason="orphan: not in tracked_sids and no open PR",
            ))
        return actions

    def reap_deleted(self, *, max_age_days: int = 7) -> int:
        """Purge ``codex-wt/.deleted-<ts>/`` dirs older than ``max_age_days``.

        Returns the number of dirs purged.  Uses a single ``shutil.rmtree``
        per stamp — this is the ONLY deletion path in the broker, and
        it operates exclusively on the ``.deleted-`` namespace that gc()
        owns.  No risk of stomping a live worktree.
        """
        import shutil as _shutil  # noqa: PLC0415 — local import to keep top-of-module tidy
        wt_root = getattr(self, "_wt_root", self.hermes_home / "codex-wt")
        if not wt_root.is_dir():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        purged = 0
        for entry in wt_root.iterdir():
            if not entry.is_dir() or not entry.name.startswith(".deleted-"):
                continue
            # Parse the trailing timestamp; skip on parse failure.
            stamp = entry.name.removeprefix(".deleted-")
            try:
                stamp_dt = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if stamp_dt < cutoff:
                _shutil.rmtree(entry, ignore_errors=True)
                purged += 1
        return purged

    def free_port(self, port: int) -> None:
        """Release a specific port back to the pool (by port number)."""
        if not self.ports_enabled:
            return
        ports_path = self._ports_path()
        with open(ports_path, "r+", encoding="utf-8") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                data = self._load_ports_fd(fd)
                key = str(port)
                if key in data and data[key] is not None:
                    data[key] = None
                    fd.seek(0)
                    json.dump(data, fd)
                    fd.truncate()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def status(self, session_id: str) -> WorktreeStatus | None:
        """Return current status for session_id, or None if not registered."""
        if session_id not in self._registry:
            return None

        wt = self._registry[session_id]
        path_exists = wt.path.exists()

        tmux_result = subprocess.run(
            ["tmux", "has-session", "-t", f"codex-sess-{session_id}"],
            capture_output=True, text=True, check=False,
        )
        tmux_alive = tmux_result.returncode == 0

        return WorktreeStatus(
            session_id=wt.session_id,
            path=wt.path,
            branch=wt.branch,
            port=wt.port,
            created_at=wt.created_at,
            path_exists=path_exists,
            tmux_alive=tmux_alive,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        """Mirror of git_janitor.py:50-55."""
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True, text=True, check=False,
        )

    def _identity_path(self, *, session_id: str) -> Path:
        safe_sid = _IDENTITY_PATH_INVALID_RE.sub("_", session_id).strip("._") or "session"
        return self.hermes_home / "state" / "worktree-broker-identities" / f"{safe_sid}.json"

    def _is_registered_worktree_path(self, wt_path: Path) -> bool:
        result = self._git("worktree", "list", "--porcelain")
        if result.returncode != 0:
            return False
        try:
            target = wt_path.resolve()
        except OSError:
            target = wt_path.absolute()
        for line in result.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            candidate = Path(line.removeprefix("worktree "))
            try:
                candidate = candidate.resolve()
            except OSError:
                candidate = candidate.absolute()
            if candidate == target:
                return True
        return False

    def _read_identity(self, *, session_id: str) -> str | None:
        path = self._identity_path(session_id=session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        identity = data.get("identity") if isinstance(data, dict) else None
        return str(identity) if identity is not None else None

    def _write_identity(self, *, session_id: str, identity: str | None) -> None:
        if identity is None:
            return
        path = self._identity_path(session_id=session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"identity": identity}, sort_keys=True), encoding="utf-8")

    def _remove_identity(self, *, session_id: str) -> None:
        try:
            self._identity_path(session_id=session_id).unlink(missing_ok=True)
        except OSError:
            log.debug("failed to remove worktree identity for %s", session_id, exc_info=True)

    def _disk_free_bytes(self) -> int:
        """df -P hermes_home; parse 'Available' column; return bytes."""
        result = subprocess.run(
            ["df", "-P", str(self.hermes_home)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            log.warning("df -P failed: %s; assuming unlimited free space", result.stderr)
            return 2 ** 63
        lines = result.stdout.strip().splitlines()
        # df -P output: header line then data lines
        # Columns: Filesystem  1024-blocks  Used  Available  Capacity  Mounted on
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 4:
                try:
                    # Available is in 1K blocks
                    return int(parts[3]) * 1024
                except ValueError:
                    continue
        log.warning("Could not parse df output; assuming unlimited free space")
        return 2 ** 63


    def _worktree_is_clean_for_removal(self, wt_path: Path, base_sha: str | None) -> bool:
        """True when the worktree has no uncommitted files and no new commits."""
        status = subprocess.run(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            return False
        if not base_sha:
            return False
        commits = subprocess.run(
            ["git", "-C", str(wt_path), "rev-list", "--count", f"{base_sha}..HEAD"],
            capture_output=True, text=True, check=False,
        )
        if commits.returncode != 0:
            return False
        try:
            return int((commits.stdout or "0").strip() or "0") == 0
        except ValueError:
            return False

    def _allocate_port(self, session_id: str) -> int | None:
        """Atomic port claim per spec §4."""
        if not self.ports_enabled:
            return None
        ports_path = self._ports_path()
        with open(ports_path, "r+", encoding="utf-8") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                data = self._load_ports_fd(fd)
                for key in sorted(data.keys(), key=int):
                    if data[key] is None:
                        data[key] = session_id
                        fd.seek(0)
                        json.dump(data, fd)
                        fd.truncate()
                        return int(key)
                # All ports occupied
                return None
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def _free_port(self, session_id: str) -> None:
        """Atomic port release per spec §4 — scans by value (session_id)."""
        if not self.ports_enabled:
            return
        ports_path = self._ports_path()
        if not ports_path.exists():
            return
        with open(ports_path, "r+", encoding="utf-8") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                data = self._load_ports_fd(fd)
                changed = False
                for key, val in data.items():
                    if val == session_id:
                        data[key] = None
                        changed = True
                if changed:
                    fd.seek(0)
                    json.dump(data, fd)
                    fd.truncate()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _ports_path(self) -> Path:
        return self.hermes_home / "codex-ports.json"

    def _sessions_path(self) -> Path:
        return self.hermes_home / "codex_sessions.json"

    def _init_ports_file(self) -> None:
        """Create codex-ports.json with all ports null."""
        data = {str(p): None for p in range(self.port_range[0], self.port_range[1])}
        ports_path = self._ports_path()
        ports_path.parent.mkdir(parents=True, exist_ok=True)
        with open(ports_path, "w", encoding="utf-8") as fd:
            json.dump(data, fd)

    def _load_ports_fd(self, fd) -> dict:
        """Read and parse ports JSON from an open file descriptor.

        On corrupt JSON, re-initialises in place and returns an all-null dict.
        """
        try:
            fd.seek(0)
            text = fd.read()
            if not text.strip():
                raise json.JSONDecodeError("empty", "", 0)
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning(
                "codex-ports.json is missing or corrupt; re-initialising."
            )
            data = {
                str(p): None
                for p in range(self.port_range[0], self.port_range[1])
            }
            fd.seek(0)
            json.dump(data, fd)
            fd.truncate()
            return data

    def _recover_ports(self) -> None:
        """Cross-reference codex-ports.json against codex_sessions.json.

        Any port whose sid has no row in codex_sessions.json is stale;
        null it and write back (spec §4 "Recovery on broker init").
        """
        sessions_path = self._sessions_path()
        live_sids: set[str] = set()
        if sessions_path.exists():
            try:
                raw = json.loads(sessions_path.read_text(encoding="utf-8"))
                # codex_sessions.json may be a dict or list; extract keys/ids
                if isinstance(raw, dict):
                    live_sids = set(raw.keys())
                elif isinstance(raw, list):
                    for entry in raw:
                        if isinstance(entry, dict) and "session_id" in entry:
                            live_sids.add(entry["session_id"])
                        elif isinstance(entry, str):
                            live_sids.add(entry)
            except (json.JSONDecodeError, OSError):
                log.warning(
                    "codex_sessions.json unreadable during port recovery; "
                    "treating all ports as stale."
                )
        else:
            log.warning(
                "codex_sessions.json absent during port recovery; "
                "nulling all non-null ports."
            )

        ports_path = self._ports_path()
        with open(ports_path, "r+", encoding="utf-8") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                data = self._load_ports_fd(fd)
                changed = False
                for key, val in data.items():
                    if val is not None and val not in live_sids:
                        data[key] = None
                        changed = True
                if changed:
                    fd.seek(0)
                    json.dump(data, fd)
                    fd.truncate()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def _detect_lock_type(self, wt_path: Path) -> str | None:
        """Scan worktree root for JS lock files; return lock type or None."""
        for lock_type, filename in _LOCK_TYPE_FILES:
            if (wt_path / filename).exists():
                return lock_type
        if (wt_path / "package.json").exists():
            return None  # JS project, no lock file
        return None
