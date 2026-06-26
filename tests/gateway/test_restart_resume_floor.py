"""finding #8 — gateway-restart auto-resume must NOT bypass the DISP-5 floor.

On gateway restart an in-flight webhook/loki run is auto-resumed via the GENERIC
``handle_message`` path (``_run_startup_resume_event`` / the queued-inbound
replay).  That path never re-runs the webhook dispatch's in-process safety
setup:

  * ``mark_autonomous_dispatch(True)``      — DISP-5 push/PR/workflow floor
  * ``register_session_deny_patterns(...)`` — per-session terminal-deny list
  * ``set_active_worktree(...)``            — worktree isolation

The first two live in a per-task ``ContextVar`` + an in-memory dict that a
restart wipes; the worktree is also a ``ContextVar``; and
``HERMES_AUTONOMOUS_DISPATCH`` is not set in any gateway unit env.  So a resumed
run could ``git push`` / ``gh pr`` / operate on the live tree — every one of
which the original dispatch physically blocked.

These tests pin the rehydration: the resume path must re-arm all three so the
floor is UP *inside* the resumed turn, plus a negative control documenting the
pre-fix gap.
"""

import asyncio
from datetime import datetime

import pytest

from agent.codex_session_context import get_active_worktree
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import _AutonomousResumeArmError
from gateway.session import SessionEntry, SessionSource
from tests.gateway.restart_test_helpers import make_restart_runner
from tools.approval import (
    _is_autonomous_dispatch,
    check_session_deny_patterns,
    register_session_deny_patterns,
)


def _webhook_adapter() -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(enabled=True, token="***", extra={})
    )


def _webhook_source() -> SessionSource:
    return SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="loki-route-1",
        chat_type="dm",
        user_id="webhook:loki",
    )


@pytest.mark.asyncio
async def test_restart_resume_rearms_floor_deny_and_worktree(tmp_path):
    """The resumed turn must see the floor ARMED, the deny-list ACTIVE, and the
    worktree BOUND — exactly as the original webhook dispatch installed them."""
    runner, _ = make_restart_runner()
    adapter = _webhook_adapter()
    runner.adapters = {Platform.WEBHOOK: adapter}

    source = _webhook_source()
    session_key = "agent:main:webhook:dm:loki-route-1"
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.WEBHOOK,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        autonomous_dispatch=True,
        worktree_path=str(tmp_path),
        deny_patterns=None,  # forces the DEFAULT_WEBHOOK_DENY_PATTERNS fallback
    )
    runner.session_store._entries = {session_key: entry}

    captured: dict = {}

    async def asserting_handle_message(event):
        # These three assertions ARE the security contract: they run inside the
        # resumed dispatch, where a regression would silently let push through.
        captured["ran"] = True
        captured["autonomous"] = _is_autonomous_dispatch()
        denied, _pat = check_session_deny_patterns(
            "git push origin main", session_key=session_key
        )
        captured["denied"] = denied
        captured["worktree"] = get_active_worktree()

    adapter.handle_message = asserting_handle_message

    event = MessageEvent(
        text="",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
    )

    try:
        await runner._run_startup_resume_event(adapter, event, session_key)

        assert captured.get("ran") is True, "resumed handler never ran"
        assert captured["autonomous"] is True, (
            "DISP-5 floor NOT armed inside the resumed run — push would slip"
        )
        assert captured["denied"] is True, (
            "per-session deny list NOT re-registered on resume — push allowed"
        )
        assert captured["worktree"] == str(tmp_path), (
            "worktree isolation NOT re-bound on resume — run hits the live tree"
        )

        # The arming ContextVars must be torn back down after the resume turn.
        assert _is_autonomous_dispatch() is False, "floor leaked past resume teardown"
        assert get_active_worktree() is None, "worktree leaked past resume teardown"
    finally:
        register_session_deny_patterns(session_key, [])


