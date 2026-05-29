from types import MappingProxyType

import pytest

from agent import role_defaults


def test_review_and_critic_models_are_pinned_to_expected_roles() -> None:
    assert role_defaults.REVIEWER_MODEL == "opus"
    assert role_defaults.ADVERSARIAL_CRITIC in role_defaults.CODEX_GPT_5_5_MODELS


def test_gemini_default_is_not_a_preview_model() -> None:
    assert "-preview" not in role_defaults.GEMINI_DEFAULT
    assert role_defaults.is_preview_gemini("gemini-2.5-pro-preview-06-05") is True
    assert role_defaults.is_preview_gemini(role_defaults.GEMINI_DEFAULT) is False


def test_anthropic_policy_marks_max_oauth_only_without_api_key_or_claude_p() -> None:
    assert role_defaults.ANTHROPIC_MAX_OAUTH_ONLY is True
    assert role_defaults.ANTHROPIC_API_KEY_ALLOWED is False
    assert role_defaults.ANTHROPIC_CLAUDE_P_ALLOWED is False


def test_assert_no_anthropic_api_key_is_pure_and_rejects_api_key() -> None:
    clean_env = MappingProxyType({"PATH": "/usr/bin"})
    role_defaults.assert_no_anthropic_api_key(clean_env)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        role_defaults.assert_no_anthropic_api_key({"ANTHROPIC_API_KEY": "sk-test"})
