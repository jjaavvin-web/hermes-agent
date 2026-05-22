#!/usr/bin/env python3
"""Claude CLI Kanban Bridge.

Dispatches a single Kanban task by spawning ``claude --print`` as a
subprocess, then writes the outcome back to the Kanban DB using the same
``complete_task`` / ``block_task`` primitives that native Hermes workers use.

Why this exists
---------------
The Hermes ``anthropic`` provider bills against Anthropic's "extra usage"
bucket, which requires separate API credits. The ``claude`` CLI uses the
logged-in Claude Code / claude.ai session bucket for Max-plan users. This
bridge lets selected Kanban workers run on Max-plan session quota with no
Anthropic API-key fallback.

ISA completion gate
-------------------
If the dispatched task is linked to an ISA (Ideal State Artifact — see
``~/.hermes/ISA-SPEC.md``), the bridge marks the task complete only once that
ISA has reached ``phase: complete`` and passes ``isa_lint``. The gate is
fail-open: a task with no linked ISA — or any error evaluating one — completes
exactly as before. See ``_isa_gate``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT_SECONDS = 600
_DEFAULT_HERMES_REPO_ROOT = "/home/josep/.local/share/hermes-agent"
_BLOCKED_AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    # Force Claude Code to use its logged-in first-party credential store, not
    # an injected token from the surrounding Hermes process.
    "CLAUDE_CODE_OAUTH_TOKEN",
)

_REPO_ROOT = Path(os.environ.get("HERMES_REPO_ROOT", _DEFAULT_HERMES_REPO_ROOT))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
# Kanban task {task_id} — {board} board

## Worker context

{worker_context}

---

## Instructions

You are executing this Kanban task as a claude-cli-bridge worker through
``claude --print``.

1. Do the work described in the worker context thoroughly.
2. When finished, emit a concise final summary (3–10 sentences) covering what
   was done, key decisions made, and any caveats or follow-up items. The
   summary is the last thing you write — it becomes the task's official
   completion record in the Kanban board.
3. Artifact evidence: if you produce files or artifacts, write a copy or
   reference to them under:
       ~/.hermes/audits/{audit_ts}-{task_id}/
   Create that directory if it does not exist.

## Collaboration gates — ALWAYS pause for user confirmation before:

- destructive — deleting / overwriting data or files irreversibly
- push — pushing commits to a remote or publishing a release
- restart — restarting services, containers, or system processes
- config — changing machine-level or account-level configuration
- billing — any action that incurs real-money cost
- security — changing credentials, secrets, or access-control rules
- scope — starting work that is outside the stated task description
- dirty-stash — touching the git stash when the working tree is dirty

## Stash rule

NEVER apply / pop / drop / clear stashes under any circumstances. If the
working tree has uncommitted changes that block progress, pause and ask the
user.

---

Begin working on the task now.
"""


def _build_prompt(task_id: str, board: str, worker_context: str, audit_ts: str) -> str:
    return _PROMPT_TEMPLATE.format(
        task_id=task_id,
        board=board,
        worker_context=worker_context.strip() or "(no worker context provided)",
        audit_ts=audit_ts,
    )


# ---------------------------------------------------------------------------
# DB helpers — thin wrappers around kanban_db primitives
# ---------------------------------------------------------------------------


def _connect_board(board: str | None):
    """Return a live sqlite3 connection to the board's kanban DB."""
    from hermes_cli import kanban_db as kb  # noqa: PLC0415

    return kb.connect(board=board)


def _fetch_task_and_context(conn, task_id: str):
    from hermes_cli import kanban_db as kb  # noqa: PLC0415

    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"Task {task_id!r} not found in the kanban DB")
    return task, kb.build_worker_context(conn, task_id)


def _complete(conn, task_id: str, summary: str, metadata: dict) -> None:
    from hermes_cli import kanban_db as kb  # noqa: PLC0415

    kb.complete_task(conn, task_id, summary=summary, metadata=metadata)


def _block(conn, task_id: str, reason: str) -> None:
    from hermes_cli import kanban_db as kb  # noqa: PLC0415

    kb.block_task(conn, task_id, reason=reason)


# ---------------------------------------------------------------------------
# ISA completion gate
# ---------------------------------------------------------------------------


