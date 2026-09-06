"""fp-184 ordering proof — the cross-profile write guard must run BEFORE
the write it gates, not merely appear somewhere in the call graph.

fp-184 (tests/security/fork_parity_manifest.json,
tests/security/fork_parity_docket.txt) pins agent/file_safety.py's
cross-profile write guard via a ``call_edge`` anchor: does
``tools/file_tools.py::_check_cross_profile_path`` call
``get_cross_profile_warning`` somewhere in its body? That anchor is
provably order-blind. Mutation class M7d-d2 (see
closure/MUTATION-EVIDENCE-GUARD-CLASSES.log, section "M7d-d2 (closed)")
relocated ``write_file_tool``'s OUTER call to ``_check_cross_profile_path``
to run AFTER ``file_ops.write_file(...)`` in both of its write branches,
leaving the inner call edge byte-for-byte untouched, and it stayed fully
GREEN under the fork-parity guard's decisive suite because:

  * the call_edge anchor only checks the call exists somewhere in
    ``_check_cross_profile_path``'s body — still true after the mutation;
  * fp-184's own proof,
    ``tests/security/test_merge_invariants.py::test_cross_profile_write_guard_not_retired``,
    calls ``agent.file_safety.get_cross_profile_warning`` directly — it
    never exercises ``write_file_tool``'s or ``patch_tool``'s call SITE;
  * a grep of tests/security/ found zero references to
    ``write_file_tool`` or ``_check_cross_profile_path`` before this file
    existed.

This file closes that gap. It drives the real ``write_file_tool`` /
``patch_tool`` entry points (tools/file_tools.py) — never the classifier
directly — against a target that already has known bytes on disk, and
asserts BOTH the refusal AND that the bytes are unchanged. That second
assertion is the one a reordered guard call cannot satisfy: a guard that
fires after the write still returns the same refusal string, but the
file is already overwritten.

Fixture mirrors tests/tools/test_cross_profile_guard.py::fake_hermes
(same two-profile layout, same monkeypatch shape) so the ordering
guarantee proven there for the external suite is proven again from
inside tests/security/, where fp-184's docket entry actually lives.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    """Two-profile Hermes layout; HERMES_HOME points at hermes-security."""
    root = tmp_path / "fake-hermes"
    (root / "skills" / "shared-skill").mkdir(parents=True)
    (root / "skills" / "shared-skill" / "SKILL.md").write_text(
        "---\nname: shared-skill\ndescription: default copy.\n---\n",
        encoding="utf-8",
    )

    sec_home = root / "profiles" / "hermes-security"
    (sec_home / "skills").mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(sec_home))

    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)

    import agent.file_safety as fs
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: sec_home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)

    return {"root": root, "sec_home": sec_home}


class TestCrossProfileGuardRunsBeforeWrite:
    """fp-184 ordering proof: the guard must gate the write, not just
    exist in the call graph.
    """

    def test_write_file_tool_refuses_and_leaves_bytes_untouched(self, fake_hermes):
        from tools.file_tools import write_file_tool

        target = fake_hermes["root"] / "skills" / "shared-skill" / "SKILL.md"
        original = target.read_text(encoding="utf-8")

        result = json.loads(
            write_file_tool(str(target), "OVERWRITTEN-BY-ORDERING-TEST")
        )

        assert result.get("error"), "cross-profile write must be refused"
        # Decisive ordering assertion: if the guard's call site in
        # write_file_tool ran AFTER file_ops.write_file(...) (M7d-d2's
        # exact reorder), the refusal above would still fire but the
        # bytes would already be gone. The call_edge anchor cannot see
        # this; only exercising the real call site can.
        assert target.read_text(encoding="utf-8") == original

    def test_patch_tool_refuses_and_leaves_bytes_untouched(self, fake_hermes):
        from tools.file_tools import patch_tool

        target = fake_hermes["root"] / "skills" / "shared-skill" / "SKILL.md"
        original = target.read_text(encoding="utf-8")

        result = json.loads(
            patch_tool(
                mode="replace",
                path=str(target),
                old_string="default copy.",
                new_string="HIJACKED-BY-ORDERING-TEST.",
            )
        )

        assert result.get("error"), "cross-profile patch must be refused"
        assert target.read_text(encoding="utf-8") == original
