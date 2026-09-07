from __future__ import annotations

import os
import time
from pathlib import Path

from hermes_cli import dashboard_os as osmod


def _latest_txt(*, config_drift: int = 0, authz_drift: bool = False, authority_drift: int = 0) -> str:
    authz_line = "DRIFT discord_allowed_roles expected=empty actual=admin" if authz_drift else "OK discord_allowed_roles expected=empty actual=empty"
    return f"""STATUS AREA SUBJECT FIELD EXPECTED ACTUAL DETAIL
OK config default model.provider openai-codex openai-codex ok
SUMMARY {{"DRIFT": {config_drift}, "OK": 113, "WARN": 1}}

== discord-authz drift ==
{authz_line}
OK hermes_mcp_serve expected=not_running actual=not_running

== authority-matrix drift ==
STATUS AREA SUBJECT FIELD EXPECTED ACTUAL DETAIL
OK schema profile:brain trust_tier medium medium ok
SUMMARY {{"DRIFT": {authority_drift}, "OK": 249, "WARN": 1}}
"""


def _write_latest(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "latest.txt"
    path.write_text(text, encoding="utf-8")
    now = time.time()
    os.utime(path, (now, now))
    return path


def test_config_drift_status_is_green_only_when_all_wrapper_sections_clean(tmp_path: Path) -> None:
    path = _write_latest(tmp_path, _latest_txt(config_drift=0, authz_drift=False, authority_drift=0))

    status = osmod._config_drift_status(path)

    assert status["status"] == "green"
    assert status["combined_rc"] == 0
    assert status["detail"].startswith("config_drift=0 authz_drift=0 authority_drift=0")


def test_config_drift_status_reds_when_config_lint_fails(tmp_path: Path) -> None:
    path = _write_latest(tmp_path, _latest_txt(config_drift=1, authz_drift=False, authority_drift=0))

    status = osmod._config_drift_status(path)

    assert status["status"] == "red"
    assert status["combined_rc"] == 1
    assert status["config_drift"] == 1


def test_config_drift_status_reds_when_discord_authz_fails(tmp_path: Path) -> None:
    path = _write_latest(tmp_path, _latest_txt(config_drift=0, authz_drift=True, authority_drift=0))

    status = osmod._config_drift_status(path)

    assert status["status"] == "red"
    assert status["combined_rc"] == 1
    assert status["authz_drift"] == 1


def test_config_drift_status_reds_when_authority_lint_fails(tmp_path: Path) -> None:
    # Regression for the fake-green bug: config SUMMARY is clean, but the
    # wrapper is still red because authority_lint found uncovered MVMS writers.
    path = _write_latest(tmp_path, _latest_txt(config_drift=0, authz_drift=False, authority_drift=1))

    status = osmod._config_drift_status(path)

    assert status["status"] == "red"
    assert status["combined_rc"] == 1
    assert status["config_drift"] == 0
    assert status["authority_drift"] == 1


def test_infra_section_contains_config_drift_chip(monkeypatch) -> None:
    monkeypatch.setattr(osmod, "_cost_summary", lambda: {"status": "green", "detail": "cost ok", "label": "$0.00"})
    monkeypatch.setattr(osmod, "_config_drift_status", lambda: {"status": "red", "detail": "authority_drift=1", "label": "combined rc=1"})
    monkeypatch.setattr(osmod, "_dr_status", lambda: {"status": "green", "detail": "DR ok", "label": "4/4"})
    monkeypatch.setattr(osmod, "_evals_status", lambda: {"status": "green", "detail": "evals ok", "label": "100%"})
    monkeypatch.setattr(osmod, "_security_status", lambda: {"status": "green", "detail": "security ok", "label": "0 breaches"})

    infra = osmod._infra_snapshot()
    items = [
        osmod._item("cost", infra["cost"].get("status", "unknown"), infra["cost"].get("detail", "cost unmeasured"), metric=infra["cost"].get("label")),
        osmod._item("config_drift", infra["config_drift"].get("status", "unknown"), infra["config_drift"].get("detail", "config drift unmeasured"), metric=infra["config_drift"].get("label")),
        osmod._item("DR", infra["dr"].get("status", "unknown"), infra["dr"].get("detail", "DR unmeasured"), metric=infra["dr"].get("label")),
        osmod._item("evals", infra["evals"].get("status", "unknown"), infra["evals"].get("detail", "evals unmeasured"), metric=infra["evals"].get("label")),
        osmod._item("security", infra["security"].get("status", "unknown"), infra["security"].get("detail", "security unmeasured"), metric=infra["security"].get("label")),
    ]
    section = osmod._section("infra", "Infra Gates", items)

    assert section["status"] == "red"
    assert next(item for item in section["items"] if item["name"] == "config_drift")["status"] == "red"
