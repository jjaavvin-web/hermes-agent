"""Tests for the ISA completion gate wired into scripts/claude_kanban_bridge.py.

Covers ISC-24..29 of the isa-enforcement-layer ISA. The gate function
``_isa_gate(task_id)`` returns ``(allowed, reason)``; it is exercised directly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


isa_common = _load("isa_common")
bridge = _load("claude_kanban_bridge")


def _run_isolated_bridge(
    tmp_path: Path,
    code: str,
    *args: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a cold bridge probe without inheriting this process's imports."""
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    cwd = tmp_path / "cwd"
    pycache = tmp_path / "pycache"
    hermes_home.mkdir(parents=True)
    cwd.mkdir()
    pycache.mkdir()
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TZ")
        if key in os.environ
    }
    env.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(pycache),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(_SCRIPTS / "claude_kanban_bridge.py"),
            *(str(arg) for arg in args),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
        env=env,
    )


def test_cold_default_binds_bundled_root_and_kanban_db_provenance(tmp_path):
    result = _run_isolated_bridge(
        tmp_path,
        r"""
import importlib.util
import json
from pathlib import Path
import sys

bridge_path = Path(sys.argv[1]).resolve()
bundled_root = bridge_path.parents[1]
sys.path.insert(0, str(bundled_root / "scripts" / ".."))
spec = importlib.util.spec_from_file_location("cold_bridge_probe", bridge_path)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
if hasattr(bridge, "_kanban_db"):
    kb = bridge._kanban_db()
else:
    from hermes_cli import kanban_db as kb
print(json.dumps({
    "repo_root": str(bridge._REPO_ROOT.resolve()),
    "sys_path_0": str(Path(sys.path[0]).resolve()),
    "bundled_root_entries": sum(
        Path(entry).resolve() == bundled_root for entry in sys.path if entry
    ),
    "kanban_db": str(Path(kb.__file__).resolve()),
}))
""",
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    bundled_root = (_SCRIPTS / "claude_kanban_bridge.py").resolve().parents[1]
    assert Path(observed["repo_root"]) == bundled_root
    assert Path(observed["sys_path_0"]) == bundled_root
    assert observed["bundled_root_entries"] == 1
    assert Path(observed["kanban_db"]) == bundled_root / "hermes_cli" / "kanban_db.py"


def test_cold_mismatched_repo_override_fails_before_db_access(tmp_path):
    foreign = tmp_path / "foreign"
    package = foreign / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "db-accessed"
    (package / "kanban_db.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def connect(*args, **kwargs):\n"
        "    Path(os.environ['DB_ACCESS_MARKER']).write_text('connect')\n",
        encoding="utf-8",
    )

    result = _run_isolated_bridge(
        tmp_path,
        r"""
import importlib.util
from pathlib import Path
import sys

bridge_path = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("override_bridge_probe", bridge_path)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
""",
        extra_env={
            "HERMES_REPO_ROOT": str(foreign),
            "DB_ACCESS_MARKER": str(marker),
        },
    )

    assert result.returncode != 0
    assert "HERMES_REPO_ROOT" in result.stderr
    assert not marker.exists()


def test_preloaded_foreign_kanban_db_fails_before_primitive_call(tmp_path):
    foreign = tmp_path / "foreign"
    package = foreign / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "primitive-called"
    (package / "kanban_db.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def connect(*args, **kwargs):\n"
        "    Path(os.environ['DB_ACCESS_MARKER']).write_text('connect')\n"
        "    return object()\n",
        encoding="utf-8",
    )

    result = _run_isolated_bridge(
        tmp_path,
        r"""
import importlib.util
from pathlib import Path
import sys

bridge_path = Path(sys.argv[1]).resolve()
foreign = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(foreign))
from hermes_cli import kanban_db as foreign_kb
assert Path(foreign_kb.__file__).resolve() == foreign / "hermes_cli" / "kanban_db.py"
spec = importlib.util.spec_from_file_location("cached_bridge_probe", bridge_path)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
bridge._connect_board(None)
""",
        foreign,
        extra_env={"DB_ACCESS_MARKER": str(marker)},
    )

    assert result.returncode != 0
    assert "kanban_db" in result.stderr
    assert "provenance" in result.stderr.lower()
    assert not marker.exists()


def test_cold_isa_gate_preserves_bundled_path_precedence(tmp_path):
    result = _run_isolated_bridge(
        tmp_path,
        r"""
import importlib.util
import json
from pathlib import Path
import sys

bridge_path = Path(sys.argv[1]).resolve()
bundled_root = bridge_path.parents[1]
scripts_dir = bridge_path.parent
spec = importlib.util.spec_from_file_location("isa_gate_path_probe", bridge_path)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
allowed, reason = bridge._isa_gate("t_no_linked_isa_transition_probe")
resolved_path = [str(Path(entry).resolve()) for entry in sys.path if entry]
print(json.dumps({
    "allowed": allowed,
    "reason": reason,
    "sys_path_0": resolved_path[0],
    "sys_path_1": resolved_path[1],
    "bundled_root_entries": resolved_path.count(str(bundled_root)),
    "scripts_dir_entries": resolved_path.count(str(scripts_dir)),
}))
""",
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    bundled_root = (_SCRIPTS / "claude_kanban_bridge.py").resolve().parents[1]
    assert observed["allowed"] is True
    assert observed["reason"] == ""
    assert Path(observed["sys_path_0"]) == bundled_root
    assert Path(observed["sys_path_1"]) == _SCRIPTS.resolve()
    assert observed["bundled_root_entries"] == 1
    assert observed["scripts_dir_entries"] == 1


def _e1_isa(card: str, phase: str, progress: str, criteria: str, verification: str) -> str:
    return f"""---
isa:      20260101-0000_gatefix
task:     "gate fixture"
tier:     E1
phase:    {phase}
progress: {progress}
card:     "{card}"
board:    "-"
branch:   b
hive:     "-"
owner:    claude
started:  2026-01-01T00:00:00Z
updated:  2026-01-01T00:00:00Z
---

## Goal
A real goal statement for the gate fixture.

## Criteria
{criteria}

## Verification
{verification}
"""


# A complete, isa_lint-clean E1 ISA.
_CLEAN_COMPLETE = _e1_isa(
    card="t_complete",
    phase="complete",
    progress="2/2",
    criteria="- [x] ISC-1: a real criterion\n- [x] ISC-2: Anti: a regression that did not happen",
    verification="ISC-1 verified — probe output ok.\nISC-2 verified — no regression observed.",
)


def _place(work_root: Path, slug: str, text: str) -> Path:
    """Write an ISA into <work_root>/<slug>/ISA.md."""
    d = work_root / slug
    d.mkdir(parents=True, exist_ok=True)
    isa = d / "ISA.md"
    isa.write_text(text, encoding="utf-8")
    return isa


def _work_root(tmp_path, monkeypatch) -> Path:
    """Point HERMES_HOME at tmp_path so find_isa_for_card scans tmp_path/work."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------
# R1 completion authority and truthful refusal
# --------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_bridge_returns_failure_when_kernel_refuses_completion(monkeypatch, capsys):
    task = type("Task", (), {"assignee": "worker"})()
    monkeypatch.setattr(bridge, "_connect_board", lambda board: object())
    monkeypatch.setattr(bridge, "_fetch_task", lambda conn, task_id: task)
    monkeypatch.setattr(bridge, "_isa_gate", lambda task_id: (True, ""))
    monkeypatch.setattr(bridge.contextlib, "closing", lambda obj: _NullClosing(obj))
    monkeypatch.setattr(bridge, "_complete", lambda *args, **kwargs: False)

    rc = bridge.run("t_deadbeef", "default", summary="refused")

    captured = capsys.readouterr()
    assert rc != 0
    assert "completed via" not in captured.err
    assert "refused" in captured.err.lower()


class _NullClosing:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


def test_specialist_bridge_rejects_foreign_task_without_mutation(
    kanban_home, tmp_path, monkeypatch,
):
    own_workspace = tmp_path / "own-bridge"
    own_workspace.mkdir()
    foreign_workspace = tmp_path / "foreign-bridge"
    foreign_workspace.mkdir()

    with kb.connect() as conn:
        own = kb.create_task(
            conn, title="own", assignee="sol-builder",
            workspace_kind="dir", workspace_path=str(own_workspace),
        )
        foreign = kb.create_task(
            conn, title="foreign", assignee="sol-verifier",
            workspace_kind="dir", workspace_path=str(foreign_workspace),
        )
        before = kb.get_task(conn, foreign)
        before_events = list(kb.list_events(conn, foreign))

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-builder")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(own_workspace))
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(bridge, "_isa_gate", lambda task_id: (True, ""))

    rc = bridge.run(foreign, "default", summary="hijack")

    assert rc != 0
    with kb.connect() as conn:
        after = kb.get_task(conn, foreign)
        assert after is not None and before is not None
        assert after.status == before.status == "ready"
        assert kb.list_events(conn, foreign) == before_events


def test_specialist_bridge_rejects_foreign_block_without_mutation(
    kanban_home, tmp_path, monkeypatch,
):
    own_workspace = tmp_path / "own-block"
    own_workspace.mkdir()
    foreign_workspace = tmp_path / "foreign-block"
    foreign_workspace.mkdir()

    with kb.connect() as conn:
        own = kb.create_task(
            conn, title="own block", assignee="sol-builder",
            workspace_kind="dir", workspace_path=str(own_workspace),
        )
        foreign = kb.create_task(
            conn, title="foreign block", assignee="sol-verifier",
            workspace_kind="dir", workspace_path=str(foreign_workspace),
        )
        before = kb.get_task(conn, foreign)
        before_events = list(kb.list_events(conn, foreign))

    monkeypatch.setenv("HERMES_WORKER_AUTHORITY", "sol-builder")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(own_workspace))
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(bridge, "_isa_gate", lambda task_id: (False, "blocked"))

    assert bridge.run(foreign, "default", summary="hijack") != 0
    with kb.connect() as conn:
        after = kb.get_task(conn, foreign)
        assert after is not None and before is not None
        assert after.status == before.status == "ready"
        assert kb.list_events(conn, foreign) == before_events


# --------------------------------------------------------------------------
# ISC-24 — block when the linked ISA is not phase: complete
# --------------------------------------------------------------------------


def test_isa_gate_isc_24_blocks_when_isa_not_complete(tmp_path, monkeypatch):
    root = _work_root(tmp_path, monkeypatch)
    _place(
        root,
        "20260101-0000_incomplete",
        _e1_isa(
            card="t_incomplete",
            phase="execute",
            progress="0/2",
            criteria="- [ ] ISC-1: a criterion\n- [ ] ISC-2: Anti: a regression",
            verification="_(filled during verify)_",
        ),
    )
    allowed, reason = bridge._isa_gate("t_incomplete")
    assert allowed is False
    assert "execute" in reason and "complete" in reason


# --------------------------------------------------------------------------
# ISC-25 — block when phase: complete but isa_lint fails
# --------------------------------------------------------------------------


def test_isa_gate_isc_25_blocks_when_complete_but_lint_fails(tmp_path, monkeypatch):
    root = _work_root(tmp_path, monkeypatch)
    # phase: complete, yet ISC-2 is still open — isa_lint must reject it.
    _place(
        root,
        "20260101-0000_dishonest",
        _e1_isa(
            card="t_open",
            phase="complete",
            progress="1/2",
            criteria="- [x] ISC-1: a criterion\n- [ ] ISC-2: Anti: a regression",
            verification="ISC-1 verified — ok.",
        ),
    )
    allowed, reason = bridge._isa_gate("t_open")
    assert allowed is False
    assert "isa_lint" in reason


# --------------------------------------------------------------------------
# ISC-26 — allow when the linked ISA is complete and lint-clean
# --------------------------------------------------------------------------


def test_isa_gate_isc_26_allows_when_isa_complete_and_lint_clean(tmp_path, monkeypatch):
    root = _work_root(tmp_path, monkeypatch)
    _place(root, "20260101-0000_done", _CLEAN_COMPLETE)
    allowed, reason = bridge._isa_gate("t_complete")
    assert allowed is True
    assert reason == ""


# --------------------------------------------------------------------------
# ISC-27 — inert when the task has no linked ISA
# --------------------------------------------------------------------------


def test_isa_gate_isc_27_allows_when_no_isa_linked(tmp_path, monkeypatch):
    root = _work_root(tmp_path, monkeypatch)
    # An ISA exists, but it links a different card.
    _place(root, "20260101-0000_other", _CLEAN_COMPLETE)
    allowed, reason = bridge._isa_gate("t_a_card_with_no_isa")
    assert allowed is True
    assert reason == ""


# --------------------------------------------------------------------------
# ISC-28 — fail-open when ISA evaluation raises
# --------------------------------------------------------------------------


def test_isa_gate_isc_28_fails_open_on_evaluation_error(tmp_path, monkeypatch):
    _work_root(tmp_path, monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("simulated ISA tooling failure")

    # _isa_gate imports isa_common from sys.modules — patch the loaded module.
    monkeypatch.setattr(isa_common, "find_isa_for_card", _boom)
    allowed, reason = bridge._isa_gate("t_anything")
    assert allowed is True
    assert reason == ""


# --------------------------------------------------------------------------
# ISC-29 — the bridge module still imports and --help still works
# --------------------------------------------------------------------------


def test_isa_gate_isc_29_bridge_imports_and_help_works():
    # Import is proven by _load("claude_kanban_bridge") at module load above.
    assert callable(bridge._isa_gate)
    assert callable(bridge.main)
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS / "claude_kanban_bridge.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "task" in result.stdout.lower()
