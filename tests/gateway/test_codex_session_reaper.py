"""Tests for gateway.codex_session_reaper (DISP-4 / ARCH-4, built dark)."""

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


def _make_worktree(path: Path, *, dirty: bool, commit_days_ago: float | None) -> Path:
    """Create a real git repo. If commit_days_ago is set, make a back-dated commit.

    ``dirty`` leaves an untracked file so ``git status --porcelain`` is non-empty.
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
    broker = broker or MagicMock()
    return CodexSessionReaper(
        dispatcher_state=disp,
        broker=broker,
        gh_open_branches_fn=lambda: open_branches if open_branches is not None else set(),
    )


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
    """Idle (primary) + clean + no open PR -> broker.release + row deleted."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "released"
    broker.release.assert_called_once_with("a")
    # row removed from state
    assert "t-a" not in disp._load_state()["sessions"]


def test_created_at_fallback_fires_when_last_message_recent_no_commits(tmp_path):
    """last_message_at recent but created_at old AND no commits since -> released.

    This is the load-bearing fallback: a chatty-but-dead zombie whose
    last_message_at keeps refreshing must still be reclaimable.
    """
    # Worktree commit predates created_at, so there are NO commits *since* created_at.
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=40)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(1), created_at=_iso(30))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "released"
    assert "fallback" in out[0]["idle_reason"]
    broker.release.assert_called_once_with("a")
    assert "t-a" not in disp._load_state()["sessions"]


def test_created_at_fallback_does_not_fire_when_commits_since(tmp_path):
    """created_at old but a recent commit exists -> fallback must NOT fire -> skipped."""
    # Commit is NEWER than created_at -> there IS progress since creation.
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=1)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(2), created_at=_iso(30))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "skipped"
    broker.release.assert_not_called()


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
    """If broker.release raises, the row is ORPHANED (not silently deleted)."""
    wt = _make_worktree(tmp_path / "wt", dirty=False, commit_days_ago=20)
    rows = {"t-a": _row("a", wt, last_message_at=_iso(20), created_at=_iso(20))}
    disp = _FakeDispatcher(tmp_path, rows)
    broker = MagicMock()
    broker.release.side_effect = RuntimeError("worktree locked")
    out = _reaper(disp, broker).reap(reap_idle_days=10, dry_run=False)

    assert out[0]["outcome"] == "orphaned"
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
        ("", False),
        ("abc123 progress\n", True),
        (None, True),
    ],
)
def test_has_commits_since_uses_log_and_fails_safe(tmp_path, git_stdout, expected):
    wt = tmp_path / "wt"
    wt.mkdir()
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    since = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    calls = []

    def fake_git(worktree: Path, *args: str):
        calls.append((worktree, args))
        return git_stdout

    reaper._git = fake_git

    assert reaper._has_commits_since(wt, since) is expected
    assert calls == [
        (
            wt,
            ("log", "--since=2026-06-23T12:00:00", "--oneline", "-n", "1"),
        )
    ]


def test_has_commits_since_missing_worktree_assumes_progress(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    reaper._git = MagicMock(return_value="")

    assert reaper._has_commits_since(tmp_path / "missing", datetime.now(timezone.utc)) is True
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


def test_safe_open_branches_returns_set_success(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = CodexSessionReaper(disp, MagicMock(), lambda: {"codex/s1/task"})

    assert reaper._safe_open_branches() == {"codex/s1/task"}


def test_safe_open_branches_coerces_list(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = CodexSessionReaper(disp, MagicMock(), lambda: ["codex/s1/task", "codex/s2/task"])

    assert reaper._safe_open_branches() == {"codex/s1/task", "codex/s2/task"}


def test_safe_open_branches_coerces_none_to_empty_set(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = CodexSessionReaper(disp, MagicMock(), lambda: None)

    assert reaper._safe_open_branches() == set()


def test_safe_open_branches_returns_empty_set_on_lookup_error(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})

    def boom():
        raise RuntimeError("gh unavailable")

    reaper = CodexSessionReaper(disp, MagicMock(), boom)

    assert reaper._safe_open_branches() == set()


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


def test_idle_reason_fires_at_exact_reap_idle_days_boundary(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    row = {"last_message_at": (now - timedelta(days=10)).isoformat()}

    assert reaper._idle_reason(row, None, now, reap_idle_days=10).startswith(
        "last_message_at idle 10.0d >= 10d"
    )


def test_idle_reason_does_not_fire_just_below_reap_idle_days_boundary(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    row = {"last_message_at": (now - timedelta(days=10) + timedelta(seconds=1)).isoformat()}

    assert reaper._idle_reason(row, None, now, reap_idle_days=10) is None


def test_idle_reason_fires_just_above_reap_idle_days_boundary(tmp_path):
    disp = _FakeDispatcher(tmp_path, {})
    reaper = _reaper(disp)
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    row = {"last_message_at": (now - timedelta(days=10, seconds=1)).isoformat()}

    assert reaper._idle_reason(row, None, now, reap_idle_days=10).startswith(
        "last_message_at idle 10.0d >= 10d"
    )


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
    assert open_pr_out[0]["uncommitted_work"] is False
    assert open_pr_out[0]["in_open_pr"] is True
    assert open_pr_out[0]["outcome"] == "orphaned"
    assert open_pr_out[0]["outcome"] != "released"

    no_pr_reaper = _reaper(disp, open_branches=set())
    no_pr_reaper._ledger_path = tmp_path / "no-pr-ledger.jsonl"
    no_pr_reaper._git = lambda worktree, *args: ""

    no_pr_out = no_pr_reaper.reap(reap_idle_days=10, dry_run=True)

    assert no_pr_out[0]["in_open_pr"] is False
    assert no_pr_out[0]["outcome"] == "released"
