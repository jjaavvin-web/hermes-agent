"""Tests for gateway.codex_session_reaper (DISP-4 / ARCH-4, armed by C7).

Updated for the C7 / Gate 7 contract.  What changed under the tests here:

* the release path writes a RELEASED **tombstone** and keeps the row; it no
  longer pops it out of the registry;
* ``broker.release_nonforce`` replaces the force ``broker.release``;
* the idle window is expressed in **hours** (``reap_idle_days`` survives as an
  explicit keyword alias, which most of these tests still use);
* the ``created_at``-plus-no-commits-since fallback is gone.  ``created_at`` is
  now consulted only when ``last_message_at`` is absent, and whether work would
  be lost is decided by unique-commit custody instead;
* ``_has_commits_since`` was replaced by ``_branch_only_commits``
  (``git rev-list HEAD --not --remotes``) and ``_safe_open_branches`` by
  ``_open_branches``, which returns ``(branches, lookup_ok)`` so the caller can
  fail **closed**.

The broader C7 contract (tombstone ordering, no-resurrection, fail-closed PR
lookup, process-owner gate, registry GC) is covered in
``test_codex_reaper_repair.py``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.codex_session_reaper import CodexSessionReaper, _parse_iso


# --------------------------------------------------------------------------- #
# fakes / helpers
# --------------------------------------------------------------------------- #
class _FakeDispatcher:
    """Round-trips state through real JSON I/O, like the production dispatcher."""

    def __init__(self, hermes_home: Path, rows: dict) -> None:
        self.hermes_home = hermes_home
        self._sessions_path = hermes_home / "codex_sessions.json"
        self._sessions_path.write_text(
            json.dumps({"version": 1, "sessions": rows}), encoding="utf-8"
        )

    def _load_state(self) -> dict:
        return json.loads(self._sessions_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict) -> None:
        self._sessions_path.write_text(json.dumps(state), encoding="utf-8")


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _git(worktree: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True, text=True, check=True,
    )


def _make_worktree(
    path: Path,
    *,
    dirty: bool,
    commit_days_ago: float | None,
    pushed: bool = True,
) -> Path:
    """Create a real git repo. If commit_days_ago is set, make a back-dated commit.

    ``dirty`` leaves an untracked file so ``git status --porcelain`` is non-empty.

    ``pushed`` (C7) mirrors the commit into a **local bare** remote beside the
    repo — no network — so ``git rev-list HEAD --not --remotes`` is empty and the
    unique-commit custody gate can clear.  Pass ``pushed=False`` to model work
    that exists only on this branch.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    if commit_days_ago is not None:
        (path / "seed.txt").write_text("seed", encoding="utf-8")
        _git(path, "add", "seed.txt")
        when = (
            datetime.now(timezone.utc) - timedelta(days=commit_days_ago)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", "seed",
             "--date", when],
            capture_output=True, text=True, check=True,
            env={"GIT_COMMITTER_DATE": when, **_base_env()},
        )
        if pushed:
            origin = path.parent / f"{path.name}-origin.git"
            subprocess.run(
                ["git", "init", "-q", "--bare", str(origin)],
                capture_output=True, text=True, check=True,
            )
            _git(path, "remote", "add", "origin", str(origin))
            _git(path, "push", "-q", "origin", "HEAD")
            _git(path, "fetch", "-q", "origin")
    if dirty:
        (path / "uncommitted.txt").write_text("WIP", encoding="utf-8")
    return path


def _base_env() -> dict:
    import os
    return dict(os.environ)


def _row(sid: str, worktree: Path, *, state: str = "EXECUTING",
         last_message_at: str | None = None, created_at: str | None = None,
         isa_slug: str = "task") -> dict:
    return {
        "session_id": sid,
        "thread_id": f"t-{sid}",
        "worktree_path": str(worktree),
        "state": state,
        "isa_slug": isa_slug,
        "last_message_at": last_message_at,
        "created_at": created_at,
    }


