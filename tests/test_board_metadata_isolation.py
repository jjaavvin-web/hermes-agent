from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

_WORKTREE = Path(__file__).resolve().parents[1]


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return path


def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


def test_write_board_metadata_round_trips_repo_root_base_branch_and_default_vcs(fresh_home, tmp_path):
    repo = _git_repo(tmp_path / "repo")

    meta = kb.write_board_metadata(
        "isolated",
        repo_root=str(repo),
        base_branch="main",
    )

    assert meta["repo_root"] == str(repo.resolve())
    assert meta["base_branch"] == "main"
    assert meta["vcs_kind"] == "git"
    raw = json.loads((fresh_home / "kanban" / "boards" / "isolated" / "board.json").read_text())
    assert raw["repo_root"] == str(repo.resolve())
    assert raw["base_branch"] == "main"
    assert raw["vcs_kind"] == "git"
    assert kb.read_board_metadata("isolated")["repo_root"] == str(repo.resolve())


def test_write_board_metadata_rejects_invalid_repo_root_and_base_branch(fresh_home, tmp_path):
    with pytest.raises(ValueError, match="repo_root.*git"):
        kb.write_board_metadata("badrepo", repo_root=str(tmp_path / "missing"), base_branch="main")

    repo = _git_repo(tmp_path / "repo")
    with pytest.raises(ValueError, match="base_branch"):
        kb.write_board_metadata("badbranch", repo_root=str(repo), base_branch="")

    with pytest.raises(ValueError, match="vcs_kind"):
        kb.write_board_metadata("badvcs", repo_root=str(repo), base_branch="main", vcs_kind="hg")


def test_existing_board_without_isolation_metadata_still_loads(fresh_home):
    board_dir = fresh_home / "kanban" / "boards" / "legacy"
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text(json.dumps({"slug": "legacy", "name": "Legacy"}) + "\n")

    meta = kb.read_board_metadata("legacy")

    assert meta["slug"] == "legacy"
    assert meta["name"] == "Legacy"
    assert meta.get("repo_root") is None
    assert meta.get("base_branch") is None
    assert meta.get("vcs_kind") is None


def test_boards_create_requires_and_persists_isolation_metadata(tmp_path):
    home = tmp_path / "home"
    repo = _git_repo(tmp_path / "repo")
    env = {"HERMES_HOME": str(home)}

    missing = _cli(["boards", "create", "no-meta"], env_extra=env)
    assert missing.returncode != 0
    assert "repo-root" in missing.stderr or "repo_root" in missing.stderr

    created = _cli(
        [
            "boards",
            "create",
            "with-meta",
            "--repo-root",
            str(repo),
            "--base-branch",
            "main",
        ],
        env_extra=env,
    )
    assert created.returncode == 0, created.stderr
    board_json = home / "kanban" / "boards" / "with-meta" / "board.json"
    raw = json.loads(board_json.read_text())
    assert raw["repo_root"] == str(repo.resolve())
    assert raw["base_branch"] == "main"
    assert raw["vcs_kind"] == "git"
