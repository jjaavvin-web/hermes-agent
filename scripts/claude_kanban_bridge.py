#!/usr/bin/env python3
"""scripts/claude_kanban_bridge — gated kanban completion.

Mark a Kanban task complete (or block it) with the ISA completion gate
in front of the normal ``kanban_db.complete_task`` primitive.

History
-------
This script previously also dispatched the task's actual work via a
non-interactive Claude subprocess. That path was retired after the
2026-06-15 Anthropic billing change moved non-interactive Claude
invocations off the Max-plan session bucket (see
``docs/PROVIDER-STACK.md``). The agentic execution now happens inside
the per-Discord-thread codex session
(``gateway/codex_session_dispatcher.py``); this bridge is the small,
testable seam that finalises the task once that work is done.

ISA completion gate
-------------------
If the task is linked to an ISA (Ideal State Artifact — see
``~/.hermes/ISA-SPEC.md``) by the ISA's ``card:`` field, the bridge
marks the task complete only once that ISA has reached
``phase: complete`` and passes ``isa_lint``. The gate is **inert by
default and fail-open**: a task with no linked ISA — and any
unexpected error while evaluating one — completes exactly as before.
Only a task whose linked ISA exists but has not reached
``phase: complete`` or fails ``isa_lint`` is blocked, with an
``isa-gate:`` reason. See :func:`_isa_gate`.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo bootstrap — make ``hermes_cli`` importable regardless of CWD.
# ---------------------------------------------------------------------------

_BUNDLED_REPO_ROOT = Path(__file__).resolve().parents[1]
_repo_root_override = os.environ.get("HERMES_REPO_ROOT")
if _repo_root_override is not None:
    if not _repo_root_override.strip():
        raise RuntimeError("HERMES_REPO_ROOT is invalid: empty path")
    try:
        _resolved_override = Path(_repo_root_override).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(
            f"HERMES_REPO_ROOT is invalid: {_repo_root_override!r}"
        ) from exc
    if _resolved_override != _BUNDLED_REPO_ROOT:
        raise RuntimeError(
            "HERMES_REPO_ROOT provenance mismatch: "
            f"expected {_BUNDLED_REPO_ROOT}, got {_resolved_override}"
        )

_REPO_ROOT = _BUNDLED_REPO_ROOT


def _is_bundled_root_entry(entry: str) -> bool:
    try:
        return Path(entry).resolve() == _REPO_ROOT
    except (OSError, RuntimeError, TypeError):
        return False


sys.path[:] = [
    str(_REPO_ROOT),
    *(entry for entry in sys.path if not _is_bundled_root_entry(entry)),
]


# ---------------------------------------------------------------------------
# DB helpers — thin wrappers around kanban_db primitives.
# ---------------------------------------------------------------------------


def _kanban_db():
    """Return the deferred kernel only when it comes from this bundle."""
    from hermes_cli import kanban_db as kb  # noqa: PLC0415

    expected = _REPO_ROOT / "hermes_cli" / "kanban_db.py"
    module_file = getattr(kb, "__file__", None)
    if not isinstance(module_file, (str, os.PathLike)):
        raise RuntimeError(
            "hermes_cli.kanban_db provenance validation failed: "
            f"expected file {expected}, got {module_file!r}"
        )
    try:
        expected_file = expected.resolve(strict=True)
        actual_file = Path(module_file).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise RuntimeError(
            "hermes_cli.kanban_db provenance validation failed: "
            f"expected file {expected}, got {module_file!r}"
        ) from exc
    if (
        not expected_file.is_file()
        or not actual_file.is_file()
        or actual_file != expected_file
    ):
        raise RuntimeError(
            "hermes_cli.kanban_db provenance mismatch: "
            f"expected {expected_file}, got {actual_file}"
        )
    return kb


def _connect_board(board: str | None):
    """Return a live sqlite3 connection to the board's kanban DB."""
    kb = _kanban_db()

    return kb.connect(board=board)


def _fetch_task(conn, task_id: str):
    kb = _kanban_db()

    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"Task {task_id!r} not found in the kanban DB")
    return task


def _complete(conn, task_id: str, summary: str, metadata: dict) -> bool:
    kb = _kanban_db()

    return bool(
        kb.complete_task(
            conn,
            task_id,
            summary=summary,
            metadata=metadata,
        )
    )


def _block(conn, task_id: str, reason: str) -> None:
    kb = _kanban_db()

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

        def _is_bundled_scripts_entry(entry: str) -> bool:
            try:
                return Path(entry).resolve() == scripts_dir
            except (OSError, RuntimeError, TypeError):
                return False

        def _set_bundled_import_precedence() -> None:
            sys.path[:] = [
                str(_REPO_ROOT),
                str(scripts_dir),
                *(
                    entry
                    for entry in sys.path
                    if not _is_bundled_root_entry(entry)
                    and not _is_bundled_scripts_entry(entry)
                ),
            ]

        _set_bundled_import_precedence()
        try:
            import isa_common  # noqa: PLC0415
            import isa_lint  # noqa: PLC0415
        finally:
            _set_bundled_import_precedence()

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
    summary: str = "",
) -> int:
    """Finalise a Kanban task with the ISA gate in front. Returns 0 on success."""
    # Read the task once so we fail fast on bad ids before touching the gate.
    with contextlib.closing(_connect_board(board)) as conn:
        task = _fetch_task(conn, task_id)

    effective_board = board or os.environ.get("HERMES_KANBAN_BOARD", "default")
    metadata = {
        "executor": "claude-kanban-bridge",
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
        completed = _complete(
            conn,
            task_id,
            summary=summary,
            metadata=metadata,
        )

    if not completed:
        _log_error(f"Kanban kernel refused completion of task {task_id}.")
        return 1

    _log_info(f"Task {task_id} completed via claude-kanban-bridge.")
    return 0


def _log_info(msg: str) -> None:
    print(f"[claude-kanban-bridge] INFO: {msg}", file=sys.stderr)


def _log_error(msg: str) -> None:
    print(f"[claude-kanban-bridge] ERROR: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Finalise a Kanban task (complete or block) with the ISA "
            "completion gate in front."
        ),
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
        "--summary",
        default="",
        help="Completion summary written into the Kanban DB.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.task:
        print("error: --task / HERMES_KANBAN_TASK is required", file=sys.stderr)
        return 2
    return run(args.task, args.board or None, summary=args.summary)


if __name__ == "__main__":
    sys.exit(main())