def _reaper(disp, broker=None, open_branches=None) -> CodexSessionReaper:
    """Reaper with the C7 ownership probes stubbed to "nobody owns this".

    ``_scan_process_owners`` walks the real ``/proc`` and ``_tmux_owner`` shells
    out to ``tmux``; neither is the subject of this file, and both would make
    every release-path test depend on whatever else the host happens to be
    running.  ``test_codex_reaper_repair.py`` exercises them directly.
    """
    broker = broker or MagicMock()
    reaper = CodexSessionReaper(
        dispatcher_state=disp,
        broker=broker,
        gh_open_branches_fn=lambda: open_branches if open_branches is not None else set(),
    )
    reaper._scan_process_owners = lambda worktrees: ({}, True)
    reaper._tmux_owner = lambda row, sid: (None, True)
    return reaper


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_idle_below_threshold_is_skipped(tmp_path):
    """last_message_at recent + created_at recent -> skipped, no teardown."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=1)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(2), created_at=_iso(3))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert len(out) == 1
    assert out[0]["outcome"] == "skipped"
    broker.release.assert_not_called()
    # row untouched
    assert disp._load_state()["sessions"]["t-a"]["state"] == "EXECUTING"


def test_uncommitted_work_orphans(tmp_path):
    """Idle + dirty worktree -> ORPHANED, never released."""
    wt = _make_worktree(tmp_path / "wt", dirty=True, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "orphaned"
    assert out[0]["uncommitted_work"] is True
    broker.release.assert_not_called()
    row = disp._load_state()["sessions"]["t-a"]
    assert row["state"] == "ORPHANED"
    assert "orphaned_reason" in row


def test_open_pr_orphans(tmp_path):
    """Idle + clean worktree but branch has an open PR -> ORPHANED."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20),
                        isa_slug="feat")}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker, open_branches={"codex/a/feat"}).reap(
        reap_idle_days=10, dry_run=False
    )

    assert out[0]["outcome"] == "orphaned"
    assert out[0]["in_open_pr"] is True
    broker.release.assert_not_called()
    assert disp._load_state()["sessions"]["t-a"]["state"] == "ORPHANED"


def test_clean_and_idle_releases(tmp_path):
    """Idle (primary) + clean + custody + no open PR -> non-force release.

    C7: the row is NOT deleted.  It becomes a RELEASED tombstone carrying a
    custody receipt, which is what stops ``discover_threads`` re-materialising
    the session on the next restart.
    """
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "released"
    broker.release_nonforce.assert_called_once_with("a")
    broker.release.assert_not_called()
    row = disp._load_state()["sessions"]["t-a"]
    assert row["state"] == "RELEASED"
    assert row["release_receipt"]["branch_only_commits"] == []


