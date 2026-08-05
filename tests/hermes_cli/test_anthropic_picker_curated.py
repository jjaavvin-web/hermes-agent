"""Regression tests for retiring spend-gated Fable from curated pickers.

``claude-fable-5`` is still a valid literal model id for deliberate,
SPEND-gated Anthropic use, but it must not be offered by Hermes' curated
interactive picker catalogs after the July-7 Fable -> Opus cutover.
"""

from unittest.mock import patch

from agent import anthropic_adapter as AA
from agent import model_metadata as MM
from hermes_cli import models as M


def test_fable_is_not_offered_by_any_static_curated_provider_catalog():
    """Static curated picker catalogs no longer offer Fable."""
    offenders = {
        provider: models
        for provider, models in M._PROVIDER_MODELS.items()
        if "claude-fable-5" in models
    }

    assert offenders == {}


def test_picker_surfaces_do_not_offer_fable_after_curated_removal():
    """Downstream picker helpers used by CLI/gateway/ACP do not offer Fable."""
    anthropic_live_without_fable = ["claude-opus-4-8", "claude-sonnet-4-6"]
    with patch.object(M, "_fetch_anthropic_models", return_value=anthropic_live_without_fable):
        anthropic = [model_id for model_id, _ in M.curated_models_for_provider("anthropic")]

    assert "claude-fable-5" not in anthropic

    with patch(
        "hermes_cli.auth.resolve_api_key_provider_credentials",
        return_value={},
    ), patch.object(
        M,
        "_merge_with_models_dev",
        side_effect=lambda _provider, curated: list(curated),
    ):
        opencode_zen = [
            model_id
            for model_id, _ in M.curated_models_for_provider("opencode-zen")
        ]

    assert "claude-fable-5" not in opencode_zen


def test_anthropic_curated_models_still_merge_when_live_omits_other_curated_alias():
    """A curated alias missing from /v1/models still surfaces (first).

    Upstream v0.20 asserted this with ``claude-fable-5``; the fork retired that
    alias from the curated picker (see hermes_cli/models.py), so the same
    upstream invariant is re-anchored onto still-curated aliases — including
    upstream's newly added ``claude-sonnet-5``.
    """
    curated = M._PROVIDER_MODELS["anthropic"]
    assert "claude-opus-4-7" in curated
    assert "claude-sonnet-5" in curated  # newest Sonnet alias is curated
    assert "claude-fable-5" not in curated  # fork: retired from the picker

    live = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    assert "claude-opus-4-7" in result
    assert "claude-sonnet-5" in result
    assert "claude-fable-5" not in result  # fork: retired from the picker
    # Curated order is preserved at the front.
    assert result[:len(curated)] == list(curated)


def test_anthropic_merge_dedupes_overlap_and_appends_live_only():
    """Models in both lists appear once; live-only models are appended."""
    live = [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-future-9-99",
    ]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    assert result.count("claude-opus-4-8") == 1
    assert "claude-future-9-99" in result
    assert result.index("claude-opus-4-8") < result.index("claude-future-9-99")


def test_anthropic_falls_back_to_curated_when_live_unavailable():
    """No creds / live failure -> curated list verbatim."""
    with patch.object(M, "_fetch_anthropic_models", return_value=None):
        result = M.provider_model_ids("anthropic")

    assert result == list(M._PROVIDER_MODELS["anthropic"])
    assert "claude-opus-4-7" in result


def test_fable_literal_compat_surfaces_remain_intact():
    """Literal Fable still resolves metadata and is accepted deliberately."""
    assert MM.DEFAULT_CONTEXT_LENGTHS["claude-fable-5"] == 1_000_000

    with patch.object(MM, "_query_anthropic_context_length", return_value=None), patch.object(
        MM, "_query_ollama_api_show", return_value=None
    ), patch.object(MM, "fetch_model_metadata", return_value={}), patch(
        "agent.models_dev.lookup_models_dev_context", return_value=None
    ):
        assert MM.get_model_context_length("claude-fable-5", provider="anthropic") == 1_000_000

    assert AA._supports_adaptive_thinking("claude-fable-5") is True
    assert AA._supports_xhigh_effort("claude-fable-5") is True
    assert AA._get_anthropic_max_output("claude-fable-5") == 128_000

    with patch.object(M, "_fetch_anthropic_models", return_value=["claude-opus-4-8"]):
        validation = M.validate_requested_model("claude-fable-5", "anthropic")

    assert validation["accepted"] is True
    assert validation["persist"] is True
    assert validation["recognized"] is False
