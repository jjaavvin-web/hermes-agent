"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def _task_rows() -> list[dict[str, object]]:
    with kb.connect_closing() as conn:
        return [kc._task_to_dict(t) for t in kb.list_tasks(conn, limit=100)]


# ---------------------------------------------------------------------------
# Argument parsing / registration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "argv,expected",
    [
        (["kanban", "list"], {"command": "kanban", "kanban_action": "list"}),
        (["kanban", "ls"], {"command": "kanban", "kanban_action": "ls"}),
        (["kanban", "boards", "list"], {"kanban_action": "boards", "boards_action": "list"}),
        (["kanban", "boards", "create", "alpha"], {"kanban_action": "boards", "boards_action": "create", "slug": "alpha"}),
        (["kanban", "specify", "t_deadbeef"], {"kanban_action": "specify", "task_id": "t_deadbeef"}),
        (["kanban", "claim", "t_deadbeef"], {"kanban_action": "claim", "task_id": "t_deadbeef"}),
        (["kanban", "complete", "t_deadbeef", "--result", "done"], {"kanban_action": "complete", "task_ids": ["t_deadbeef"], "result": "done"}),
        (["kanban", "block", "t_deadbeef", "needs", "input"], {"kanban_action": "block", "task_id": "t_deadbeef", "reason": ["needs", "input"]}),
        (["kanban", "unblock", "t_deadbeef"], {"kanban_action": "unblock", "task_ids": ["t_deadbeef"]}),
        (["kanban", "open-pr", "t_deadbeef"], {"kanban_action": "open-pr", "task_id": "t_deadbeef"}),
    ],
)
def test_parser_registers_main_kanban_subcommands(argv, expected):
    args = _build_cli_parser().parse_args(argv)

    for attr, value in expected.items():
        assert getattr(args, attr) == value


def test_parser_rejects_malformed_args_without_dispatching():
    parser = _build_cli_parser()

    with pytest.raises(SystemExit) as missing_task:
        parser.parse_args(["kanban", "open-pr"])
    assert missing_task.value.code == 2

    with pytest.raises(SystemExit) as invalid_status:
        parser.parse_args(["kanban", "list", "--status", "bogus"])
    assert invalid_status.value.code == 2


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_no_args_shows_usage(kanban_home):
    out = kc.run_slash("")
    assert "kanban" in out.lower()
    assert "create" in out.lower() or "subcommand" in out.lower() or "action" in out.lower()


def test_run_slash_create_and_list(kanban_home):
    out = kc.run_slash("create 'ship feature' --assignee alice")
    assert "Created" in out
    out = kc.run_slash("list")
    assert "ship feature" in out
    assert "alice" in out


def test_cli_read_only_commands_use_tmp_board_without_task_mutation(kanban_home, tmp_path):
    """Read-only list/show/context/boards run entirely against tmp HERMES_HOME."""
    workspace = tmp_path / "fixture-workspace"
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="fixture readonly task",
            body="worker-visible body",
            assignee="alice",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        kb.add_comment(conn, tid, "tester", "read-only fixture comment")
        before_comments = [c.body for c in kb.list_comments(conn, tid)]
    before_tasks = _task_rows()

    assert "fixture readonly task" in kc.run_slash("list")
    assert "read-only fixture comment" in kc.run_slash(f"show {tid}")
    assert "worker-visible body" in kc.run_slash(f"context {tid}")
    boards = kc.run_slash("boards list")
    assert "default" in boards

    with kb.connect_closing() as conn:
        after_comments = [c.body for c in kb.list_comments(conn, tid)]
    assert _task_rows() == before_tasks
    assert after_comments == before_comments
    assert str(kanban_home) != "/home/josep/.hermes"


