"""Acceptance / stress tests for the READY_SPEC trust-compiler dispatch gate.

``HERMES_KANBAN_ENFORCE_READY_SPEC`` controls three modes tested here:

  off     (default/unset) — gate is a no-op; every card reaches claim_task
  warn    — gate validates + emits an event but always proceeds to claim
  enforce — cards that fail validation are skipped; claim_task is NEVER
             called for them; they remain in 'ready' and appear in
             ``DispatchResult.skipped_ready_spec`` as ``(task_id, codes)``

The harness mirrors ``test_kanban_dispatch.py`` exactly: same
``kanban_home`` + ``spawnable`` fixtures, same in-memory DB construction,
same ``spawn_fn`` stub pattern.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures — identical to test_kanban_dispatch.py
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def spawnable(monkeypatch):
    """Make every assignee a valid Hermes profile so dispatch reaches
    the READY_SPEC gate (the profile-exists check sits just before it)."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spawn_stub():
    """Stub spawn_fn that records calls and returns a fake PID."""
    spawned: list[str] = []

    def fake_spawn(task, workspace):
        spawned.append(task.id)
        return 12345  # fake pid

    return fake_spawn, spawned


def _make_claim_spy(monkeypatch):
    """Wrap kb.claim_task with a recording spy; return the call log."""
    real_claim = kb.claim_task
    call_log: list[str] = []

    def spy(conn, task_id, **kwargs):
        call_log.append(task_id)
        return real_claim(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "claim_task", spy)
    return call_log


# ---------------------------------------------------------------------------
# Card body templates
# A: well-formed — every field valid; resolves to "scratch" workspace
# B: malformed   — no scope, /tmp evidence_path, and mismatched allowed_workspace
#                  (the capital-J path "/home/Josep/..." != resolved "scratch", so
#                  the validator emits "allowed_workspace_mismatch", NOT
#                  "unsafe_allowed_workspace" — the mismatch check fires first).
# C: broken fence — syntactically invalid YAML inside the fence
# D: no fence     — plain body, no ready-spec block at all
# ---------------------------------------------------------------------------

_BODY_A = textwrap.dedent("""\
    Card A — well-formed ready-spec; all fields valid.

    ```ready-spec
    scope: validate the READY_SPEC dispatch gate acceptance test
    allowed_workspace: scratch
    evidence_path: ~/.hermes/audits/test-board/valid-card-1/
    stop_gates: [config, auth]
    verifier: h2reviewer
    ```
""")

_BODY_B = textwrap.dedent("""\
    Card B — malformed: no scope, mismatched allowed_workspace (path != resolved 'scratch'), /tmp evidence path.

    ```ready-spec
    allowed_workspace: /home/Josep/.local/share/hermes-agent
    evidence_path: /tmp/x
    ```
""")

_BODY_C = textwrap.dedent("""\
    Card C — syntactically broken YAML inside the fence.

    ```ready-spec
    scope: [unclosed
      bad: : :
    ```
""")

_BODY_D = "Card D — plain body with NO ready-spec fence at all."


@pytest.fixture
def four_cards(kanban_home, spawnable):
    """Four ready scratch tasks: A (valid), B (malformed), C (broken), D (absent)."""
    with kb.connect() as conn:
        tid_a = kb.create_task(
            conn,
            title="card-A-valid",
            body=_BODY_A,
            assignee="alice",
            workspace_kind="scratch",
        )
        tid_b = kb.create_task(
            conn,
            title="card-B-malformed",
            body=_BODY_B,
            assignee="alice",
            workspace_kind="scratch",
        )
        tid_c = kb.create_task(
            conn,
            title="card-C-broken-fence",
            body=_BODY_C,
            assignee="alice",
            workspace_kind="scratch",
        )
        tid_d = kb.create_task(
            conn,
            title="card-D-no-block",
            body=_BODY_D,
            assignee="alice",
            workspace_kind="scratch",
        )
    return tid_a, tid_b, tid_c, tid_d


# ===========================================================================
# Test 1 — enforce mode: claim_task gating
# ===========================================================================


