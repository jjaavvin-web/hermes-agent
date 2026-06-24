"""Read-only cost ledger reconciliation.

This module classifies recorded ``turn_usage`` spend rows by billing mode using
Josep's canonical provider-stack lock, rolls spend up by lane/provider, and
surfaces paid-fallback violations without mutating ``state.db`` or config files.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import yaml

BillingMode = Literal["subscription_included", "paid_api", "local_free"]
LaneKey = Literal["default", "premium", "vision", "honcho", "other"]

LOCK_PATH = Path("/home/josep/.hermes/scripts/config_canon/provider-stack.lock.yaml")
LOCK_REGENERATOR = Path("/home/josep/.hermes/scripts/config_canon/extract_lock.py")


class ReconciliationError(RuntimeError):
    """Raised when reconciliation inputs are malformed or unavailable."""


def _state_db_path() -> Path:
    """Resolve the canonical ``state.db`` path."""

    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "state.db"
    except Exception:
        return Path.home() / ".hermes" / "state.db"


def _connect_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open ``state.db`` read-only, or return ``None`` if it does not exist."""

    if not db_path.exists():
        return None
    uri = f"file:{quote(str(db_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _norm_provider(value: Any) -> str:
    return _norm(value).removeprefix("custom:")


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _row_get(row: sqlite3.Row | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else default
    return row.get(key, default)


def load_lane_policy(lock_path: Path = LOCK_PATH) -> dict[str, Any]:
    """Load the canonical provider-stack lock.

    The classifier intentionally fails loudly if the deterministic lock is
    absent. Falling back to guessed defaults would make reconciliation fake-green.
    """

    if not lock_path.exists():
        raise FileNotFoundError(
            f"Provider-stack lock not found at {lock_path}. "
            f"Regenerate it with {LOCK_REGENERATOR}; refusing guessed defaults."
        )
    with lock_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    lock = data.get("lock")
    if not isinstance(lock, dict):
        raise ReconciliationError(f"Malformed provider-stack lock at {lock_path}: missing 'lock' map")
    return lock


def _policy_sets(policy: dict[str, Any]) -> dict[str, Any]:
    default_lane = policy.get("default_lane") or {}
    premium_lane = policy.get("premium_lane") or {}
    vision_aux = policy.get("vision_aux") or {}
    honcho_memory = policy.get("honcho_memory") or {}

    subscription_providers = {
        _norm_provider(default_lane.get("provider")),
        _norm_provider(premium_lane.get("provider")),
        _norm_provider(vision_aux.get("provider")),
    } - {""}
    expected_paid_providers = {_norm_provider(honcho_memory.get("provider"))} - {""}
    allowed_premium_models = {_norm(model) for model in premium_lane.get("models_allowed", [])}
    forbidden = {_norm(item) for item in policy.get("forbidden", [])}
    return {
        "subscription_providers": subscription_providers,
        "expected_paid_providers": expected_paid_providers,
        "default_provider": _norm_provider(default_lane.get("provider")),
        "default_model": _norm(default_lane.get("model")),
        "premium_provider": _norm_provider(premium_lane.get("provider")),
        "premium_models": allowed_premium_models,
        "vision_provider": _norm_provider(vision_aux.get("provider")),
        "vision_model": _norm(vision_aux.get("model")),
        "honcho_provider": _norm_provider(honcho_memory.get("provider")),
        "honcho_models": {_norm(model) for model in honcho_memory.get("models", [])},
        "forbidden": forbidden,
    }


def _effective_provider(row: sqlite3.Row | dict[str, Any]) -> str:
    return _norm_provider(
        _row_get(row, "provider")
        or _row_get(row, "turn_provider")
        or _row_get(row, "billing_provider")
        or _row_get(row, "session_billing_provider")
    )


def _effective_model(row: sqlite3.Row | dict[str, Any]) -> str:
    return _norm(_row_get(row, "model") or _row_get(row, "turn_model") or _row_get(row, "session_model"))


def classify_billing_mode(
    row: sqlite3.Row | dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> BillingMode:
    """Classify a spend row into subscription, paid API, or local-free.

    Precedence mirrors ``dashboard_cost._is_metered_leak`` for the metered path
    while generalizing to a three-way classifier.
    """

    lock = policy if policy is not None else load_lane_policy()
    sets = _policy_sets(lock)
    provider = _effective_provider(row)
    session_billing_provider = _norm_provider(_row_get(row, "billing_provider"))
    status = _norm(_row_get(row, "cost_status"))
    session_billing_mode = _norm(_row_get(row, "session_billing_mode") or _row_get(row, "billing_mode"))
    base_url = _norm(_row_get(row, "billing_base_url") or _row_get(row, "base_url"))
    est_cost = _float(_row_get(row, "estimated_cost_usd") or _row_get(row, "turn_estimated_cost_usd"))

    if (
        status == "included"
        or session_billing_mode == "subscription_included"
        or provider in sets["subscription_providers"]
        or session_billing_provider in sets["subscription_providers"]
    ):
        return "subscription_included"

    if provider in {"anthropic", "openrouter"} or session_billing_provider in {"anthropic", "openrouter"}:
        return "paid_api"

    if (
        provider in {"", "test", "local", "custom", "openai-compat"}
        or provider.startswith("local")
        or provider.startswith("test")
        or provider.startswith("custom")
        or (provider == "openai-compat" and ("localhost" in base_url or "127.0.0.1" in base_url))
        or (est_cost == 0.0 and provider not in {"anthropic", "openrouter"})
    ):
        return "local_free"

    return "local_free"


def derive_lane_key(row: sqlite3.Row | dict[str, Any], policy: dict[str, Any] | None = None) -> LaneKey:
    """Derive a stable lane label from provider/model/source against the lock."""

    lock = policy if policy is not None else load_lane_policy()
    sets = _policy_sets(lock)
    provider = _effective_provider(row)
    billing_provider = _norm_provider(_row_get(row, "billing_provider"))
    model = _effective_model(row)
    session_model = _norm(_row_get(row, "session_model"))
    source = _norm(_row_get(row, "source"))
    haystack = " ".join(
        [
            provider,
            billing_provider,
            model,
            session_model,
            source,
            _norm(_row_get(row, "prompt_version")),
            _norm(_row_get(row, "session_id")),
            _norm(_row_get(row, "turn_id")),
        ]
    )

    if "honcho" in haystack or provider == sets["honcho_provider"] or billing_provider == sets["honcho_provider"]:
        return "honcho"
    if provider == sets["premium_provider"] or billing_provider == sets["premium_provider"]:
        return "premium"
    if sets["premium_models"] and (model in sets["premium_models"] or session_model in sets["premium_models"]):
        return "premium"
    if "vision" in haystack:
        return "vision"
    if provider == sets["default_provider"] or billing_provider == sets["default_provider"]:
        return "default"
    if provider == sets["vision_provider"] and model == sets["vision_model"] and "vision" in source:
        return "vision"
    return "other"


def _joined_turn_rows(conn: sqlite3.Connection, since_ts: float) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            tu.turn_id,
            tu.session_id,
            tu.ts,
            tu.provider,
            tu.model,
            tu.prompt_version,
            tu.input_tokens,
            tu.output_tokens,
            tu.cache_read_tokens,
            tu.cache_write_tokens,
            tu.reasoning_tokens,
            tu.total_tokens,
            tu.estimated_cost_usd,
            tu.cost_status,
            tu.cost_source,
            tu.latency_ms,
            s.source,
            s.model AS session_model,
            s.billing_provider,
            s.billing_base_url,
            s.billing_mode AS session_billing_mode,
            s.estimated_cost_usd AS session_estimated_cost_usd,
            s.actual_cost_usd,
            s.cost_status AS session_cost_status,
            s.cost_source AS session_cost_source,
            s.started_at,
            s.ended_at
        FROM turn_usage tu
        LEFT JOIN sessions s ON s.id = tu.session_id
        WHERE COALESCE(tu.ts, 0) >= ?
        """,
        (since_ts,),
    ).fetchall()


def _zero_report(db_path: Path, window_days: int | float, reason: str) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "generatedAt": now,
        "dbPath": str(db_path),
        "window": {"days": window_days, "sinceTs": None, "reason": reason},
        "byBillingMode": {
            "subscription_included": {"turns": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
            "paid_api": {"turns": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
            "local_free": {"turns": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
        },
        "budgetRollup": [],
        "paidFallbackViolations": [],
        "violationCount": 0,
        "violationCostUsd": 0.0,
    }


def _round_usd(value: float) -> float:
    return round(value, 6)


def budget_rollup(
    conn: sqlite3.Connection,
    since_ts: float,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate spend by ``(lane_key, billing_provider, billing_mode)``."""

    lock = policy if policy is not None else load_lane_policy()
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _joined_turn_rows(conn, since_ts):
        billing_mode = classify_billing_mode(row, lock)
        lane_key = derive_lane_key(row, lock)
        billing_provider = _norm_provider(row["billing_provider"] or row["provider"]) or "unknown"
        key = (lane_key, billing_provider, billing_mode)
        bucket = buckets.setdefault(
            key,
            {
                "lane_key": lane_key,
                "billing_provider": billing_provider,
                "billing_mode": billing_mode,
                "turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
            },
        )
        bucket["turns"] += 1
        for token_key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            bucket[token_key] += _int(row[token_key])
        bucket["estimated_cost_usd"] += _float(row["estimated_cost_usd"])

    rows = list(buckets.values())
    for row in rows:
        row["estimated_cost_usd"] = _round_usd(row["estimated_cost_usd"])
    rows.sort(key=lambda item: (-item["estimated_cost_usd"], item["lane_key"], item["billing_provider"]))
    return rows


def _ts_iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _matches_forbidden(row: sqlite3.Row | dict[str, Any], policy: dict[str, Any]) -> str | None:
    sets = _policy_sets(policy)
    forbidden = sets["forbidden"]
    provider = _effective_provider(row)
    billing_provider = _norm_provider(_row_get(row, "billing_provider"))
    model = _effective_model(row)
    session_model = _norm(_row_get(row, "session_model"))
    source = _norm(_row_get(row, "source"))
    prompt_version = _norm(_row_get(row, "prompt_version"))
    base_url = _norm(_row_get(row, "billing_base_url") or _row_get(row, "base_url"))
    haystack = " ".join([provider, billing_provider, model, session_model, source, prompt_version, base_url])

    if "native_anthropic_api_pins" in forbidden and (provider == "anthropic" or billing_provider == "anthropic"):
        return "native_anthropic_api_pins"
    if "openrouter_fallback_providers" in forbidden and (provider == "openrouter" or billing_provider == "openrouter"):
        return "openrouter_fallback_providers"
    if "bare_claude_p" in forbidden and ("claude -p" in haystack or "claude --print" in haystack):
        return "bare_claude_p"
    if "gemini_preview_routes" in forbidden and "gemini" in haystack and "preview" in haystack:
        return "gemini_preview_routes"
    if "anthropic_oauth_spoof_provider" in forbidden and "anthropic" in haystack and "oauth" in haystack:
        return "anthropic_oauth_spoof_provider"
    return None


def _is_expected_honcho_paid(row: sqlite3.Row | dict[str, Any], policy: dict[str, Any]) -> bool:
    sets = _policy_sets(policy)
    provider = _effective_provider(row)
    billing_provider = _norm_provider(_row_get(row, "billing_provider"))
    lane = derive_lane_key(row, policy)
    return lane == "honcho" or provider in sets["expected_paid_providers"] or billing_provider in sets["expected_paid_providers"]


def paid_fallback_violations(
    conn: sqlite3.Connection,
    since_ts: float,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return paid rows that violate subscription/trusted lane policy."""

    lock = policy if policy is not None else load_lane_policy()
    violations: list[dict[str, Any]] = []
    for row in _joined_turn_rows(conn, since_ts):
        if classify_billing_mode(row, lock) != "paid_api":
            continue
        if _is_expected_honcho_paid(row, lock):
            continue
        why = _matches_forbidden(row, lock)
        lane_key = derive_lane_key(row, lock)
        if why is None and lane_key in {"default", "premium", "vision"}:
            why = f"{lane_key}_lane_metered_paid_api"
        if why is None:
            continue
        violations.append(
            {
                "turn_id": row["turn_id"],
                "session_id": row["session_id"],
                "ts": _ts_iso(row["ts"]),
                "provider": row["provider"] or row["billing_provider"] or "unknown",
                "model": row["model"] or row["session_model"] or "unknown",
                "est_cost_usd": _round_usd(_float(row["estimated_cost_usd"])),
                "why": why,
            }
        )
    violations.sort(key=lambda item: (-item["est_cost_usd"], item["ts"] or ""))
    return violations[:200]


def _billing_mode_rollup(
    conn: sqlite3.Connection,
    since_ts: float,
    policy: dict[str, Any],
) -> dict[str, dict[str, int | float]]:
    totals: dict[str, dict[str, int | float]] = {
        "subscription_included": {"turns": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
        "paid_api": {"turns": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
        "local_free": {"turns": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
    }
    for row in _joined_turn_rows(conn, since_ts):
        mode = classify_billing_mode(row, policy)
        totals[mode]["turns"] = int(totals[mode]["turns"]) + 1
        totals[mode]["total_tokens"] = int(totals[mode]["total_tokens"]) + _int(row["total_tokens"])
        totals[mode]["estimated_cost_usd"] = float(totals[mode]["estimated_cost_usd"]) + _float(row["estimated_cost_usd"])
    for mode in totals:
        totals[mode]["estimated_cost_usd"] = _round_usd(float(totals[mode]["estimated_cost_usd"]))
    return totals


def build_reconciliation(
    window_days: int | float = 7,
    db_path: Path | None = None,
    lock_path: Path = LOCK_PATH,
) -> dict[str, Any]:
    """Build a JSON-able reconciliation report for the requested time window."""

    resolved_db_path = db_path if db_path is not None else _state_db_path()
    since_ts = time.time() - (float(window_days) * 86400.0)
    conn = _connect_ro(resolved_db_path)
    if conn is None:
        return _zero_report(resolved_db_path, window_days, "state_db_absent")
    with conn:
        if not _table_exists(conn, "turn_usage") or not _table_exists(conn, "sessions"):
            return _zero_report(resolved_db_path, window_days, "missing_turn_usage_or_sessions")
        policy = load_lane_policy(lock_path)
        violations = paid_fallback_violations(conn, since_ts, policy)
        return {
            "generatedAt": datetime.now(UTC).isoformat(),
            "dbPath": str(resolved_db_path),
            "window": {"days": window_days, "sinceTs": since_ts, "sinceIso": _ts_iso(since_ts)},
            "byBillingMode": _billing_mode_rollup(conn, since_ts, policy),
            "budgetRollup": budget_rollup(conn, since_ts, policy),
            "paidFallbackViolations": violations,
            "violationCount": len(violations),
            "violationCostUsd": _round_usd(sum(_float(row["est_cost_usd"]) for row in violations)),
        }


def main() -> None:
    """Print the live read-only reconciliation report as indented JSON."""

    print(json.dumps(build_reconciliation(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
