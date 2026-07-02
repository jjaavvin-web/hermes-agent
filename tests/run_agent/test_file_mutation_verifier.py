"""Tests for the per-turn file-mutation verifier footer.

Covers the three moving pieces:

1. ``_extract_file_mutation_targets`` — pulls file paths from write_file /
   patch (replace + V4A) tool-call argument dicts.
2. ``AIAgent._record_file_mutation_result`` — builds the per-turn state
   dict, removing entries when a later success supersedes an earlier
   failure for the same path.
3. ``AIAgent._format_file_mutation_failure_footer`` — renders the dict
   as a user-visible advisory.

Regression target: the "Ben Eng llm-wiki" session where grok-4.1-fast
batched parallel patches, half failed, and the model summarised the
turn claiming every file was edited.  This verifier makes over-claiming
structurally impossible past the model: the user always sees the real
list of files that did NOT change.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.turn_finalizer import finalize_turn
from run_agent import (
    AIAgent,
    _FILE_MUTATING_TOOLS,
    _extract_error_preview,
    _extract_file_mutation_targets,
)


# ---------------------------------------------------------------------------
# _extract_file_mutation_targets
# ---------------------------------------------------------------------------


class TestExtractFileMutationTargets:
    def test_non_mutating_tool_returns_empty(self):
        assert _extract_file_mutation_targets("read_file", {"path": "/x"}) == []
        assert _extract_file_mutation_targets("terminal", {"command": "ls"}) == []

    def test_write_file_returns_single_path(self):
        out = _extract_file_mutation_targets("write_file", {"path": "/tmp/a.md", "content": "x"})
        assert out == ["/tmp/a.md"]

    def test_write_file_missing_path_returns_empty(self):
        assert _extract_file_mutation_targets("write_file", {"content": "x"}) == []

    def test_patch_replace_mode_returns_path(self):
        args = {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"}
        assert _extract_file_mutation_targets("patch", args) == ["/tmp/a.md"]

    def test_patch_default_mode_is_replace(self):
        # Mode omitted — schema default is ``replace``.
        args = {"path": "/tmp/a.md", "old_string": "x", "new_string": "y"}
        assert _extract_file_mutation_targets("patch", args) == ["/tmp/a.md"]

    def test_patch_v4a_single_file(self):
        body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/a.md\n"
            "@@ ctx @@\n"
            " line1\n"
            "-bad\n"
            "+good\n"
            "*** End Patch\n"
        )
        args = {"mode": "patch", "patch": body}
        assert _extract_file_mutation_targets("patch", args) == ["/tmp/a.md"]

    def test_patch_v4a_multi_file(self):
        body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/a.md\n"
            "@@ @@\n-a\n+b\n"
            "*** Add File: /tmp/new.md\n"
            "+fresh\n"
            "*** Delete File: /tmp/old.md\n"
            "*** End Patch\n"
        )
        args = {"mode": "patch", "patch": body}
        paths = _extract_file_mutation_targets("patch", args)
        assert paths == ["/tmp/a.md", "/tmp/new.md", "/tmp/old.md"]

    def test_patch_v4a_missing_body_returns_empty(self):
        assert _extract_file_mutation_targets("patch", {"mode": "patch"}) == []
        assert _extract_file_mutation_targets("patch", {"mode": "patch", "patch": ""}) == []


# ---------------------------------------------------------------------------
# _extract_error_preview
# ---------------------------------------------------------------------------


class TestExtractErrorPreview:
    def test_json_error_field_preferred(self):
        raw = json.dumps({"success": False, "error": "Could not find old_string in /tmp/x"})
        assert _extract_error_preview(raw) == "Could not find old_string in /tmp/x"

    def test_plain_string_falls_through(self):
        assert _extract_error_preview("Error executing tool: boom") == "Error executing tool: boom"

    def test_long_preview_truncated(self):
        long = "x" * 500
        out = _extract_error_preview(long, max_len=50)
        assert len(out) <= 50
        assert out.endswith("…")

    def test_none_returns_empty(self):
        assert _extract_error_preview(None) == ""


# ---------------------------------------------------------------------------
# _record_file_mutation_result — state transitions
# ---------------------------------------------------------------------------


def _bare_agent() -> AIAgent:
    """Skip __init__ and only attach the per-turn state dict.

    AIAgent.__init__ takes ~60 parameters and touches network, auth, and
    the filesystem.  For these tests we only need the two methods —
    ``_record_file_mutation_result`` and ``_format_file_mutation_failure_footer``.
    Using ``object.__new__`` mirrors the gateway-test pattern documented in
    the agent pitfalls list.
    """
    agent = object.__new__(AIAgent)
    agent._turn_failed_file_mutations = {}
    return agent


class TestRecordFileMutationResult:
    def test_non_mutating_tool_ignored(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "read_file", {"path": "/tmp/x"}, "{}", is_error=True,
        )
        assert agent._turn_failed_file_mutations == {}

    def test_failure_recorded(self):
        agent = _bare_agent()
        result = json.dumps({"success": False, "error": "Could not find old_string"})
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"},
            result, is_error=True,
        )
        state = agent._turn_failed_file_mutations
        assert "/tmp/a.md" in state
        assert state["/tmp/a.md"]["tool"] == "patch"
        assert "Could not find old_string" in state["/tmp/a.md"]["error_preview"]

    def test_success_removes_prior_failure(self):
        agent = _bare_agent()
        # First attempt fails
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "x", "new_string": "y"},
            json.dumps({"error": "not found"}), is_error=True,
        )
        assert "/tmp/a.md" in agent._turn_failed_file_mutations
        # Second attempt with corrected old_string succeeds
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "real", "new_string": "fixed"},
            json.dumps({"success": True, "diff": "..."}), is_error=False,
        )
        assert agent._turn_failed_file_mutations == {}

    def test_write_file_with_lint_error_counts_as_landed(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "write_file",
            {"path": "/tmp/a.py", "content": "bad"},
            json.dumps({"error": "write failed"}),
            is_error=True,
        )
        assert "/tmp/a.py" in agent._turn_failed_file_mutations

        result = json.dumps({
            "bytes_written": 24,
            "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
        })

        agent._record_file_mutation_result(
            "write_file",
            {"path": "/tmp/a.py", "content": "def nope(:\n"},
            result,
            is_error=True,
        )

        assert agent._turn_failed_file_mutations == {}

    def test_patch_with_lsp_diagnostics_counts_as_landed(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "/tmp/a.py", "old_string": "x", "new_string": "y"},
            json.dumps({"error": "Could not find old_string"}),
            is_error=True,
        )
        assert "/tmp/a.py" in agent._turn_failed_file_mutations

        result = json.dumps({
            "success": True,
            "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
            "files_modified": ["/tmp/a.py"],
            "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
        })

        agent._record_file_mutation_result(
            "patch",
            {"mode": "replace", "path": "/tmp/a.py", "old_string": "x", "new_string": "y"},
            result,
            is_error=True,
        )

        assert agent._turn_failed_file_mutations == {}

    def test_repeated_failure_keeps_first_error(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "v1", "new_string": "y"},
            json.dumps({"error": "first error"}), is_error=True,
        )
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md", "old_string": "v2", "new_string": "y"},
            json.dumps({"error": "second error"}), is_error=True,
        )
        # Keep the original error — swapping to the latest would obscure
        # the initial root cause.
        assert "first error" in agent._turn_failed_file_mutations["/tmp/a.md"]["error_preview"]

    def test_v4a_multi_file_all_tracked(self):
        agent = _bare_agent()
        body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/a.md\n@@ @@\n-a\n+b\n"
            "*** Update File: /tmp/b.md\n@@ @@\n-a\n+b\n"
            "*** End Patch\n"
        )
        agent._record_file_mutation_result(
            "patch", {"mode": "patch", "patch": body},
            json.dumps({"error": "parse failure"}), is_error=True,
        )
        assert set(agent._turn_failed_file_mutations) == {"/tmp/a.md", "/tmp/b.md"}

    def test_no_state_dict_silent_noop(self):
        """When called outside run_conversation the state dict is absent.

        The record helper must never raise — a tool dispatched from, say,
        a direct ``chat()`` call should not blow up the call site just
        because the verifier state hasn't been initialised.
        """
        agent = object.__new__(AIAgent)  # no state attached
        # Should not raise
        agent._record_file_mutation_result(
            "patch", {"mode": "replace", "path": "/tmp/a.md"},
            json.dumps({"error": "x"}), is_error=True,
        )

    def test_missing_path_arg_recorded_nowhere(self):
        agent = _bare_agent()
        agent._record_file_mutation_result(
            "patch", {"mode": "replace"},  # no path
            json.dumps({"error": "path required"}), is_error=True,
        )
        # No path → nothing to key on, state stays empty.  The per-turn
        # state is about file paths, not individual tool-call IDs.
        assert agent._turn_failed_file_mutations == {}


# ---------------------------------------------------------------------------
# _format_file_mutation_failure_footer
# ---------------------------------------------------------------------------


class TestFormatFooter:
    def test_empty_returns_empty_string(self):
        assert AIAgent._format_file_mutation_failure_footer({}) == ""

    def test_single_failure(self):
        out = AIAgent._format_file_mutation_failure_footer(
            {"/tmp/a.md": {"tool": "patch", "error_preview": "Could not find old_string"}},
        )
        assert "1 file(s) were NOT modified" in out
        assert "/tmp/a.md" in out
        assert "Could not find old_string" in out
        assert "git status" in out  # user-actionable hint

    def test_truncation_at_10_entries(self):
        failed = {
            f"/tmp/f{i}.md": {"tool": "patch", "error_preview": "err"}
            for i in range(15)
        }
        out = AIAgent._format_file_mutation_failure_footer(failed)
        assert "15 file(s) were NOT modified" in out
        assert "… and 5 more" in out
        # Ten file bullets + header + "and X more" line
        lines = out.split("\n")
        bullet_lines = [ln for ln in lines if ln.lstrip().startswith("•")]
        assert len(bullet_lines) == 11  # 10 shown + 1 summary

    def test_paths_are_backtick_wrapped(self):
        """Footer paths must be inline-code wrapped so the gateway's bare-path
        media extractor can't auto-attach them (#35584 defense-in-depth)."""
        out = AIAgent._format_file_mutation_failure_footer(
            {"/home/u/.hermes/config.yaml": {
                "tool": "patch",
                "error_preview": (
                    "Write denied: '/home/u/.hermes/config.yaml' is a "
                    "protected system/credential file."
                ),
            }},
        )
        # Path still human-readable.
        assert "/home/u/.hermes/config.yaml" in out
        # Bullet path is backticked.
        assert "`/home/u/.hermes/config.yaml`" in out
        # The path echoed inside the preview is ALSO backticked (the real
        # file_operations.py denial message embeds it in single quotes, which
        # do NOT block the gateway extractor's regex).
        assert "'`/home/u/.hermes/config.yaml`'" in out
        # No double-backticking anywhere.
        assert "``" not in out

    def test_footer_path_not_extracted_by_gateway(self):
        """End-to-end: the gateway's extract_local_files must NOT pull a
        config.yaml path out of the rendered footer (#35584)."""
        import os
        import tempfile
        from gateway.platforms.base import BasePlatformAdapter

        tmp = tempfile.mkdtemp(prefix="hermes_footer_")
        try:
            cfg = os.path.join(tmp, "config.yaml")
            with open(cfg, "w") as fh:
                fh.write("openrouter_api_key: sk-LEAK\n")
            footer = AIAgent._format_file_mutation_failure_footer(
                {cfg: {
                    "tool": "patch",
                    "error_preview": (
                        f"Write denied: '{cfg}' is a protected "
                        "system/credential file."
                    ),
                }},
            )
            response = "I updated your config.\n\n" + footer
            paths, _ = BasePlatformAdapter.extract_local_files(response)
            assert paths == [], f"footer leaked deliverable path(s): {paths}"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# turn finalizer — failed mutations become hard failures
