"""Tests for the all-territories Nexus truth API."""
from __future__ import annotations

import ast
import copy
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import dashboard_nexus as nexus
from hermes_cli import dashboard_nexus_slice as slice_mod

ALL_TRUTH_KEYS = set(slice_mod.TRUTH_KEYS)


def _fixture() -> dict[str, Any]:
    fixture = json.loads((Path(__file__).with_name("nexus_donor_fixture.json")).read_text(encoding="utf-8"))
    fixture["os"].setdefault("graph", nexus.dashboard_os._build_os_graph(fixture["os"]["sections"]))
    return fixture


def _patch_donors(monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any] | None = None) -> None:
    fixture = copy.deepcopy(fixture or _fixture())
    nexus.clear_cache_for_tests()
    slice_mod.clear_cache_for_tests()
    monkeypatch.setattr(nexus.dashboard_os, "get_os_snapshot", lambda: copy.deepcopy(fixture["os"]))
    monkeypatch.setattr(nexus.dashboard_connectome, "get_connectome_snapshot", lambda: copy.deepcopy(fixture["connectome"]))
    truth = {
        "id": slice_mod.TRUTH_ID,
        "probe_state": "broken",
        "freshness_age_s": 123.0,
        "confidence": "single-probe",
        "evidence_refs": ["file:fixture-offbox-marker:missing"],
        "last_checked": "2026-07-03T00:00:00+00:00",
        "safe_next_action": "re-probe only",
        "locked_actions": ["fix/restart/deploy/config/provider/credential/cron mutations"],
        "what_would_prove_green": "replication success marker present",
        "what_breaks_if_false": "false off-box state hides backup loss risk",
    }
    monkeypatch.setattr(nexus.dashboard_nexus_slice, "backup_offbox_slice", lambda: copy.deepcopy(truth))


def _envelope(monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any] | None = None) -> dict[str, Any]:
    _patch_donors(monkeypatch, fixture)
    return nexus.build_envelope()


def _all_truths(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    return [*(t["truth"] for t in envelope["territories"]), *(s["truth"] for s in envelope["systems"]), *(e["truth"] for e in envelope["edges"])]


def test_topology_counts_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _envelope(monkeypatch)
    assert len(env["territories"]) == 9
    assert len(env["systems"]) == 40
    assert len(env["edges"]) == 38
    recount = {
        "territories_total": len(env["territories"]),
        "systems_total": len(env["systems"]),
        "edges_total": len(env["edges"]),
        "edges_probed": sum(1 for e in env["edges"] if e["kind"] == "probed"),
        "edges_manual": sum(1 for e in env["edges"] if e["kind"] == "manual"),
    }
    assert env["coverage"] == recount == {
        "territories_total": 9,
        "systems_total": 40,
        "edges_total": 38,
        "edges_probed": 2,
        "edges_manual": 36,
    }


def test_every_truth_object_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _envelope(monkeypatch)
    truths = _all_truths(env)
    assert len(truths) == 87
    for truth in truths:
        assert set(truth) == ALL_TRUTH_KEYS
        assert truth["probe_state"] in slice_mod.PROBE_STATES
        assert isinstance(truth["evidence_refs"], list) and truth["evidence_refs"]
        assert isinstance(truth["freshness_age_s"], int | float) and truth["freshness_age_s"] >= 0
        json.dumps(truth)
        assert nexus.validate_truth_object(dict(truth), truth["id"]) == truth


def test_backup_edge_joins_shipped_slice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_donors(monkeypatch)
    env = nexus.build_envelope()
    edge_truth = next(e["truth"] for e in env["edges"] if e["id"] == slice_mod.TRUTH_ID)
    assert edge_truth["id"] is slice_mod.TRUTH_ID
    assert edge_truth == slice_mod.backup_offbox_slice()
    source = Path(nexus.__file__).read_text(encoding="utf-8")
    assert "edge:nightly-backup-off-box" not in source
    assert "edge:nightly-backup-offbox" not in source

    web_dist = tmp_path / "web_dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    env_vars = os.environ.copy()
    env_vars["HERMES_HOME"] = str(tmp_path / ".hermes")
    env_vars["HERMES_WEB_DIST"] = str(web_dist)
    env_vars["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env_vars.get("PYTHONPATH", "")
    script = """
from fastapi.testclient import TestClient
from hermes_cli import web_server, dashboard_nexus_slice
from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN
client = TestClient(web_server.app)
r = client.get('/api/dashboard/nexus/object/edge:nightly-backup->off-box', headers={_SESSION_HEADER_NAME: _SESSION_TOKEN})
assert r.status_code == 200, r.text
assert r.json()['object']['truth']['id'] == dashboard_nexus_slice.TRUTH_ID
"""
    subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2], env=env_vars, check=True, text=True, capture_output=True)


