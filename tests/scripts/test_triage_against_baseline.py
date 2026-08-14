"""Tests for scripts/triage_against_baseline.py.

Covers marker/pytest-log extraction, baseline bucketing (NEW/KNOWN/
RESOLVED) with granularity-tolerant matching, the --update flow, the
UNREVIEWED gate, and end-to-end exit codes via tmp_path fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


triage = _load("triage_against_baseline")


# ---------------------------------------------------------------------------
# extract_failing_targets
# ---------------------------------------------------------------------------


def test_extract_marker_style_log():
    log = (
        "some preamble\n"
        "  ╔╍ Failed: tests/agent/test_foo.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        "  ║ FAILED tests/agent/test_foo.py::test_bar\n"
        "  ╚╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        "\n"
        "  ╔╍ Failed: tests/tools/test_baz.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
    )
    assert triage.extract_failing_targets(log) == [
        "tests/agent/test_foo.py",
        "tests/tools/test_baz.py",
    ]


def test_extract_marker_style_dedupes_and_preserves_order():
    log = (
        "  ╔╍ Failed: tests/a.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        "  ╔╍ Failed: tests/b.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        "  ╔╍ Failed: tests/a.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
    )
    assert triage.extract_failing_targets(log) == ["tests/a.py", "tests/b.py"]


def test_extract_falls_back_to_plain_pytest_failed_lines():
    log = (
        "============================= test session starts ==============\n"
        "collected 3 items\n"
        "\n"
        "tests/foo.py::test_a PASSED\n"
        "tests/foo.py::test_b FAILED\n"
        "\n"
        "=========================== short test summary info ============\n"
        "FAILED tests/foo.py::test_b - AssertionError: boom\n"
        "FAILED tests/bar.py::TestX::test_c - ValueError\n"
    )
    assert triage.extract_failing_targets(log) == [
        "tests/foo.py::test_b",
        "tests/bar.py::TestX::test_c",
    ]


def test_extract_prefers_markers_over_embedded_pytest_failed_lines():
    # The run_tests_parallel log format embeds "FAILED path::test" lines
    # *inside* the marker block (indented with "  ║ "). When markers are
    # present, extraction should stay at file granularity and not also
    # pick up the nested test-level lines.
    log = (
        "  ╔╍ Failed: tests/foo.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        "  ║ FAILED tests/foo.py::test_b\n"
        "  ╚╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
    )
    assert triage.extract_failing_targets(log) == ["tests/foo.py"]


def test_extract_strips_ansi_color_codes():
    log = "\x1b[31mFAILED\x1b[0m tests/foo.py::test_a\x1b[0m\n"
    assert triage.extract_failing_targets(log) == ["tests/foo.py::test_a"]


def test_extract_returns_empty_list_for_clean_log():
    assert triage.extract_failing_targets("all green, nothing to see\n") == []


# ---------------------------------------------------------------------------
# _covers / bucket
# ---------------------------------------------------------------------------


def test_covers_exact_match():
    assert triage._covers("tests/a.py::test_x", "tests/a.py::test_x")


def test_covers_file_level_baseline_covers_any_test_in_file():
    assert triage._covers("tests/a.py", "tests/a.py::test_x")
    assert triage._covers("tests/a.py", "tests/a.py")


def test_covers_file_level_failing_target_covered_by_test_level_baseline():
    # run_tests_parallel logs only report file granularity; a baseline
    # entry recorded at test-id granularity must still cover it.
    assert triage._covers("tests/a.py::test_x", "tests/a.py")


def test_covers_rejects_different_files():
    assert not triage._covers("tests/a.py::test_x", "tests/b.py::test_x")


def test_covers_rejects_different_test_in_same_file_both_test_level():
    assert not triage._covers("tests/a.py::test_x", "tests/a.py::test_y")


def test_bucket_splits_new_known_resolved():
    baseline = [
        {"target": "tests/known.py", "reason": "pre-existing"},
        {"target": "tests/unmatched.py", "reason": "no longer failing"},
    ]
    failing = ["tests/known.py::test_a", "tests/new.py::test_b"]
    new_failures, known, resolved = triage.bucket(failing, baseline)

    assert new_failures == ["tests/new.py::test_b"]
    assert [t for t, _ in known] == ["tests/known.py::test_a"]
    assert known[0][1]["target"] == "tests/known.py"
    assert [b["target"] for b in resolved] == ["tests/unmatched.py"]


def test_bucket_empty_baseline_marks_everything_new():
    new_failures, known, resolved = triage.bucket(["tests/a.py"], [])
    assert new_failures == ["tests/a.py"]
    assert known == []
    assert resolved == []


def test_bucket_no_failures_marks_everything_resolved():
    baseline = [{"target": "tests/a.py", "reason": "x"}]
    new_failures, known, resolved = triage.bucket([], baseline)
    assert new_failures == []
    assert known == []
    assert [b["target"] for b in resolved] == ["tests/a.py"]


# ---------------------------------------------------------------------------
# load_baseline / save_baseline
# ---------------------------------------------------------------------------


def test_load_baseline_missing_file_returns_empty_list(tmp_path: Path):
    assert triage.load_baseline(tmp_path / "nope.json") == []


def test_load_baseline_round_trips(tmp_path: Path):
    path = tmp_path / "debt.json"
    entries = [{"target": "tests/a.py", "reason": "r", "recorded": "2026-08-13"}]
    path.write_text(json.dumps(entries), encoding="utf-8")
    assert triage.load_baseline(path) == entries


def test_load_baseline_rejects_malformed_json(tmp_path: Path):
    path = tmp_path / "debt.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        triage.load_baseline(path)


def test_load_baseline_rejects_non_array(tmp_path: Path):
    path = tmp_path / "debt.json"
    path.write_text(json.dumps({"target": "tests/a.py"}), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON array"):
        triage.load_baseline(path)


def test_load_baseline_rejects_entry_without_target(tmp_path: Path):
    path = tmp_path / "debt.json"
    path.write_text(json.dumps([{"reason": "no target here"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        triage.load_baseline(path)


def test_save_baseline_sorts_by_target(tmp_path: Path):
    path = tmp_path / "debt.json"
    triage.save_baseline(
        path,
        [
            {"target": "tests/z.py", "reason": "r"},
            {"target": "tests/a.py", "reason": "r"},
        ],
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [e["target"] for e in data] == ["tests/a.py", "tests/z.py"]
    # trailing newline for clean diffs
    assert path.read_text(encoding="utf-8").endswith("\n")


# ---------------------------------------------------------------------------
# apply_update
# ---------------------------------------------------------------------------


def test_apply_update_appends_unreviewed_entries():
    baseline = [{"target": "tests/known.py", "reason": "pre-existing"}]
    updated = triage.apply_update(
        baseline,
        ["tests/new.py"],
        Path("run.log"),
        date(2026, 8, 13),
    )
    assert len(updated) == 2
    new_entry = next(e for e in updated if e["target"] == "tests/new.py")
    assert new_entry["reason"].startswith("UNREVIEWED")
    assert "2026-08-13" in new_entry["reason"]
    assert new_entry["recorded"] == "2026-08-13"
    assert "run.log" in new_entry["evidence"]
    # original list untouched
    assert baseline == [{"target": "tests/known.py", "reason": "pre-existing"}]


def test_apply_update_does_not_duplicate_existing_targets():
    baseline = [{"target": "tests/known.py", "reason": "pre-existing"}]
    updated = triage.apply_update(
        baseline, ["tests/known.py"], Path("run.log"), date(2026, 8, 13)
    )
    assert updated == baseline


# ---------------------------------------------------------------------------
# run() end-to-end
# ---------------------------------------------------------------------------


def _write_log(tmp_path: Path, failing_files: list[str]) -> Path:
    log_path = tmp_path / "run.log"
    lines = []
    for f in failing_files:
        lines.append(f"  ╔╍ Failed: {f} ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _write_baseline(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "debt.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_run_exits_zero_when_all_failures_are_known_debt(tmp_path: Path, capsys):
    log_path = _write_log(tmp_path, ["tests/known.py"])
    baseline_path = _write_baseline(
        tmp_path, [{"target": "tests/known.py", "reason": "pre-existing"}]
    )
    rc = triage.run([str(log_path), "--baseline", str(baseline_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_run_exits_nonzero_on_new_failures(tmp_path: Path, capsys):
    log_path = _write_log(tmp_path, ["tests/new.py"])
    baseline_path = _write_baseline(tmp_path, [])
    rc = triage.run([str(log_path), "--baseline", str(baseline_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "tests/new.py" in out
    assert "NEW failures" in out


def test_run_exits_zero_when_no_failures_at_all(tmp_path: Path, capsys):
    log_path = tmp_path / "run.log"
    log_path.write_text("all green\n", encoding="utf-8")
    baseline_path = _write_baseline(tmp_path, [])
    rc = triage.run([str(log_path), "--baseline", str(baseline_path)])
    assert rc == 0


def test_run_missing_log_file_exits_two(tmp_path: Path, capsys):
    baseline_path = _write_baseline(tmp_path, [])
    rc = triage.run(
        [str(tmp_path / "does_not_exist.log"), "--baseline", str(baseline_path)]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "not found" in err


def test_run_malformed_baseline_exits_two(tmp_path: Path, capsys):
    log_path = _write_log(tmp_path, [])
    baseline_path = tmp_path / "debt.json"
    baseline_path.write_text("not json", encoding="utf-8")
    rc = triage.run([str(log_path), "--baseline", str(baseline_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not valid JSON" in err


def test_run_update_flag_appends_new_failures_to_baseline_file(
    tmp_path: Path, capsys
):
    log_path = _write_log(tmp_path, ["tests/new.py"])
    baseline_path = _write_baseline(tmp_path, [])
    rc = triage.run(
        [str(log_path), "--baseline", str(baseline_path), "--update"]
    )
    capsys.readouterr()

    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["target"] == "tests/new.py"
    assert data[0]["reason"].startswith("UNREVIEWED")

    # --update documents the failure (it's no longer "new"/uncovered), but
    # the freshly-added UNREVIEWED entry still gates the run until a human
    # edits the reason (or passes --allow-unreviewed) — same non-zero exit,
    # different cause.
    assert rc == 1


def test_run_update_then_allow_unreviewed_passes(tmp_path: Path, capsys):
    log_path = _write_log(tmp_path, ["tests/new.py"])
    baseline_path = _write_baseline(tmp_path, [])
    triage.run([str(log_path), "--baseline", str(baseline_path), "--update"])
    capsys.readouterr()

    rc = triage.run(
        [
            str(log_path),
            "--baseline",
            str(baseline_path),
            "--allow-unreviewed",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_run_unreviewed_baseline_entry_gates_even_without_update(
    tmp_path: Path, capsys
):
    # An UNREVIEWED entry left over from a previous --update run (not yet
    # edited by a human) must keep failing the gate on subsequent runs,
    # even when nothing in the current log is uncovered.
    log_path = tmp_path / "run.log"
    log_path.write_text("all green\n", encoding="utf-8")
    baseline_path = _write_baseline(
        tmp_path,
        [
            {
                "target": "tests/stale.py",
                "reason": "UNREVIEWED — added by --update on 2026-08-01",
            }
        ],
    )
    rc = triage.run([str(log_path), "--baseline", str(baseline_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNREVIEWED" in out


def test_run_allow_unreviewed_does_not_mask_genuine_new_failures(
    tmp_path: Path, capsys
):
    # --allow-unreviewed only bypasses the UNREVIEWED-entries gate; a
    # separate, uncovered NEW failure must still block.
    log_path = _write_log(tmp_path, ["tests/new.py"])
    baseline_path = _write_baseline(
        tmp_path,
        [{"target": "tests/stale.py", "reason": "UNREVIEWED — stale"}],
    )
    rc = triage.run(
        [str(log_path), "--baseline", str(baseline_path), "--allow-unreviewed"]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "tests/new.py" in out


def test_run_update_does_not_duplicate_already_known_debt(tmp_path: Path, capsys):
    log_path = _write_log(tmp_path, ["tests/known.py", "tests/new.py"])
    baseline_path = _write_baseline(
        tmp_path, [{"target": "tests/known.py", "reason": "pre-existing"}]
    )
    triage.run([str(log_path), "--baseline", str(baseline_path), "--update"])
    capsys.readouterr()

    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert {e["target"] for e in data} == {"tests/known.py", "tests/new.py"}
    known_entry = next(e for e in data if e["target"] == "tests/known.py")
    assert known_entry["reason"] == "pre-existing"  # untouched


def test_run_reports_resolved_debt_without_failing(tmp_path: Path, capsys):
    log_path = _write_log(tmp_path, [])
    baseline_path = _write_baseline(
        tmp_path, [{"target": "tests/gone_now.py", "reason": "was flaky"}]
    )
    rc = triage.run([str(log_path), "--baseline", str(baseline_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RESOLVED debt (1)" in out
    assert "tests/gone_now.py" in out


# ---------------------------------------------------------------------------
# the real seeded baseline file
# ---------------------------------------------------------------------------


def test_repo_baseline_file_is_well_formed():
    entries = triage.load_baseline(triage.DEFAULT_BASELINE)
    assert len(entries) >= 2
    targets = {e["target"] for e in entries}
    assert "tests/tools/test_file_tools_fail_closed_confinement.py" in targets
    assert "tests/tools/test_terminal_worktree_confinement.py" in targets
    for entry in entries:
        assert entry.get("reason")
        assert entry.get("recorded")
        assert entry.get("evidence")
        assert not str(entry["reason"]).startswith("UNREVIEWED")