# ---------------------------------------------------------------------------


def _finalizer_agent(*, failed_mutations=None, verifier_enabled=True):
    agent = SimpleNamespace()
    agent.max_iterations = 90
    agent.iteration_budget = SimpleNamespace(remaining=89, used=1, max_total=90)
    agent.quiet_mode = True
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.base_url = None
    agent.session_id = "test-session"
    agent._turn_failed_file_mutations = failed_mutations or {}
    agent._interrupt_message = None
    agent._stream_callback = None
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = []
    agent._response_was_previewed = False
    agent._tool_guardrail_halt_decision = None
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "unknown"
    agent.session_cost_source = "none"
    agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)

    agent._save_trajectory = lambda *args, **kwargs: None
    agent._cleanup_task_resources = lambda *args, **kwargs: None
    agent._drop_trailing_empty_response_scaffolding = lambda *args, **kwargs: None
    agent._persist_session = lambda *args, **kwargs: None
    agent._file_mutation_verifier_enabled = lambda: verifier_enabled
    agent._format_file_mutation_failure_footer = AIAgent._format_file_mutation_failure_footer
    agent._turn_completion_explainer_enabled = lambda: False
    agent._drain_pending_steer = lambda: None
    agent.clear_interrupt = lambda: None
    agent._sync_external_memory_for_turn = lambda *args, **kwargs: None
    agent._spawn_background_review = lambda *args, **kwargs: None
    return agent