def test_enforce_blocks_malformed_claims_only_valid(four_cards, monkeypatch):
    """enforce: claim_task is called for A and NEVER for B/C/D."""
    tid_a, tid_b, tid_c, tid_d = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    claim_log = _make_claim_spy(monkeypatch)
    spawn_fn, _ = _make_spawn_stub()

    with kb.connect() as conn:
        kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    assert tid_a in claim_log, "Card A (valid spec) must reach claim_task"
    assert tid_b not in claim_log, "Card B (missing scope) must be blocked before claim_task"
    assert tid_c not in claim_log, "Card C (broken fence) must be blocked before claim_task"
    assert tid_d not in claim_log, "Card D (no fence) must be blocked before claim_task"


def test_enforce_skipped_ready_spec_contains_bcd_not_a(four_cards, monkeypatch):
    """enforce: result.skipped_ready_spec lists B/C/D, not A."""
    tid_a, tid_b, tid_c, tid_d = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    skipped_ids = {entry[0] for entry in result.skipped_ready_spec}
    assert tid_b in skipped_ids, "B must appear in skipped_ready_spec"
    assert tid_c in skipped_ids, "C must appear in skipped_ready_spec"
    assert tid_d in skipped_ids, "D must appear in skipped_ready_spec"
    assert tid_a not in skipped_ids, "A (valid spec) must NOT appear in skipped_ready_spec"


def test_enforce_b_codes_include_missing_scope(four_cards, monkeypatch):
    """enforce: card B's skipped_ready_spec codes include 'missing_scope'."""
    _, tid_b, _, _ = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    codes_by_id = {entry[0]: entry[1] for entry in result.skipped_ready_spec}
    codes_b = codes_by_id.get(tid_b, "")
    assert "missing_scope" in codes_b, (
        f"Expected 'missing_scope' in B's error codes; got: {codes_b!r}"
    )


def test_enforce_c_codes_include_parse_error(four_cards, monkeypatch):
    """enforce: card C's skipped_ready_spec codes include 'ready_spec_parse_error'."""
    _, _, tid_c, _ = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    codes_by_id = {entry[0]: entry[1] for entry in result.skipped_ready_spec}
    codes_c = codes_by_id.get(tid_c, "")
    assert "ready_spec_parse_error" in codes_c, (
        f"Expected 'ready_spec_parse_error' in C's error codes; got: {codes_c!r}"
    )


def test_enforce_d_codes_include_missing_scope(four_cards, monkeypatch):
    """enforce: card D (absent fence) codes include 'missing_scope'."""
    _, _, _, tid_d = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    codes_by_id = {entry[0]: entry[1] for entry in result.skipped_ready_spec}
    codes_d = codes_by_id.get(tid_d, "")
    assert "missing_scope" in codes_d, (
        f"Expected 'missing_scope' in D's error codes (absent fence); got: {codes_d!r}"
    )


def test_enforce_bcd_status_stays_ready(four_cards, monkeypatch):
    """enforce: skipped cards remain in 'ready' status — no status mutation."""
    _, tid_b, tid_c, tid_d = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    with kb.connect() as conn:
        for tid in (tid_b, tid_c, tid_d):
            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"Task {tid} should remain 'ready' after enforce-skip; got {task.status!r}"
            )


def test_enforce_gate_exception_fails_closed(four_cards, monkeypatch):
    """enforce: when ready_spec_evaluate itself raises an exception the gate FAILS
    CLOSED -- affected cards do NOT reach claim_task and appear in
    result.skipped_ready_spec with code 'ready_spec_gate_error'.

    This guards the invariant that an infra/import/DB crash in the evaluator
    NEVER becomes a silent claim bypass (the security reviewer flagged this
    exception path as untested).
    """
    tid_a, tid_b, tid_c, tid_d = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    def _always_raise(conn, task_id, board, mode):
        raise RuntimeError("injected gate failure")

    monkeypatch.setattr(kb, "ready_spec_evaluate", _always_raise)

    claim_log = _make_claim_spy(monkeypatch)
    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    assert claim_log == [], (
        "claim_task must not be called for any card when the gate evaluator raises; "
        f"got calls: {claim_log}"
    )
    skipped_ids = {entry[0] for entry in result.skipped_ready_spec}
    skipped_codes = {entry[0]: entry[1] for entry in result.skipped_ready_spec}
    for tid in (tid_a, tid_b, tid_c, tid_d):
        assert tid in skipped_ids, (
            f"Card {tid} must appear in skipped_ready_spec when gate evaluator raises"
        )
        assert skipped_codes[tid] == "ready_spec_gate_error", (
            f"Card {tid} must carry code 'ready_spec_gate_error'; "
            f"got {skipped_codes[tid]!r}"
        )


