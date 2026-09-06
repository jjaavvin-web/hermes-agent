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


@pytest.mark.parametrize(
    ("model_config", "expected_path"),
    [
        ("gemini-fixture-preview", "cfg.model"),
        ({"default": " gemini-fixture-preview "}, "cfg.model.default"),
        ({"model": "gemini-fixture-preview"}, "cfg.model.model"),
        (
            {"default": {"provider": "fixture", "model": "gemini-fixture-preview"}},
            "cfg.model.default.model",
        ),
    ],
)
def test_startup_safety_checks_selected_chat_model_shape(model_config, expected_path):
    from agent.startup_safety import StartupInvariantError, check_role_invariants

    cfg = {"model": model_config}
    violations = check_role_invariants({}, cfg)

    assert [(item.code, item.path, item.actual) for item in violations] == [
        ("preview_gemini_default", expected_path, "gemini-fixture-preview"),
    ]
    with pytest.raises(StartupInvariantError):
        check_role_invariants({"HERMES_STRICT_ROLE_INVARIANTS": "1"}, cfg)


def test_startup_safety_ignores_dormant_media_models_and_chat_alias(caplog):
    from agent.startup_safety import enforce_startup_role_invariants

    cfg = {
        "model": {"default": "fixture-chat", "model": "gemini-unused-preview"},
        "tts": {
            "provider": "edge",
            "gemini": {"model": "gemini-fixture-preview-tts"},
        },
        "stt": {"provider": "fixture-stt", "gemini": {"model": "gemini-unused-preview"}},
    }
    caplog.set_level(logging.WARNING, logger="agent.startup_safety")

    violations = enforce_startup_role_invariants(
        env={"HERMES_STRICT_ROLE_INVARIANTS": "1"}, cfg=cfg,
    )

    assert violations == []
    assert "Hermes role invariant violation" not in caplog.text


def test_startup_safety_keeps_role_and_auxiliary_checks():
    from agent.startup_safety import check_role_invariants

    cfg = {
        "roles": {
            "reviewer": {"model": role_defaults.REVIEWER_MODEL},
            "critic": "gemini-role-preview",
        },
        "auxiliary": {"compression": {"model": "gemini-aux-preview"}},
    }

    violations = check_role_invariants({}, cfg)

    assert {(item.code, item.path) for item in violations} == {
        ("preview_gemini_default", "cfg.roles.critic"),
        ("preview_gemini_default", "cfg.auxiliary.compression.model"),
    }


def test_startup_safety_accepts_shipped_defaults_without_loading_user_config():
    from agent.startup_safety import check_role_invariants
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert check_role_invariants(
        {"HERMES_STRICT_ROLE_INVARIANTS": "1"}, DEFAULT_CONFIG,
    ) == []