def test_created_at_is_used_only_when_no_message_was_ever_received(tmp_path):
    """No last_message_at + old created_at -> idle on the created_at clock.

    C7 replaced the pre-C7 "created_at old AND no commits since" fallback: that
    conflated "made no commits" with "holds nothing worth keeping".  ``created_at``
    now only covers a row that never received a message at all.
    """
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=40)
    rows = {"t-a": _row("a", wt, last_message_at=None, created_at=_iso(30))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "released"
    assert "no message ever received" in out[0]["idle_reason"]
    broker.release_nonforce.assert_called_once_with("a")
    assert disp._load_state()["sessions"]["t-a"]["state"] == "RELEASED"


def test_recent_message_beats_an_ancient_created_at(tmp_path):
    """A chatty row is not idle, however old created_at is -> skipped."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=1)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(2), created_at=_iso(30))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "skipped"
    broker.release_nonforce.assert_not_called()
    broker.release.assert_not_called()


def test_unpushed_commits_orphan_instead_of_releasing(tmp_path):
    """C7 custody gate: a commit on no remote is work that would be lost."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20, pushed=False)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "orphaned"
    assert len(out[0]["branch_only_commits"]) == 1
    broker.release_nonforce.assert_not_called()
    broker.release.assert_not_called()
    assert disp._load_state()["sessions"]["t-a"]["state"] == "ORPHANED"


def test_dry_run_performs_no_teardown(tmp_path):
    """dry_run=True still ledgers a 'released' decision but mutates nothing."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    reaper = _reaper(disp, broker)
    # point ledger into tmp
    reaper._ledger_path = tmp_path / "state" / "codex-reaper" / "reap-ledger.jsonl"

    out = reaper.reap(reap_idle_days=10, dry_run=True)

    assert out[0]["outcome"] == "released"
    assert out[0]["dry_run"] is True
    broker.release_nonforce.assert_not_called()
    broker.release.assert_not_called()
    # state untouched
    assert disp._load_state()["sessions"]["t-a"]["state"] == "EXECUTING"
    # ledger written
    assert reaper._ledger_path.exists()
    lines = reaper._ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["outcome"] == "released"


def test_terminal_state_rows_are_ignored(tmp_path):
    """A non-CLAIMED/EXECUTING row is never considered, even if ancient + clean."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=40)
    rows = {"t-a": _row("a", wt, state="DONE",
                        last_message_at=_iso(40), created_at=_iso(40))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out == []
    broker.release.assert_not_called()
    assert disp._load_state()["sessions"]["t-a"]["state"] == "DONE"


def test_release_failure_downgrades_to_orphaned(tmp_path):
    """If the non-force release is refused, the row is ORPHANED, never deleted."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    broker.release_nonforce.side_effect = RuntimeError("worktree locked")
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "orphaned"
    broker.release.assert_not_called()
    row = disp._load_state()["sessions"]["t-a"]
    assert row["state"] == "ORPHANED"


def test_ledger_records_every_decision(tmp_path):
    """Every swept row appends exactly one JSONL line to the ledger."""
    wt1 = _make_worktree(tmp_path / "wt1", dirty=False, commit_days_ago=20)
    wt2 = _make_worktree(tmp_path / "wt2", dirty=True, commit_days_ago=20)
    rows = {
        "t-a": _row("a", wt1, last_message_at=_iso(20), created_at=_iso(20)),
        "t-b": _row("b", wt2, last_message_at=_iso(20), created_at=_iso(20)),
    }
    disp = _FakeDispatcher(tmp_path, rows)
    reaper = _reaper(disp)
    reaper._ledger_path = tmp_path / "ledger.jsonl"

    reaper.reap(reap_idle_days=10, dry_run=True)

    lines = reaper._ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    outcomes = {json.loads(line)["session_id"]: json.loads(line)["outcome"] for line in lines}
    assert outcomes == {"a": "released", "b": "orphaned"}


# --------------------------------------------------------------------------- #
# parsing and small pure helpers
# --------------------------------------------------------------------------- #
def test_parse_iso_accepts_trailing_z_as_utc():
    parsed = _parse_iso("2026-06-23T12:34:56Z")

    assert parsed == datetime(2026, 6, 23, 12, 34, 56, tzinfo=timezone.utc)


def test_parse_iso_assumes_naive_datetime_is_utc():
    parsed = _parse_iso("2026-06-23T12:34:56")

    assert parsed == datetime(2026, 6, 23, 12, 34, 56, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["not-a-date", None, 123])
def test_parse_iso_returns_none_for_invalid_values(value):
    assert _parse_iso(value) is None


def test_branch_for_uses_sid_and_isa_slug(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)

    assert reaper._branch_for({"session_id": "sid1", "isa_slug": "task-a"}) == "codex/sid1/task-a"


def test_branch_for_uses_sid_only_when_isa_slug_missing(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)

    assert reaper._branch_for({"session_id": "sid1"}) == "codex/sid1"


def test_branch_for_returns_empty_without_sid(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)

    assert reaper._branch_for({"isa_slug": "task-a"}) == ""


# --------------------------------------------------------------------------- #
# git-backed gates, with git calls stubbed out
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("git_stdout", "expected"),
    [
        ("", False),
        ("?? untracked.txt\n M changed.py\n", True),
        (None, True),
    ],
)
def test_has_uncommitted_work_uses_porcelain_and_fails_safe(tmp_path, git_stdout, expected):
    wt = tmp_path / "wt"
    wt.mkdir()
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    calls = []

    def fake_git(worktree: Path, *args: str):
        calls.append((worktree, args))
        return git_stdout

    reaper._git = fake_git

    assert reaper._has_uncommitted_work(wt) is expected
    assert calls == [(wt, ("status", "--porcelain"))]


def test_has_uncommitted_work_missing_worktree_is_not_dirty(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    reaper._git = MagicMock(return_value="?? would-not-be-called\n")

    assert reaper._has_uncommitted_work(tmp_path / "missing") is False
    reaper._git.assert_not_called()


@pytest.mark.parametrize(
    ("git_stdout", "expected"),
    [
        ("", ([], True)),
        ("abc123\ndef456\n", (["abc123", "def456"], True)),
        (None, ([], False)),
    ],
)
def test_branch_only_commits_uses_rev_list_and_fails_safe(tmp_path, git_stdout, expected):
    """C7 replacement for ``_has_commits_since``: real custody, not activity."""
    wt = tmp_path / "wt"
    wt.mkdir()
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    calls = []

    def fake_git(worktree: Path, *args: str):
        calls.append((worktree, args))
        return git_stdout

    reaper._git = fake_git

    assert reaper._branch_only_commits(wt) == expected
    assert calls == [(wt, ("rev-list", "HEAD", "--not", "--remotes"))]


def test_branch_only_commits_missing_worktree_is_unprovable(tmp_path):
    """No worktree means no HEAD to prove custody from — never a clean pass."""
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    reaper._git = MagicMock(return_value="")

    assert reaper._branch_only_commits(tmp_path / "missing") == ([], False)
    reaper._git.assert_not_called()


# --------------------------------------------------------------------------- #
# open-PR branch helpers
# --------------------------------------------------------------------------- #
def test_branch_in_open_prs_matches_exact_branch(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)

    assert reaper._branch_in_open_prs("codex/s1/task", "s1", {"codex/s1/task"}) is True


def test_branch_in_open_prs_returns_false_when_absent(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)

    assert reaper._branch_in_open_prs("codex/s1/task", "s1", {"codex/other/task"}) is False


def test_branch_in_open_prs_matches_sid_substring_heuristic(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)

    assert reaper._branch_in_open_prs("renamed", "s1", {"review/s1/downstream"}) is True


def test_branch_in_open_prs_matches_branch_ending_with_sid(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)

    assert reaper._branch_in_open_prs("renamed", "s1", {"review/s1"}) is True


def test_branch_in_open_prs_empty_open_branches_is_false(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)

    assert reaper._branch_in_open_prs("codex/s1/task", "s1", set()) is False


def test_open_branches_returns_set_and_ok_on_success(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = CodexSessionReaper(disp, MagicMock(), lambda: {"codex/s1/task"})

    assert reaper._open_branches() == ({"codex/s1/task"}, True)


def test_open_branches_coerces_list(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = CodexSessionReaper(disp, MagicMock(), lambda: ["codex/s1/task", "codex/s2/task"])

    assert reaper._open_branches() == ({"codex/s1/task", "codex/s2/task"}, True)


def test_open_branches_treats_none_as_a_failed_lookup(tmp_path):
    """C7: ``None`` is not "no open PRs" — it is "we do not know"."""
    disp = _FakeDispatcher(tmp_path, {})
    reaper = CodexSessionReaper(disp, MagicMock(), lambda: None)

    assert reaper._open_branches() == (set(), False)


def test_open_branches_fails_closed_on_lookup_error(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})

    def boom():
        raise RuntimeError("gh unavailable")

    reaper = CodexSessionReaper(disp, MagicMock(), boom)

    assert reaper._open_branches() == (set(), False)


# --------------------------------------------------------------------------- #
# ledger and idle-boundary behavior
# --------------------------------------------------------------------------- #
def test_append_ledger_autocreates_parent_and_appends_sorted_json_lines(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    reaper._ledger_path = tmp_path / "new" / "nested" / "ledger.jsonl"

    reaper._append_ledger({"b": 2, "a": 1})
    reaper._append_ledger({"d": 4, "c": 3})

    lines = reaper._ledger_path.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"a": 1, "b": 2}', '{"c": 3, "d": 4}']
    assert [json.loads(line) for line in lines] == [{"a": 1, "b": 2}, {"c": 3, "d": 4}]


def test_idle_reason_fires_at_the_exact_boundary(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    row = {"last_message_at": (now - timedelta(hours=6)).isoformat()}

    reason, block = reaper._idle_reason(row, now, 6.0)
    assert block is None
    assert reason.startswith("idle 6.0h >= 6.0h")


def test_idle_reason_does_not_fire_just_below_the_boundary(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    row = {"last_message_at": (now - timedelta(hours=6) + timedelta(seconds=1)).isoformat()}

    assert reaper._idle_reason(row, now, 6.0) == (None, None)


def test_idle_reason_fires_just_above_the_boundary(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    row = {"last_message_at": (now - timedelta(hours=6, seconds=1)).isoformat()}

    reason, block = reaper._idle_reason(row, now, 6.0)
    assert block is None
    assert reason.startswith("idle 6.0h >= 6.0h")


# --------------------------------------------------------------------------- #
# load-bearing safety invariants: idle work with WIP/open PR never releases
# --------------------------------------------------------------------------- #
def test_reap_dry_run_orphans_idle_uncommitted_work_but_releases_same_clean_session(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    rows = {"t-safety": _row("safety", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    dirty_reaper = _reaper(disp)
    dirty_reaper._ledger_path = tmp_path / "dirty-ledger.jsonl"
    dirty_reaper._git = lambda worktree, *args: "?? uncommitted.txt\n"

    dirty_out = dirty_reaper.reap(reap_idle_days=10, dry_run=True)

    assert dirty_out[0]["idle_reason"] is not None
    assert dirty_out[0]["uncommitted_work"] is True
    assert dirty_out[0]["outcome"] == "orphaned"
    assert dirty_out[0]["outcome"] != "released"

    clean_reaper = _reaper(disp)
    clean_reaper._ledger_path = tmp_path / "clean-ledger.jsonl"
    clean_reaper._git = lambda worktree, *args: ""

    clean_out = clean_reaper.reap(reap_idle_days=10, dry_run=True)

    assert clean_out[0]["uncommitted_work"] is False
    assert clean_out[0]["outcome"] == "released"


def test_reap_dry_run_orphans_idle_open_pr_branch_but_releases_without_open_pr(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    rows = {
        "t-pr": _row(
            "openpr",
            wt,
            last_message_at=_iso(20),
            created_at=_iso(20),
            isa_slug="feature",
        )
    }
    disp = _FakeDispatcher(tmp_path, rows)
    open_pr_reaper = _reaper(disp, open_branches={"codex/openpr/feature"})
    open_pr_reaper._ledger_path = tmp_path / "open-pr-ledger.jsonl"
    open_pr_reaper._git = lambda worktree, *args: ""

    open_pr_out = open_pr_reaper.reap(reap_idle_days=10, dry_run=True)

    assert open_pr_out[0]["idle_reason"] is not None
    # C7 orders the open-PR guard BEFORE the dirty probe, so the worktree is
    # never even inspected once a PR protects the branch.
    assert "uncommitted_work" not in open_pr_out[0]
    assert open_pr_out[0]["in_open_pr"] is True
    assert open_pr_out[0]["outcome"] == "orphaned"
    assert open_pr_out[0]["outcome"] != "released"

    no_pr_reaper = _reaper(disp, open_branches=set())
    no_pr_reaper._ledger_path = tmp_path / "no-pr-ledger.jsonl"
    no_pr_reaper._git = lambda worktree, *args: ""

    no_pr_out = no_pr_reaper.reap(reap_idle_days=10, dry_run=True)

    assert no_pr_out[0]["in_open_pr"] is False
    assert no_pr_out[0]["outcome"] == "released"
