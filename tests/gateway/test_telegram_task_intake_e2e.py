"""End-to-end tests: Telegram /task command → kanban-DB pipeline.

These tests exercise the full parser-to-DB path without starting the
gateway process or requiring a real Telegram bot.

Architecture
------------
``process_telegram_task`` is a free function that mirrors the core logic
of ``_handle_task_command`` in ``gateway/run.py`` lines 8813-8887.
It receives an open SQLite connection directly (instead of opening its
own via ``_kb.connect()``) so every test works with an isolated in-process
DB and can inspect state after each call.

If ``_handle_task_command`` changes, update this harness to match.

The fixture style mirrors ``tests/hermes_cli/test_kanban_db.py``:
a ``kanban_home`` fixture points ``HERMES_HOME`` at a ``tmp_path``
subdirectory and patches ``Path.home`` so no live board is touched.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as _kb
from gateway.telegram_grammar import (
    parse_task_command,
    normalize_title,
    build_idempotency_key,
    format_syntax_error,
    format_ack,
    format_conflict,
    LANE_HEAVY,
    LANE_LIGHT,
    LANE_ASSIGNEE,
)


# ---------------------------------------------------------------------------
# In-process harness  (mirrors gateway/run.py _handle_task_command L8813-8887)
# ---------------------------------------------------------------------------

def process_telegram_task(
    text: str,
    *,
    chat_id: str,
    user_id: str,
    conn,
    platform_str: str = "telegram",
    thread_id: str = "",
    now_epoch: int | None = None,
) -> str:
    """Parse a /task command and persist the card into ``conn``.

    Mirrors ``_handle_task_command`` minus the ``asyncio.to_thread``
    wrapper and ``self.*`` references. Pass ``conn`` in directly; the
    function does NOT open or close it.

    SOURCE: gateway/run.py _handle_task_command (lines 8813-8887).
    """
    text = (text or "").strip()
    parsed = parse_task_command(text)

    if not parsed["ok"]:
        return format_syntax_error(parsed)

    chat_id = (chat_id or "").strip()
    thread_id = (thread_id or "").strip()
    user_id = (user_id or "").strip() or None

    normalized = normalize_title(parsed["description"])
    epoch = now_epoch if now_epoch is not None else int(time.time())
    idempotency_key = build_idempotency_key(
        normalized_title=normalized,
        chat_id=chat_id,
        now_epoch=epoch,
    )

    # Race-free dedup marker — unique per call, compared post-create.
    created_by_marker = f"telegram:{user_id or chat_id}:{time.time_ns()}"

    task_id = _kb.create_task(
        conn,
        title=parsed["description"],
        assignee=parsed["assignee"],
        priority=parsed["priority"],
        idempotency_key=idempotency_key,
        created_by=created_by_marker,
    )

    row = conn.execute(
        "SELECT created_by FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    stored_marker = row["created_by"] if row else None
    is_duplicate = (stored_marker != created_by_marker)

    if not is_duplicate and platform_str and chat_id:
        try:
            _kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform=platform_str,
                chat_id=chat_id,
                thread_id=thread_id or None,
                user_id=user_id,
                notifier_profile=None,
            )
            # Seed cursor above the `created` event we just acked, so
            # the watcher does not re-deliver it on the next tick.
            max_row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m "
                "FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            seed_id = int(max_row["m"]) if max_row else 0
            if seed_id > 0:
                with _kb.write_txn(conn):
                    conn.execute(
                        "UPDATE kanban_notify_subs "
                        "SET last_event_id = ? "
                        "WHERE task_id = ? AND platform = ? "
                        "AND chat_id = ? AND thread_id = ?",
                        (seed_id, task_id, platform_str,
                         chat_id, thread_id or ""),
                    )
        except Exception:
            pass  # non-fatal — mirror the gateway's warning-only handling

    if is_duplicate:
        return format_conflict(existing_task_id=task_id, parsed=parsed)
    return format_ack(task_id=task_id, parsed=parsed)


# ---------------------------------------------------------------------------
# Fixtures — same pattern as tests/hermes_cli/test_kanban_db.py
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    """Open connection to the isolated kanban DB; closed after the test."""
    c = _kb.connect()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Scenario 1: heavy lane, default priority (p2 / 200)
# ---------------------------------------------------------------------------

def test_heavy_lane_ack_contains_inbox_emoji(conn):
    """Response must start with the 📥 inbox emoji."""
    result = process_telegram_task(
        "/task heavy: build the new auth flow",
        chat_id="c1", user_id="u1", conn=conn,
    )
    assert "📥" in result


def test_heavy_lane_ack_contains_chip(conn):
    """Response must contain 'heavy/p2' lane+priority chip."""
    result = process_telegram_task(
        "/task heavy: build the new auth flow",
        chat_id="c1", user_id="u1", conn=conn,
    )
    assert "heavy/p2" in result


def test_heavy_lane_ack_contains_task_id(conn):
    """Response must contain the newly created task id."""
    result = process_telegram_task(
        "/task heavy: build the new auth flow",
        chat_id="c1", user_id="u1", conn=conn,
    )
    # task ids start with 't_'
    assert "t_" in result


def test_heavy_lane_card_assignee(conn):
    """Card assignee must be claude-code-coder for the heavy lane."""
    process_telegram_task(
        "/task heavy: build the new auth flow",
        chat_id="c1", user_id="u1", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].assignee == LANE_ASSIGNEE[LANE_HEAVY]


def test_heavy_lane_card_priority(conn):
    """Card priority is 200 (p2 default — no explicit #pN tag)."""
    process_telegram_task(
        "/task heavy: build the new auth flow",
        chat_id="c1", user_id="u1", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].priority == 200


# ---------------------------------------------------------------------------
# Scenario 2: light lane, explicit #p0, correct title
# ---------------------------------------------------------------------------

def test_light_p0_lane(conn):
    """light: prefix → lane=light."""
    process_telegram_task(
        "/task light: #p0 fix the kanban dispatch crash",
        chat_id="c2", user_id="u2", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].assignee == LANE_ASSIGNEE[LANE_LIGHT]


def test_light_p0_priority(conn):
    """#p0 tag → priority=400."""
    process_telegram_task(
        "/task light: #p0 fix the kanban dispatch crash",
        chat_id="c2", user_id="u2", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].priority == 400


def test_light_p0_title(conn):
    """Description stored correctly after stripping lane/priority tokens."""
    process_telegram_task(
        "/task light: #p0 fix the kanban dispatch crash",
        chat_id="c2", user_id="u2", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].title == "fix the kanban dispatch crash"


# ---------------------------------------------------------------------------
# Scenario 3: no prefix → default lane (light) + default priority (p2 / 200)
# ---------------------------------------------------------------------------

def test_no_prefix_default_lane(conn):
    """No lane prefix → default lane (light) → assignee=h2coder."""
    process_telegram_task(
        "/task summarize this PR",
        chat_id="c3", user_id="u3", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].assignee == LANE_ASSIGNEE[LANE_LIGHT]


def test_no_prefix_default_priority(conn):
    """No #pN tag → default priority 200 (p2)."""
    process_telegram_task(
        "/task summarize this PR",
        chat_id="c3", user_id="u3", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].priority == 200


# ---------------------------------------------------------------------------
# Scenario 4: #p1 without lane prefix
# ---------------------------------------------------------------------------

def test_p1_priority_stored(conn):
    """#p1 tag → priority=300."""
    process_telegram_task(
        "/task #p1 update the README",
        chat_id="c4", user_id="u4", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].priority == 300


def test_p1_title(conn):
    """#p1 tag is consumed; description stored without the tag."""
    process_telegram_task(
        "/task #p1 update the README",
        chat_id="c4", user_id="u4", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].title == "update the README"


def test_p1_lane(conn):
    """No lane prefix → default lane (light)."""
    process_telegram_task(
        "/task #p1 update the README",
        chat_id="c4", user_id="u4", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].assignee == LANE_ASSIGNEE[LANE_LIGHT]


# ---------------------------------------------------------------------------
# Scenario 5: LIGHT (uppercase) is case-insensitive
# ---------------------------------------------------------------------------

def test_uppercase_light_lane(conn):
    """LIGHT: prefix is accepted as lane=light (case-insensitive)."""
    process_telegram_task(
        "/task LIGHT: this should be case-insensitive",
        chat_id="c5", user_id="u5", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].assignee == LANE_ASSIGNEE[LANE_LIGHT]


def test_uppercase_light_description(conn):
    """Description preserved correctly after uppercase lane prefix stripped."""
    process_telegram_task(
        "/task LIGHT: this should be case-insensitive",
        chat_id="c5", user_id="u5", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].title == "this should be case-insensitive"


# ---------------------------------------------------------------------------
# Scenario 6: unknown #tag becomes label, not priority
# ---------------------------------------------------------------------------

def test_unknown_hashtag_default_priority(conn):
    """#fakepriority is not a priority alias → priority stays at default 200."""
    process_telegram_task(
        "/task #fakepriority test that unknown #tags become labels not priority",
        chat_id="c6", user_id="u6", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].priority == 200


def test_unknown_hashtag_card_created(conn):
    """Unknown #tag does not block card creation."""
    result = process_telegram_task(
        "/task #fakepriority test that unknown #tags become labels not priority",
        chat_id="c6", user_id="u6", conn=conn,
    )
    assert "📥" in result


# ---------------------------------------------------------------------------
# Scenario 7: heavy bare-word (no colon) — voice-input tolerance
# ---------------------------------------------------------------------------

def test_heavy_bare_word_lane(conn):
    """'heavy' without colon still routes to heavy lane."""
    process_telegram_task(
        "/task heavy build x",
        chat_id="c7", user_id="u7", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].assignee == LANE_ASSIGNEE[LANE_HEAVY]


def test_heavy_bare_word_description(conn):
    """Description after bare 'heavy' token is stored correctly."""
    process_telegram_task(
        "/task heavy build x",
        chat_id="c7", user_id="u7", conn=conn,
    )
    tasks = _kb.list_tasks(conn)
    assert tasks[0].title == "build x"


# ---------------------------------------------------------------------------
# Scenario 8: /task (empty) → syntax error, no card created
# ---------------------------------------------------------------------------

def test_empty_command_returns_syntax_error(conn):
    """Empty /task → response starts with ⚠️ SYNTAX."""
    result = process_telegram_task(
        "/task",
        chat_id="c8", user_id="u8", conn=conn,
    )
    assert result.startswith("⚠️ SYNTAX")


def test_empty_command_no_card_created(conn):
    """Empty /task must not insert any card into the DB."""
    process_telegram_task(
        "/task",
        chat_id="c8", user_id="u8", conn=conn,
    )
    assert _kb.list_tasks(conn) == []


# ---------------------------------------------------------------------------
# Scenario 9: whitespace-only /task → syntax error, no card created
# ---------------------------------------------------------------------------

def test_whitespace_only_returns_syntax_error(conn):
    """Whitespace-only /task → response starts with ⚠️ SYNTAX."""
    result = process_telegram_task(
        "/task   ",
        chat_id="c9", user_id="u9", conn=conn,
    )
    assert result.startswith("⚠️ SYNTAX")


def test_whitespace_only_no_card_created(conn):
    """Whitespace-only /task must not insert any card."""
    process_telegram_task(
        "/task   ",
        chat_id="c9", user_id="u9", conn=conn,
    )
    assert _kb.list_tasks(conn) == []


# ---------------------------------------------------------------------------
# Scenario 10: dedup — same command twice yields ↪ CONFLICT, one card only
# ---------------------------------------------------------------------------

def test_dedup_second_call_returns_conflict(conn):
    """Second identical /task within the same 30s bucket returns ↪ CONFLICT."""
    fixed_epoch = int(time.time())
    process_telegram_task(
        "/task fix the bug",
        chat_id="c10", user_id="u10", conn=conn, now_epoch=fixed_epoch,
    )
    result = process_telegram_task(
        "/task fix the bug",
        chat_id="c10", user_id="u10", conn=conn, now_epoch=fixed_epoch,
    )
    assert result.startswith("↪ CONFLICT")


def test_dedup_only_one_card_in_db(conn):
    """Two identical /task calls in the same bucket produce exactly one card."""
    fixed_epoch = int(time.time())
    process_telegram_task(
        "/task fix the bug",
        chat_id="c10", user_id="u10", conn=conn, now_epoch=fixed_epoch,
    )
    process_telegram_task(
        "/task fix the bug",
        chat_id="c10", user_id="u10", conn=conn, now_epoch=fixed_epoch,
    )
    assert len(_kb.list_tasks(conn)) == 1


def test_dedup_conflict_contains_task_id(conn):
    """↪ CONFLICT response includes the original task id."""
    fixed_epoch = int(time.time())
    first = process_telegram_task(
        "/task fix the bug",
        chat_id="c10", user_id="u10", conn=conn, now_epoch=fixed_epoch,
    )
    # Extract task id from the first ack (format: "📥 t_XXXXX created — ...")
    task_id = first.split()[1]  # second word is the task id

    second = process_telegram_task(
        "/task fix the bug",
        chat_id="c10", user_id="u10", conn=conn, now_epoch=fixed_epoch,
    )
    assert task_id in second


# ---------------------------------------------------------------------------
# Scenario 11: Auth pre-check contract (documented, not invoked)
# ---------------------------------------------------------------------------

def test_auth_precondition_documented():
    """Telegram auth runs BEFORE _handle_task_command in the gateway.

    The handler under test only runs for authorized users. The gateway's
    auth path (gateway/run.py telegram allowlist check) rejects
    non-allowlisted chats before any command dispatch. This test
    documents that contract rather than re-testing it — the relevant
    tests live in tests/gateway/test_telegram_group_gating.py and
    tests/gateway/test_unauthorized_dm_behavior.py.

    We assert that those test files exist so a maintainer knows where
    to look if auth behavior changes.
    """
    import os
    gateway_test_dir = Path(__file__).parent
    auth_test_files = [
        gateway_test_dir / "test_telegram_group_gating.py",
        gateway_test_dir / "test_unauthorized_dm_behavior.py",
    ]
    missing = [str(f) for f in auth_test_files if not f.exists()]
    assert not missing, f"Auth test files missing: {missing}"


# ---------------------------------------------------------------------------
# Scenario 12: Phase-3 token rejection (+dep:) → ⚠️ NOT IMPLEMENTED YET
# ---------------------------------------------------------------------------

def test_phase3_dep_token_returns_not_implemented(conn):
    """'+dep:t_abc' is a Phase 3 token → response starts with ⚠️ NOT IMPLEMENTED YET."""
    result = process_telegram_task(
        "/task +dep:t_abc fix it",
        chat_id="c12", user_id="u12", conn=conn,
    )
    assert result.startswith("⚠️ NOT IMPLEMENTED YET")


def test_phase3_dep_token_no_card_created(conn):
    """Phase-3 token rejection must not insert any card into the DB."""
    process_telegram_task(
        "/task +dep:t_abc fix it",
        chat_id="c12", user_id="u12", conn=conn,
    )
    assert _kb.list_tasks(conn) == []


# ---------------------------------------------------------------------------
# Extra: ack chip for light lane
# ---------------------------------------------------------------------------

def test_light_ack_contains_lane_chip(conn):
    """Ack for a light-lane card must contain 'light/p2' chip."""
    result = process_telegram_task(
        "/task light: refactor the session module",
        chat_id="cx1", user_id="ux1", conn=conn,
    )
    assert "light/p2" in result


# ---------------------------------------------------------------------------
# Extra: different chat_ids produce separate cards (no cross-chat dedup)
# ---------------------------------------------------------------------------

def test_different_chats_no_dedup(conn):
    """Same description from two distinct chat_ids creates two cards."""
    fixed_epoch = int(time.time())
    process_telegram_task(
        "/task update the docs now",
        chat_id="chatA", user_id="uA", conn=conn, now_epoch=fixed_epoch,
    )
    process_telegram_task(
        "/task update the docs now",
        chat_id="chatB", user_id="uB", conn=conn, now_epoch=fixed_epoch,
    )
    assert len(_kb.list_tasks(conn)) == 2


# ---------------------------------------------------------------------------
# Extra: created_by field reflects the telegram user
# ---------------------------------------------------------------------------

def test_created_by_field(conn):
    """created_by stored with the 'telegram:<user_id>:' prefix.

    The suffix is a per-call uniqueness marker (nanosecond timestamp)
    used as the race-free dedup signal, so we assert the prefix
    rather than the exact value.
    """
    process_telegram_task(
        "/task write integration tests",
        chat_id="cX", user_id="u99", conn=conn,
    )
    row = conn.execute("SELECT created_by FROM tasks LIMIT 1").fetchone()
    assert row["created_by"].startswith("telegram:u99:")
