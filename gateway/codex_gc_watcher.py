"""Periodic, fail-closed worktree GC and deleted-bucket reaping.

The watcher performs two independent safety preflights before any broker or
session-reaper phase: a lock-disciplined validation of ``codex_sessions.json``
and a repo-bound, tri-state lookup of open pull-request branches.  Unknown
registry or PR state skips the complete tick; only verified empty state is
represented by an empty set.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from gateway.codex_session_dispatcher import load_locked_json

log = logging.getLogger(__name__)

_COMMAND_TIMEOUT_SEC = 30
_GITHUB_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class CheckedRegistryError(RuntimeError):
    """The watcher could not establish a clean v1 registry truth."""

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        super().__init__(fingerprint)


class OpenPrLookupError(RuntimeError):
    """Typed unknown state from the repo-bound open-PR lookup."""

    def __init__(self, code: str, detail: str | int | None = None) -> None:
        self.code = code
        self.fingerprint = code if detail is None else f"{code}:{detail}"
        super().__init__(self.fingerprint)


def _checked_tracked_sids(dispatcher: Any) -> set[str]:
    """Read and validate the dispatcher registry without recovery mutation."""
    configured_path = getattr(dispatcher, "_sessions_path", None)
    if not isinstance(configured_path, (str, Path)):
        raise CheckedRegistryError("registry-path-unavailable")
    sessions_path = Path(configured_path)
    try:
        if not sessions_path.exists():
            raise CheckedRegistryError("registry-absent")
        raw = load_locked_json(sessions_path)
    except CheckedRegistryError:
        raise
    except FileNotFoundError as exc:
        raise CheckedRegistryError("registry-absent") from exc
    except json.JSONDecodeError as exc:
        raise CheckedRegistryError("registry-json") from exc
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise CheckedRegistryError("registry-io") from exc
    except Exception as exc:  # fail closed for an unexpected loader failure
        raise CheckedRegistryError("registry-unknown") from exc

    if not isinstance(raw, dict):
        raise CheckedRegistryError("registry-top-level")
    if type(raw.get("version")) is not int or raw.get("version") != 1:
        raise CheckedRegistryError("registry-version")
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        raise CheckedRegistryError("registry-sessions")

    tracked_sids: set[str] = set()
    for thread_id, row in sessions.items():
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise CheckedRegistryError("registry-thread-id")
        if not isinstance(row, dict):
            raise CheckedRegistryError("registry-row")
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            raise CheckedRegistryError("registry-session-id")
        tracked_sids.add(sid)
    return tracked_sids


def _run_git(root: Path, args: list[str], *, phase: str) -> subprocess.CompletedProcess:
    argv = ["git", "-C", str(root), *args]
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SEC,
        )
    except FileNotFoundError as exc:
        raise OpenPrLookupError("git-missing", phase) from exc
    except subprocess.TimeoutExpired as exc:
        raise OpenPrLookupError("git-timeout", phase) from exc
    except subprocess.SubprocessError as exc:
        raise OpenPrLookupError("git-subprocess", phase) from exc
    except OSError as exc:
        raise OpenPrLookupError("git-oserror", phase) from exc


def _github_slug(remote_url: str) -> str:
    """Parse the accepted GitHub fork SSH/HTTPS URL forms to owner/repo."""
    owner: str | None = None
    repo: str | None = None
    if not isinstance(remote_url, str):
        raise OpenPrLookupError("fork-remote-output")
    scp_match = re.fullmatch(r"git@github\.com:([^/]+)/([^/]+)", remote_url)
    if scp_match:
        owner, repo = scp_match.groups()
    else:
        try:
            parsed = urlsplit(remote_url)
            parsed_port = parsed.port
        except ValueError as exc:
            raise OpenPrLookupError("fork-remote-unsupported") from exc
        valid_https = (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.username is None
            and parsed.password is None
            and parsed_port is None
        )
        valid_ssh = (
            parsed.scheme == "ssh"
            and parsed.hostname == "github.com"
            and parsed.username == "git"
            and parsed.password is None
            and parsed_port is None
        )
        parts = [part for part in parsed.path.split("/") if part]
        if (valid_https or valid_ssh) and not parsed.query and not parsed.fragment and len(parts) == 2:
            owner, repo = parts

    if repo and repo.endswith(".git"):
        repo = repo[:-4]
    if (
        not owner
        or not repo
        or owner in {".", ".."}
        or repo in {".", ".."}
        or _GITHUB_COMPONENT_RE.fullmatch(owner) is None
        or _GITHUB_COMPONENT_RE.fullmatch(repo) is None
    ):
        raise OpenPrLookupError("fork-remote-unsupported")
    return f"{owner}/{repo}"


def _gh_list_open_branches(repo_root: Path) -> set[str]:
    """Return verified open PR branch names for the broker's fork remote.

    Every unknown state raises :class:`OpenPrLookupError`; only successful JSON
    containing zero rows returns ``set()``.
    """
    try:
        root = Path(repo_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise OpenPrLookupError("repo-root-invalid") from exc
    if not root.is_dir():
        raise OpenPrLookupError("repo-root-not-directory")

    verified = _run_git(root, ["rev-parse", "--show-toplevel"], phase="repo-verify")
    if verified.returncode != 0:
        raise OpenPrLookupError("repo-verify-nonzero", verified.returncode)
    try:
        reported_root = Path(verified.stdout.strip()).resolve(strict=True)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise OpenPrLookupError("repo-verify-output") from exc
    if reported_root != root:
        raise OpenPrLookupError("repo-verify-mismatch")

    remote = _run_git(root, ["remote", "get-url", "fork"], phase="fork-remote")
    if remote.returncode != 0:
        raise OpenPrLookupError("fork-remote-nonzero", remote.returncode)
    try:
        remote_url = remote.stdout.strip()
    except (AttributeError, TypeError) as exc:
        raise OpenPrLookupError("fork-remote-output") from exc
    slug = _github_slug(remote_url)

    argv = [
        "gh",
        "pr",
        "list",
        "--repo",
        slug,
        "--state",
        "open",
        "--json",
        "headRefName",
        "--limit",
        "200",
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SEC,
            cwd=root,
        )
    except FileNotFoundError as exc:
        raise OpenPrLookupError("gh-missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise OpenPrLookupError("gh-timeout") from exc
    except subprocess.SubprocessError as exc:
        raise OpenPrLookupError("gh-subprocess") from exc
    except OSError as exc:
        raise OpenPrLookupError("gh-oserror") from exc
    if result.returncode != 0:
        raise OpenPrLookupError("gh-nonzero", result.returncode)

    try:
        rows = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OpenPrLookupError("gh-json") from exc
    if not isinstance(rows, list):
        raise OpenPrLookupError("gh-top-level")

    branches: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise OpenPrLookupError("gh-row")
        branch = row.get("headRefName")
        if not isinstance(branch, str) or not branch.strip():
            raise OpenPrLookupError("gh-head-ref")
        branches.add(branch)
    return branches


class CodexGcWatcher:
    """Background task that periodically sweeps broker-owned worktree state."""

    def __init__(
        self,
        *,
        dispatcher: Any,
        worktree_broker: Any,
        poll_interval_sec: float = 3600.0,
        reap_max_age_days: int = 7,
        gh_list_open_branches: Callable[[], set[str]] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._broker = worktree_broker
        self._poll_interval = poll_interval_sec
        self._reap_max_age_days = reap_max_age_days
        if gh_list_open_branches is None:
            broker_repo_root = getattr(worktree_broker, "repo_root", None)
            self._gh_list_open_branches = lambda: _gh_list_open_branches(broker_repo_root)
        else:
            self._gh_list_open_branches = gh_list_open_branches
        self._last_pr_failure_fingerprint: str | None = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="codex-gc-watcher")
        log.info(
            "CodexGcWatcher started (interval=%.1fs, reap_max_age=%dd)",
            self._poll_interval,
            self._reap_max_age_days,
        )

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:  # pragma: no cover - defensive
                pass
        self._task = None
        self._stop_event = None
        log.info("CodexGcWatcher stopped")

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("CodexGcWatcher tick crashed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval,
                )
            except asyncio.TimeoutError:
                continue

    def _verified_open_branches(self) -> set[str] | None:
        try:
            branches = self._gh_list_open_branches()
            if not isinstance(branches, set) or any(
                not isinstance(branch, str) or not branch.strip()
                for branch in branches
            ):
                raise OpenPrLookupError("callback-result")
        except Exception as exc:
            if isinstance(exc, OpenPrLookupError):
                fingerprint = exc.fingerprint
            else:
                digest = hashlib.sha256(
                    f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace")
                ).hexdigest()[:12]
                fingerprint = f"callback-{type(exc).__name__}-{digest}"
            if fingerprint == self._last_pr_failure_fingerprint:
                log.debug(
                    "CodexGcWatcher: repeated open-PR lookup failure (%s); tick skipped",
                    fingerprint,
                )
            else:
                log.warning(
                    "CodexGcWatcher: open-PR lookup failed (%s); skipping gc and reapers",
                    fingerprint,
                )
            self._last_pr_failure_fingerprint = fingerprint
            return None

        if self._last_pr_failure_fingerprint is not None:
            log.info("CodexGcWatcher: open-PR lookup recovered")
            self._last_pr_failure_fingerprint = None
        return set(branches)

    async def _tick(self) -> None:
        """Run one tick only after registry and open-PR truth are verified."""
        try:
            tracked_sids = _checked_tracked_sids(self._dispatcher)
        except CheckedRegistryError as exc:
            log.warning(
                "CodexGcWatcher: checked registry failed (%s); "
                "skipping gc, deleted-bucket reap, and session reaper",
                exc.fingerprint,
            )
            return

        live_branches = self._verified_open_branches()
        if live_branches is None:
            return

        try:
            actions = self._broker.gc(
                tracked_sids=tracked_sids,
                live_branches=live_branches,
            )
            if actions:
                log.info(
                    "CodexGcWatcher: gc renamed %d orphan(s) to .deleted-<ts>",
                    len(actions),
                )
        except Exception as exc:
            log.warning("CodexGcWatcher: broker.gc failed: %s", exc)

        try:
            purged = self._broker.reap_deleted(max_age_days=self._reap_max_age_days)
            if purged:
                log.info(
                    "CodexGcWatcher: reap_deleted purged %d expired bucket(s)",
                    purged,
                )
        except Exception as exc:
            log.warning("CodexGcWatcher: broker.reap_deleted failed: %s", exc)

        try:
            from gateway.codex_session_reaper import CodexSessionReaper

            verified_snapshot = frozenset(live_branches)
            decisions = CodexSessionReaper(
                dispatcher_state=self._dispatcher,
                broker=self._broker,
                gh_open_branches_fn=lambda: set(verified_snapshot),
            ).reap(reap_idle_days=10, dry_run=True)
            if decisions:
                log.info(
                    "CodexGcWatcher: session reaper dry-run evaluated %d decision(s)",
                    len(decisions),
                )
        except Exception as exc:
            log.warning("CodexGcWatcher: session reaper dry-run failed: %s", exc)
