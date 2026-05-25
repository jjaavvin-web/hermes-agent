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
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

log = logging.getLogger(__name__)

# ── Public error classes ──────────────────────────────────────────────────────


class DiskPressureError(OSError):
    """Raised when ~/.hermes free space is below 4 GB before allocate."""


class BranchCollisionError(ValueError):
    """Raised when the branch codex/<sid>/<isa-slug> already exists.

    Structurally impossible with UUID4 sids, but handled explicitly.
    """


class RepoStateError(RuntimeError):
    """Raised when git worktree add fails due to dirty parent repo state.

    Broker does NOT stash — that would silently discard operator work.
    """


# ── Data classes ──────────────────────────────────────────────────────────────

_LOCK_TYPE_FILES = [
    ("pnpm", "pnpm-lock.yaml"),
    ("npm", "package-lock.json"),
    ("yarn", "yarn.lock"),
]


@dataclass
class Worktree:
    session_id: str        # UUID4 assigned by dispatcher
    path: Path             # ~/.hermes/codex-wt/<sid>/
    branch: str            # codex/<sid>/<isa-slug>
    port: int | None       # None if no port was available
    created_at: datetime
    lock_type: str | None = field(default=None)  # pnpm | npm | yarn | None


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
        existing_sessions: dict[str, str] | None = None,
    ) -> None:
        """Initialise the broker.

        repo_root         — absolute path to the hermes-agent git repo.
        hermes_home       — typically Path("~/.hermes").expanduser()
        port_range        — half-open [lo, hi); default covers 50000-50007.
        existing_sessions — optional {session_id: worktree_path} mapping read
                            from codex_sessions.json by the caller on restart.
        """
        self.repo_root = Path(repo_root)
        self.hermes_home = Path(hermes_home)
        self.port_range = port_range

        # In-memory registry keyed by session_id → Worktree
        self._registry: dict[str, Worktree] = {}

        # Pre-populate from existing_sessions (M7 amendment — bot-restart path)
        if existing_sessions:
            for sid, wt_path_str in existing_sessions.items():
                wt_path = Path(wt_path_str)
                branch = f"codex/{sid}/unknown"
                self._registry[sid] = Worktree(
                    session_id=sid,
                    path=wt_path,
                    branch=branch,
                    port=None,
                    created_at=datetime.now(timezone.utc),
                )

        # Ensure codex-wt/ directory exists (no-op if already present)
        wt_dir = self.hermes_home / "codex-wt"
        wt_dir.mkdir(parents=True, exist_ok=True)

        # Initialise ports file if absent; run recovery if present
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
            return self._registry[session_id]

        # Step 3: git worktree add
        branch = f"codex/{session_id}/{isa_slug}"
        wt_path = self.hermes_home / "codex-wt" / session_id
        result = self._git(
            "worktree", "add",
            str(wt_path), "-b", branch, base_branch,
        )
        if result.returncode != 0:
            stderr = result.stderr or ""
            if "already exists" in stderr:
                raise BranchCollisionError(
                    f"Branch {branch} already exists. "
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
        port = self._allocate_port(session_id)
        if port is None:
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
                content = workspace_yaml.read_text(errors="replace")
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
        )
        self._registry[session_id] = wt
        return wt

    def release(self, session_id: str) -> None:
        """Tear down the worktree for the given session (idempotent)."""
        if session_id not in self._registry:
            return

        wt = self._registry[session_id]
        wt_path = wt.path

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

        # Step 2: remove git worktree
        rm_result = self._git(
            "worktree", "remove", "--force", str(wt_path)
        )
        if rm_result.returncode != 0:
            log.warning(
                "git worktree remove failed for %s: %s",
                wt_path,
                rm_result.stderr.strip(),
            )

        # Step 3: free port
        self._free_port(session_id)

        # Step 4: remove from registry
        del self._registry[session_id]

    def gc(self) -> list[GcAction]:
        """P5 ONLY — not implemented in P1."""
        raise NotImplementedError("gc() is a P5 feature; not available in P1.")

    def free_port(self, port: int) -> None:
        """Release a specific port back to the pool (by port number)."""
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

    def _allocate_port(self, session_id: str) -> int | None:
        """Atomic port claim per spec §4."""
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
                raw = json.loads(sessions_path.read_text())
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
