from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from hermes_cli import personas


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def isolated_personas_dir(tmp_path, monkeypatch):
    persona_dir = tmp_path / "isolated-hermes-home" / "personas"
    monkeypatch.setattr(personas, "_PERSONAS_DIR", persona_dir)
    return persona_dir


def _model(provider: str = "fake-provider", model: str = "fake-model") -> personas.ModelSpec:
    return personas.ModelSpec(provider=provider, model=model)


def _create_body(name: str = "Café Agent 🚀") -> personas.PersonaCreate:
    return personas.PersonaCreate(
        name=name,
        role_one_liner="Understands unicode souls",
        soul_md="# Soul\nΔ strategy — 火",
        planner=_model("planner-provider", "planner-model"),
        executor=_model("executor-provider", "executor-model"),
        critic=_model("critic-provider", "critic-model"),
    )


def test_slug_normalizes_names_and_rejects_unicode_only_names():
    assert personas._slug("  Café Agent 🚀!!  ") == "caf-agent"
    assert personas._slug("Alpha---Beta   Gamma") == "alpha-beta-gamma"
    assert personas._slug("火🔥Δ") == ""


def test_personas_dir_uses_test_path_and_is_created(isolated_personas_dir):
    assert not isolated_personas_dir.exists()

    created = personas._personas_dir()

    assert created == isolated_personas_dir
    assert created.is_dir()
    assert Path.home() / ".hermes" not in created.parents


def test_read_missing_and_malformed_persona_returns_none(isolated_personas_dir):
    isolated_personas_dir.mkdir(parents=True)
    (isolated_personas_dir / "broken.json").write_text("{not-json", encoding="utf-8")

    assert personas._read_persona("missing") is None
    assert personas._read_persona("broken") is None


def test_write_and_list_personas_preserves_unicode_and_ignores_bad_files(isolated_personas_dir):
    personas._write_persona(
        "zeta",
        {"name": "Zeta", "slug": "zeta", "soul_md": "unicode ✓ 火"},
    )
    personas._write_persona(
        "alpha",
        {"name": "Alpha", "slug": "alpha", "soul_md": "first"},
    )
    (isolated_personas_dir / ".tmp.json").write_text(
        json.dumps({"name": "hidden"}), encoding="utf-8"
    )
    (isolated_personas_dir / "malformed.json").write_text("not json", encoding="utf-8")

    listed = personas._list_personas()

    assert [p["slug"] for p in listed] == ["alpha", "zeta"]
    assert listed[1]["soul_md"] == "unicode ✓ 火"
    assert not list(isolated_personas_dir.glob("*.tmp"))


def test_create_persona_writes_slug_defaults_avatar_and_rejects_duplicates(
    isolated_personas_dir, monkeypatch
):
    monkeypatch.setattr(personas, "_now_iso", lambda: "2026-06-10T00:00:00Z")

    created = _run(personas.create_persona(_create_body()))

    assert created["slug"] == "caf-agent"
    assert created["avatar_variant"] == "caf-agent"
    assert created["created_at"] == "2026-06-10T00:00:00Z"
    assert created["updated_at"] == "2026-06-10T00:00:00Z"
    written = json.loads((isolated_personas_dir / "caf-agent.json").read_text(encoding="utf-8"))
    assert written["soul_md"] == "# Soul\nΔ strategy — 火"
    assert written["planner"] == {"provider": "planner-provider", "model": "planner-model"}

    with pytest.raises(personas.HTTPException) as duplicate:
        _run(personas.create_persona(_create_body("Caf Agent!!!")))
    assert duplicate.value.status_code == 409
    assert "caf-agent" in duplicate.value.detail

    with pytest.raises(personas.HTTPException) as invalid:
        _run(personas.create_persona(_create_body("火🔥Δ")))
    assert invalid.value.status_code == 400


