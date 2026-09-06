from __future__ import annotations


def test_work_section_command_center_exception_returns_honest_no_data(monkeypatch):
    from hermes_cli import dashboard_command_center
    from hermes_cli import dashboard_os as osmod

    def raise_probe_error():
        raise RuntimeError("command center boom")

    monkeypatch.setattr(dashboard_command_center, "get_command_center", raise_probe_error)

    section, payload = osmod._work_section_from_command_center()

    assert section["items"][0]["status"] == "unknown"
    assert isinstance(payload["error"], str)
    assert payload["error"]
    assert payload["projects_completion_pct"] is None
    assert payload["live_runtimes"] is None
    assert payload["counts"]["projects"] is None
    assert payload["counts"]["decisions"] is None
    assert payload["counts"]["live_runtimes"] is None
    assert payload["counts"]["stalled"] is None
    assert payload["projects"] == []
    assert payload["live"]["runtimes"] == []
    assert payload["decisions"] == []
    assert payload["stalled"] == []


def test_activity_section_pulse_exception_returns_honest_no_data(monkeypatch):
    from hermes_cli import dashboard_health
    from hermes_cli import dashboard_os as osmod
    from hermes_cli import pulse_data

    def raise_queue_probe_error(_window: str):
        raise RuntimeError("pulse queue boom")

    def raise_pulse_probe_error(*_args, **_kwargs):
        raise RuntimeError("pulse kpis boom")

    monkeypatch.setattr(dashboard_health, "_get_queue_depth", raise_queue_probe_error)
    monkeypatch.setattr(pulse_data, "build_pulse_kpis", raise_pulse_probe_error)
    monkeypatch.setattr(pulse_data, "build_pulse_queue", raise_pulse_probe_error)

    section, payload = osmod._activity_section_from_pulse()

    assert section["items"][0]["status"] == "unknown"
    assert isinstance(payload["error"], str)
    assert payload["error"]
    assert payload["created_7d"] is None
    assert payload["open_now"] is None
    assert payload["queue_7d"]["openNow"] is None
    assert payload["queue_7d"]["points"] == []
    assert payload["queue"]["cards"] == []
    assert payload["cards"] == []
    assert payload["kpis"] == {}
