from pathlib import Path

import pytest

from hermes_cli import gitnexus_repo_manager as mgr


def test_stage_repo_targets_container_path(monkeypatch, tmp_path):
    host_path = tmp_path / "hermes-agent"
    host_path.mkdir()
    docker_calls = []
    archive_calls = []

    def fake_run_docker(*args, check=True):
        docker_calls.append(args)

        class Result:
            stdout = ""

        return Result()

    def fake_archive(path, container_path):
        archive_calls.append((path, container_path))

    monkeypatch.setattr(mgr, "_run_docker", fake_run_docker)
    monkeypatch.setattr(mgr, "_archive_tracked_files_to_container", fake_archive)

    container_path = mgr._stage_repo_in_container(host_path, "hermes-agent")

    assert container_path == "/data/gitnexus/repos/hermes-agent"
    assert docker_calls[0] == (
        "sh",
        "-c",
        "rm -rf /data/gitnexus/repos/hermes-agent && mkdir -p /data/gitnexus/repos/hermes-agent",
    )
    assert docker_calls[1] == (
        "sh",
        "-c",
        "find /data/gitnexus/repos/hermes-agent \\( -name .env -o -name auth.json \\) -print",
    )
    assert archive_calls == [(host_path, "/data/gitnexus/repos/hermes-agent")]


def test_archive_stream_uses_git_archive_pipe_without_shell(monkeypatch, tmp_path):
    host_path = tmp_path / "repo"
    host_path.mkdir()
    popen_calls = []

    class FakeStdout:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        def __init__(self, args, stdout=None, stdin=None):
            self.args = args
            self.stdout = FakeStdout() if stdout == mgr.subprocess.PIPE else stdout
            self.stdin = stdin
            self.killed = False

        def wait(self):
            return 0

        def poll(self):
            return 0

        def kill(self):
            self.killed = True

    def fake_popen(args, stdout=None, stdin=None):
        proc = FakeProcess(args, stdout=stdout, stdin=stdin)
        popen_calls.append(proc)
        return proc

    monkeypatch.setattr(mgr.subprocess, "Popen", fake_popen)

    mgr._archive_tracked_files_to_container(host_path, "/data/gitnexus/repos/repo")

    assert popen_calls[0].args == [
        "git",
        "-C",
        str(host_path),
        "archive",
        "--format=tar",
        "HEAD",
    ]
    assert popen_calls[1].args == [
        "docker",
        "exec",
        "-i",
        mgr.GITNEXUS_CONTAINER,
        "tar",
        "-x",
        "-C",
        "/data/gitnexus/repos/repo",
    ]
    assert popen_calls[1].stdin is popen_calls[0].stdout


def test_secret_guard_aborts_before_analyze(monkeypatch, tmp_path):
    host_path = tmp_path / "gitnexus"
    host_path.mkdir()
    api_calls = []

    monkeypatch.setattr(
        mgr,
        "_stage_repo_in_container",
        lambda path, label: None,
    )

    def fake_api(method, path, body=None):
        api_calls.append((method, path, body))
        raise AssertionError("analyze should not run after secret guard failure")

    monkeypatch.setattr(mgr, "_api", fake_api)

    assert mgr._index_repo(host_path, "gitnexus") is False
    assert api_calls == []


def test_stage_secret_guard_reports_forbidden_files(monkeypatch, tmp_path):
    host_path = tmp_path / "gitnexus"
    host_path.mkdir()
    archived = []

    monkeypatch.setattr(
        mgr,
        "_recreate_container_staging",
        lambda label: f"/data/gitnexus/repos/{label}",
    )
    monkeypatch.setattr(
        mgr,
        "_archive_tracked_files_to_container",
        lambda path, container_path: archived.append((path, container_path)),
    )
    monkeypatch.setattr(
        mgr,
        "_container_secret_hits",
        lambda container_path: [f"{container_path}/.env"],
    )

    assert mgr._stage_repo_in_container(host_path, "gitnexus") is None
    assert archived == [(host_path, "/data/gitnexus/repos/gitnexus")]


def test_index_repo_posts_container_path(monkeypatch, tmp_path):
    host_path = tmp_path / "gitnexus"
    host_path.mkdir()
    api_calls = []

    monkeypatch.setattr(
        mgr,
        "_stage_repo_in_container",
        lambda path, label: "/data/gitnexus/repos/gitnexus",
    )
    monkeypatch.setattr(mgr, "_wait_for_job", lambda job_id, label: True)

    def fake_api(method, path, body=None):
        api_calls.append((method, path, body))
        return {"jobId": "job-123"}

    monkeypatch.setattr(mgr, "_api", fake_api)

    assert mgr._index_repo(host_path, "gitnexus") is True
    assert api_calls == [
        ("POST", "/api/analyze", {"path": "/data/gitnexus/repos/gitnexus"})
    ]


def test_resolve_repo_selection_label_and_path(tmp_path):
    hermes_repo = tmp_path / "hermes-agent"
    gitnexus_repo = tmp_path / "gitnexus"
    outside_repo = tmp_path / "outside"
    config = {
        "repos": [
            {"path": str(hermes_repo), "label": "hermes-agent"},
            {"path": str(gitnexus_repo), "label": "gitnexus"},
        ]
    }

    assert mgr._resolve_repo_selection(config, repo_label="gitnexus") == (
        gitnexus_repo,
        "gitnexus",
    )
    assert mgr._resolve_repo_selection(config, host_path=str(hermes_repo)) == (
        hermes_repo,
        "hermes-agent",
    )
    assert mgr._resolve_repo_selection(config, host_path=str(outside_repo)) == (
        outside_repo,
        "outside",
    )

    with pytest.raises(ValueError, match="repo label not found"):
        mgr._resolve_repo_selection(config, repo_label="missing")


def test_hook_install_is_idempotent_and_preserves_user_hook(monkeypatch, tmp_path):
    repo_path = tmp_path / "gitnexus"
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_path = hooks_dir / "post-commit"
    hook_path.write_text("#!/bin/sh\necho user hook\n", encoding="utf-8")
    monkeypatch.setattr(mgr, "_git_common_dir", lambda path: tmp_path / ".git")

    first_path = mgr._install_hook(repo_path, "gitnexus", "post-commit")
    second_path = mgr._install_hook(repo_path, "gitnexus", "post-commit")

    assert first_path == second_path == hook_path
    text = hook_path.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\necho user hook\n")
    assert text.count(mgr.HOOK_MARKER_START) == 1
    assert text.count(mgr.HOOK_MARKER_END) == 1
    assert (
        "/home/josep/.local/share/hermes-agent/venv/bin/python -m "
        "hermes_cli.gitnexus_repo_manager --path \"$repo_root\""
    ) in text
    assert "flock -n -E 75 \"$lockfile\"" in text
