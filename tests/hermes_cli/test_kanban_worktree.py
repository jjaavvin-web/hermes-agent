"""P2: deterministic worktree creation for code cards (kanban_db._ensure_kanban_worktree)."""
import subprocess

from hermes_cli import kanban_db


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return repo


def _branch_of(wt):
    return subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                          capture_output=True, text=True).stdout.strip()


def test_creates_branch_off_head(tmp_path):
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt"
    assert kanban_db._ensure_kanban_worktree(wt, "kanban/t_x", repo_root=repo)
    assert (wt / ".git").exists()
    assert (wt / "f.txt").read_text() == "hi"   # branched off HEAD content
    assert _branch_of(wt) == "kanban/t_x"


def test_idempotent(tmp_path):
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt"
    assert kanban_db._ensure_kanban_worktree(wt, "kanban/t_y", repo_root=repo)
    assert kanban_db._ensure_kanban_worktree(wt, "kanban/t_y", repo_root=repo)  # already there


def test_reuses_existing_branch(tmp_path):
    repo = _make_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "kanban/t_z"], check=True)
    wt = tmp_path / "wt"
    assert kanban_db._ensure_kanban_worktree(wt, "kanban/t_z", repo_root=repo)
    assert _branch_of(wt) == "kanban/t_z"


def test_bad_repo_returns_false_not_raise(tmp_path):
    wt = tmp_path / "wt"
    assert kanban_db._ensure_kanban_worktree(wt, "b", repo_root=tmp_path / "nope") is False


def test_real_hermes_repo_root_resolves():
    # the live package IS a git checkout → repo root resolves
    root = kanban_db._hermes_repo_root()
    assert root is not None and (root / ".git").exists()
    assert (root / "hermes_cli").is_dir()
