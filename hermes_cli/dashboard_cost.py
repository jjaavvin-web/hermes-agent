"""Cost dashboard API router (audit WC-3 payoff).

Exposes ``GET /api/dashboard/cost`` — a read-only rollup of token spend
recorded in the ``turn_usage`` table of ``state.db``.  Returns today + 7d
spend/tokens grouped by ``provider + model + prompt_version``, plus a
``metered_leak`` list flagging rows that accrued real metered charges on an
Anthropic or OpenRouter path (i.e. NOT covered by a subscription / "included"
billing route — the leak WC-3 was created to surface).

Read-only by construction: the database is opened with the sqlite ``mode=ro``
URI, exactly like ``hermes_cli.dashboard_health``.  No writes, no schema
mutation, and the empty-table case (turn_usage populates as turns flow) is
handled gracefully — an absent DB or empty table yields zero-valued rollups.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-cost"])


def _state_db_path() -> Path:
    """Resolve the canonical ``state.db`` path.

    Prefers the single-source-of-truth ``get_hermes_home()`` resolver so the
    router honours ``HERMES_HOME`` / active-profile selection; falls back to
    the platform default ``~/.hermes/state.db`` if that import is unavailable.
    """
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "state.db"
    except Exception:
        return Path.home() / ".hermes" / "state.db"


def _connect_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """Open ``state.db`` read-only, or return ``None`` if it does not exist."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


# A row is a "metered leak" when it accrued a real charge on a metered
# provider path.  Subscription routes (Max / Codex) record cost_status
# 'included' and are explicitly NOT leaks.
_METERED_PROVIDERS = ("anthropic", "openrouter")
_NON_LEAK_STATUSES = ("included",)


def _is_metered_leak(provider: Optional[str], cost_status: Optional[str], cost_source: Optional[str]) -> bool:
    prov = (provider or "").strip().lower()
    if not any(p in prov for p in _METERED_PROVIDERS):
        return False
    status = (cost_status or "").strip().lower()
    # 'included' == covered by a subscription billing route → not a leak.
    if status in _NON_LEAK_STATUSES:
        return False
    src = (cost_source or "").strip().lower()
    # An explicitly-zero / subscription source also signals no metered charge.
    if src in ("none", ""):
        # No cost source attached and not 'included' → still ambiguous; only
        # treat as a leak if a status indicates a billed path.
        return status in ("actual", "estimated")
    return True


def _rollup(conn: sqlite3.Connection, since_ts: float) -> dict[str, Any]:
    """Aggregate turn_usage rows with ts >= ``since_ts`` grouped by
    provider + model + prompt_version.
    """
    cur = conn.execute(
        """
        SELECT
            COALESCE(provider, 'unknown')        AS provider,
            COALESCE(model, 'unknown')           AS model,
            COALESCE(prompt_version, 'unknown')  AS prompt_version,
            COUNT(*)                             AS turns,
            COALESCE(SUM(input_tokens), 0)       AS input_tokens,
            COALESCE(SUM(output_tokens), 0)      AS output_tokens,
            COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
            COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
            COALESCE(SUM(reasoning_tokens), 0)   AS reasoning_tokens,
            COALESCE(SUM(total_tokens), 0)       AS total_tokens,
            COALESCE(SUM(estimated_cost_usd), 0.0) AS cost_usd
        FROM turn_usage
        WHERE ts >= ?
        GROUP BY provider, model, prompt_version
        ORDER BY cost_usd DESC, total_tokens DESC
        """,
        (since_ts,),
    )
    groups: list[dict[str, Any]] = []
    total_cost = 0.0
    total_tokens = 0
    total_turns = 0
    for row in cur.fetchall():
        cost = round(float(row["cost_usd"] or 0.0), 6)
        total_cost += cost
        total_tokens += int(row["total_tokens"] or 0)
        total_turns += int(row["turns"] or 0)
        groups.append(
            {
                "provider": row["provider"],
                "model": row["model"],
                "promptVersion": row["prompt_version"],
                "turns": int(row["turns"] or 0),
                "inputTokens": int(row["input_tokens"] or 0),
                "outputTokens": int(row["output_tokens"] or 0),
                "cacheReadTokens": int(row["cache_read_tokens"] or 0),
                "cacheWriteTokens": int(row["cache_write_tokens"] or 0),
                "reasoningTokens": int(row["reasoning_tokens"] or 0),
                "totalTokens": int(row["total_tokens"] or 0),
                "costUsd": cost,
            }
        )
    return {
        "totalCostUsd": round(total_cost, 6),
        "totalTokens": total_tokens,
        "totalTurns": total_turns,
        "groups": groups,
    }