def test_get_update_and_delete_persona_round_trip(isolated_personas_dir, monkeypatch):
    monkeypatch.setattr(personas, "_now_iso", lambda: "2026-06-10T01:00:00Z")
    created = _run(personas.create_persona(_create_body("Round Trip")))

    fetched = _run(personas.get_persona("round-trip"))
    assert fetched == created

    monkeypatch.setattr(personas, "_now_iso", lambda: "2026-06-10T02:00:00Z")
    updated = _run(
        personas.update_persona(
            "round-trip",
            personas.PersonaUpdate(
                name="Renamed Persona",
                role_one_liner="New role",
                soul_md="Updated soul ✓",
                planner=_model("new-provider", "new-planner"),
                avatar_variant="custom-avatar",
            ),
        )
    )

    assert updated["slug"] == "round-trip"  # update does not silently rename files
    assert updated["name"] == "Renamed Persona"
    assert updated["role_one_liner"] == "New role"
    assert updated["soul_md"] == "Updated soul ✓"
    assert updated["planner"] == {"provider": "new-provider", "model": "new-planner"}
    assert updated["executor"] == created["executor"]
    assert updated["critic"] == created["critic"]
    assert updated["avatar_variant"] == "custom-avatar"
    assert updated["updated_at"] == "2026-06-10T02:00:00Z"

    assert _run(personas.delete_persona("round-trip")) == {"ok": True}
    assert not (isolated_personas_dir / "round-trip.json").exists()

    for call in (personas.get_persona, personas.delete_persona):
        with pytest.raises(personas.HTTPException) as missing:
            _run(call("round-trip"))
        assert missing.value.status_code == 404

    with pytest.raises(personas.HTTPException) as missing_update:
        _run(personas.update_persona("round-trip", personas.PersonaUpdate(name="Nope")))
    assert missing_update.value.status_code == 404


def test_persona_create_model_rejects_malformed_model_specs():
    with pytest.raises(ValidationError) as exc:
        personas.PersonaCreate.model_validate(
            {
                "name": "Broken",
                "role_one_liner": "bad planner",
                "planner": {"provider": "fake-provider"},
                "executor": {"provider": "fake-provider", "model": "ok"},
                "critic": {"provider": "fake-provider", "model": "ok"},
            }
        )

    assert "planner" in str(exc.value)
    assert "model" in str(exc.value)


def test_summon_persona_creates_session_with_persona_soul_and_planner_model(
    isolated_personas_dir, monkeypatch
):
    personas._write_persona(
        "summoner",
        {
            "slug": "summoner",
            "soul_md": "secret-free persona soul ✓",
            "planner": {"provider": "fake-provider", "model": "planner-model"},
        },
    )
    created_sessions = []
    closed = []

    class FakeSessionDB:
        def create_session(self, **kwargs):
            created_sessions.append(kwargs)

        def close(self):
            closed.append(True)

    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=FakeSessionDB))
    monkeypatch.setattr(personas.secrets, "token_urlsafe", lambda n: "TOKEN")

    result = _run(personas.summon_persona("summoner"))

    assert result == {"session_id": "persona-summoner-TOKEN", "persona_slug": "summoner"}
    assert created_sessions == [
        {
            "session_id": "persona-summoner-TOKEN",
            "source": "pantheon",
            "system_prompt": "secret-free persona soul ✓",
            "model": "planner-model",
        }
    ]
    assert closed == [True]


def test_summon_persona_is_best_effort_when_session_db_fails(isolated_personas_dir, monkeypatch):
    personas._write_persona(
        "resilient",
        {"slug": "resilient", "soul_md": "still returned", "planner": {}},
    )

    class FailingSessionDB:
        def __init__(self):
            raise RuntimeError("db unavailable")

    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=FailingSessionDB))
    monkeypatch.setattr(personas.secrets, "token_urlsafe", lambda n: "SAFE")

    assert _run(personas.summon_persona("resilient")) == {
        "session_id": "persona-resilient-SAFE",
        "persona_slug": "resilient",
    }

    with pytest.raises(personas.HTTPException) as missing:
        _run(personas.summon_persona("missing"))
    assert missing.value.status_code == 404


def test_seed_default_personas_is_idempotent_and_preserves_existing_personas(
    isolated_personas_dir, capsys
):
    personas._write_persona(
        "orpheus",
        {"slug": "orpheus", "name": "Custom Orpheus", "soul_md": "do not overwrite"},
    )

    personas.seed_default_personas()
    first = capsys.readouterr().out

    assert "Seeded personas: atlas, hermes" in first
    assert "Already present (skipped): orpheus" in first
    assert json.loads((isolated_personas_dir / "orpheus.json").read_text(encoding="utf-8"))["name"] == "Custom Orpheus"
    assert {p["slug"] for p in personas._list_personas()} == {"orpheus", "atlas", "hermes"}

    personas.seed_default_personas()
    second = capsys.readouterr().out

    assert "Seeded personas" not in second
    assert "Already present (skipped): orpheus, atlas, hermes" in second


def test_seed_default_personas_reports_no_work_when_seed_list_is_empty(
    isolated_personas_dir, monkeypatch, capsys
):
    monkeypatch.setattr(personas, "_SEED_PERSONAS", [])

    personas.seed_default_personas()

    assert capsys.readouterr().out.strip() == "No personas to seed."
    assert personas._list_personas() == []
