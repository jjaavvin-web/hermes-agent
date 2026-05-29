"""Single-source role/model invariants for Hermes elite-architecture rails.

These constants intentionally do not read environment variables, files, provider
state, or credentials at import time. Enforcement and call-site rewiring happen
in later slices; this module only declares the pinned defaults.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

REVIEWER_MODEL: Final[str] = "opus"
"""Reviewer role pin: the Opus reviewer rail is selected by role, not ad hoc."""

CODEX_GPT_5_5_MODELS: Final[frozenset[str]] = frozenset(
    {
        "gpt-5.5",
        "openai-codex/gpt-5.5",
        "codex/gpt-5.5",
    }
)
"""Model identifiers accepted for Codex gpt-5.5 role rails."""

ADVERSARIAL_CRITIC: Final[str] = "openai-codex/gpt-5.5"
"""Adversarial critic role pin; must stay inside ``CODEX_GPT_5_5_MODELS``."""

GEMINI_DEFAULT: Final[str] = "gemini-2.5-pro"
"""Default Gemini model rail; avoid the ``-preview`` Gemini landmine."""

ANTHROPIC_MAX_OAUTH_ONLY: Final[bool] = True
"""Anthropic rail is Claude Max OAuth only, never paid API-key fallback."""

ANTHROPIC_API_KEY_ALLOWED: Final[bool] = False
"""Anthropic API keys are disallowed for this Max-OAuth-only rail."""

ANTHROPIC_CLAUDE_P_ALLOWED: Final[bool] = False
"""Plain ``claude -p`` is disallowed; use the approved interactive/OAuth path."""


def is_preview_gemini(model: str) -> bool:
    """Return whether ``model`` names a Gemini preview variant.

    This is a pure string predicate for enforcing the no-``-preview`` Gemini
    invariant without consulting runtime provider state.
    """

    return "gemini" in model.lower() and "-preview" in model.lower()


def assert_no_anthropic_api_key(env: Mapping[str, str | None]) -> None:
    """Reject Anthropic API-key fallback for the Max-OAuth-only rail.

    The helper is pure: callers pass an environment-like mapping, and this
    function only inspects that mapping. It does not read ``os.environ`` or
    mutate state.
    """

    if env.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not allowed for the Anthropic Max-OAuth-only rail"
        )