@pytest.mark.asyncio
async def test_restart_resume_uses_persisted_deny_patterns(tmp_path):
    """When the entry carries an explicit deny list, the resume re-registers
    those exact patterns (not just the webhook default)."""
    runner, _ = make_restart_runner()
    adapter = _webhook_adapter()
    runner.adapters = {Platform.WEBHOOK: adapter}

    source = _webhook_source()
    session_key = "agent:main:webhook:dm:loki-route-1"
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.WEBHOOK,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        autonomous_dispatch=True,
        worktree_path=None,
        deny_patterns=[r"\bterraform\s+apply\b"],
    )
    runner.session_store._entries = {session_key: entry}

    captured: dict = {}

    async def asserting_handle_message(event):
        captured["custom"] = check_session_deny_patterns(
            "terraform apply -auto-approve", session_key=session_key
        )[0]
        # The DISP-5 floor still fails CLOSED on push even though the custom
        # deny list does not name it.
        captured["push"] = check_session_deny_patterns(
            "git push origin main", session_key=session_key
        )[0]

    adapter.handle_message = asserting_handle_message
    event = MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True
    )

    try:
        await runner._run_startup_resume_event(adapter, event, session_key)
        assert captured.get("custom") is True, "persisted deny pattern not re-registered"
        assert captured.get("push") is True, "DISP-5 floor down despite custom deny list"
    finally:
        register_session_deny_patterns(session_key, [])


def test_negative_control_generic_path_leaves_floor_down():
    """NEGATIVE CONTROL — documents the finding #8 gap.

    The GENERIC handle_message path, WITHOUT ``_arm_autonomous_resume_floor``,
    leaves the floor DOWN: ``git push`` is allowed, there is no deny list, and
    no worktree is bound.  This is precisely the live-tree / push regression a
    restart auto-resume exhibited before the rehydration landed — and what the
    positive tests above prove is now closed.
    """
    sk = "agent:main:webhook:dm:no-floor-control"
    # No arming has happened in this context.
    assert _is_autonomous_dispatch() is False
    assert check_session_deny_patterns("git push origin main", session_key=sk)[0] is False
    assert get_active_worktree() is None


@pytest.mark.asyncio
async def test_restart_resume_skips_non_autonomous_session(tmp_path):
    """A non-autonomous (interactive) resumed session must NOT get the floor —
    the floor is a dispatch-only control and must not leak into normal chats."""
    runner, adapter = make_restart_runner()  # TELEGRAM adapter

    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="u1"
    )
    session_key = "agent:main:telegram:dm:123"
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        autonomous_dispatch=False,
    )
    runner.session_store._entries = {session_key: entry}

    captured: dict = {}

    async def asserting_handle_message(event):
        captured["autonomous"] = _is_autonomous_dispatch()
        captured["worktree"] = get_active_worktree()

    adapter.handle_message = asserting_handle_message
    event = MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True
    )

    await runner._run_startup_resume_event(adapter, event, session_key)
    assert captured.get("autonomous") is False, "floor wrongly armed for interactive resume"
    assert captured.get("worktree") is None


# --- finding #8 follow-up: review-flagged resume-floor gaps -----------------


