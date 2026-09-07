from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from hermes_cli import dashboard_os as osmod


def _completed(payload: dict, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["probe"], returncode, stdout=json.dumps(payload), stderr="")


def test_security_status_runs_redteam_and_reds_on_breach(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "run_redteam.py"
    script.write_text("# fake redteam\n", encoding="utf-8")
    monkeypatch.setattr(osmod, "_redteam_script_path", lambda: script)
    monkeypatch.setattr(
        osmod,
        "_run",
        lambda cmd, timeout=3.0: _completed({"passed": 37, "total": 38, "breach_count": 1}),
    )

    status = osmod._security_status()

    assert status["status"] == "red"
    assert status["breach_count"] == 1
    assert status["label"] == "1 breach"


def test_evals_status_gates_on_worst_fresh_holdout(monkeypatch, tmp_path: Path) -> None:
    hermes_home = tmp_path / ".hermes"
    score_path = hermes_home / "evals" / "recall" / "score-history.jsonl"
    score_path.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        {"ts": now, "holdout_file": str(score_path.parent / "holdout.jsonl"), "agg": {"k": 10, "n": 30, "recall_at_k": 0.9667}},
        {"ts": now, "holdout_file": str(score_path.parent / "holdout_wave2.jsonl"), "agg": {"k": 10, "n": 18, "recall_at_k": 0.6667}},
    ]
    score_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    monkeypatch.setattr(osmod, "HERMES_HOME", hermes_home)

    status = osmod._evals_status()

    assert status["status"] == "red"
    assert status["label"] == "worst 67%"
    assert status["worst_holdout"] == "holdout_wave2.jsonl"
    assert {row["holdout"] for row in status["sets"]} == {"holdout.jsonl", "holdout_wave2.jsonl"}


def test_dr_status_surfaces_all_stores_and_undrilled_is_amber(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path
    script = home / ".hermes" / "scripts" / "dr-status.py"
    python = home / ".local" / "share" / "hermes-agent" / "venv" / "bin" / "python"
    script.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    script.write_text("# fake dr status\n", encoding="utf-8")
    python.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    rows = [
        {"store": "mvms", "verdict": "GREEN", "rto_obs": "45s", "rto_target": "30m00s", "rpo_obs": "13h", "rpo_target": "24h", "offbox": "116h"},
        {"store": "honcho", "verdict": "N/A", "rto_obs": "—", "rto_target": "30m00s", "rpo_obs": "—", "rpo_target": "24h", "offbox": "—"},
        {"store": "app_state", "verdict": "N/A", "rto_obs": "—", "rto_target": "15m00s", "rpo_obs": "—", "rpo_target": "24h", "offbox": "—"},
        {"store": "state_db", "verdict": "GREEN", "rto_obs": "1m15s", "rto_target": "15m00s", "rpo_obs": "8h", "rpo_target": "24h", "offbox": "n/a"},
    ]
    monkeypatch.setattr(osmod, "HOME", home)
    monkeypatch.setattr(osmod, "HERMES_HOME", home / ".hermes")
    monkeypatch.setattr(osmod, "_run", lambda cmd, timeout=3.0: _completed({"exit_code": 0, "green": True, "rows": rows, "failures": []}))

    status = osmod._dr_status()

    assert status["status"] == "amber"
    assert status["label"] == "2/4 drilled · 2 untested"
    assert [row["store"] for row in status["rows"]] == ["mvms", "honcho", "app_state", "state_db"]
    assert [row["status"] for row in status["rows"]] == ["green", "amber", "amber", "green"]
