"""Focused probe-hardening tests for the OS-tab dashboard.

Covers two false-positive fixes in ``hermes_cli.dashboard_os``:

1. ``_probe_gateway`` must not flag amber when the recorded state-file pid is
   dead but the real gateway (systemd MainPID) is healthy and active.
2. ``_section_host`` load-average probe must be core-count-aware so a high-core
   box at moderate absolute load does not false-alarm.
"""
from __future__ import annotations

import json

import hermes_cli.dashboard_os as dos


def _write_state(tmp_path, pid, gw_state="running"):
    state_dir = tmp_path / ".hermes"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "gateway_state.json").write_text(
        json.dumps({"pid": pid, "gateway_state": gw_state}), encoding="utf-8"
    )
    return state_dir


# ---------------------------------------------------------------------------
# (1) gateway_state stale-pid false positive
# ---------------------------------------------------------------------------

def test_gateway_stale_pid_falls_back_to_systemd_active(tmp_path, monkeypatch):
    """Dead state-file pid + active systemd MainPID → green, not amber."""
    monkeypatch.setattr(dos, "HERMES_HOME", _write_state(tmp_path, pid=424242))
    # Recorded pid is dead; systemd MainPID is alive and the unit is active.
    monkeypatch.setattr(dos, "_pid_alive", lambda pid: pid == 999001)
    monkeypatch.setattr(dos, "_gateway_systemd_main", lambda *a, **k: (999001, "active"))

    item = dos._probe_gateway()
    assert item["status"] == "green", item
    assert "systemd MainPID=999001" in item["detail"]


def test_gateway_stale_pid_amber_when_unit_inactive(tmp_path, monkeypatch):
    """Dead state-file pid + inactive systemd unit → still amber."""
    monkeypatch.setattr(dos, "HERMES_HOME", _write_state(tmp_path, pid=424242))
    monkeypatch.setattr(dos, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(dos, "_gateway_systemd_main", lambda *a, **k: (0, "failed"))

    item = dos._probe_gateway()
    assert item["status"] == "amber", item
    assert item.get("reason")


def test_gateway_live_pid_is_green_without_systemd(tmp_path, monkeypatch):
    """Live recorded pid stays green and never consults systemd."""
    monkeypatch.setattr(dos, "HERMES_HOME", _write_state(tmp_path, pid=12345))
    monkeypatch.setattr(dos, "_pid_alive", lambda pid: True)

    def _boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("systemd fallback should not run when pid is alive")

    monkeypatch.setattr(dos, "_gateway_systemd_main", _boom)
    item = dos._probe_gateway()
    assert item["status"] == "green", item


def test_gateway_systemd_main_parses_show_output(monkeypatch):
    monkeypatch.setattr(
        dos,
        "_run",
        lambda *a, **k: type("R", (), {"stdout": "MainPID=4321\nActiveState=active\n"})(),
    )
    assert dos._gateway_systemd_main() == (4321, "active")


def test_gateway_systemd_main_mainpid_zero_is_none(monkeypatch):
    monkeypatch.setattr(
        dos,
        "_run",
        lambda *a, **k: type("R", (), {"stdout": "MainPID=0\nActiveState=inactive\n"})(),
    )
    assert dos._gateway_systemd_main() == (None, "inactive")


# ---------------------------------------------------------------------------
# (2) load_avg core-count awareness
# ---------------------------------------------------------------------------

def _load_item(monkeypatch, *, load1, cores):
    fake = type(
        "R",
        (),
        {
            "returncode": 0,
            "stdout": f"up 1 day,  load average: {load1}, {load1}, {load1}",
            "stderr": "",
        },
    )()
    monkeypatch.setattr(dos, "_run", lambda *a, **k: fake)
    monkeypatch.setattr(dos.os, "cpu_count", lambda: cores)
    section = dos._section_host()
    return next(i for i in section["items"] if i["name"] == "load_avg")


def test_load_avg_high_core_box_not_amber(monkeypatch):
    """20-core box at load ~14 (~70%) must stay green."""
    item = _load_item(monkeypatch, load1=14.0, cores=20)
    assert item["status"] == "green", item
    assert "20 cores" in item["detail"]


def test_load_avg_low_core_box_amber_at_same_load(monkeypatch):
    """Same absolute load 14 on a 4-core box is overloaded → red."""
    item = _load_item(monkeypatch, load1=14.0, cores=4)
    assert item["status"] == "red", item


def test_load_avg_amber_band(monkeypatch):
    """Ratio in [0.85, 1.5) → amber (e.g. load 12 on 10 cores = 1.2)."""
    item = _load_item(monkeypatch, load1=12.0, cores=10)
    assert item["status"] == "amber", item
