"""Tests for hermes_cli/code_lane_gate.py — the deterministic CODE-lane gate.

Every subprocess call is routed through an INJECTED fake ``run`` so the suite
never spawns a real git/pytest/ruff (and so never reaches the webbrowser code
that the hermetic conftest guard only neuters inside a pytest process). The
base-vs-head delta worktree is stubbed via the module's
``_provision_base_worktree`` / ``_cleanup_base_worktree`` seams.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli import code_lane_gate as clg


# ──────────────────────────────────────────────────────────────────────
# Fake subprocess runner
# ──────────────────────────────────────────────────────────────────────


def _cp(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class FakeRun:
    """Records calls and dispatches by command shape."""

    def __init__(
        self,
        *,
        diff_lines=None,
        diff_rc=0,
        worktree_diff_lines=None,
        untracked_lines=None,
        merge_base="",
        ruff_json="[]",
        ruff_rc=0,
        pytest_map=None,
        raise_on=None,
    ):
        # ``diff_lines`` = committed (three-dot) diff; the working-tree
        # (two-dot) and untracked sources default empty so existing tests
        # that only set ``diff_lines`` keep their committed-only behaviour.
        self.diff_lines = diff_lines or []
        self.diff_rc = diff_rc
        self.worktree_diff_lines = worktree_diff_lines or []
        self.untracked_lines = untracked_lines or []
        self.merge_base = merge_base
        self.ruff_json = ruff_json
        self.ruff_rc = ruff_rc
        # pytest_map: {(cwd_str, testfile): (rc, output)}; default = green.
        self.pytest_map = pytest_map or {}
        self.raise_on = raise_on  # callable(cmd) -> bool
        self.calls = []
        self.pytest_calls = []  # (cwd, testfile)

    @staticmethod
    def _pytest_testfile(cmd):
        # The path is the positional arg after the ``--`` end-of-options
        # separator (falls back to the last token if absent).
        if "--" in cmd:
            return cmd[cmd.index("--") + 1]
        return cmd[-1]

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append((cmd, kwargs))
        if self.raise_on and self.raise_on(cmd):
            raise OSError("injected subprocess failure")
        if "git" in str(cmd[0]) and "diff" in cmd and "--name-only" in cmd:
            # Three-dot (committed) vs two-dot (working-tree-vs-base).
            if any("..." in str(a) for a in cmd):
                return _cp(self.diff_rc, "\n".join(self.diff_lines))
            return _cp(0, "\n".join(self.worktree_diff_lines))
        if "ls-files" in cmd:
            return _cp(0, "\n".join(self.untracked_lines))
        if "merge-base" in cmd:
            return _cp(0, self.merge_base)
        if "worktree" in cmd:
            return _cp(0, "")
        if "check" in cmd and "PLW1514" in cmd:
            return _cp(self.ruff_rc, self.ruff_json)
        if "pytest" in cmd:
            testfile = self._pytest_testfile(cmd)
            cwd = kwargs.get("cwd")
            self.pytest_calls.append((cwd, testfile))
            rc, out = self.pytest_map.get((cwd, testfile), (0, ""))
            return _cp(rc, out)
        return _cp(0, "")


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# stub\n", encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# changed_files
# ──────────────────────────────────────────────────────────────────────


def test_changed_files_parses_diff(tmp_path):
    run = FakeRun(diff_lines=["hermes_cli/x.py", "", "docs/readme.md"])
    out = clg.changed_files(tmp_path, "fork/main", run)
    assert out == ["hermes_cli/x.py", "docs/readme.md"]


def test_changed_files_fail_open_on_rc(tmp_path):
    run = FakeRun(diff_rc=128)
    assert clg.changed_files(tmp_path, "fork/main", run) is None


def test_changed_files_fail_open_on_exception(tmp_path):
    run = FakeRun(raise_on=lambda cmd: "diff" in cmd)
    assert clg.changed_files(tmp_path, "fork/main", run) is None


def test_changed_files_includes_uncommitted_and_untracked(tmp_path):
    # MED1: an EMPTY committed (three-dot) diff but a dirty worktree must
    # still surface the unstaged edits AND the untracked files — otherwise a
    # worker that never committed sails past the gate.
    run = FakeRun(
        diff_lines=[],  # committed: nothing
        worktree_diff_lines=["hermes_cli/edited.py"],  # uncommitted tracked edit
        untracked_lines=["hermes_cli/brand_new.py"],  # untracked new file
    )
    out = clg.changed_files(tmp_path, "fork/main", run)
    assert out == ["hermes_cli/edited.py", "hermes_cli/brand_new.py"]


def test_changed_files_unions_and_dedups_all_three_sources(tmp_path):
    # A path appearing in more than one source is reported once, first-seen.
    run = FakeRun(
        diff_lines=["a.py", "shared.py"],
        worktree_diff_lines=["shared.py", "b.py"],
        untracked_lines=["c.py", "a.py"],
    )
    out = clg.changed_files(tmp_path, "fork/main", run)
    assert out == ["a.py", "shared.py", "b.py", "c.py"]


def test_changed_files_failopen_only_on_committed_diff(tmp_path):
    # The committed diff is authoritative: if IT fails the call fails open,
    # regardless of what the best-effort sources would have returned.
    run = FakeRun(diff_rc=128, worktree_diff_lines=["x.py"], untracked_lines=["y.py"])
    assert clg.changed_files(tmp_path, "fork/main", run) is None


# ──────────────────────────────────────────────────────────────────────
# map_tests
# ──────────────────────────────────────────────────────────────────────


def test_map_tests_mirror_and_existing(tmp_path):
    _touch(tmp_path / "tests/hermes_cli/test_x.py")
    _touch(tmp_path / "tests/test_cli.py")
    changed = [
        "hermes_cli/x.py",        # mirror exists -> included
        "hermes_cli/missing.py",  # no mirror -> excluded
        "cli.py",                 # top-level mirror tests/test_cli.py exists
        "hermes_cli/__init__.py", # skipped
        "docs/readme.md",         # not py
    ]
    out = clg.map_tests(changed, tmp_path)
    assert out == ["tests/hermes_cli/test_x.py", "tests/test_cli.py"]


def test_map_tests_includes_changed_test_files(tmp_path):
    _touch(tmp_path / "tests/hermes_cli/test_z.py")
    _touch(tmp_path / "tests/hermes_cli/conftest.py")
    changed = ["tests/hermes_cli/test_z.py", "tests/hermes_cli/conftest.py"]
    out = clg.map_tests(changed, tmp_path)
    # Only test_*.py files are run as targets (not conftest helpers).
    assert out == ["tests/hermes_cli/test_z.py"]


def test_map_tests_never_returns_paths_outside_tests(tmp_path):
    # Even a source file whose mirror happens to exist resolves to a
    # tests/ path; nothing else can sneak in.
    _touch(tmp_path / "tests/agent/test_thing.py")
    out = clg.map_tests(["agent/thing.py", "agent/other.py"], tmp_path)
    assert out == ["tests/agent/test_thing.py"]
    assert all(p.startswith("tests/") for p in out)


# ──────────────────────────────────────────────────────────────────────
# run_ruff
# ──────────────────────────────────────────────────────────────────────


def test_run_ruff_parses_plw1514(tmp_path):
    payload = json.dumps(
        [
            {
                "code": "PLW1514",
                "filename": str(tmp_path / "hermes_cli/x.py"),
                "location": {"row": 12},
                "message": "`open` in text mode without explicit `encoding`",
            },
            {"code": "E501", "filename": "other.py", "location": {"row": 1}, "message": "x"},
        ]
    )
    run = FakeRun(ruff_json=payload, ruff_rc=1)
    out = clg.run_ruff(["hermes_cli/x.py"], tmp_path, run, timeout=30)
    assert out is not None
    assert len(out) == 1  # only the PLW1514 entry
    assert "PLW1514" in out[0]
    assert "hermes_cli/x.py:12" in out[0]


def test_run_ruff_clean(tmp_path):
    run = FakeRun(ruff_json="[]", ruff_rc=0)
    assert clg.run_ruff(["hermes_cli/x.py"], tmp_path, run, timeout=30) == []


def test_run_ruff_empty_file_list_skips(tmp_path):
    run = FakeRun()
    assert clg.run_ruff([], tmp_path, run, timeout=30) == []
    assert run.calls == []  # ruff never invoked


def test_run_ruff_fail_open_on_bad_json(tmp_path):
    run = FakeRun(ruff_json="not json", ruff_rc=0)
    assert clg.run_ruff(["x.py"], tmp_path, run, timeout=30) is None


def test_run_ruff_fail_open_on_exception(tmp_path):
    run = FakeRun(raise_on=lambda cmd: "check" in cmd)
    assert clg.run_ruff(["x.py"], tmp_path, run, timeout=30) is None


# ──────────────────────────────────────────────────────────────────────
# _failed_ids
# ──────────────────────────────────────────────────────────────────────


def test_failed_ids_parses_failed_and_error():
    out = (
        "FAILED tests/hermes_cli/test_x.py::test_a - AssertionError: nope\n"
        "ERROR tests/hermes_cli/test_x.py::test_b\n"
        "1 passed, 2 failed in 0.3s\n"
    )
    ids = clg._failed_ids(out)
    assert ids == {
        "tests/hermes_cli/test_x.py::test_a",
        "tests/hermes_cli/test_x.py::test_b",
    }


# ──────────────────────────────────────────────────────────────────────
# run() — orchestration
# ──────────────────────────────────────────────────────────────────────


def _wt(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt


def _stub_base(monkeypatch, base_dir):
    monkeypatch.setattr(clg, "_provision_base_worktree", lambda *a, **k: base_dir)
    monkeypatch.setattr(clg, "_cleanup_base_worktree", lambda *a, **k: None)


def test_run_no_changes_passes(tmp_path):
    wt = _wt(tmp_path)
    run = FakeRun(diff_lines=[])
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is True
    assert res.passed is True


def test_run_all_green_passes(tmp_path):
    wt = _wt(tmp_path)
    _touch(wt / "tests/hermes_cli/test_x.py")
    run = FakeRun(diff_lines=["hermes_cli/x.py"])  # mapped test green by default
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is True
    assert res.passed is True
    # pytest only on the mapped tests/ file.
    assert run.pytest_calls == [(str(wt), "tests/hermes_cli/test_x.py")]


def test_run_ruff_violation_fails(tmp_path):
    wt = _wt(tmp_path)
    _touch(wt / "tests/hermes_cli/test_x.py")
    _touch(wt / "hermes_cli/x.py")
    payload = json.dumps(
        [
            {
                "code": "PLW1514",
                "filename": str(wt / "hermes_cli/x.py"),
                "location": {"row": 3},
                "message": "missing encoding",
            }
        ]
    )
    run = FakeRun(diff_lines=["hermes_cli/x.py"], ruff_json=payload, ruff_rc=1)
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is True
    assert res.passed is False
    assert res.ruff_violations
    assert "PLW1514" in res.report


def test_run_newly_red_test_fails(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    base = tmp_path / "base"
    _touch(wt / "tests/hermes_cli/test_x.py")
    _touch(base / "tests/hermes_cli/test_x.py")
    _stub_base(monkeypatch, base)

    pytest_map = {
        # red at HEAD
        (str(wt), "tests/hermes_cli/test_x.py"): (
            1,
            "FAILED tests/hermes_cli/test_x.py::test_new - boom\n1 failed\n",
        ),
        # green at base -> the failure is NEW
        (str(base), "tests/hermes_cli/test_x.py"): (0, "1 passed\n"),
    }
    run = FakeRun(diff_lines=["hermes_cli/x.py"], pytest_map=pytest_map)
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is True
    assert res.passed is False
    assert "tests/hermes_cli/test_x.py::test_new" in res.tests_red


def test_run_preexisting_flake_suppressed(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    base = tmp_path / "base"
    _touch(wt / "tests/hermes_cli/test_x.py")
    _touch(base / "tests/hermes_cli/test_x.py")
    _stub_base(monkeypatch, base)

    same_failure = "FAILED tests/hermes_cli/test_x.py::test_flaky - flake\n1 failed\n"
    pytest_map = {
        (str(wt), "tests/hermes_cli/test_x.py"): (1, same_failure),
        # SAME failure at base -> pre-existing flake, must be subtracted.
        (str(base), "tests/hermes_cli/test_x.py"): (1, same_failure),
    }
    run = FakeRun(diff_lines=["hermes_cli/x.py"], pytest_map=pytest_map)
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is True
    assert res.passed is True
    assert res.tests_red == []


def test_run_new_test_file_not_at_base_counts_all_red(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    base = tmp_path / "base"
    base.mkdir()
    _touch(wt / "tests/hermes_cli/test_brand_new.py")
    # NOT created under base -> file is new, all failures are new.
    _stub_base(monkeypatch, base)

    pytest_map = {
        (str(wt), "tests/hermes_cli/test_brand_new.py"): (
            1,
            "FAILED tests/hermes_cli/test_brand_new.py::test_a - x\n1 failed\n",
        ),
    }
    run = FakeRun(
        diff_lines=["tests/hermes_cli/test_brand_new.py"], pytest_map=pytest_map
    )
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is True
    assert res.passed is False
    assert "tests/hermes_cli/test_brand_new.py::test_a" in res.tests_red


def test_run_fail_open_on_diff_error(tmp_path):
    wt = _wt(tmp_path)
    run = FakeRun(diff_rc=128)
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is False


def test_run_fail_open_on_base_provision_failure(tmp_path, monkeypatch):
    wt = _wt(tmp_path)
    _touch(wt / "tests/hermes_cli/test_x.py")
    monkeypatch.setattr(clg, "_provision_base_worktree", lambda *a, **k: None)
    pytest_map = {
        (str(wt), "tests/hermes_cli/test_x.py"): (
            1,
            "FAILED tests/hermes_cli/test_x.py::test_a - x\n1 failed\n",
        ),
    }
    run = FakeRun(diff_lines=["hermes_cli/x.py"], pytest_map=pytest_map)
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is False


def test_run_unattributable_head_error_fail_open(tmp_path):
    wt = _wt(tmp_path)
    _touch(wt / "tests/hermes_cli/test_x.py")
    # rc=2 (interrupted) with no parseable FAILED ids -> cannot attribute.
    pytest_map = {
        (str(wt), "tests/hermes_cli/test_x.py"): (2, "INTERNALERROR boom\n"),
    }
    run = FakeRun(diff_lines=["hermes_cli/x.py"], pytest_map=pytest_map)
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is False


def test_run_require_mapped_tests_fails_when_no_test(tmp_path):
    wt = _wt(tmp_path)
    # changed source but NO mirror test exists.
    run = FakeRun(diff_lines=["hermes_cli/orphan.py"])
    res = clg.run(wt, "fork/main", run=run, require_mapped_tests=True)
    assert res.ran is True
    assert res.passed is False
    assert "require_mapped_tests" in res.report


def test_run_never_invokes_pytest_outside_tests(tmp_path):
    wt = _wt(tmp_path)
    _touch(wt / "tests/hermes_cli/test_y.py")
    _touch(wt / "tests/hermes_cli/test_z.py")
    changed = [
        "hermes_cli/y.py",          # mirror exists
        "scripts/foo.py",           # mirror does NOT exist
        "docs/readme.md",           # not py
        "tests/hermes_cli/test_z.py",
    ]
    run = FakeRun(diff_lines=changed)
    res = clg.run(wt, "fork/main", run=run)
    assert res.ran is True
    # Every pytest target lives under tests/.
    assert run.pytest_calls
    for _cwd, target in run.pytest_calls:
        assert target.startswith("tests/")


def test_run_unsupported_map_strategy_fail_open(tmp_path):
    wt = _wt(tmp_path)
    run = FakeRun(diff_lines=["hermes_cli/x.py"])
    res = clg.run(wt, "fork/main", run=run, map_strategy="ast")
    assert res.ran is False


# ──────────────────────────────────────────────────────────────────────
# base worktree — merge-base baseline (flake-safe delta)
# ──────────────────────────────────────────────────────────────────────


def test_provision_base_worktree_uses_merge_base(tmp_path):
    # The flake baseline must be checked out at the base_ref↔HEAD MERGE-BASE
    # (matching the three-dot committed diff), NOT the base_ref tip.
    run = FakeRun(merge_base="abc123def456")
    base_dir = clg._provision_base_worktree(tmp_path, "fork/main", run, timeout=30)
    try:
        assert base_dir is not None
        # merge-base was resolved against HEAD.
        mb_calls = [c[0] for c in run.calls if "merge-base" in c[0]]
        assert mb_calls
        assert mb_calls[0][-2:] == ["fork/main", "HEAD"]
        # The detached worktree was added at the resolved SHA, not "fork/main".
        add_calls = [
            c[0] for c in run.calls if "worktree" in c[0] and "add" in c[0]
        ]
        assert add_calls
        assert add_calls[0][-1] == "abc123def456"
    finally:
        clg._rmtree(base_dir)


def test_provision_base_worktree_falls_back_to_base_ref(tmp_path):
    # merge-base unresolvable (empty stdout) -> fall back to the base_ref tip
    # rather than provisioning at a bogus SHA.
    run = FakeRun(merge_base="")
    base_dir = clg._provision_base_worktree(tmp_path, "fork/main", run, timeout=30)
    try:
        add_calls = [
            c[0] for c in run.calls if "worktree" in c[0] and "add" in c[0]
        ]
        assert add_calls
        assert add_calls[0][-1] == "fork/main"
    finally:
        clg._rmtree(base_dir)
