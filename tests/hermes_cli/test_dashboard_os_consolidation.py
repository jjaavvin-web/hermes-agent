from __future__ import annotations

from fastapi.testclient import TestClient


def test_os_snapshot_includes_consolidated_sections(monkeypatch):
    from hermes_cli import dashboard_os as osmod

    def section(section_id: str, label: str):
        return osmod._section(section_id, label, [osmod._item("probe", "green", "ok")])

    for name, section_id, label in [
        ("_section_gateway", "gateway", "Gateway"),
        ("_section_providers", "providers", "Providers"),
        ("_section_containers", "containers", "Containers"),
        ("_section_systemd", "systemd", "Systemd"),
        ("_section_backups", "backups", "Backups"),
        ("_section_memory_stores", "memory_stores", "Memory Stores"),
        ("_section_cron", "cron", "Cron"),
        ("_section_host", "host", "Host"),
    ]:
        monkeypatch.setattr(osmod, name, lambda section_id=section_id, label=label: section(section_id, label))

    monkeypatch.setattr(
        osmod,
        "_repo_section_from_git_health",
        lambda: (
            osmod._section("repo", "Repo", [osmod._item("readiness", "amber", "1/2 lanes ready", "50%")]),
            {"readiness_pct": 50, "summary": {"total_uncommitted": 1}, "best_move": {"text": "commit", "severity": "warn"}},
        ),
    )
    monkeypatch.setattr(
        osmod,
        "_work_section_from_command_center",
        lambda: (
            osmod._section("work", "Work", [osmod._item("projects_completion", "green", "projects", "90%")]),
            {"projects": [], "live": {"runtimes": []}, "decisions": [], "stalled": [], "projects_completion_pct": 90, "live_runtimes": 0, "counts": {"projects": 0, "decisions": 0, "live_runtimes": 0, "stalled": 0}},
        ),
    )
    monkeypatch.setattr(
        osmod,
        "_activity_section_from_pulse",
        lambda: (
            osmod._section("activity", "Activity", [osmod._item("created_7d", "green", "recent tasks", "3")]),
            {"queue_7d": {"range": "7d", "points": [], "openNow": 0}, "created_7d": 3, "open_now": 0, "cards": []},
        ),
    )
    monkeypatch.setattr(osmod, "_build_os_graph", lambda sections: {"nodes": [], "edges": [], "section_count": len(sections)})

    snapshot = osmod._build_os_snapshot()

    section_ids = [section["id"] for section in snapshot["sections"]]
    assert section_ids[-3:] == ["repo", "work", "activity"]
    assert len(snapshot["sections"]) == 12
    assert snapshot["attention"]["posture"] == snapshot["overall"]
    assert snapshot["repo"]["readiness_pct"] == 50
    assert snapshot["work"]["projects_completion_pct"] == 90
    assert snapshot["activity"]["created_7d"] == 3


def test_os_endpoint_serves_consolidated_snapshot(monkeypatch):
    from hermes_cli import dashboard_os as osmod
    from hermes_cli import web_server as ws

    payload = {
        "generated_at": "2026-06-12T00:00:00+00:00",
        "overall": "green",
        "sections": [
            {"id": "repo", "label": "Repo", "status": "green", "items": []},
            {"id": "work", "label": "Work", "status": "green", "items": []},
            {"id": "activity", "label": "Activity", "status": "green", "items": []},
        ],
        "diagnostics": [],
        "attention": {"posture": "green", "chips": []},
        "repo": {"readiness_pct": 100, "summary": {}, "best_move": {"text": "clear", "severity": "ready"}},
        "work": {"projects": [], "live": {"runtimes": []}, "decisions": [], "stalled": [], "projects_completion_pct": 100, "live_runtimes": 0, "counts": {"projects": 0, "decisions": 0, "live_runtimes": 0, "stalled": 0}},
        "activity": {"queue_7d": {"range": "7d", "points": [], "openNow": 0}, "created_7d": 0, "open_now": 0, "cards": []},
        "graph": {"nodes": [], "edges": []},
    }
    monkeypatch.setattr(osmod, "get_os_snapshot", lambda: payload)
    monkeypatch.setattr(osmod, "_OS_CACHE", None)
    ws.app.state.auth_required = False

    client = TestClient(ws.app)
    response = client.get("/api/dashboard/os", headers={"X-Hermes-Session-Token": ws._SESSION_TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert {"attention", "repo", "work", "activity"}.issubset(body)
    assert [section["id"] for section in body["sections"]] == ["repo", "work", "activity"]
