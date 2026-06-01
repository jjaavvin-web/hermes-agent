"""Tests for per-route webhook relay worktree binding."""

import os
from pathlib import Path
from types import SimpleNamespace

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter

# The live relay surface can export HERMES_WEBHOOK_WORKTREE=1 into this worker
# process. The existing adapter suites exercise normal webhook handling and
# assume worktree mode is disabled unless a test opts in. Keep the focused
# command hermetic while these direct-method tests still cover worktree binding.
os.environ.pop("HERMES_WEBHOOK_WORKTREE", None)
os.environ.pop("HERMES_WEBHOOK_BASE_BRANCH", None)
os.environ.pop("HERMES_REPO_ROOT", None)


def _make_adapter() -> WebhookAdapter:
    return WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))


def _capture_worktree_adds(monkeypatch):
    add_cmds: list[list[str]] = []

    def _fake_run(cmd, capture_output=True, text=True):
        if cmd[:5] == ["git", "-C", cmd[2], "rev-parse", "--verify"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if len(cmd) >= 6 and cmd[0] == "git" and cmd[3:6] == ["worktree", "add", cmd[5]]:
            add_cmds.append(list(cmd))
            Path(cmd[5]).mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run command: {cmd!r}")

    monkeypatch.setattr("gateway.platforms.webhook.subprocess.run", _fake_run)
    return add_cmds


def test_relay_route_defaults_construct_backward_compat_add_cmd(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    add_cmds = _capture_worktree_adds(monkeypatch)
    adapter = _make_adapter()

    path = adapter._ensure_relay_worktree("relay", {})

    expected_dir = hermes_home / "relay-wt" / "relay"
    assert path == str(expected_dir)
    assert add_cmds == [
        [
            "git",
            "-C",
            str(Path("gateway/platforms/webhook.py").resolve().parents[2]),
            "worktree",
            "add",
            str(expected_dir),
            "-b",
            "relay/work",
            "fork/main",
        ]
    ]


def test_route_worktree_branch_and_dir_override_add_cmd(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    add_cmds = _capture_worktree_adds(monkeypatch)
    adapter = _make_adapter()
    wt_dir = tmp_path / "relay-wt" / "proj-a"

    path = adapter._ensure_relay_worktree(
        "proj-a",
        {"worktree_branch": "proj-a/work", "worktree_dir": str(wt_dir)},
    )

    assert path == str(wt_dir)
    assert add_cmds == [
        [
            "git",
            "-C",
            str(Path("gateway/platforms/webhook.py").resolve().parents[2]),
            "worktree",
            "add",
            str(wt_dir),
            "-b",
            "proj-a/work",
            "fork/main",
        ]
    ]


def test_worktree_paths_are_cached_per_route(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _capture_worktree_adds(monkeypatch)
    adapter = _make_adapter()

    relay_path = adapter._ensure_relay_worktree("relay", {})
    project_path = adapter._ensure_relay_worktree("proj-b", {})

    assert relay_path == str(hermes_home / "relay-wt" / "relay")
    assert project_path == str(hermes_home / "relay-wt" / "proj-b")
    assert adapter._wt_paths == {
        "relay": str(hermes_home / "relay-wt" / "relay"),
        "proj-b": str(hermes_home / "relay-wt" / "proj-b"),
    }