@pytest.mark.asyncio
async def test_worktree_gone_fails_closed(tmp_path):
    """MED — a persisted worktree that no longer exists must FAIL CLOSED.

    Pre-fix the resume proceeded with NO worktree bind and ONLY the default
    push/PR deny floor, leaving local git-mutation (commit/reset against the
    LIVE tree) and recursive rm wide open.  The fix augments the deny list with
    WORKTREE_GONE_EXTRA_DENY so those are blocked inside the resumed turn.
    """
    runner, _ = make_restart_runner()
    adapter = _webhook_adapter()
    runner.adapters = {Platform.WEBHOOK: adapter}

    gone = tmp_path / "relay-worktree-that-was-cleaned-up"
    # NOTE: never created — os.path.isdir(gone) is False.
    source = _webhook_source()
    session_key = "agent:main:webhook:dm:loki-route-1"
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.WEBHOOK,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        autonomous_dispatch=True,
        worktree_path=str(gone),
        deny_patterns=None,
    )
    runner.session_store._entries = {session_key: entry}

    captured: dict = {}

    async def asserting_handle_message(event):
        captured["worktree"] = get_active_worktree()
        captured["commit"] = check_session_deny_patterns(
            "git commit -am wip", session_key=session_key
        )[0]
        captured["reset"] = check_session_deny_patterns(
            "git reset --hard origin/main", session_key=session_key
        )[0]
        captured["rmrf"] = check_session_deny_patterns(
            "rm -rf /home/josep/.local/share/hermes-agent", session_key=session_key
        )[0]
        # Long-form and space-separated destructive rm must ALSO be denied —
        # the regex must not only catch the bundled -rf/-fr/-r short flags.
        captured["rm_long"] = check_session_deny_patterns(
            "rm --recursive --force /home/josep/.local/share/hermes-agent",
            session_key=session_key,
        )[0]
        captured["rm_split"] = check_session_deny_patterns(
            "rm -r -f /home/josep/.local/share/hermes-agent", session_key=session_key
        )[0]
        captured["rm_trailing_flag"] = check_session_deny_patterns(
            "rm /home/josep/.local/share/hermes-agent -rf", session_key=session_key
        )[0]
        # A non-destructive single-file rm (no r/f flag) is NOT a fail-closed
        # escape and must not be swept up by the destructive-form regex.
        captured["rm_plain"] = check_session_deny_patterns(
            "rm /home/josep/notes.txt", session_key=session_key
        )[0]
        captured["push"] = check_session_deny_patterns(
            "git push origin main", session_key=session_key
        )[0]

    adapter.handle_message = asserting_handle_message
    event = MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True
    )

    try:
        await runner._run_startup_resume_event(adapter, event, session_key)
        # Isolation is lost — but the run must NOT be bound to the live tree.
        assert captured.get("worktree") is None, (
            "a gone worktree was somehow bound — isolation illusion"
        )
        # FAIL CLOSED: local mutation against the live tree is blocked.
        assert captured.get("commit") is True, (
            "git commit allowed against the LIVE tree on worktree-gone resume"
        )
        assert captured.get("reset") is True, "git reset --hard allowed on worktree-gone resume"
        assert captured.get("rmrf") is True, "rm -rf allowed on worktree-gone resume"
        assert captured.get("rm_long") is True, (
            "rm --recursive --force allowed on worktree-gone resume (long-form not denied)"
        )
        assert captured.get("rm_split") is True, (
            "rm -r -f allowed on worktree-gone resume (space-separated flags not denied)"
        )
        assert captured.get("rm_trailing_flag") is True, (
            "rm <path> -rf allowed on worktree-gone resume (trailing flag not denied)"
        )
        assert captured.get("rm_plain") is False, (
            "non-destructive single-file rm spuriously denied — regex over-broad"
        )
        assert captured.get("push") is True, "DISP-5 push floor down on worktree-gone resume"
    finally:
        register_session_deny_patterns(session_key, [])


@pytest.mark.asyncio
async def test_key_divergence_registers_under_both_keys(tmp_path):
    """MED — re-registration must land under the key tool-exec actually queries.

    The original dispatch registered the deny list under ``approval_key`` (the
    build_session_key output, no profile namespace).  When that diverges from
    the resume run's tool-exec key (``session_key``, profile-namespaced), a
    re-registration under only ONE key leaves the floor DOWN under the other.
    The fix re-registers under BOTH; this pins that tool-exec is covered no
    matter which key it resolves.
    """
    runner, _ = make_restart_runner()
    adapter = _webhook_adapter()
    runner.adapters = {Platform.WEBHOOK: adapter}

    source = _webhook_source()
    # Simulate divergence: tool-exec key is profile-namespaced, the dispatch
    # registered under the legacy no-profile key.
    session_key = "p:work:webhook:dm:loki-route-1"      # what tool-exec queries
    approval_key = "agent:main:webhook:dm:loki-route-1"  # what the dispatch used
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.WEBHOOK,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        autonomous_dispatch=True,
        worktree_path=str(tmp_path),
        deny_patterns=[r"\bterraform\s+apply\b"],
        approval_key=approval_key,
    )
    runner.session_store._entries = {session_key: entry}

    captured: dict = {}

    async def asserting_handle_message(event):
        # The deny must fire under BOTH keys: whichever one tool-exec resolves.
        captured["under_session_key"] = check_session_deny_patterns(
            "terraform apply -auto-approve", session_key=session_key
        )[0]
        captured["under_approval_key"] = check_session_deny_patterns(
            "terraform apply -auto-approve", session_key=approval_key
        )[0]

    adapter.handle_message = asserting_handle_message
    event = MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True
    )

    try:
        await runner._run_startup_resume_event(adapter, event, session_key)
        assert captured.get("under_session_key") is True, (
            "deny not registered under the resume run's tool-exec key"
        )
        assert captured.get("under_approval_key") is True, (
            "deny not registered under the dispatch's approval key — "
            "floor DOWN in production if tool-exec resolves that key"
        )
        # finding #8 low: the DIVERGENT approval_key registration must be
        # cleared on teardown.  The resumed run's own end-of-turn teardown
        # clears only session_key; the divergent approval_key has no other
        # owner, so _disarm_autonomous_resume_floor must remove it.  Without
        # that, the deny leaks and could shadow a future benign run reusing
        # the key.  (session_key here is owned by the run teardown / the
        # finally below; we assert specifically on the divergent key.)
        assert check_session_deny_patterns(
            "terraform apply -auto-approve", session_key=approval_key
        )[0] is False, (
            "divergent approval_key deny leaked after resume teardown — "
            "_disarm_autonomous_resume_floor did not clear it"
        )
    finally:
        register_session_deny_patterns(session_key, [])
        register_session_deny_patterns(approval_key, [])


