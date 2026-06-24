from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from hermes_cli import dashboard_connectome as dc


EXPECTED_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "auth",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "jwt",
    "key",
    "password",
    "secret",
    "token",
)


def _task_row(tmp_path: Path, status: str | None, started_at: str | None = None) -> sqlite3.Row:
    db_path = tmp_path / "row.db"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE tasks (status TEXT, started_at TEXT)")
    con.execute("INSERT INTO tasks VALUES (?, ?)", (status, started_at))
    row = con.execute("SELECT status, started_at FROM tasks").fetchone()
    con.close()
    return row


def _create_tasks_db(tmp_path: Path, rows: list[tuple[str, str, str, int, str | None, str | None, str | None, str]]) -> Path:
    db_path = tmp_path / "kanban.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE tasks (
            id TEXT,
            title TEXT,
            status TEXT,
            priority INTEGER,
            started_at TEXT,
            completed_at TEXT,
            branch_name TEXT,
            created_at TEXT
        )
        """
    )
    con.executemany(
        "INSERT INTO tasks (id, title, status, priority, started_at, completed_at, branch_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return db_path


def _assert_probe_shape(result: dc.ProbeResult, hub_id: str) -> None:
    assert set(result) == {"hub", "leaves", "edges"}
    assert result["hub"]["id"] == hub_id
    assert result["hub"]["kind"] == "hub"
    assert isinstance(result["leaves"], list)
    assert isinstance(result["edges"], list)
    assert result["hub"]["provSource"] == result["hub"]["provenance"]["source"]


# ---------------------------------------------------------------------------
# Redaction + provenance
# ---------------------------------------------------------------------------


def test_secret_markers_match_source_contract() -> None:
    assert dc.SECRET_MARKERS == EXPECTED_SECRET_MARKERS


@pytest.mark.parametrize("marker", EXPECTED_SECRET_MARKERS)
def test_redact_value_redacts_each_secret_marker(marker: str) -> None:
    assert dc._redact_value("visible-secret", f"root.{marker}") == "[REDACTED]"


@pytest.mark.parametrize("empty_value", [None, "", [], {}])
def test_redact_value_preserves_empty_secret_values(empty_value: object) -> None:
    assert dc._redact_value(empty_value, "model.api_key") == empty_value


def test_redact_value_recurses_into_nested_dicts_and_leaves_benign_values() -> None:
    value = {"model": {"api_key": "abc123", "provider": "openai"}, "plain": "safe"}
    assert dc._redact_value(value) == {
        "model": {"api_key": "[REDACTED]", "provider": "openai"},
        "plain": "safe",
    }


def test_redact_value_recurses_into_list_elements_when_parent_path_is_benign() -> None:
    value = [{"name": "a", "token": "secret-token"}, {"name": "b", "count": 2}]
    assert dc._redact_value(value, "providers") == [
        {"name": "a", "token": "[REDACTED]"},
        {"name": "b", "count": 2},
    ]


def test_redact_value_redacts_whole_nonempty_list_when_parent_path_is_secret() -> None:
    assert dc._redact_value(["a", "b"], "providers.tokens") == "[REDACTED]"


def test_redact_value_truncates_long_plain_strings() -> None:
    long_value = "x" * 121
    redacted = dc._redact_value(long_value, "plain.detail")
    assert redacted == f"{'x' * 117}..."
    assert len(redacted) == 120


def test_prov_shape_redacts_secret_field_value() -> None:
    prov = dc._prov("config", "query", "model.api_key", "abc123")
    assert set(prov) == {"source", "query", "field", "value", "lastSeen"}
    assert prov["value"] == "[REDACTED]"
    assert datetime.fromisoformat(prov["lastSeen"]).tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


def test_worst_status_empty_defaults_unknown() -> None:
    assert dc._worst_status([]) == "unknown"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["completed", "ok", "source unreachable"], "source unreachable"),
        (["red", "completed", "blocked"], "red"),
        (["queued", "unknown", "completed"], "queued"),
        (["mystery", "completed"], "mystery"),
    ],
)
def test_worst_status_uses_status_rank_precedence(statuses: list[str], expected: str) -> None:
    assert dc._worst_status(statuses) == expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "unknown"),
        (["source unreachable", "source unreachable"], "source unreachable"),
        (["blocked", "red"], "blocked"),
        (["blocked", "ok", "red", "completed", "queued"], "degraded"),
        (["blocked", "ok", "completed", "active", "queued"], "active"),
        (["queued", "completed", "ok"], "ok"),
    ],
)
def test_hub_rollup_status_thresholds(statuses: list[str], expected: str) -> None:
    assert dc._hub_rollup_status(statuses) == expected


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_paginate_round_trip_cursor_covers_all_leaves_without_overlap() -> None:
    leaves = [{"id": f"leaf-{idx}"} for idx in range(5)]
    page1, cursor1 = dc._paginate(leaves, None, 2)
    page2, cursor2 = dc._paginate(leaves, cursor1, 2)
    page3, cursor3 = dc._paginate(leaves, cursor2, 2)
    assert [item["id"] for item in page1 + page2 + page3] == [item["id"] for item in leaves]
    assert {item["id"] for item in page1}.isdisjoint({item["id"] for item in page2})
    assert cursor1 == "2"
    assert cursor2 == "4"
    assert cursor3 is None


@pytest.mark.parametrize(
    ("leaves", "cursor", "limit", "expected_page", "expected_cursor"),
    [
        ([{"id": "a"}, {"id": "b"}], None, 5, [{"id": "a"}, {"id": "b"}], None),
        ([], None, 5, [], None),
        ([{"id": "a"}], "9", 5, [], None),
        ([{"id": "a"}, {"id": "b"}], "garbage", 1, [{"id": "a"}], "1"),
        ([{"id": "a"}, {"id": "b"}], "-10", 1, [{"id": "a"}], "1"),
        ([{"id": "a"}, {"id": "b"}], None, 0, [], "0"),
    ],
)
def test_paginate_boundaries_and_malformed_cursor(
    leaves: list[dict[str, str]],
    cursor: str | None,
    limit: int,
    expected_page: list[dict[str, str]],
    expected_cursor: str | None,
) -> None:
    assert dc._paginate(leaves, cursor, limit) == (expected_page, expected_cursor)


# ---------------------------------------------------------------------------
# Envelope, node, edge, provenance flattening
# ---------------------------------------------------------------------------


def test_safe_envelope_success_shape() -> None:
    envelope = dc._safe_envelope()
    assert envelope["nodes"] == []
    assert envelope["edges"] == []
    assert envelope["meta"]["status"] == "ok"
    assert envelope["meta"]["stub"] is False
    assert "error" not in envelope["meta"]


def test_safe_envelope_error_shape() -> None:
    envelope = dc._safe_envelope("boom")
    assert envelope["meta"]["status"] == "degraded"
    assert envelope["meta"]["error"] == "boom"


def test_with_flat_prov_adds_compatibility_fields() -> None:
    node = {"id": "n", "provenance": {"source": "s", "query": "q", "field": "f", "value": "v", "lastSeen": "t"}}
    flattened = dc._with_flat_prov(node)
    assert flattened["real"] is True
    assert flattened["provSource"] == "s"
    assert flattened["provQuery"] == "q"
    assert flattened["provField"] == "f"
    assert flattened["provValue"] == "v"
    assert flattened["lastSeen"] == "t"


def test_node_includes_optional_count_detail_metric_and_extra() -> None:
    node = dc._node(
        "node-1",
        "Node 1",
        "projects",
        "task",
        "queued",
        dc._prov("source", "query", "field", "value"),
        count=3,
        detail="details",
        metric={"a": 1},
        extra={"task_id": "T1"},
    )
    assert node["id"] == "node-1"
    assert node["label"] == "Node 1"
    assert node["cluster"] == "projects"
    assert node["kind"] == "task"
    assert node["status"] == "queued"
    assert node["count"] == 3
    assert node["detail"] == "details"
    assert node["metric"] == {"a": 1}
    assert node["task_id"] == "T1"
    assert node["provSource"] == "source"


def test_hub_node_uses_canonical_label_and_hub_kind() -> None:
    hub = dc._hub_node("projects", 7, "active", dc._prov("s", "q", "f", "v"), metric={"active": 7})
    assert hub["id"] == "projects"
    assert hub["label"] == dc.HUB_LABELS["projects"]
    assert hub["kind"] == "hub"
    assert hub["count"] == 7
    assert hub["metric"] == {"active": 7}


def test_edge_default_shape_and_flattened_provenance() -> None:
    edge = dc._edge("e1", "a", "b", "bridge-label")
    assert edge["id"] == "e1"
    assert edge["source"] == "a"
    assert edge["target"] == "b"
    assert edge["label"] == "bridge-label"
    assert edge["mechanism"] == "bridge-label"
    assert edge["kind"] == "bridge"
    assert edge["verified"] is True
    assert edge["provSource"] == "connectome"


def test_edge_accepts_custom_provenance_and_verified_false() -> None:
    edge = dc._edge("e2", "a", "b", "manual", provenance=dc._prov("s", "q", "f", "v"), verified=False)
    assert edge["verified"] is False
    assert edge["provSource"] == "s"
    assert edge["provValue"] == "v"


def test_unreachable_hub_shape() -> None:
    result = dc._unreachable_hub("deploy", "src", "query", RuntimeError("nope"))
    _assert_probe_shape(result, "deploy")
    assert result["hub"]["status"] == "source unreachable"
    assert result["hub"]["detail"] == "nope"
    assert result["hub"]["count"] == 0
    assert result["leaves"] == []


# ---------------------------------------------------------------------------
# Mappings + timestamps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "started_at", "expected"),
    [
        ("done", None, "completed"),
        ("archived", None, "completed"),
        ("blocked", None, "blocked"),
        ("ready", "2026-01-01T00:00:00Z", "in-progress"),
        ("ready", None, "queued"),
        ("scheduled", None, "queued"),
        ("todo", None, "queued"),
        ("triage", None, "queued"),
        ("custom", None, "custom"),
        (None, None, "unknown"),
    ],
)
def test_project_status_mapping(tmp_path: Path, raw: str | None, started_at: str | None, expected: str) -> None:
    assert dc._project_status(_task_row(tmp_path, raw, started_at)) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("green", "ok"), ("red", "blocked"), ("amber", "queued"), ("", "unknown"), ("blue", "blue")],
)
def test_translate_os_status_mapping(raw: str, expected: str) -> None:
    assert dc._translate_os_status(raw) == expected


def test_now_returns_utc_iso_timestamp() -> None:
    parsed = datetime.fromisoformat(dc._now())
    assert parsed.tzinfo == timezone.utc


def test_iso_from_timestamp_converts_numeric_to_utc_iso() -> None:
    assert dc._iso_from_timestamp(0) == "1970-01-01T00:00:00+00:00"


def test_iso_from_timestamp_none_delegates_to_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dc, "_now", lambda: "fixed-now")
    assert dc._iso_from_timestamp(None) == "fixed-now"


# ---------------------------------------------------------------------------
# Dependency-injected probes
# ---------------------------------------------------------------------------


def test_projects_probe_reads_synthetic_kanban_db(tmp_path: Path) -> None:
    db_path = _create_tasks_db(
        tmp_path,
        [
            ("T1", "Ready task", "ready", 3, None, None, "feature/a", "2026-01-03"),
            ("T2", "Running task", "todo", 2, "2026-01-02", None, "feature/b", "2026-01-02"),
            ("T3", "Done task", "done", 1, "2026-01-01", "2026-01-04", "feature/c", "2026-01-01"),
            ("T4", "Archived task", "archived", 9, None, None, "feature/d", "2026-01-04"),
        ],
    )
    result = dc._projects_probe(db_path=db_path)
    _assert_probe_shape(result, "projects")
    assert result["hub"]["count"] == 3
    assert result["hub"]["status"] == "active"
    by_task_id = {leaf["task_id"]: leaf for leaf in result["leaves"]}
    assert set(by_task_id) == {"T1", "T2", "T3"}
    assert by_task_id["T1"]["status"] == "queued"
    assert by_task_id["T2"]["status"] == "in-progress"
    assert by_task_id["T3"]["status"] == "completed"
    assert by_task_id["T1"]["metric"]["branch_name"] == "feature/a"


def test_projects_probe_empty_tasks_table_is_completed_hub(tmp_path: Path) -> None:
    db_path = _create_tasks_db(tmp_path, [])
    result = dc._projects_probe(db_path=db_path)
    _assert_probe_shape(result, "projects")
    assert result["hub"]["count"] == 0
    assert result["hub"]["status"] == "completed"
    assert result["leaves"] == []


def test_projects_probe_missing_table_degrades_to_unreachable(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-table.db"
    sqlite3.connect(db_path).close()
    result = dc._projects_probe(db_path=db_path)
    _assert_probe_shape(result, "projects")
    assert result["hub"]["status"] == "source unreachable"
    assert "no such table" in result["hub"]["detail"]


def test_config_probe_reads_yaml_and_redacts_secret_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "profiles" / "default").mkdir(parents=True)
    (hermes_home / "profiles" / "brain").mkdir(parents=True)
    monkeypatch.setattr(dc, "HERMES_HOME", hermes_home)
    monkeypatch.setenv("HERMES_PROFILE", "brain")
    config_path = tmp_path / "config.yaml"
    config = {
        "model": {"default": "gpt-test", "provider": "openai-codex"},
        "providers": {
            "benign": {"base_url": "http://127.0.0.1"},
            "private": {"api_key": "sk-test", "model": "m"},
        },
        "toolsets": ["terminal", "file"],
        "auxiliary": {"vision": {"provider": "local"}},
        "mcp_servers": {"demo": {"command": "demo"}},
        "personalities": {"direct": {"tone": "plain"}},
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    result = dc._config_probe(config_path=config_path)

    _assert_probe_shape(result, "config")
    assert result["hub"]["status"] == "ok"
    leaves = {leaf["id"]: leaf for leaf in result["leaves"]}
    assert leaves["config:model.default"]["metric"] == "gpt-test"
    assert leaves["config:providers.benign"]["metric"] == {"base_url": "http://127.0.0.1"}
    assert leaves["config:providers.private"]["metric"] == {"api_key": "[REDACTED]", "model": "m"}
    assert leaves["config:active_profile"]["status"] == "active"
    assert "config:profile:brain" in leaves


def test_config_probe_missing_file_degrades_to_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dc, "HERMES_HOME", tmp_path / "home")
    result = dc._config_probe(config_path=tmp_path / "missing.yaml")
    _assert_probe_shape(result, "config")
    assert result["hub"]["status"] == "source unreachable"
    assert "missing.yaml" in result["hub"]["provSource"]


def test_add_config_leaf_applies_metric_and_provenance_redaction(tmp_path: Path) -> None:
    leaves: list[dict[str, object]] = []
    dc._add_config_leaf(leaves, "provider", "provider", "provider", "providers.demo", {"token": "abc", "name": "demo"}, tmp_path / "config.yaml")
    assert leaves[0]["metric"] == {"token": "[REDACTED]", "name": "demo"}
    assert "[REDACTED]" in str(leaves[0]["provValue"])


def test_first_markdown_heading_returns_first_heading(tmp_path: Path) -> None:
    path = tmp_path / "program" / "MASTER-GAMEPLAN.md"
    path.parent.mkdir()
    path.write_text("intro\n# Main Heading\n## Later\n", encoding="utf-8")
    assert dc._first_markdown_heading(path) == "Main Heading"


def test_first_markdown_heading_falls_back_to_parent_name_when_no_heading(tmp_path: Path) -> None:
    path = tmp_path / "fallback-program" / "MASTER-GAMEPLAN.md"
    path.parent.mkdir()
    path.write_text("plain text\n", encoding="utf-8")
    assert dc._first_markdown_heading(path) == "fallback-program"


def test_first_markdown_heading_missing_file_falls_back_to_parent_name(tmp_path: Path) -> None:
    assert dc._first_markdown_heading(tmp_path / "missing-program" / "MASTER-GAMEPLAN.md") == "missing-program"


def test_programs_probe_reads_master_and_recent_build_specs(tmp_path: Path) -> None:
    audits_dir = tmp_path / "audits"
    old_dir = audits_dir / "old"
    new_dir = audits_dir / "new"
    build_dir = audits_dir / "build"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    build_dir.mkdir(parents=True)
    old_file = old_dir / "MASTER-GAMEPLAN.md"
    new_file = new_dir / "MASTER-GAMEPLAN.md"
    build_file = build_dir / "BUILD-SPEC.md"
    old_file.write_text("# Old Program\n", encoding="utf-8")
    new_file.write_text("# New Program\n", encoding="utf-8")
    build_file.write_text("# Build Program\n", encoding="utf-8")
    old_mtime = time.time() - 20 * 86400
    current_mtime = time.time()
    for path, mtime in [(old_file, old_mtime), (new_file, current_mtime), (build_file, current_mtime)]:
        path.touch()
        path.chmod(0o644)
        os.utime(path, (mtime, mtime))

    result = dc._programs_probe(audits_dir=audits_dir)

    _assert_probe_shape(result, "programs")
    labels = {leaf["label"]: leaf for leaf in result["leaves"]}
    assert set(labels) == {"Old Program", "New Program", "Build Program"}
    assert labels["Old Program"]["status"] == "stale"
    assert labels["New Program"]["status"] == "active"
    assert labels["Build Program"]["kind"] == "gameplan"
    assert result["hub"]["count"] == 3


def test_programs_probe_empty_directory_degrades_hub_status_without_raising(tmp_path: Path) -> None:
    audits_dir = tmp_path / "empty-audits"
    audits_dir.mkdir()
    result = dc._programs_probe(audits_dir=audits_dir)
    _assert_probe_shape(result, "programs")
    assert result["hub"]["status"] == "source unreachable"
    assert result["hub"]["count"] == 0
    assert result["leaves"] == []


def test_deploy_probe_reads_tiny_tmp_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, timeout=10)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True, timeout=10)
    subprocess.run(["git", "branch", dc.SERVING_DEPLOY_BRANCH], cwd=repo, check=True, capture_output=True, text=True, timeout=10)

    result = dc._deploy_probe(repo_dir=repo)

    _assert_probe_shape(result, "deploy")
    assert result["hub"]["status"] == "serving"
    assert result["hub"]["count"] == 1
    assert result["leaves"][0]["metric"]["head_equals_serving"] is True
    assert result["leaves"][0]["metric"]["serving_branch"] == dc.SERVING_DEPLOY_BRANCH


def test_deploy_probe_non_repo_degrades_without_raising(tmp_path: Path) -> None:
    result = dc._deploy_probe(repo_dir=tmp_path)
    _assert_probe_shape(result, "deploy")
    assert result["hub"]["status"] == "source unreachable"
    assert result["leaves"] == []


# ---------------------------------------------------------------------------
# HTTP JSON seam — httpx is fully stubbed, no network
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], exc: Exception | None = None) -> None:
        self._chunks = chunks
        self._exc = exc

    def __enter__(self) -> "_FakeStreamResponse":
        if self._exc:
            raise self._exc
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self) -> list[bytes]:
        return self._chunks


class _FakeClient:
    chunks: list[bytes] = []
    exc: Exception | None = None
    requested: list[tuple[str, str, dict[str, str]]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def stream(self, method: str, url: str, headers: dict[str, str]) -> _FakeStreamResponse:
        self.requested.append((method, url, headers))
        return _FakeStreamResponse(self.chunks, self.exc)


def test_read_http_json_success_uses_httpx_stream_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.chunks = [json.dumps({"ok": True}).encode("utf-8")]
    _FakeClient.exc = None
    _FakeClient.requested = []
    monkeypatch.setattr(dc.httpx, "Client", _FakeClient)
    assert dc._read_http_json("http://example.invalid/data", timeout=1.5, byte_ceiling=100) == {"ok": True}
    assert _FakeClient.requested == [("GET", "http://example.invalid/data", {"Accept": "application/json"})]


def test_read_http_json_raises_when_byte_ceiling_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.chunks = [b"abc", b"def"]
    _FakeClient.exc = None
    _FakeClient.requested = []
    monkeypatch.setattr(dc.httpx, "Client", _FakeClient)
    with pytest.raises(RuntimeError, match="response exceeded byte ceiling 5"):
        dc._read_http_json("http://example.invalid/large", byte_ceiling=5)


def test_read_http_json_propagates_httpx_timeout_from_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.chunks = []
    _FakeClient.exc = dc.httpx.TimeoutException("timeout")
    _FakeClient.requested = []
    monkeypatch.setattr(dc.httpx, "Client", _FakeClient)
    with pytest.raises(dc.httpx.TimeoutException, match="timeout"):
        dc._read_http_json("http://example.invalid/timeout", timeout=0.01, byte_ceiling=100)
