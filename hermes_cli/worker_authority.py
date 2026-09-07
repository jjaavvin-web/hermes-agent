"""Fail-closed authority policy for dispatcher-spawned specialist workers.

The policy is intentionally small and environment-activated. Normal interactive
Hermes sessions are unaffected. Kanban spawns set dispatcher identity pins;
tool dispatch validates both the tool class and effective post-middleware args.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SPECIALIST_AUTHORITIES = frozenset({"sol-verifier", "sol-builder", "sol-router", "sol-spec"})
_READ_TOOLS = frozenset({"read_file", "search_files"})
_MUTATING_FILE_TOOLS = frozenset({"write_file"})
_READ_KANBAN_TOOLS = frozenset({"kanban_show"})
_LIFECYCLE_KANBAN_TOOLS = frozenset({
    "kanban_heartbeat", "kanban_comment", "kanban_complete", "kanban_block",
})
_SPECIALIST_TEST_TOOL = "specialist_test"
_VERIFIER_ALLOWED = _READ_TOOLS | _READ_KANBAN_TOOLS | _LIFECYCLE_KANBAN_TOOLS | {_SPECIALIST_TEST_TOOL}
_BUILDER_ALLOWED = _READ_TOOLS | _MUTATING_FILE_TOOLS | _READ_KANBAN_TOOLS | _LIFECYCLE_KANBAN_TOOLS | {_SPECIALIST_TEST_TOOL}
_ROUTER_SPEC_ALLOWED = frozenset({"kanban_show", "kanban_comment"})
_PATH_KEYS = frozenset({"path", "target", "workdir"})
_PATH_LIST_KEYS = frozenset({"paths", "targets"})
_SHELL_META = re.compile(r"[;&|`$<>\n\r]|\$\(|\|\|")
_EXPECTED_ARGS = {
    "read_file": frozenset({"path", "offset", "limit"}),
    "search_files": frozenset({"pattern", "target", "path", "file_glob", "limit", "offset", "output_mode", "context"}),
    "write_file": frozenset({"path", "content", "cross_profile"}),
    "patch": frozenset({"mode", "path", "old_string", "new_string", "replace_all", "cross_profile"}),
    "specialist_test": frozenset({"targets", "timeout"}),
    "kanban_show": frozenset({"task_id", "id", "board"}),
    "kanban_heartbeat": frozenset({"task_id", "note", "board"}),
    "kanban_comment": frozenset({"task_id", "body", "board"}),
    "kanban_complete": frozenset({"task_id", "summary", "metadata", "result", "artifacts", "board"}),
    "kanban_block": frozenset({"task_id", "reason", "kind", "board"}),
}
_GENERIC_HEARTBEATS = frozenset({"alive", "working", "heartbeat", "still working", "progress"})


def _affirmative_verdict(arguments: Mapping[str, Any], marker: str) -> bool:
    """Require an explicit positive verdict, not a substring coincidence."""
    metadata = arguments.get("metadata")
    metadata_verdict = ""
    if isinstance(metadata, Mapping):
        metadata_verdict = str(metadata.get("verdict") or "").strip().upper()
    text = "\n".join(
        str(arguments.get(key) or "").strip()
        for key in ("summary", "result")
        if str(arguments.get(key) or "").strip()
    )
    upper = text.upper()
    if marker == "APPROVED" and re.search(
        r"\b(?:NOT\s+APPROVED|DISAPPROVED|UNAPPROVED)\b", upper
    ):
        return False
    if metadata_verdict == marker:
        return True
    return any(
        re.match(
            rf"^{re.escape(marker)}(?:\s|[—:;-]|$)",
            line.strip(),
            flags=re.IGNORECASE,
        )
        for line in text.splitlines()
    )


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    reason: str = ""


def _workspace_root(workspace: str | None) -> Path | None:
    raw = str(workspace or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _candidate_path(root: Path, value: Any) -> Path | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = Path(os.fspath(value)).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _paths_confined(root: Path, arguments: Mapping[str, Any]) -> bool:
    for key in _PATH_KEYS:
        if key in arguments and _candidate_path(root, arguments[key]) is None:
            return False
    for key in _PATH_LIST_KEYS:
        if key not in arguments:
            continue
        values = arguments[key]
        if not isinstance(values, list) or any(_candidate_path(root, item) is None for item in values):
            return False
    return True


def _specialist_test_args_safe(root: Path, arguments: Mapping[str, Any]) -> bool:
    targets = arguments.get("targets")
    if not isinstance(targets, list) or not targets:
        return False
    if any(not isinstance(item, str) or item.startswith("-") for item in targets):
        return False
    if any(_SHELL_META.search(item) for item in targets):
        return False
    return all(_candidate_path(root, item) is not None for item in targets)


def _lifecycle_args_safe(
    role: str,
    root: Path,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    pinned_task: str | None,
    pinned_run: str | None,
    pinned_board: str | None,
    pinned_profile: str | None,
) -> AuthorityDecision:
    """Authorize only the dispatcher-pinned card/run identity."""
    task = str(pinned_task or "").strip()
    run = str(pinned_run or "").strip()
    board = str(pinned_board or "").strip()
    profile = str(pinned_profile or "").strip().lower()
    if not task or not run or not board or not profile:
        return AuthorityDecision(False, "specialist authority denied: missing dispatcher lifecycle pin")
    try:
        if int(run) <= 0:
            raise ValueError
    except ValueError:
        return AuthorityDecision(False, "specialist authority denied: invalid dispatcher run pin")
    if profile != role:
        return AuthorityDecision(False, "specialist authority denied: profile/role pin mismatch")

    requested_task = arguments.get("task_id") or arguments.get("id") or task
    if str(requested_task).strip() != task:
        return AuthorityDecision(False, "specialist authority denied sibling or unassigned task")
    requested_board = arguments.get("board") or board
    if str(requested_board).strip() != board:
        return AuthorityDecision(False, "specialist authority denied foreign board")

    if tool_name == "kanban_heartbeat":
        note = str(arguments.get("note") or "").strip()
        if len(note) < 12 or note.lower() in _GENERIC_HEARTBEATS:
            return AuthorityDecision(False, "specialist authority denied non-meaningful heartbeat")
    elif tool_name == "kanban_comment":
        body = str(arguments.get("body") or "").strip()
        if len(body) < 8:
            return AuthorityDecision(False, "specialist authority denied empty evidence comment")
    elif tool_name == "kanban_complete":
        if role == "sol-builder":
            if (
                not _affirmative_verdict(arguments, "DONE_REVIEW_REQUIRED")
                or _affirmative_verdict(arguments, "APPROVED")
            ):
                return AuthorityDecision(False, "builder completion requires DONE_REVIEW_REQUIRED and cannot self-approve")
        elif role == "sol-verifier" and not _affirmative_verdict(arguments, "APPROVED"):
            return AuthorityDecision(False, "verifier completion requires an APPROVED evidence verdict")
        artifacts = arguments.get("artifacts")
        if artifacts is not None:
            if not isinstance(artifacts, list) or any(_candidate_path(root, item) is None for item in artifacts):
                return AuthorityDecision(False, "specialist authority denied artifact outside assigned workspace")
    elif tool_name == "kanban_block":
        reason = str(arguments.get("reason") or "").strip()
        if reason.count("?") != 1:
            return AuthorityDecision(False, "specialist block must ask exactly one answerable question")
    return AuthorityDecision(True)


def authorize_worker_tool(
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    authority: str | None = None,
    workspace: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    board: str | None = None,
    profile: str | None = None,
) -> AuthorityDecision:
    role = str(authority or "").strip().lower()
    if not role:
        return AuthorityDecision(True)
    if role not in SPECIALIST_AUTHORITIES:
        return AuthorityDecision(False, f"specialist authority denied unknown role {role!r}")
    root = _workspace_root(workspace)
    if root is None:
        return AuthorityDecision(False, "specialist authority denied: missing or invalid absolute workspace")
    args = arguments if isinstance(arguments, Mapping) else {}
    allowed = (
        _VERIFIER_ALLOWED if role == "sol-verifier"
        else _BUILDER_ALLOWED if role == "sol-builder"
        else _ROUTER_SPEC_ALLOWED
    )
    if tool_name not in allowed:
        return AuthorityDecision(False, f"specialist authority denied tool {tool_name!r} for {role}")
    expected = _EXPECTED_ARGS.get(tool_name)
    if expected is not None and any(key not in expected for key in args):
        return AuthorityDecision(False, "specialist authority denied unexpected tool argument")
    if tool_name == "patch":
        if args.get("mode", "replace") != "replace" or "path" not in args:
            return AuthorityDecision(False, "specialist authority denied bulk patch mode")
    if not _paths_confined(root, args):
        return AuthorityDecision(False, "specialist authority denied path outside assigned workspace")
    if tool_name == _SPECIALIST_TEST_TOOL and not _specialist_test_args_safe(root, args):
        return AuthorityDecision(False, "specialist authority denied unsafe test target")
    if tool_name in _READ_KANBAN_TOOLS | _LIFECYCLE_KANBAN_TOOLS:
        lifecycle = _lifecycle_args_safe(
            role,
            root,
            tool_name,
            args,
            pinned_task=task_id,
            pinned_run=run_id,
            pinned_board=board,
            pinned_profile=profile,
        )
        if not lifecycle.allowed:
            return lifecycle
    return AuthorityDecision(True)


def authorize_current_worker(tool_name: str, arguments: Mapping[str, Any] | None) -> AuthorityDecision:
    return authorize_worker_tool(
        tool_name,
        arguments,
        authority=os.environ.get("HERMES_WORKER_AUTHORITY"),
        workspace=os.environ.get("HERMES_KANBAN_WORKSPACE"),
        task_id=os.environ.get("HERMES_KANBAN_TASK"),
        run_id=os.environ.get("HERMES_KANBAN_RUN_ID"),
        board=os.environ.get("HERMES_KANBAN_BOARD"),
        profile=os.environ.get("HERMES_PROFILE"),
    )