def _isa_gate(task_id: str) -> tuple[bool, str]:
    """Decide whether ``task_id`` may be marked complete, per its linked ISA.

    Returns ``(allowed, reason)``. The gate is **inert by default and
    fail-open** (ISA-SPEC §10): a task with no linked ISA — and any unexpected
    error while evaluating one — yields ``(True, "")``. Only a task whose
    linked ISA exists but has not reached ``phase: complete`` and passed
    ``isa_lint`` is blocked.
    """
    try:
        scripts_dir = Path(__file__).resolve().parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import isa_common  # noqa: PLC0415
        import isa_lint  # noqa: PLC0415

        isa_path = isa_common.find_isa_for_card(task_id)
        if isa_path is None:
            return True, ""  # no ISA linked — gate is inert

        isa = isa_common.parse_isa(isa_path)
        if isa.phase != "complete":
            return False, (
                f"linked ISA {isa_path} is at phase '{isa.phase}', not 'complete'"
            )

        result = isa_lint.lint(isa)
        if not result.ok:
            return False, (
                f"linked ISA {isa_path} is phase: complete but fails isa_lint "
                f"with {len(result.failures)} failure(s): {result.failures}"
            )
        return True, ""
    except Exception as exc:  # gate is fail-open by design
        _log_error(
            f"ISA gate evaluation errored for task {task_id}: {exc!r} "
            f"— proceeding (fail-open)"
        )
        return True, ""


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(
    task_id: str,
    board: str | None,
    *,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    model: str | None = None,
    effort: str | None = None,
) -> int:
    """Execute the bridge logic. Returns 0 on success, non-zero on failure."""
    audit_ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    with contextlib.closing(_connect_board(board)) as conn:
        task, worker_context = _fetch_task_and_context(conn, task_id)

    effective_board = board or os.environ.get("HERMES_KANBAN_BOARD", "default")
    prompt = _build_prompt(
        task_id=task_id,
        board=effective_board,
        worker_context=worker_context,
        audit_ts=audit_ts,
    )

    claude_bin = _find_claude()
    cmd = [claude_bin, "--print", "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_claude_subprocess_env(),
        )
    except subprocess.TimeoutExpired as exc:
        _log_error(f"claude subprocess timed out after {timeout}s for task {task_id}")
        stderr_bytes = exc.stderr
        if isinstance(stderr_bytes, bytes):
            stderr_snippet = stderr_bytes.decode("utf-8", errors="replace")[:500]
        else:
            stderr_snippet = (stderr_bytes or "")[:500]
        with contextlib.closing(_connect_board(board)) as conn:
            _block(
                conn,
                task_id,
                reason=(
                    f"bridge-error: claude subprocess timed out after {timeout}s. "
                    f"stderr: {stderr_snippet}"
                ),
            )
        return 1
    except FileNotFoundError:
        _log_error("'claude' CLI not found on PATH. Install Claude Code to use this bridge.")
        with contextlib.closing(_connect_board(board)) as conn:
            _block(conn, task_id, reason="bridge-error: 'claude' CLI not found on PATH")
        return 1

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if result.returncode != 0:
        _log_error(
            f"claude exited with code {result.returncode} for task {task_id}. "
            f"stderr: {stderr[:500]}"
        )
        with contextlib.closing(_connect_board(board)) as conn:
            _block(
                conn,
                task_id,
                reason=(
                    f"bridge-error: claude exited with code {result.returncode}. "
                    f"stderr: {stderr[:500]}"
                ),
            )
        return result.returncode

    summary = stdout.strip()
    if not summary:
        _log_error(f"claude exited 0 but produced empty output for task {task_id}")
        with contextlib.closing(_connect_board(board)) as conn:
            _block(conn, task_id, reason="bridge-error: claude exited 0 with empty output")
        return 1

    metadata = {
        "executor": "claude-cli-bridge",
        "claude_exit_code": result.returncode,
        "claude_model": model or "default",
        "audit_dir": str(Path.home() / ".hermes" / "audits" / f"{audit_ts}-{task_id}"),
        "board": effective_board,
        "assignee": task.assignee,
    }

    allowed, gate_reason = _isa_gate(task_id)
    if not allowed:
        _log_error(f"ISA gate blocked completion of task {task_id}: {gate_reason}")
        with contextlib.closing(_connect_board(board)) as conn:
            _block(conn, task_id, reason=f"isa-gate: {gate_reason}")
        return 1

    with contextlib.closing(_connect_board(board)) as conn:
        _complete(conn, task_id, summary=summary, metadata=metadata)

    _log_info(f"Task {task_id} completed successfully via claude-cli-bridge.")
    return 0


def _claude_subprocess_env() -> dict[str, str]:
    """Return a Claude CLI env with Anthropic API-key fallbacks stripped."""
    env = dict(os.environ)
    for name in _BLOCKED_AUTH_ENV:
        env.pop(name, None)
    env.setdefault("CLAUDE_CODE_DISABLE_FAST_MODE", "1")
    env.setdefault("HERMES_REPO_ROOT", str(_REPO_ROOT))
    return env


def _find_claude() -> str:
    """Return the path to the ``claude`` CLI, or raise FileNotFoundError."""
    import shutil  # noqa: PLC0415

    path = shutil.which("claude")
    if path:
        return path
    raise FileNotFoundError("'claude' not found on PATH")


def _log_info(msg: str) -> None:
    print(f"[claude-kanban-bridge] INFO: {msg}", file=sys.stderr)


def _log_error(msg: str) -> None:
    print(f"[claude-kanban-bridge] ERROR: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dispatch a Kanban task via the claude CLI subprocess bridge.",
    )
    parser.add_argument(
        "--task",
        default=os.environ.get("HERMES_KANBAN_TASK", ""),
        help="Kanban task id. Also read from HERMES_KANBAN_TASK env.",
    )
    parser.add_argument(
        "--board",
        default=os.environ.get("HERMES_KANBAN_BOARD", None),
        help="Board slug. Also read from HERMES_KANBAN_BOARD env.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("HERMES_BRIDGE_TIMEOUT", str(_DEFAULT_TIMEOUT_SECONDS))),
        help=f"Max seconds to wait for claude (default {_DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("HERMES_CLAUDE_MODEL", ""),
        help="Optional Claude Code model selector, e.g. opus or sonnet.",
    )
    parser.add_argument(
        "--effort",
        default=os.environ.get("HERMES_CLAUDE_EFFORT", ""),
        help="Optional Claude Code effort selector, e.g. max.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.task:
        print("error: --task / HERMES_KANBAN_TASK is required", file=sys.stderr)
        return 2
    return run(
        args.task,
        args.board or None,
        timeout=args.timeout,
        model=args.model or None,
        effort=args.effort or None,
    )


if __name__ == "__main__":
    sys.exit(main())
