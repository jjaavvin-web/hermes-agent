"""Startup role/model invariant checks for Hermes boot paths.

The checks here are deliberately pure unless ``enforce_startup_role_invariants``
is called. Importing this module must not read files, environment variables, or
provider state.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent import role_defaults

logger = logging.getLogger(__name__)
_STRICT_ENV = "HERMES_STRICT_ROLE_INVARIANTS"


@dataclass(frozen=True)
class RoleInvariantViolation:
    code: str
    message: str
    path: str | None = None
    actual: str | None = None
    expected: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "actual": self.actual,
            "expected": self.expected,
        }


class StartupInvariantError(RuntimeError):
    """Raised when strict startup invariant enforcement is enabled."""

    def __init__(self, violations: list[RoleInvariantViolation]):
        self.violations = violations
        super().__init__("; ".join(v.message for v in violations))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _iter_model_paths(node: Any, prefix: str = "cfg"):
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{prefix}.{key}"
            if key == "model" and isinstance(value, str) and value.strip():
                yield path, value.strip()
            else:
                yield from _iter_model_paths(value, path)
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _iter_model_paths(value, f"{prefix}[{idx}]")


def _resolve_reviewer_model(cfg: Mapping[str, Any]) -> str | None:
    roles = cfg.get("roles") if isinstance(cfg, Mapping) else None
    if isinstance(roles, Mapping):
        reviewer = roles.get("reviewer")
        if isinstance(reviewer, Mapping):
            model = reviewer.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
        elif isinstance(reviewer, str) and reviewer.strip():
            return reviewer.strip()
    reviewer = cfg.get("reviewer") if isinstance(cfg, Mapping) else None
    if isinstance(reviewer, Mapping):
        model = reviewer.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


def check_role_invariants(
    env: Mapping[str, str | None],
    cfg: Mapping[str, Any],
) -> list[RoleInvariantViolation]:
    """Return startup role/default invariant violations for an env/config pair.

    Pure function: callers pass the environment and loaded config. This function
    does not read ``os.environ``, files, credentials, or provider state.
    """

    violations: list[RoleInvariantViolation] = []
    if not role_defaults.ANTHROPIC_API_KEY_ALLOWED and env.get(role_defaults.ANTHROPIC_API_KEY_ENV):
        violations.append(
            RoleInvariantViolation(
                code="anthropic_api_key_present",
                path=f"env.{role_defaults.ANTHROPIC_API_KEY_ENV}",
                actual="present",
                expected="absent",
                message=(
                    f"{role_defaults.ANTHROPIC_API_KEY_ENV} is present but Anthropic "
                    "must use the Max-OAuth-only rail"
                ),
            )
        )

    reviewer_model = _resolve_reviewer_model(cfg)
    if reviewer_model and reviewer_model != role_defaults.REVIEWER_MODEL:
        violations.append(
            RoleInvariantViolation(
                code="reviewer_model_mismatch",
                path="cfg.roles.reviewer.model",
                actual=reviewer_model,
                expected=role_defaults.REVIEWER_MODEL,
                message=(
                    f"Reviewer role resolves to {reviewer_model!r}; expected "
                    f"{role_defaults.REVIEWER_MODEL!r}"
                ),
            )
        )

    for path, model in _iter_model_paths(cfg):
        if role_defaults.is_preview_gemini(model):
            violations.append(
                RoleInvariantViolation(
                    code="preview_gemini_default",
                    path=path,
                    actual=model,
                    expected="non-preview Gemini model",
                    message=f"Gemini preview default detected at {path}: {model!r}",
                )
            )
    if violations and _truthy(env.get(_STRICT_ENV)):
        raise StartupInvariantError(violations)
    return violations


def enforce_startup_role_invariants(
    *,
    env: Mapping[str, str | None],
    cfg: Mapping[str, Any],
) -> list[RoleInvariantViolation]:
    """Warn for invariant misses by default; raise when strict mode is enabled."""

    violations = check_role_invariants(env, cfg)
    for violation in violations:
        logger.warning("Hermes role invariant violation: %s", violation.message)
    return violations
