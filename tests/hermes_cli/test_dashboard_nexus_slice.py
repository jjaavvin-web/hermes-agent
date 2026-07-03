"""Regression tests for the thin Nexus backup -> off-box live slice."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hermes_cli import dashboard_nexus_slice as slice_mod

TRUTH_KEYS = set(slice_mod.TRUTH_KEYS)
PROBE_STATES = slice_mod.PROBE_STATES


def _touch(path: Path, age_s: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")
    when = time.time() - age_s
    os.utime(path, (when, when))


def _seed_backups(root: Path) -> None:
    mvms_dir = root / "backups" / "mvms"
    _touch(mvms_dir / "mvms-canonical-20260702.sql.gz", age_s=120)
    _touch(mvms_dir / "honcho-live-store-20260702.sql.gz", age_s=180)
    _touch(mvms_dir / "hermes-app-state-20260702.tar.gz", age_s=240)


def _patch_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(slice_mod.dashboard_os, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(slice_mod.dashboard_os, "HOME", tmp_path)
    slice_mod.clear_cache_for_tests()


def _truth() -> dict[str, Any]:
    payload = slice_mod.backup_offbox_slice()
    assert set(payload) == TRUTH_KEYS
    assert payload["id"] == slice_mod.TRUTH_ID
    assert payload["probe_state"] in PROBE_STATES
    assert isinstance(payload["evidence_refs"], list)
    assert payload["evidence_refs"]
    json.dumps(payload)
    return payload


def test_marker_present_and_fresh_returns_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _patch_home(monkeypatch, tmp_path)
    _seed_backups(tmp_path)
    _touch(tmp_path / "backups" / "mvms" / "OFFBOX-REPLICATION-OK", age_s=60)

    payload = _truth()

    assert payload["probe_state"] == "live"
    assert payload["freshness_age_s"] < 300
    assert any("OFFBOX-REPLICATION-OK" in ref for ref in payload["evidence_refs"])


def test_marker_absent_returns_broken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _patch_home(monkeypatch, tmp_path)
    _seed_backups(tmp_path)

    payload = _truth()

    assert payload["probe_state"] == "broken"
    assert any(ref.startswith("marker-scan:") for ref in payload["evidence_refs"])
    assert "replication success marker" in payload["what_would_prove_green"]


def test_marker_present_but_old_returns_stale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _patch_home(monkeypatch, tmp_path)
    _seed_backups(tmp_path)
    _touch(tmp_path / "backups" / "mvms" / "OFFBOX-REPLICATION-OK", age_s=15 * 24 * 3600)

    payload = _truth()

    assert payload["probe_state"] == "stale"


def test_probe_exception_degrades_to_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _patch_home(monkeypatch, tmp_path)
    _seed_backups(tmp_path)

    def boom() -> dict[str, Any]:
        raise RuntimeError("synthetic probe failure")

    monkeypatch.setattr(slice_mod.dashboard_os, "_section_backups", boom)

    payload = _truth()

    assert payload["probe_state"] == "unknown"
    assert any("synthetic probe failure" in ref for ref in payload["evidence_refs"])


def test_anti_wedge_route_body_json_expected_id_and_auth(tmp_path: Path):
    web_dist = tmp_path / "web_dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "assets" / "index.js").write_text("console.log('ok');", encoding="utf-8")
    (web_dist / "index.html").write_text(
        "<!doctype html><html><head></head><body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    env["HERMES_WEB_DIST"] = str(web_dist)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    expected_keys_json = json.dumps(list(slice_mod.TRUTH_KEYS))
    expected_states_json = json.dumps(list(slice_mod.PROBE_STATES))
    script = textwrap.dedent(
        f"""
        import json
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN

        client = TestClient(web_server.app)
        anon = client.get('/api/dashboard/nexus/slice/backup-offbox')
        assert anon.status_code == 401, anon.text
        response = client.get(
            '/api/dashboard/nexus/slice/backup-offbox',
            headers={{_SESSION_HEADER_NAME: _SESSION_TOKEN}},
        )
        assert response.status_code == 200
        assert response.content, '200 with empty body is a wedge'
        payload = response.json()
        assert payload['id'] == 'edge:nightly-backup->off-box'
        assert set(payload) == set(json.loads({expected_keys_json!r}))
        assert payload['probe_state'] in set(json.loads({expected_states_json!r}))
        """
    )
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def test_static_demo_page_route_registered_with_token_and_labels(tmp_path: Path):
    web_dist = tmp_path / "web_dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "assets" / "index.js").write_text("console.log('ok');", encoding="utf-8")
    (web_dist / "index.html").write_text("<!doctype html><html><head></head><body></body></html>", encoding="utf-8")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    env["HERMES_WEB_DIST"] = str(web_dist)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
    script = textwrap.dedent(
        """
        from fastapi.testclient import TestClient
        from hermes_cli import web_server

        client = TestClient(web_server.app)
        response = client.get('/nexus-slice')
        assert response.status_code == 200
        assert response.text
        assert 'window.__HERMES_SESSION_TOKEN__=' in response.text
        assert response.text.count('data-live-bound="true"') == 1
        assert response.text.count('sampled — static') >= 8
        """
    )
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def test_new_module_ast_read_only_and_get_only_and_encoding_guard():
    module_path = Path(slice_mod.__file__)
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_attrs = {"write_text", "write_bytes", "open", "unlink", "rename", "replace", "mkdir", "touch"}
    forbidden_subprocess = {"run", "Popen", "call", "check_call", "check_output"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                pytest.fail(f"mutating file I/O call present: {func.attr}")
            if (
                isinstance(func, ast.Attribute)
                and func.attr in forbidden_subprocess
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                pytest.fail(f"mutating subprocess call present: {func.attr}")
        if isinstance(node, ast.Import):
            assert not any(alias.name == "subprocess" for alias in node.names)
    route_methods: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                route_methods.append(dec.func.attr)
    assert route_methods == ["get"]

    for py_path in [
        module_path,
        module_path.parent.parent / "tests" / "hermes_cli" / "test_dashboard_nexus_slice.py",
    ]:
        py_source = py_path.read_text(encoding="utf-8")
        py_tree = ast.parse(py_source)
        for node in ast.walk(py_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"read_text", "write_text"}:
                    assert any(kw.arg == "encoding" for kw in node.keywords), (
                        f"{py_path}:{node.lineno} missing explicit encoding"
                    )
