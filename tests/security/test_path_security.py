"""Path-security invariants for tool boundary helpers.

Additive coverage for traversal rejection used by skill, cron, and
credential-file tooling before user-controlled paths reach disk operations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.path_security import has_traversal_component, validate_within_dir


def test_validate_within_dir_accepts_real_child_path(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    child_dir = root / "nested"
    child_dir.mkdir(parents=True)
    child = child_dir / "artifact.txt"
    child.write_text("safe", encoding="utf-8")

    assert validate_within_dir(child, root) is None


def test_validate_within_dir_rejects_sibling_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "evil"
    outside.mkdir()

    error = validate_within_dir(outside, root)

    assert error is not None
    assert error.startswith("Path escapes allowed directory")


def test_validate_within_dir_rejects_dotdot_escape(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    escape = root / ".." / ".." / "etc"

    error = validate_within_dir(escape, root)

    assert error is not None
    assert error.startswith("Path escapes allowed directory")


@pytest.mark.parametrize(
    ("path_str", "expected"),
    (
        ("a/../b", True),
        ("a/b/c", False),
        ("..", True),
        ("/var/lib/hermes/safe", False),
    ),
)
def test_has_traversal_component_detects_literal_parent_segments(path_str: str, expected: bool) -> None:
    assert has_traversal_component(path_str) is expected