# ===========================================================================
# Test 2 -- enforce + dry_run: only A appears in spawned; no claim_task calls
# ===========================================================================


def test_enforce_dry_run_reports_only_valid_spawnable(four_cards, monkeypatch):
    """enforce + dry_run=True: only A appears in result.spawned."""
    tid_a, tid_b, tid_c, tid_d = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=True, max_spawn=10)

    spawned_ids = {entry[0] for entry in result.spawned}
    assert tid_a in spawned_ids, "A must appear in dry_run spawned list"
    assert tid_b not in spawned_ids, "B must NOT appear in dry_run spawned list"
    assert tid_c not in spawned_ids, "C must NOT appear in dry_run spawned list"
    assert tid_d not in spawned_ids, "D must NOT appear in dry_run spawned list"


def test_enforce_dry_run_does_not_call_claim_task(four_cards, monkeypatch):
    """enforce + dry_run=True: claim_task is never called (dry-run is observation only).

    The gate still runs under dry_run: only card A (valid spec) reaches the dry_run
    spawn shortcut; B/C/D are rejected by the gate before that shortcut and are therefore
    absent from result.spawned.  This means the test would fail if the gate were removed
    (B/C/D would appear in spawned), making the claim_log assertion non-tautological.
    """
    tid_a, tid_b, tid_c, tid_d = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "enforce")

    claim_log = _make_claim_spy(monkeypatch)
    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=True, max_spawn=10)

    assert claim_log == [], (
        f"claim_task must never be called under dry_run; called with {claim_log}"
    )
    spawned_ids = {entry[0] for entry in result.spawned}
    assert tid_a in spawned_ids, (
        "Card A (valid spec) must appear in dry_run spawned list — gate must pass it through"
    )
    assert tid_b not in spawned_ids, (
        "Card B must be absent from spawned — gate rejects it before the dry_run shortcut"
    )
    assert tid_c not in spawned_ids, (
        "Card C must be absent from spawned — gate rejects it before the dry_run shortcut"
    )
    assert tid_d not in spawned_ids, (
        "Card D must be absent from spawned — gate rejects it before the dry_run shortcut"
    )


# ===========================================================================
# Test 3 — off (default): gate is completely inert
# ===========================================================================


def test_off_is_unchanged(four_cards, monkeypatch):
    """With env unset (off), claim_task is attempted for all four cards."""
    tid_a, tid_b, tid_c, tid_d = four_cards
    monkeypatch.delenv("HERMES_KANBAN_ENFORCE_READY_SPEC", raising=False)

    claim_log = _make_claim_spy(monkeypatch)
    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    for tid in (tid_a, tid_b, tid_c, tid_d):
        assert tid in claim_log, (
            f"Card {tid} must reach claim_task when READY_SPEC gate is off"
        )


def test_off_skipped_ready_spec_is_empty(four_cards, monkeypatch):
    """With env unset (off), result.skipped_ready_spec is always empty."""
    four_cards  # ensure tasks are created
    monkeypatch.delenv("HERMES_KANBAN_ENFORCE_READY_SPEC", raising=False)

    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    assert result.skipped_ready_spec == []


# ===========================================================================
# Test 4 — warn: validates + emits but always proceeds to claim
# ===========================================================================


def test_warn_validates_but_still_claims(four_cards, monkeypatch):
    """warn: B/C/D still get claimed — warn never blocks dispatch."""
    tid_a, tid_b, tid_c, tid_d = four_cards
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "warn")

    claim_log = _make_claim_spy(monkeypatch)
    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    for tid in (tid_a, tid_b, tid_c, tid_d):
        assert tid in claim_log, (
            f"Card {tid} must reach claim_task under warn mode (warn never blocks)"
        )


def test_warn_skipped_ready_spec_is_empty(four_cards, monkeypatch):
    """warn: result.skipped_ready_spec is empty (no cards skipped under warn)."""
    four_cards  # ensure tasks are created
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_READY_SPEC", "warn")

    spawn_fn, _ = _make_spawn_stub()
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False, max_spawn=10)

    assert result.skipped_ready_spec == []


# ===========================================================================
# Boundary: DispatchResult dataclass baseline
# ===========================================================================


def test_dispatch_result_has_skipped_ready_spec_field():
    """DispatchResult exposes skipped_ready_spec as an empty list by default."""
    result = kb.DispatchResult()
    assert result.skipped_ready_spec == []