def _run_finalizer(agent, *, final_response="OK LOKI-5 DONE: shipped"):
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": final_response},
    ]
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id=None,
        turn_id="turn-1",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="text_response",
    )


class TestTurnFinalizerFileMutationHardFailure:
    def test_failed_expected_mutation_converts_success_to_blocked_failure(self, monkeypatch):
        import hermes_cli.plugins as plugins

        monkeypatch.setattr(plugins, "invoke_hook", lambda *args, **kwargs: [])
        agent = _finalizer_agent(
            failed_mutations={
                "/tmp/denied.md": {
                    "tool": "patch",
                    "error_preview": "sandbox denied write",
                },
            },
            verifier_enabled=True,
        )

        result = _run_finalizer(agent)

        assert result["completed"] is False
        assert result["failed"] is True
        assert result["turn_exit_reason"] == "file_mutation_verifier_failed"
        assert result["failure_reason"] == "file_mutation_verifier_failed"
        assert result["error"].startswith("File-mutation verifier blocked success")
        assert result["final_response"].startswith("BLOCKED: File-mutation verifier blocked success")
        assert "OK LOKI-5 DONE: shipped" in result["final_response"]
        assert "1 file(s) were NOT modified" in result["final_response"]
        assert "`/tmp/denied.md`" in result["final_response"]

    def test_clean_turn_without_failed_mutations_stays_success(self, monkeypatch):
        import hermes_cli.plugins as plugins

        monkeypatch.setattr(plugins, "invoke_hook", lambda *args, **kwargs: [])
        agent = _finalizer_agent(failed_mutations={}, verifier_enabled=True)

        result = _run_finalizer(agent, final_response="OK LOKI-5 DONE: clean")

        assert result["completed"] is True
        assert result["failed"] is False
        assert result["turn_exit_reason"] == "text_response"
        assert result["final_response"] == "OK LOKI-5 DONE: clean"
        assert "error" not in result
        assert "failure_reason" not in result

    def test_failed_mutation_stays_advisory_when_verifier_disabled(self, monkeypatch):
        import hermes_cli.plugins as plugins

        monkeypatch.setattr(plugins, "invoke_hook", lambda *args, **kwargs: [])
        agent = _finalizer_agent(
            failed_mutations={
                "/tmp/denied.md": {"tool": "patch", "error_preview": "denied"},
            },
            verifier_enabled=False,
        )

        result = _run_finalizer(agent)

        assert result["completed"] is True
        assert result["failed"] is False
        assert result["turn_exit_reason"] == "text_response"
        assert result["final_response"] == "OK LOKI-5 DONE: shipped"


    def test_failed_mutation_hard_fails_even_without_model_response(self, monkeypatch):
        import hermes_cli.plugins as plugins

        monkeypatch.setattr(plugins, "invoke_hook", lambda *args, **kwargs: [])
        agent = _finalizer_agent(
            failed_mutations={
                "/tmp/denied.md": {"tool": "patch", "error_preview": "denied"},
            },
            verifier_enabled=True,
        )

        result = _run_finalizer(agent, final_response=None)

        assert result["completed"] is False
        assert result["failed"] is True
        assert result["turn_exit_reason"] == "file_mutation_verifier_failed"
        assert result["failure_reason"] == "file_mutation_verifier_failed"
        assert result["final_response"].startswith("BLOCKED: File-mutation verifier blocked success")
        assert "1 file(s) were NOT modified" in result["final_response"]