def test_manual_edges_stay_manual_and_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _envelope(monkeypatch)
    expected_age = nexus._manual_freshness_age_s()
    for edge in env["edges"]:
        if edge["kind"] == "manual":
            truth = edge["truth"]
            assert truth["probe_state"] == "manual"
            assert truth["confidence"] == "claimed"
            assert any(ref.startswith("design:") for ref in truth["evidence_refs"])
            assert truth["freshness_age_s"] >= 0
            assert abs(truth["freshness_age_s"] - expected_age) <= 5
            assert edge["id"] not in {d["object_id"] for d in env["diagnostics"]}


def test_no_missing_state_ever(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture()
    fixture["os"]["sections"] = []
    env = _envelope(monkeypatch, fixture)
    states = [t["probe_state"] for t in _all_truths(env)]
    assert "missing" not in states
    assert "unknown" in states
    assert next(e for e in env["edges"] if e["id"] == slice_mod.TRUTH_ID)["truth"]["probe_state"] == "broken"


@pytest.mark.parametrize("donor", ["os", "connectome", "slice"])
def test_never_500_on_donor_failure(monkeypatch: pytest.MonkeyPatch, donor: str) -> None:
    _patch_donors(monkeypatch)
    if donor == "os":
        monkeypatch.setattr(nexus.dashboard_os, "get_os_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("os boom")))
    elif donor == "connectome":
        monkeypatch.setattr(nexus.dashboard_connectome, "get_connectome_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("conn boom")))
    else:
        monkeypatch.setattr(nexus.dashboard_nexus_slice, "backup_offbox_slice", lambda: (_ for _ in ()).throw(RuntimeError("slice boom")))
    nexus.clear_cache_for_tests()
    app = FastAPI()
    app.include_router(nexus.router)
    client = TestClient(app)
    response = client.get("/api/dashboard/nexus")
    assert response.status_code == 200
    env = response.json()
    assert env["status"] == "degraded"
    assert len(env["territories"]) == 9 and len(env["systems"]) == 40 and len(env["edges"]) == 38
    assert any(any(str(ref).startswith("exception:") for ref in t["evidence_refs"]) for t in _all_truths(env))


def test_selector_audit_against_real_fixture() -> None:
    fixture = _fixture()
    for system_id, selector in nexus.SYSTEM_SOURCE_MAP.items():
        donor, path = selector
        assert nexus._resolve_selector(fixture[donor], donor, path) is not None, (system_id, selector)


def test_fixture_hygiene() -> None:
    text = Path(__file__).with_name("nexus_donor_fixture.json").read_text(encoding="utf-8")
    forbidden = [r"token", r"secret", r"api[_-]?key", r"authorization", r"bearer", r"[A-Fa-f0-9]{40,}", r"fresh", r"tailadb109", r"josep", r"/home/josep"]
    hits = {pat: re.findall(pat, text, flags=re.IGNORECASE) for pat in forbidden if re.findall(pat, text, flags=re.IGNORECASE)}
    assert hits == {}


def test_cache_ttl_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_donors(monkeypatch)
    first = nexus.get_cached_envelope()
    second = nexus.get_cached_envelope()
    assert first["generated_at"] == second["generated_at"]
    time.sleep(0.01)
    nexus.clear_cache_for_tests()
    third = nexus.get_cached_envelope()
    assert third["generated_at"] != first["generated_at"]


def test_anonymous_401_and_anti_wedge(tmp_path: Path) -> None:
    web_dist = tmp_path / "web_dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "assets" / "index.js").write_text("console.log('ok')", encoding="utf-8")
    (web_dist / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    env["HERMES_WEB_DIST"] = str(web_dist)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    script = textwrap.dedent(
        """
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN
        client = TestClient(web_server.app)
        routes = ['/api/dashboard/nexus', '/api/dashboard/nexus/coverage', '/api/dashboard/nexus/object/system:memory/mvms']
        for route in routes:
            anon = client.get(route)
            assert anon.status_code == 401, (route, anon.status_code, anon.text)
        authed = client.get('/api/dashboard/nexus', headers={_SESSION_HEADER_NAME: _SESSION_TOKEN})
        assert authed.status_code == 200, authed.text
        assert authed.content, '200 with empty body is a wedge'
        payload = authed.json()
        assert {'generated_at','territories','systems','edges','coverage'} <= set(payload)
        assert len(payload['territories']) == 9 and len(payload['systems']) == 40 and len(payload['edges']) == 38
        """
    )
    subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2], env=env, check=True, text=True, capture_output=True)


def test_object_route_all_three_id_families(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_donors(monkeypatch)
    app = FastAPI()
    app.include_router(nexus.router)
    client = TestClient(app)
    for object_id in ["territory:memory", "system:memory/mvms", slice_mod.TRUTH_ID]:
        response = client.get(f"/api/dashboard/nexus/object/{object_id}")
        assert response.status_code == 200, (object_id, response.text)
        body = response.json()
        assert body["object"]["id"] == object_id
        assert set(body["object"]["truth"]) == ALL_TRUTH_KEYS
    missing = client.get("/api/dashboard/nexus/object/system:nope/nada")
    assert missing.status_code == 404
    assert missing.json()["error"] == "unknown truth object id"


def test_read_only_ast_guard() -> None:
    source = Path(nexus.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_attrs = {"write_text", "write_bytes", "open", "unlink", "rename", "replace", "mkdir", "touch"}
    forbidden_modules = {"subprocess"}
    route_methods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name in forbidden_modules for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden_modules
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                pytest.fail(f"mutating file I/O call present: {func.attr}")
            if isinstance(func, ast.Name) and func.id in {"open"}:
                pytest.fail("open() call present")
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Attribute):
                    route_methods.append(target.attr)
    assert set(route_methods) <= {"get"}
    assert route_methods.count("get") == 3


def test_territory_rollup_worst_wins_and_content(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _envelope(monkeypatch)
    memory = next(t for t in env["territories"] if t["id"] == "territory:memory")
    members = [next(s for s in env["systems"] if s["id"] == member_id) for member_id in memory["members"]]
    worst = min(members, key=lambda s: nexus.STATE_RANK[s["truth"]["probe_state"]])
    truth = memory["truth"]
    assert truth["probe_state"] == worst["truth"]["probe_state"]
    assert truth["evidence_refs"][0].startswith("rollup:")
    assert truth["evidence_refs"][1] == f"worst:{worst['id']}:{worst['truth']['probe_state']}"
    assert truth["freshness_age_s"] == max(s["truth"]["freshness_age_s"] for s in members)
    assert truth["last_checked"] == env["generated_at"]

    all_manual = [
        nexus._manual_system_truth("system:test/a", "now"),
        nexus._manual_system_truth("system:test/b", "now"),
    ]
    rolled = nexus._territory_truth_from_members("territory:test", all_manual, "now")
    assert rolled["probe_state"] == "manual"
    assert rolled["confidence"] == "claimed"


def test_bridge_edge_absent_is_unknown_never_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture()
    fixture["connectome"]["edges"] = [e for e in fixture["connectome"].get("edges", []) if e.get("id") != "projects-brain"]
    env = _envelope(monkeypatch, fixture)
    bridge = next(e for e in env["edges"] if e["id"] == "edge:kanban-db->mvms--bridge")
    assert bridge["truth"]["probe_state"] == "unknown"
    assert any("verifier-absent:projects-brain" in ref for ref in bridge["truth"]["evidence_refs"])
    assert bridge["id"] not in {d["object_id"] for d in env["diagnostics"]}

    fixture = _fixture()
    env = _envelope(monkeypatch, fixture)
    bridge = next(e for e in env["edges"] if e["id"] == "edge:kanban-db->mvms--bridge")
    assert bridge["truth"]["probe_state"] == "live"
    assert any("donor:connectome:edges/projects-brain" in ref for ref in bridge["truth"]["evidence_refs"])


def test_honesty_caps_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture()
    for section in fixture["os"]["sections"]:
        for item in section.get("items", []):
            item["status"] = "green"
    for node in fixture["connectome"]["nodes"]:
        node["status"] = "ok"
    env = _envelope(monkeypatch, fixture)
    by_id = {s["id"]: s["truth"] for s in env["systems"]}
    for system_id in [
        "system:providers/claude-max",
        "system:providers/openrouter",
        "system:ingest/ict-brain",
        "system:ingest/opus-extractor",
        "system:ingest/x_search",
    ]:
        assert by_id[system_id]["probe_state"] == "manual"
        assert by_id[system_id]["confidence"] == "claimed"
        assert any(ref.startswith("honesty-cap:") for ref in by_id[system_id]["evidence_refs"])
    assert by_id["system:memory/hermes-memories"]["probe_state"] == "unknown"
    assert by_id["system:surfaces/dashboard"]["probe_state"] == "unknown"
    assert by_id["system:protection/compactor"]["probe_state"] == "gated"
    assert not any(by_id[system_id]["probe_state"] == "live" for system_id in nexus.HONESTY_CAPS)

    fixture = _fixture()
    for section in fixture["os"]["sections"]:
        for item in section.get("items", []):
            if item.get("name") == "claude_cli":
                item["status"] = "red"
    env = _envelope(monkeypatch, fixture)
    by_id = {s["id"]: s["truth"] for s in env["systems"]}
    assert by_id["system:providers/claude-max"]["probe_state"] == "broken"


def test_real_freshness_parsed_from_donor_age_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _envelope(monkeypatch)
    systems = {system["id"]: system["truth"] for system in env["systems"]}

    backup = systems["system:protection/nightly-backup"]
    assert backup["freshness_age_s"] == pytest.approx(22.3 * 3600, rel=0.001)

    state_db = systems["system:memory/state-db"]
    assert state_db["freshness_age_s"] == pytest.approx(0.0, abs=1.0)

    veracrypt = systems["system:protection/veracrypt"]
    assert veracrypt["freshness_age_s"] == pytest.approx(3.5 * 86400, rel=0.001)

    bridge = next(edge["truth"] for edge in env["edges"] if edge["id"] == "edge:kanban-db->mvms--bridge")
    last_seen = "2026-07-03T07:53:30.983074+00:00"
    expected = (
        datetime.fromisoformat(env["generated_at"]) - datetime.fromisoformat(last_seen)
    ).total_seconds()
    assert bridge["freshness_age_s"] == pytest.approx(expected, abs=2.0)
    assert bridge["freshness_age_s"] > 0


def test_unlocked_connectome_status_falls_through_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _envelope(monkeypatch)
    deploy = next(system["truth"] for system in env["systems"] if system["id"] == "system:git/deploy-div")
    assert deploy["probe_state"] == "unknown"
    assert "unmapped-status:not-serving" in deploy["evidence_refs"]


@pytest.mark.parametrize("donor,status", [("os", "mystery-os"), ("connectome", "mystery-connectome")])
def test_any_unrecognized_donor_status_maps_unknown_with_unmapped_tag(donor: str, status: str) -> None:
    state, evidence = nexus._status_to_state(donor, status)
    assert state == "unknown"
    assert evidence == f"unmapped-status:{status}"


@pytest.mark.parametrize(
    ("system_id", "selector"),
    [
        ("system:ingest/ict-brain", "graph/ict-brain"),
        ("system:ingest/opus-extractor", "graph/opus_extractor"),
        ("system:ingest/x_search", "graph/x_search"),
        ("system:protection/compactor", "graph/mvms-compactor"),
    ],
)
def test_honesty_cap_selectors_resolve_real_graph_nodes(monkeypatch: pytest.MonkeyPatch, system_id: str, selector: str) -> None:
    fixture = _fixture()
    donor, path = nexus.SYSTEM_SOURCE_MAP[system_id]
    assert donor == "os"
    assert path == selector
    selected = nexus._resolve_selector(fixture["os"], donor, path)
    assert selected is not None
    assert selected["id"] == selector.split("/", 1)[1]

    env = _envelope(monkeypatch, fixture)
    truth = next(system["truth"] for system in env["systems"] if system["id"] == system_id)
    assert any(f"donor:os:{selector}" in ref for ref in truth["evidence_refs"])
