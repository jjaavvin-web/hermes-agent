"""DISP-5: a KNOWN autonomous dispatch must fail CLOSED on the git push/PR/workflow
floor even when no session deny list resolves (empty list / contextvar-key mismatch).
Interactive sessions (marker unset) are unaffected.
"""

from tools import approval


def test_floor_blocks_push_under_marker_with_no_deny_list():
    tok = approval.mark_autonomous_dispatch(True)
    try:
        denied, reason = approval.check_session_deny_patterns(
            "git push origin main", session_key="unregistered-key"
        )
        assert denied is True and "DISP-5" in reason
        assert approval.check_session_deny_patterns("gh workflow run ci.yml", session_key="x")[0] is True
        assert approval.check_session_deny_patterns("gh pr merge --admin 5", session_key="x")[0] is True
        assert approval.check_session_deny_patterns("gh pr create -t x", session_key="x")[0] is True
        # benign commands NOT blocked even under the marker
        assert approval.check_session_deny_patterns("git status", session_key="x") == (False, None)
        assert approval.check_session_deny_patterns("ls -la", session_key="x") == (False, None)
    finally:
        approval._autonomous_dispatch_marker.reset(tok)


def test_interactive_session_unaffected():
    # marker unset (default) -> floor never fires at this layer
    assert approval.check_session_deny_patterns(
        "git push origin main", session_key="interactive"
    ) == (False, None)
    assert approval.check_session_deny_patterns(
        "git push origin main", session_key=None
    ) == (False, None)


def test_registered_deny_still_works_and_floor_is_defense_in_depth():
    approval.register_session_deny_patterns("sess1", [r"rm\s+-rf"])
    try:
        assert approval.check_session_deny_patterns("rm -rf /", session_key="sess1")[0] is True
        tok = approval.mark_autonomous_dispatch(True)
        try:
            # push blocked even though not in the registered list (floor defense-in-depth)
            assert approval.check_session_deny_patterns("git push origin main", session_key="sess1")[0] is True
        finally:
            approval._autonomous_dispatch_marker.reset(tok)
    finally:
        approval.clear_session("sess1")


def test_env_marker_also_triggers(monkeypatch):
    monkeypatch.setenv("HERMES_AUTONOMOUS_DISPATCH", "1")
    assert approval.check_session_deny_patterns("git push -f origin main", session_key="nomatch")[0] is True