def test_run_slash_create_worktree_path_and_branch(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    target_arg = target.as_posix()
    out = kc.run_slash(
        f"create 'ship worktree' --workspace worktree:{target_arg} --branch wt/t6-wire"
    )
    assert "Created" in out

    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
    task = tasks[0]
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == target_arg
    assert task.branch_name == "wt/t6-wire"


def test_run_slash_rejects_branch_without_worktree(kanban_home):
    out = kc.run_slash("create 'bad branch' --workspace scratch --branch wt/bad")
    assert "--branch is only valid with --workspace worktree" in out


def test_run_slash_create_with_parent_and_cascade(kanban_home):
    # Parent then child via --parent
    out1 = kc.run_slash("create 'parent' --assignee alice")
    # Extract the "t_xxxx" id from "Created t_xxxx (ready, ...)"
    import re
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    p = m.group(1)
    out2 = kc.run_slash(f"create 'child' --assignee bob --parent {p}")
    assert "todo" in out2  # child starts as todo

    # Complete parent; list should promote child to ready
    kc.run_slash(f"complete {p}")
    # Explicit filter: child should now be ready (was todo before complete).
    ready_list = kc.run_slash("list --status ready")
    assert "child" in ready_list


def test_run_slash_show_includes_comments(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    show = kc.run_slash(f"show {tid}")
    assert "performance section" in show


def test_run_slash_comment_max_len_trims_long_body(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} '{'x' * 30}' --max-len 20")
    show = kc.run_slash(f"show {tid}")
    assert "trimmed to 20 chars by --max-len" in show
    assert "x" * 30 not in show


def test_run_slash_block_unblock_cycle(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    # Claim first so block() finds it running
    kc.run_slash(f"claim {tid}")
    assert "Blocked" in kc.run_slash(f"block {tid} 'need decision'")
    assert "Unblocked" in kc.run_slash(f"unblock {tid}")


def test_run_slash_json_output(kanban_home):
    out = kc.run_slash("create 'jsontask' --assignee alice --json")
    payload = json.loads(out)
    assert payload["title"] == "jsontask"
    assert payload["assignee"] == "alice"
    assert payload["status"] == "ready"


def test_run_slash_dispatch_dry_run_counts(kanban_home):
    kc.run_slash("create 'a' --assignee alice")
    kc.run_slash("create 'b' --assignee bob")
    out = kc.run_slash("dispatch --dry-run")
    assert "Spawned:" in out


def test_run_slash_context_output_format(kanban_home):
    out = kc.run_slash("create 'tech spec' --assignee alice --body 'write an RFC'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    ctx = kc.run_slash(f"context {tid}")
    assert "tech spec" in ctx
    assert "write an RFC" in ctx
    assert "performance section" in ctx


def test_run_slash_tenant_filter(kanban_home):
    kc.run_slash("create 'biz-a task' --tenant biz-a --assignee alice")
    kc.run_slash("create 'biz-b task' --tenant biz-b --assignee alice")
    a = kc.run_slash("list --tenant biz-a")
    b = kc.run_slash("list --tenant biz-b")
    assert "biz-a task" in a and "biz-b task" not in a
    assert "biz-b task" in b and "biz-a task" not in b


def test_run_slash_session_filter(kanban_home):
    """`hermes kanban list --session <id>` filters by the originating
    chat session id stamped on tasks created from inside an ACP loop."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="from sess-1 a", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-1 b", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-2", assignee="alice", session_id="sess-2"
        )
        kb.create_task(conn, title="cli only", assignee="alice")
    out_1 = kc.run_slash("list --session sess-1")
    out_2 = kc.run_slash("list --session sess-2")
    assert "from sess-1 a" in out_1
    assert "from sess-1 b" in out_1
    assert "from sess-2" not in out_1
    assert "cli only" not in out_1
    assert "from sess-2" in out_2
    assert "from sess-1 a" not in out_2


def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# open-pr outcome routing
# ---------------------------------------------------------------------------

def test_open_pr_pr_outcome_comments_and_blocks_task(kanban_home, monkeypatch, tmp_path, capsys):
    worktree = tmp_path / "code-wt"
    worktree.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="ship code",
            workspace_kind="dir",
            workspace_path=str(worktree),
        )

    calls = []

    def fake_open_pr(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "mode": "pr",
            "pr_url": "https://github.com/acme/hermes/pull/123",
            "classification": "sensitive",
            "label": "needs-human",
        }

    monkeypatch.setattr("hermes_cli.kanban_pr.open_pr", fake_open_pr)
    monkeypatch.setattr(kc, "_hermes_base_branch", lambda: "relay/work")

    rc = kc.kanban_command(_build_cli_parser().parse_args(["kanban", "open-pr", tid]))

    captured = capsys.readouterr()
    assert rc == 0
    assert "https://github.com/acme/hermes/pull/123" in captured.out
    assert calls[0]["worktree"] == worktree
    assert calls[0]["branch"] == f"kanban/{tid}"
    assert calls[0]["base_branch"] == "relay/work"
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        comments = [c.body for c in kb.list_comments(conn, tid)]
    assert task is not None
    assert task.status == "blocked"
    assert any("PR opened: https://github.com/acme/hermes/pull/123" in c for c in comments)
    assert any("sensitive → needs-human" in c for c in comments)


def test_open_pr_local_branch_outcome_comments_and_blocks_task(kanban_home, monkeypatch, tmp_path):
    worktree = tmp_path / "local-wt"
    worktree.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="local only", workspace_kind="dir", workspace_path=str(worktree))

    monkeypatch.setattr(
        "hermes_cli.kanban_pr.open_pr",
        lambda **kwargs: {"ok": True, "mode": "local-branch", "branch": kwargs["branch"]},
    )
    monkeypatch.setattr(kc, "_hermes_base_branch", lambda: "main")

    rc = kc.kanban_command(_build_cli_parser().parse_args(["kanban", "open-pr", tid]))

    assert rc == 0
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        comments = [c.body for c in kb.list_comments(conn, tid)]
    assert task is not None
    assert task.status == "blocked"
    assert any("local branch `kanban/" in c and "remoteless repo" in c for c in comments)


def test_open_pr_noop_outcome_comments_without_blocking(kanban_home, monkeypatch, tmp_path):
    worktree = tmp_path / "noop-wt"
    worktree.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="nothing changed", workspace_kind="dir", workspace_path=str(worktree))

    monkeypatch.setattr(
        "hermes_cli.kanban_pr.open_pr",
        lambda **kwargs: {"ok": True, "mode": "noop", "note": "no commits ahead"},
    )
    monkeypatch.setattr(kc, "_hermes_base_branch", lambda: "main")

    rc = kc.kanban_command(_build_cli_parser().parse_args(["kanban", "open-pr", tid]))

    assert rc == 0
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        comments = [c.body for c in kb.list_comments(conn, tid)]
    assert task is not None
    assert task.status == "ready"
    assert any("open-pr: no commits ahead" in c for c in comments)


def test_open_pr_error_outcome_comments_blocks_and_returns_nonzero(kanban_home, monkeypatch, tmp_path, capsys):
    worktree = tmp_path / "error-wt"
    worktree.mkdir()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="push fails", workspace_kind="dir", workspace_path=str(worktree))

    monkeypatch.setattr(
        "hermes_cli.kanban_pr.open_pr",
        lambda **kwargs: {"ok": False, "mode": "error", "error": "git push failed"},
    )
    monkeypatch.setattr(kc, "_hermes_base_branch", lambda: "main")

    rc = kc.kanban_command(_build_cli_parser().parse_args(["kanban", "open-pr", tid]))

    captured = capsys.readouterr()
    assert rc == 1
    assert "ERROR git push failed" in captured.err
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        comments = [c.body for c in kb.list_comments(conn, tid)]
    assert task is not None
    assert task.status == "blocked"
    assert any("open-pr failed: git push failed" in c for c in comments)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_cli_unknown_task_id_errors_are_nonzero(kanban_home, capsys):
    rc = kc.kanban_command(_build_cli_parser().parse_args(["kanban", "show", "t_missing"]))

    captured = capsys.readouterr()
    assert rc == 1
    assert "no such task: t_missing" in captured.err


def test_run_slash_open_pr_unknown_task_id_reports_error(kanban_home):
    out = kc.run_slash("open-pr t_missing")

    assert "no such task t_missing" in out


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


