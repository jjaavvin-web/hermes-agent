"""CI wrapper for the hermetic outbound exfil red-team runner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).resolve().parent / "run_redteam.py"


def test_outbound_exfil_redteam_runner_is_green() -> None:
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--json"],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["total"] >= 51
    assert summary["expect_denied"] >= 40
    assert summary["expect_allowed"] >= 8
    assert summary["misses"] == 0
    assert summary["false_positives"] == 0