# ---------------------------------------------------------------------------
# _file_mutation_verifier_enabled — env + config precedence
# ---------------------------------------------------------------------------


class TestVerifierEnabled:
    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("HERMES_FILE_MUTATION_VERIFIER", raising=False)
        agent = _bare_agent()
        # With no env and no config present, safe default is True.
        # load_config may surface a user config.yaml in some envs — stub it.
        import hermes_cli.config as _cfg_mod
        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {})
        assert agent._file_mutation_verifier_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
    def test_env_disables(self, monkeypatch, value):
        monkeypatch.setenv("HERMES_FILE_MUTATION_VERIFIER", value)
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is False

    def test_env_enables_over_config(self, monkeypatch):
        monkeypatch.setenv("HERMES_FILE_MUTATION_VERIFIER", "1")
        import hermes_cli.config as _cfg_mod
        monkeypatch.setattr(
            _cfg_mod, "load_config",
            lambda: {"display": {"file_mutation_verifier": False}},
        )
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is True

    def test_config_disables_when_no_env(self, monkeypatch):
        monkeypatch.delenv("HERMES_FILE_MUTATION_VERIFIER", raising=False)
        import hermes_cli.config as _cfg_mod
        monkeypatch.setattr(
            _cfg_mod, "load_config",
            lambda: {"display": {"file_mutation_verifier": False}},
        )
        agent = _bare_agent()
        assert agent._file_mutation_verifier_enabled() is False


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


def test_file_mutating_tools_set_shape():
    """write_file + patch are the only tools the verifier tracks.

    Guard rail: if someone adds a third file-mutating tool (e.g. a new
    ``append_file``), they should also audit whether the verifier should
    track it.  This test fails loudly on unilateral additions.
    """
    assert _FILE_MUTATING_TOOLS == frozenset({"write_file", "patch"})