def _metered_leaks(conn: sqlite3.Connection, since_ts: float, limit: int = 200) -> list[dict[str, Any]]:
    """Return individual turn rows whose cost_source/cost_status indicates a
    metered Anthropic / OpenRouter charge accrued (a billing leak).
    """
    cur = conn.execute(
        """
        SELECT turn_id, session_id, ts, provider, model, prompt_version,
               total_tokens, estimated_cost_usd, cost_status, cost_source
        FROM turn_usage
        WHERE ts >= ?
          AND (
                LOWER(COALESCE(provider, '')) LIKE '%anthropic%'
             OR LOWER(COALESCE(provider, '')) LIKE '%openrouter%'
              )
        """,
        (since_ts,),
    )
    leaks: list[dict[str, Any]] = []
    for row in cur.fetchall():
        # Re-filter in Python: the SQL OR above is deliberately broad (to keep
        # the index usable); the precise leak predicate lives in one place.
        if float(row["ts"] or 0.0) < since_ts:
            continue
        if not _is_metered_leak(row["provider"], row["cost_status"], row["cost_source"]):
            continue
        leaks.append(
            {
                "turnId": row["turn_id"],
                "sessionId": row["session_id"],
                "ts": float(row["ts"] or 0.0),
                "provider": row["provider"],
                "model": row["model"],
                "promptVersion": row["prompt_version"],
                "totalTokens": int(row["total_tokens"] or 0),
                "costUsd": round(float(row["estimated_cost_usd"] or 0.0), 6),
                "costStatus": row["cost_status"],
                "costSource": row["cost_source"],
            }
        )
    # Most expensive first; the SQL OR can over-return, so sort + cap here.
    leaks.sort(key=lambda r: (r["costUsd"], r["ts"]), reverse=True)
    return leaks[:limit]


def _build_cost_snapshot() -> dict[str, Any]:
    db_path = _state_db_path()
    now = time.time()
    day_ago = now - 86_400.0
    week_ago = now - 7 * 86_400.0

    empty_rollup = {"totalCostUsd": 0.0, "totalTokens": 0, "totalTurns": 0, "groups": []}
    snapshot: dict[str, Any] = {
        "generatedAt": now,
        "dbPath": str(db_path),
        "today": dict(empty_rollup),
        "last7d": dict(empty_rollup),
        "meteredLeak": [],
        "meteredLeakCount": 0,
        "meteredLeakCostUsd": 0.0,
    }

    conn = _connect_ro(db_path)
    if conn is None:
        # DB not yet created — return a graceful zero snapshot.
        return snapshot
    try:
        if not _table_exists(conn, "turn_usage"):
            return snapshot
        snapshot["today"] = _rollup(conn, day_ago)
        snapshot["last7d"] = _rollup(conn, week_ago)
        leaks = _metered_leaks(conn, week_ago)
        snapshot["meteredLeak"] = leaks
        snapshot["meteredLeakCount"] = len(leaks)
        snapshot["meteredLeakCostUsd"] = round(sum(r["costUsd"] for r in leaks), 6)
        return snapshot
    finally:
        conn.close()


@router.get("/cost")
def get_cost() -> dict[str, Any]:
    """Read-only token-spend rollup from ``turn_usage``.

    Returns today + 7d spend/tokens grouped by provider+model+prompt_version,
    plus a ``meteredLeak`` list of rows that accrued metered Anthropic /
    OpenRouter charges.  Empty/absent ``turn_usage`` yields zero rollups.
    """
    try:
        return _build_cost_snapshot()
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=f"cost snapshot failed: {exc}") from exc
