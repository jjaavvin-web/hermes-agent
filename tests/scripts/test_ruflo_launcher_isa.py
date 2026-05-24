"""Tests for the Ruflo launcher ISA-briefing wiring (ISC-30..32).

The launcher helper ``isa_brief_objective`` and the launch templates live
out-of-repo under ``~/.hermes/scripts/`` (ISA storage is out-of-repo by
design — see ISA-SPEC §2). These tests source/inspect those files directly
and skip cleanly when the tree is absent, e.g. on a fresh CI checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_HERMES_SCRIPTS = Path.home() / ".hermes" / "scripts"
_HELPERS = _HERMES_SCRIPTS / "ruflo-launcher-helpers.sh"
_TEMPLATES = _HERMES_SCRIPTS / "templates"
_LAUNCH = _TEMPLATES / "ruflo-launch.template.sh"
_LAUNCH_INTERACTIVE = _TEMPLATES / "ruflo-launch-interactive.template.sh"

_need_helpers = pytest.mark.skipif(
    not _HELPERS.is_file(),
    reason="ruflo-launcher-helpers.sh not present (out-of-repo)",
)


def _run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


@_need_helpers
def test_isc_30_brief_appended_when_isa_given(tmp_path):
    objective = tmp_path / "objective.md"
    objective.write_text("original objective content\n", encoding="utf-8")
    isa = tmp_path / "ISA.md"
    isa.write_text("---\nisa: fixture\n---\n## Goal\nfixture\n", encoding="utf-8")

    result = _run_bash(f'source "{_HELPERS}"\nisa_brief_objective "{isa}" "{objective}"')
    assert result.returncode == 0, result.stderr

    body = objective.read_text(encoding="utf-8")
    assert "original objective content" in body  # preamble preserved
    assert "WORK SPEC" in body                   # brief appended
    assert str(isa) in body                      # the ISA path is named


@_need_helpers
def test_isc_31_noop_when_isa_empty(tmp_path):
    objective = tmp_path / "objective.md"
    objective.write_text("original objective content\n", encoding="utf-8")
    before = objective.read_bytes()

    result = _run_bash(
        f'source "{_HELPERS}"\nisa_brief_objective "" "{objective}"\necho "rc=$?"'
    )
    assert result.returncode == 0
    assert "rc=0" in result.stdout
    assert objective.read_bytes() == before  # objective byte-unchanged


@pytest.mark.skipif(
    not _LAUNCH.is_file() or not _LAUNCH_INTERACTIVE.is_file(),
    reason="ruflo launch templates not present (out-of-repo)",
)
def test_isc_32_templates_invoke_isa_brief_objective():
    for template in (_LAUNCH, _LAUNCH_INTERACTIVE):
        text = template.read_text(encoding="utf-8")
        assert "isa_brief_objective" in text, (
            f"{template.name} does not invoke isa_brief_objective"
        )
