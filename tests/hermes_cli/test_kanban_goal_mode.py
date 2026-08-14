"""Tests for kanban goal_mode — per-card Ralph-style goal loop.

Covers three layers:

1. DB: goal_mode / goal_max_turns persist through create_task + from_row,
   and a legacy DB (without the columns) migrates cleanly.
2. Spawn: _default_spawn sets the HERMES_KANBAN_GOAL_MODE env vars only
   when the card opts in.
3. Loop: goals.run_kanban_goal_loop continuation / completion / budget
   behaviour, driven entirely through injected callbacks (no live model).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import goals


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    home.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli: [file, terminal]\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def test_goal_mode_defaults_off(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="worker")
        task = kb.get_task(conn, tid)
    assert task.goal_mode is False
    assert task.goal_max_turns is None


def test_goal_mode_persists(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="open-ended task",
            assignee="worker",
            goal_mode=True,
            goal_max_turns=7,
        )
        task = kb.get_task(conn, tid)
    assert task.goal_mode is True
    assert task.goal_max_turns == 7


def test_goal_mode_without_max_turns(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", goal_mode=True
        )
        task = kb.get_task(conn, tid)
    assert task.goal_mode is True
    assert task.goal_max_turns is None


def test_legacy_db_migrates_goal_columns(tmp_path, monkeypatch):
    """A tasks table created without goal columns must gain them on init."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal legacy schema: tasks table missing goal_mode / goal_max_turns.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    # init_db runs the additive migration.
    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "goal_mode" in cols
        assert "goal_max_turns" in cols
        task = kb.get_task(conn, "legacy1")
    # Existing row keeps the safe default.
    assert task.goal_mode is False
    assert task.goal_max_turns is None


# ---------------------------------------------------------------------------
# Spawn env
# ---------------------------------------------------------------------------

def test_spawn_sets_goal_env_only_when_enabled(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="goal task",
            assignee="default",
            goal_mode=True,
            goal_max_turns=5,
        )
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert env.get("HERMES_KANBAN_GOAL_MODE") == "1"
    assert env.get("HERMES_KANBAN_GOAL_MAX_TURNS") == "5"


def test_spawn_no_goal_env_for_plain_task(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4243

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="default")
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert "HERMES_KANBAN_GOAL_MODE" not in env
    assert "HERMES_KANBAN_GOAL_MAX_TURNS" not in env