@pytest.mark.asyncio
async def test_replay_queue_rearms_floor(tmp_path):
    """MED — the queued-inbound REPLAY path (_drain_startup_restore_queue) must
    re-arm the floor/deny/worktree inside the replayed turn, just like the
    direct startup-resume path."""
    runner, _ = make_restart_runner()
    adapter = _webhook_adapter()
    runner.adapters = {Platform.WEBHOOK: adapter}

    source = _webhook_source()
    session_key = runner._session_key_for_source(source)
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.WEBHOOK,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        autonomous_dispatch=True,
        worktree_path=str(tmp_path),
        deny_patterns=None,
    )
    runner.session_store._entries = {session_key: entry}

    captured: dict = {}

    async def asserting_handle_message(event):
        captured["autonomous"] = _is_autonomous_dispatch()
        captured["denied"] = check_session_deny_patterns(
            "git push origin main", session_key=session_key
        )[0]
        captured["worktree"] = get_active_worktree()

    adapter.handle_message = asserting_handle_message
    event = MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True
    )
    runner._startup_restore_queue = [event]

    try:
        drained = await runner._drain_startup_restore_queue()
        assert drained == 1, "replay event not drained"
        assert captured.get("autonomous") is True, (
            "DISP-5 floor NOT armed inside the REPLAYED turn — push would slip"
        )
        assert captured.get("denied") is True, "deny list NOT re-registered on replay"
        assert captured.get("worktree") == str(tmp_path), (
            "worktree isolation NOT re-bound on replay"
        )
        # Teardown after the replayed turn.
        assert _is_autonomous_dispatch() is False, "floor leaked past replay teardown"
        assert get_active_worktree() is None, "worktree leaked past replay teardown"
    finally:
        register_session_deny_patterns(session_key, [])


@pytest.mark.asyncio
async def test_non_webhook_persisted_envelope_treated_autonomous(tmp_path):
    """MED — a NON-webhook autonomous lane (loki/relay) whose ``autonomous_dispatch``
    bit failed to persist must still be re-armed when ANY persisted envelope
    component survives (deny_patterns / worktree_path / approval_key)."""
    runner, adapter = make_restart_runner()  # TELEGRAM-platform adapter

    # Non-webhook source, autonomous_dispatch bit MISSING, but the envelope's
    # deny list + worktree persisted — proof this was an autonomous dispatch.
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="loki-relay-7", chat_type="dm", user_id="relay"
    )
    session_key = "agent:main:telegram:dm:loki-relay-7"
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
        autonomous_dispatch=False,             # bit did NOT stick
        worktree_path=str(tmp_path),
        deny_patterns=[r"\bgit\s+push\b"],
    )
    runner.session_store._entries = {session_key: entry}

    captured: dict = {}

    async def asserting_handle_message(event):
        captured["autonomous"] = _is_autonomous_dispatch()
        captured["denied"] = check_session_deny_patterns(
            "git push origin main", session_key=session_key
        )[0]
        captured["worktree"] = get_active_worktree()

    adapter.handle_message = asserting_handle_message
    event = MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True
    )

    try:
        await runner._run_startup_resume_event(adapter, event, session_key)
        assert captured.get("autonomous") is True, (
            "non-webhook lane with a persisted envelope resumed floor-NAKED"
        )
        assert captured.get("denied") is True, "persisted deny list not re-registered"
        assert captured.get("worktree") == str(tmp_path)
    finally:
        register_session_deny_patterns(session_key, [])


