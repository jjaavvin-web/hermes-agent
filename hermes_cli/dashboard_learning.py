"""Learning dashboard API.

GET /api/dashboard/learning

Surfaces only measurable learning-loop signals. Anything that cannot be traced to a
real local artifact is returned as ``unmeasured`` instead of guessed. The request
path is read-only and never raises 500 for missing/malformed artifacts.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from hermes_constants import get_hermes_home

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-learning"])

_TTL_SECONDS = 20.0
_CACHE: tuple[dict[str, Any], float] | None = None
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _paths() -> dict[str, Path]:
    home = get_hermes_home()
    state = home / "state"
    return {
        "mvms_snapshot": state / "learning-index" / "snapshot-latest.json",
        "mvms_history": state / "learning-index" / "history.jsonl",
        # Prompt-specified chokepoint path first; current deployed writer stores
        # the same stream under learning-index/. Support both, report actual path.
        "recall_events_primary": state / "recall-events.jsonl",
        "recall_events_fallback": state / "learning-index" / "recall-events.jsonl",
        "recall_eval_history": home / "evals" / "recall" / "score-history.jsonl",
        "recall_eval_timeseries": state / "learning-index" / "recall-eval-timeseries.jsonl",
        "promote_latest_md": state / "learning-loop" / "promote-ready-latest.md",
        "promote_latest_jsonl": state / "learning-loop" / "promote-ready-latest.jsonl",
        "promote_log": state / "learning-loop" / "learning-loop-promote.log",
        "verify_log": state / "learning-loop" / "verify-latest.log",
        "critic_latest": state / "learning-loop" / "critic-latest.md",
        "canary_result": state / "learning-canary" / "result-latest.json",
    }


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        if not path.exists():
            return None, f"missing: {path}"
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"failed to read {path}: {exc}"


def _read_jsonl(path: Path, *, limit_tail: int | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], [f"missing: {path}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return [], [f"failed to read {path}: {exc}"]
    if limit_tail is not None:
        start = max(0, len(lines) - limit_tail)
        selected = lines[start:]
        first_line = start + 1
    else:
        selected = lines
        first_line = 1
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_no, raw in enumerate(selected, start=first_line):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            if isinstance(row, dict):
                rows.append(row)
        except Exception as exc:
            errors.append(f"jsonl parse failed {path}:{line_no}: {exc}")
    return rows, errors


def _iso_from_epoch(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except Exception:
        return None


def _age_seconds_from_epoch(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, time.time() - float(value))


def _read_mvms_snapshot(paths: dict[str, Path]) -> tuple[dict[str, Any], list[str]]:
    snapshot, err = _read_json(paths["mvms_snapshot"])
    errors = [err] if err else []
    if not isinstance(snapshot, dict):
        return {
            "status": "unmeasured",
            "source": str(paths["mvms_snapshot"]),
            "provenance": "learning-index snapshot generated from MVMS read-only SELECTs",
        }, errors
    return {
        "status": "measured",
        "source": str(paths["mvms_snapshot"]),
        "provenance": snapshot.get("source") or "learning-index snapshot generated from MVMS read-only SELECTs",
        "generated_at": snapshot.get("generated_at"),
        "lessons_total": snapshot.get("lessons_total"),
        "trusted_count": snapshot.get("trusted_count"),
        "trusted_ratio": snapshot.get("trusted_ratio"),
        "actionable_lessons_total": snapshot.get("actionable_lessons_total"),
        "trusted_actionable_ratio": snapshot.get("trusted_actionable_ratio"),
        "auto_bridged_count": snapshot.get("auto_bridged_count"),
        "quarantine_count": snapshot.get("quarantine_count"),
        "dup_ratio": snapshot.get("dup_ratio"),
        "importance_hist": snapshot.get("importance_hist"),
        "embed_coverage": snapshot.get("embed_coverage"),
        "scope": snapshot.get("scope"),
    }, errors


def _read_recall_activity(paths: dict[str, Path]) -> tuple[dict[str, Any], list[str]]:
    source = paths["recall_events_primary"] if paths["recall_events_primary"].exists() else paths["recall_events_fallback"]
    rows, errors = _read_jsonl(source)
    now = time.time()
    ts_values: list[float] = [float(r["ts"]) for r in rows if isinstance(r.get("ts"), (int, float))]
    latest_ts = max(ts_values) if ts_values else None
    recent_1h = sum(1 for ts in ts_values if now - ts <= 3600)
    recent_24h = sum(1 for ts in ts_values if now - ts <= 86400)
    recent_7d = sum(1 for ts in ts_values if now - ts <= 604800)
    return {
        "status": "measured" if rows else "unmeasured",
        "source": str(source),
        "provenance": "recall-events.jsonl written by the loki_send.py recall chokepoint/warm recall service",
        "total_events": len(rows),
        "recent_1h": recent_1h,
        "recent_24h": recent_24h,
        "recent_7d": recent_7d,
        "latest_ts": latest_ts,
        "latest_at": _iso_from_epoch(latest_ts),
        "latest_age_seconds": _age_seconds_from_epoch(latest_ts),
        "sources": sorted({str(r.get("source") or "unknown") for r in rows}),
    }, errors


def _record_metric(row: dict[str, Any]) -> float | None:
    agg = row.get("agg") if isinstance(row.get("agg"), dict) else None
    if agg and isinstance(agg.get("recall_at_k"), (int, float)):
        return float(agg["recall_at_k"])
    metrics = row.get("metrics_recall_on") if isinstance(row.get("metrics_recall_on"), dict) else None
    if metrics and isinstance(metrics.get("recall_at_k"), (int, float)):
        return float(metrics["recall_at_k"])
    return None


def _classify_eval(row: dict[str, Any]) -> str:
    holdout = str(row.get("holdout_file") or row.get("holdout_path") or "")
    name = Path(holdout).name
    if "wave2" in name or "blind" in name:
        return "blind-heldout"
    if name == "holdout.jsonl" or not holdout:
        return "self-seeded-or-default"
    return "holdout-unclassified"


def _read_recall_eval(paths: dict[str, Path]) -> tuple[dict[str, Any], list[str]]:
    rows, errors = _read_jsonl(paths["recall_eval_history"])
    if not rows:
        alt_rows, alt_errors = _read_jsonl(paths["recall_eval_timeseries"])
        rows = alt_rows
        errors.extend(alt_errors)
    measured = [r for r in rows if _record_metric(r) is not None]
    blind = [r for r in measured if _classify_eval(r) == "blind-heldout"]
    self_seeded = [r for r in measured if _classify_eval(r) == "self-seeded-or-default"]
    selected = blind[-1] if blind else None
    selected_metric = _record_metric(selected) if selected else None
    selected_agg = selected.get("agg") if selected and isinstance(selected.get("agg"), dict) else {}
    self_latest = self_seeded[-1] if self_seeded else None
    return {
        "status": "measured" if selected else "unmeasured",
        "source": str(paths["recall_eval_history"] if paths["recall_eval_history"].exists() else paths["recall_eval_timeseries"]),
        "provenance": "latest blind-heldout row in recall score history; self-seeded/default rows are shown only as contrast",
        "label": "blind held-out RECALL@10 (not self-seeded)",
        "k": selected_agg.get("k") or selected.get("top_k") if selected else None,
        "n": selected_agg.get("n") or selected.get("n") if selected else None,
        "n_target_resolved": selected_agg.get("n_target_resolved") if selected else None,
        "recall_at_k": selected_metric,
        "mrr": selected_agg.get("mrr") if selected_agg else None,
        "ndcg_at_k": selected_agg.get("ndcg_at_k") if selected_agg else None,
        "holdout_file": selected.get("holdout_file") or selected.get("holdout_path") if selected else None,
        "ts": selected.get("ts") if selected else None,
        "self_seeded_latest": {
            "recall_at_k": _record_metric(self_latest),
            "holdout_file": self_latest.get("holdout_file") or self_latest.get("holdout_path"),
            "ts": self_latest.get("ts"),
            "classification": _classify_eval(self_latest),
        } if self_latest else None,
    }, errors


def _systemd_unit(name: str) -> dict[str, Any]:
    try:
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        active = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return {
            "name": name,
            "enabled": enabled.stdout.strip() or enabled.stderr.strip() or "unknown",
            "active": active.stdout.strip() or active.stderr.strip() or "unknown",
            "enabled_rc": enabled.returncode,
            "active_rc": active.returncode,
        }
    except Exception as exc:
        return {"name": name, "enabled": "unknown", "active": "unknown", "error": str(exc)}


def _parse_promote_md(path: Path) -> dict[str, Any]:
    data, err = _read_json(path)
    if isinstance(data, dict):
        return data
    if not path.exists():
        return {"error": err}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"error": str(exc)}
    found: dict[str, Any] = {}
    for key in ("processed", "store_writes", "ledger_writebacks", "queue_writebacks", "accept_quality_promotable"):
        m = re.search(rf'"?{re.escape(key)}"?\s*[:=]\s*(\d+)', text)
        if m:
            found[key] = int(m.group(1))
    return found


def _read_promotion(paths: dict[str, Path]) -> tuple[dict[str, Any], list[str]]:
    latest = _parse_promote_md(paths["promote_latest_md"])
    errors: list[str] = []
    if "error" in latest:
        errors.append(f"promotion artifact unavailable: {latest['error']}")
    return {
        "status": "measured" if "error" not in latest else "unmeasured",
        "source": str(paths["promote_latest_md"]),
        "jsonl_source": str(paths["promote_latest_jsonl"]),
        "log_source": str(paths["promote_log"]),
        "provenance": "learning-loop-promote.timer/service latest promote-ready artifact",
        "timer": _systemd_unit("learning-loop-promote.timer"),
        "service": _systemd_unit("learning-loop-promote.service"),
        "latest": latest,
    }, errors


def _read_verify(paths: dict[str, Path]) -> tuple[dict[str, Any], list[str]]:
    path = paths["verify_log"]
    errors: list[str] = []
    if not path.exists():
        return {
            "status": "unmeasured",
            "source": str(path),
            "provenance": "learning-verify.timer/service latest verify log",
            "timer": _systemd_unit("learning-verify.timer"),
            "service": _systemd_unit("learning-verify.service"),
        }, [f"missing: {path}"]
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"status": "unmeasured", "source": str(path), "error": str(exc)}, [f"failed to read {path}: {exc}"]
    recall_match = re.search(r"OK\s+recall@(?P<k>\d+)=(?P<recall>[0-9.]+)\s+mrr=(?P<mrr>[0-9.]+)\s+ndcg@(?P<ndcg_k>\d+)=(?P<ndcg>[0-9.]+)", text)
    critic_match = re.search(r"critic\s+(PASS|FAIL)\s+hard_failures=(\d+)", text)
    return {
        "status": "measured",
        "source": str(path),
        "provenance": "learning-verify.timer/service latest verify log; default recall metric may be self-seeded unless paired with blind eval history",
        "timer": _systemd_unit("learning-verify.timer"),
        "service": _systemd_unit("learning-verify.service"),
        "critic_status": critic_match.group(1) if critic_match else None,
        "hard_failures": int(critic_match.group(2)) if critic_match else None,
        "default_recall_at_k": float(recall_match.group("recall")) if recall_match else None,
        "default_recall_k": int(recall_match.group("k")) if recall_match else None,
        "default_mrr": float(recall_match.group("mrr")) if recall_match else None,
        "default_ndcg": float(recall_match.group("ndcg")) if recall_match else None,
        "log_tail": "\n".join(text.splitlines()[-12:]),
    }, errors


def _read_canary(paths: dict[str, Path]) -> tuple[dict[str, Any], list[str]]:
    result, err = _read_json(paths["canary_result"])
    if not isinstance(result, dict):
        return {"status": "unmeasured", "source": str(paths["canary_result"]), "provenance": "learning canary latest result"}, [err] if err else []
    return {
        "status": "measured",
        "source": str(paths["canary_result"]),
        "provenance": "production recall canary result-latest.json",
        "pass": result.get("pass"),
        "rank": result.get("rank"),
        "recalled": result.get("recalled"),
        "avoided_mistake": result.get("avoided_mistake"),
        "probe_ranker": result.get("probe_ranker"),
        "probe_mode_used": result.get("probe_mode_used"),
        "embedding_path": result.get("embedding_path"),
        "ts": result.get("ts"),
    }, []


def _overall_status(blocks: list[dict[str, Any]], errors: list[str]) -> str:
    if any(block.get("status") == "unmeasured" for block in blocks):
        return "amber"
    if errors:
        return "amber"
    return "green"


def get_learning_snapshot() -> dict[str, Any]:
    global _CACHE
    now = time.monotonic()
    with _LOCK:
        if _CACHE and now - _CACHE[1] < _TTL_SECONDS:
            return _CACHE[0]

        paths = _paths()
        errors: list[str] = []
        mvms, e = _read_mvms_snapshot(paths); errors.extend(e)
        recall_eval, e = _read_recall_eval(paths); errors.extend(e)
        recall_activity, e = _read_recall_activity(paths); errors.extend(e)
        promotion, e = _read_promotion(paths); errors.extend(e)
        verify, e = _read_verify(paths); errors.extend(e)
        canary, e = _read_canary(paths); errors.extend(e)
        blocks = [mvms, recall_eval, recall_activity, promotion, verify, canary]

        payload: dict[str, Any] = {
            "generated_at": _now(),
            "cache_ttl_seconds": _TTL_SECONDS,
            "status": _overall_status(blocks, errors),
            "files": {name: str(path) for name, path in paths.items()},
            "mvms_lessons": mvms,
            "recall_eval": recall_eval,
            "recall_activity": recall_activity,
            "promotion": promotion,
            "verify": verify,
            "canary": canary,
            "errors": errors,
            # Back-compat for older bundled JS while the dev server is rebuilding.
            "snapshot_latest": {
                "SIGNAL_SCORE": mvms.get("trusted_count"),
                "ACTIONABLE_SIGNAL_SCORE": mvms.get("actionable_lessons_total"),
                "trusted_count": mvms.get("trusted_count"),
                "trusted_ratio": mvms.get("trusted_ratio"),
                "lessons_total": mvms.get("lessons_total"),
                "dup_ratio": mvms.get("dup_ratio"),
                "auto_bridged_count": mvms.get("auto_bridged_count"),
                "quarantine_count": mvms.get("quarantine_count"),
                "importance_hist": mvms.get("importance_hist"),
                "embed_coverage": mvms.get("embed_coverage"),
            },
            "result_latest": canary,
            "history_tail": [],
            "history_tail_count": 0,
            "recall_filters": {
                "include_quarantine": False,
                "exclude_auto_bridged": True,
                "effective": "current clean pool",
            },
        }
        _CACHE = (payload, now)
        return payload


@router.get("/learning", summary="Live honest Learning dashboard snapshot")
async def get_learning() -> dict[str, Any]:
    try:
        return get_learning_snapshot()
    except Exception as exc:
        return {
            "generated_at": _now(),
            "cache_ttl_seconds": _TTL_SECONDS,
            "status": "amber",
            "files": {name: str(path) for name, path in _paths().items()},
            "mvms_lessons": {"status": "unmeasured"},
            "recall_eval": {"status": "unmeasured"},
            "recall_activity": {"status": "unmeasured"},
            "promotion": {"status": "unmeasured"},
            "verify": {"status": "unmeasured"},
            "canary": {"status": "unmeasured"},
            "errors": [f"learning snapshot build failed: {exc}"],
            "snapshot_latest": None,
            "result_latest": None,
            "history_tail": [],
            "history_tail_count": 0,
        }
