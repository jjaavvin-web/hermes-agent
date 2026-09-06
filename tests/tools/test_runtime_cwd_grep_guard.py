"""Static guard: subprocess cwd sites must route through confinement resolver."""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Explicit subprocess cwd sites that are not runtime/tool execution cwd selection.
# Keep this list narrow and documented; a new unlisted cwd= call under tools/ or
# agent/ must fail and justify routing through resolve_confined_cwd().
_ALLOWED_FILES = {
    "tools/process_registry.py",  # receives already-resolved default_cwd from terminal_tool
    "tools/checkpoint_manager.py",  # checkpoint git helpers run in explicit snapshot roots
    "tools/environments/local.py",  # environment backend; caller supplies resolved cwd
    "agent/copilot_acp_client.py",  # external ACP process cwd, not lane tool execution
    "agent/claude_cli_runtime.py",  # Claude CLI runner has its own confinement checks
    "agent/context_references.py",  # read-only git context extraction against explicit repos
    "agent/skill_preprocessing.py",  # read-only skill preprocessing helper
    "tools/specialist_test_tool.py",  # cwd is a disposable copy inside a freshly
                                       # created tempfile.TemporaryDirectory(), never
                                       # the session/runtime cwd; the child also runs
                                       # namespace-isolated via unshare(1)
}


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in {"Popen", "run"}
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def test_subprocess_explicit_cwd_sites_are_confined_or_allowlisted():
    offenders: list[str] = []
    for root in (REPO / "tools", REPO / "agent"):
        for path in root.rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            src = path.read_text(encoding="utf-8")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
                    continue
                if not any(kw.arg == "cwd" for kw in node.keywords):
                    continue
                window = "\n".join(src.splitlines()[max(0, node.lineno - 20): node.lineno + 5])
                routed = "resolve_confined_cwd" in window or "_resolve_child_cwd" in window
                if rel not in _ALLOWED_FILES and not routed:
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "confinement-blind subprocess cwd= sites: " + ", ".join(offenders)
