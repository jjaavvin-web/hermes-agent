from __future__ import annotations

import importlib
import logging

import pytest

from agent import role_defaults


def test_startup_safety_reports_anthropic_api_key_from_env():
    from agent.startup_safety import StartupInvariantError, check_role_invariants

    violations = check_role_invariants({role_defaults.ANTHROPIC_API_KEY_ENV: "present"}, {})

    assert [v.code for v in violations] == ["anthropic_api_key_present"]
    assert role_defaults.ANTHROPIC_API_KEY_ENV in violations[0].message
    with pytest.raises(StartupInvariantError):
        check_role_invariants(
            {
                role_defaults.ANTHROPIC_API_KEY_ENV: "present",
                "HERMES_STRICT_ROLE_INVARIANTS": "1",
            },
            {},
        )


def test_startup_safety_reports_reviewer_not_resolving_to_opus():
    from agent.startup_safety import check_role_invariants

    cfg = {"roles": {"reviewer": {"model": "sonnet"}}}

    violations = check_role_invariants({}, cfg)

    assert [v.code for v in violations] == ["reviewer_model_mismatch"]
    assert role_defaults.REVIEWER_MODEL in violations[0].message


def test_startup_safety_reports_preview_gemini_defaults_via_role_defaults_helper(monkeypatch):
    from agent.startup_safety import check_role_invariants

    seen: list[str] = []

    def spy(model: str) -> bool:
        seen.append(model)
        return model == "gemini-test-preview"

    monkeypatch.setattr(role_defaults, "is_preview_gemini", spy)
    cfg = {"auxiliary": {"compression": {"model": "gemini-test-preview"}}}

    violations = check_role_invariants({}, cfg)

    assert seen == ["gemini-test-preview"]
    assert [v.code for v in violations] == ["preview_gemini_default"]


def test_startup_safety_warns_by_default_and_strict_raises(caplog):
    from agent.startup_safety import StartupInvariantError, enforce_startup_role_invariants

    env = {role_defaults.ANTHROPIC_API_KEY_ENV: "present"}
    caplog.set_level(logging.WARNING, logger="agent.startup_safety")

    violations = enforce_startup_role_invariants(env=env, cfg={})

    assert [v.code for v in violations] == ["anthropic_api_key_present"]
    assert "Hermes role invariant violation" in caplog.text
    with pytest.raises(StartupInvariantError):
        enforce_startup_role_invariants(env={**env, "HERMES_STRICT_ROLE_INVARIANTS": "1"}, cfg={})


def test_startup_safety_imports_are_side_effect_free(capsys):
    import agent.role_defaults as rd
    import agent.startup_safety as ss

    importlib.reload(rd)
    importlib.reload(ss)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