# ---------------------------------------------------------------------------
# Goal loop logic (callback-injected, no live model)
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    """Make judge_goal return a scripted sequence of verdicts."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        # 4-tuple contract: (verdict, reason, parse_failed, wait_directive)
        return v, f"scripted:{v}", False, None

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_loop_stops_when_worker_already_completed(monkeypatch):
    # Worker called kanban_complete on its first turn — no judging needed.
    _patch_judge(monkeypatch, ["continue"])  # should never be consulted
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="done already",
    )
    assert res["outcome"] == "completed_by_worker"
    assert turns == []  # no extra turns


def test_loop_continues_then_worker_completes(monkeypatch):
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t2",
        goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or f"turn{len(turns)}",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="started",
    )
    assert res["outcome"] == "completed_by_worker"
    # Two continuation turns fed before the worker completed.
    assert len(turns) == 2
    assert all("not done yet" in p for p in turns)


def test_loop_blocks_on_budget_exhaustion(monkeypatch):
    _patch_judge(monkeypatch, ["continue"] * 10)
    blocked = {}

    def _block(reason):
        blocked["reason"] = reason

    res = goals.run_kanban_goal_loop(
        task_id="t3",
        goal_text="endless task",
        run_turn=lambda p: "still going",
        task_status_fn=lambda: "running",
        block_fn=_block,
        max_turns=3,
        first_response="turn1",
    )
    assert res["outcome"] == "blocked_budget"
    assert res["turns_used"] == 3
    assert "turn budget" in blocked["reason"].lower()


def test_loop_finalize_nudge_when_judge_done_but_open(monkeypatch):
    # Judge says done, but worker never terminated → one finalize nudge,
    # then worker completes.
    _patch_judge(monkeypatch, ["done", "done"])
    statuses = iter(["running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t4",
        goal_text="task",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="looks done",
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1
    assert "still open" in turns[0]


def test_loop_blocks_when_judge_done_but_never_finalizes(monkeypatch):
    # Judge keeps saying done, worker never calls kanban_complete → block
    # after the single finalize nudge.
    _patch_judge(monkeypatch, ["done", "done"])
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="t5",
        goal_text="task",
        run_turn=lambda p: "still not finalizing",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=10,
        first_response="looks done",
    )
    assert res["outcome"] == "blocked_budget"
    assert "finalize" in blocked["reason"].lower()


def test_loop_stops_if_task_reclaimed(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])
    res = goals.run_kanban_goal_loop(
        task_id="t6",
        goal_text="task",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "archived",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="x",
    )
    assert res["outcome"] == "stopped"


# ---------------------------------------------------------------------------
# Deterministic code gate (A3) — PRIMARY veto, LLM judge demoted to secondary
# ---------------------------------------------------------------------------


def _gate(passed, *, ran=True, report="REPORT", tests_red=None, ruff_violations=None):
    """Build a GateResult-shaped stub."""
    return SimpleNamespace(
        passed=passed,
        ran=ran,
        report=report,
        tests_red=tests_red or [],
        ruff_violations=ruff_violations or [],
    )


def test_loop_code_gate_blocks_done(monkeypatch):
    # Gate FAILS — on the RUNNING turn the loop CONTINUEs with the gate's
    # report and never consults the LLM judge. Then the worker marks the card
    # done while the gate is STILL red: MED2 means the self-completion is
    # REJECTED (blocked_by_code_gate), not accepted.
    judge_calls = []

    def _fake_judge(goal, response, subgoals=None):
        judge_calls.append(1)
        return "done", "judge-would-say-done", False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)

    statuses = iter(["running", "done"])
    turns = []
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="g1",
        goal_text="write code",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=10,
        first_response="claims done",
        code_gate_fn=lambda: _gate(False, report="REPORT", tests_red=["t.py::a"]),
    )
    assert res["outcome"] == "blocked_by_code_gate"
    # One continuation turn was fed on the RUNNING turn before the worker
    # flipped the card to done.
    assert len(turns) == 1
    assert "deterministic code gate failed" in turns[0]
    assert "REPORT" in turns[0]
    # The judge never got a say (veto turn) nor on the rejected completion.
    assert judge_calls == []
    # The completion was rejected via block_fn with the gate report.
    assert "REPORT" in blocked["reason"]


def test_loop_code_gate_passes_lets_judge_finalize(monkeypatch):
    # Gate PASSES (ran, passed) -> judge is the secondary check. Judge says
    # done -> finalize nudge -> worker completes.
    _patch_judge(monkeypatch, ["done", "done"])
    statuses = iter(["running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="g2",
        goal_text="task",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="looks done",
        code_gate_fn=lambda: _gate(True),
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1
    # Finalize template (judge path), NOT the code-gate template.
    assert "still open" in turns[0]
    assert "deterministic code gate" not in turns[0]


def test_loop_code_gate_none_preserves_behavior(monkeypatch):
    # code_gate_fn=None must behave exactly like the pre-gate loop.
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="g3",
        goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or f"turn{len(turns)}",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="started",
        code_gate_fn=None,
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 2
    assert all("not done yet" in p for p in turns)


def test_loop_code_gate_failopen_when_gate_raises(monkeypatch):
    # A gate that raises must fall through to the judge (fail-open), never
    # wedge the loop.
    _patch_judge(monkeypatch, ["continue"])
    statuses = iter(["running", "done"])
    turns = []

    def _boom():
        raise RuntimeError("gate exploded")

    res = goals.run_kanban_goal_loop(
        task_id="g4",
        goal_text="task",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="x",
        code_gate_fn=_boom,
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1
    # Judge continuation template (fail-open path), not the gate template.
    assert "not done yet" in turns[0]


def test_loop_code_gate_not_ran_falls_through_to_judge(monkeypatch):
    # Gate ran=False (fail-open inside the gate) -> judge decides.
    _patch_judge(monkeypatch, ["continue"])
    statuses = iter(["running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="g5",
        goal_text="task",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="x",
        code_gate_fn=lambda: _gate(False, ran=False),
    )
    assert res["outcome"] == "completed_by_worker"
    assert "not done yet" in turns[0]


def test_loop_code_gate_veto_respects_budget(monkeypatch):
    # Gate vetoes forever -> the loop must still block on budget exhaustion,
    # not spin past max_turns.
    monkeypatch.setattr(
        goals, "judge_goal", lambda *a, **k: pytest.fail("judge must not run")
    )
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="g6",
        goal_text="task",
        run_turn=lambda p: "still red",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=3,
        first_response="x",
        code_gate_fn=lambda: _gate(False),
    )
    assert res["outcome"] == "blocked_budget"
    assert res["turns_used"] == 3
    assert "turn budget" in blocked["reason"].lower()


# ---------------------------------------------------------------------------
# MED2 — worker self-completion must NOT bypass the deterministic code gate
# ---------------------------------------------------------------------------


def test_loop_code_gate_blocks_worker_self_completion(monkeypatch):
    # (c) Worker marks the card done on its FIRST turn, but the gate vetoes:
    # the completion is REJECTED (blocked_by_code_gate), no extra turn runs,
    # and the judge is never consulted.
    monkeypatch.setattr(
        goals, "judge_goal", lambda *a, **k: pytest.fail("judge must not run")
    )
    blocked = {}
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="g7",
        goal_text="write code",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: "done",
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=10,
        first_response="claims done",
        code_gate_fn=lambda: _gate(False, report="BOOM", tests_red=["t.py::a"]),
    )
    assert res["outcome"] == "blocked_by_code_gate"
    assert turns == []  # the bogus completion never bought another turn
    assert "BOOM" in blocked["reason"]


def test_loop_code_gate_done_passing_gate_completes(monkeypatch):
    # (d) Worker marks the card done and the gate PASSES -> completion accepted.
    monkeypatch.setattr(
        goals, "judge_goal", lambda *a, **k: pytest.fail("judge must not run")
    )
    res = goals.run_kanban_goal_loop(
        task_id="g8",
        goal_text="task",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="done",
        code_gate_fn=lambda: _gate(True),
    )
    assert res["outcome"] == "completed_by_worker"


def test_loop_code_gate_done_absent_gate_completes():
    # (d) No gate wired -> worker completion accepted exactly as before.
    res = goals.run_kanban_goal_loop(
        task_id="g9",
        goal_text="task",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="done",
        code_gate_fn=None,
    )
    assert res["outcome"] == "completed_by_worker"


def test_loop_code_gate_done_failopen_when_not_ran():
    # (d) Gate ran=False (fail-open inside the gate) on a completed card ->
    # accept the completion rather than wedging it.
    res = goals.run_kanban_goal_loop(
        task_id="g10",
        goal_text="task",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="done",
        code_gate_fn=lambda: _gate(False, ran=False),
    )
    assert res["outcome"] == "completed_by_worker"


def test_loop_code_gate_done_failopen_when_gate_raises():
    # A gate that raises on a completed card must fail open (accept), never
    # wedge or block on infra noise.
    def _boom():
        raise RuntimeError("gate exploded")

    res = goals.run_kanban_goal_loop(
        task_id="g11",
        goal_text="task",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="done",
        code_gate_fn=_boom,
    )
    assert res["outcome"] == "completed_by_worker"
