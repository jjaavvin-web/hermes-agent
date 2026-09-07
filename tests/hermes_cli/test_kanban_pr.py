"""Tests for the kanban code→PR keystone (hermes_cli.kanban_pr.open_pr).

git/gh are fully faked via an injected ``run``; ``classify`` is injected too so
these tests never touch a real repo, remote, or GitHub.
"""
import json
from types import SimpleNamespace

from hermes_cli import kanban_pr


class FakeRun:
    def __init__(self, *, remotes="fork", ahead=2, push_rc=0,
                 slug_url="https://github.com/jjaavvin-web/hermes-agent.git",
                 existing=None, create_rc=0,
                 create_out="https://github.com/jjaavvin-web/hermes-agent/pull/123"):
        self.remotes, self.ahead, self.push_rc = remotes, ahead, push_rc
        self.slug_url, self.existing = slug_url, existing
        self.create_rc, self.create_out = create_rc, create_out
        self.calls = []

    @staticmethod
    def _r(rc=0, out="", err=""):
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "git":
            sub = argv[3]
            if sub == "remote" and len(argv) == 4:
                return self._r(out=self.remotes)
            if sub == "remote" and argv[4] == "get-url":
                return self._r(out=self.slug_url)
            if sub == "rev-list":
                return self._r(out=str(self.ahead))
            if sub == "push":
                return self._r(rc=self.push_rc, err="denied" if self.push_rc else "")
        if argv[0] == "gh" and argv[1] == "pr":
            if argv[2] == "list":
                return self._r(out=json.dumps(self.existing) if self.existing else "")
            if argv[2] == "create":
                return self._r(rc=self.create_rc, out=self.create_out,
                               err="boom" if self.create_rc else "")
            if argv[2] == "edit":
                return self._r()
        return self._r()

    def ran(self, *needles):
        return [c for c in self.calls if all(n in c for n in needles)]


_SENSITIVE = lambda *a: "sensitive"
_SAFE = lambda *a: "safe"


def _open(run, **kw):
    return kanban_pr.open_pr(worktree="/tmp/wt", branch="kanban/t_x",
                             title="t", body="b", run=run, classify=kw.pop("classify", _SENSITIVE), **kw)


def test_remoteless_repo_records_local_branch():
    run = FakeRun(remotes="")  # no remotes → ict-brain-style
    res = _open(run)
    assert res["ok"] and res["mode"] == "local-branch" and res["pr_url"] is None
    assert not run.ran("push")  # never attempted a push


def test_noop_when_no_commits_ahead():
    run = FakeRun(ahead=0)
    res = _open(run)
    assert res["ok"] and res["mode"] == "noop"
    assert not run.ran("push")


def test_push_failure_is_error_not_raise():
    run = FakeRun(push_rc=1)
    res = _open(run)
    assert res["ok"] is False and res["mode"] == "error"
    assert "git push failed" in res["error"]


def test_happy_path_opens_pr_needs_human_for_sensitive():
    run = FakeRun()
    res = _open(run, classify=_SENSITIVE)
    assert res["ok"] and res["mode"] == "pr"
    assert res["pr_url"].endswith("/pull/123") and res["pr_number"] == 123
    assert res["label"] == "needs-human"
    # labelled the PR needs-human
    assert run.ran("gh", "edit", "--add-label", "needs-human")


def test_safe_change_gets_auto_merge_label():
    run = FakeRun()
    res = _open(run, classify=_SAFE)
    assert res["label"] == "auto-merge"
    assert run.ran("gh", "edit", "--add-label", "auto-merge")


def test_idempotent_reuses_existing_pr():
    run = FakeRun(existing=[{"number": 99, "url": "https://github.com/x/y/pull/99"}])
    res = _open(run)
    assert res["ok"] and res["pr_number"] == 99
    assert not run.ran("create")  # did NOT create a duplicate