@pytest.mark.asyncio
async def test_floor_arm_failure_aborts_turn(tmp_path, monkeypatch):
    """LOW(a) — if arming the DISP-5 marker FAILS for a known-autonomous entry,
    the resume must ABORT (fail CLOSED), never run handle_message unguarded."""
    runner, _ = make_restart_runner()
    adapter = _webhook_adapter()
    runner.adapters = {Platform.WEBHOOK: adapter}

    source = _webhook_source()
    session_key = "agent:main:webhook:dm:loki-route-1"
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.WEBHOOK,
        chat_type="dm",
        resume_pending=True,
        autonomous_dispatch=True,
    )
    runner.session_store._entries = {session_key: entry}

    ran = {"handle_message": False}

    async def must_not_run(event):
        ran["handle_message"] = True

    adapter.handle_message = must_not_run

    # Force the floor-arm to blow up.
    import tools.approval as _approval

    def _boom(_v):
        raise RuntimeError("contextvar backend exploded")

    monkeypatch.setattr(_approval, "mark_autonomous_dispatch", _boom)

    # The helper itself raises the abort signal...
    event = MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True
    )
    with pytest.raises(_AutonomousResumeArmError):
        runner._arm_autonomous_resume_floor(event, session_key)

    # ...and the caller swallows it and SKIPS the dispatch.
    await runner._run_startup_resume_event(adapter, event, session_key)
    assert ran["handle_message"] is False, (
        "resume ran the dispatch despite a failed floor-arm — FAIL OPEN regression"
    )


@pytest.mark.asyncio
async def test_floor_visible_in_create_task_child_after_disarm(tmp_path):
    """LOW(c) — pins the create_task CONTEXT COPY, not mere same-context visibility.

    handle_message spawns a child via ``asyncio.create_task`` (the exact
    mechanism the real run task uses).  That child runs in a COPY of the
    context taken at create-time, so the armed DISP-5 floor must remain visible
    INSIDE the child even AFTER the parent disarms its own context.
    """
    runner, _ = make_restart_runner()
    adapter = _webhook_adapter()
    runner.adapters = {Platform.WEBHOOK: adapter}

    source = _webhook_source()
    session_key = "agent:main:webhook:dm:loki-route-1"
    entry = SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.WEBHOOK,
        chat_type="dm",
        resume_pending=True,
        autonomous_dispatch=True,
        worktree_path=str(tmp_path),
    )
    runner.session_store._entries = {session_key: entry}

    captured: dict = {}
    child_result: dict = {}

    async def stub_handle_message(event):
        async def child():
            # Runs in the context copied at create_task() time — the floor was
            # armed then, so it must still read True here even though the parent
            # tears its own copy down before this body executes.
            child_result["autonomous"] = _is_autonomous_dispatch()
            child_result["worktree"] = get_active_worktree()
        captured["child_task"] = asyncio.create_task(child())

    adapter.handle_message = stub_handle_message
    event = MessageEvent(
        text="", message_type=MessageType.TEXT, source=source, internal=True
    )

    try:
        await runner._run_startup_resume_event(adapter, event, session_key)
        # Parent context has been disarmed by now.
        assert _is_autonomous_dispatch() is False, "parent floor not torn down"
        assert get_active_worktree() is None, "parent worktree not torn down"
        # The create_task child still sees the armed snapshot.
        await captured["child_task"]
        assert child_result.get("autonomous") is True, (
            "create_task child lost the DISP-5 floor — the run task would push"
        )
        assert child_result.get("worktree") == str(tmp_path), (
            "create_task child lost worktree isolation"
        )
    finally:
        register_session_deny_patterns(session_key, [])
