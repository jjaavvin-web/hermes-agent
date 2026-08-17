"""Hermes SLO measurement/export helpers.

Reads the live Hermes SQLite state database in read-only URI mode and combines
that turn_usage ledger with bounded user-journal scans. It writes a lightweight
JSONL time-series plus latest.json under ~/.hermes/observability by default.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
DEFAULT_STATE_DB = HERMES_HOME / "state.db"
DEFAULT_OUTPUT_DIR = HERMES_HOME / "observability"
DEFAULT_TIMESERIES = DEFAULT_OUTPUT_DIR / "slo-timeseries.jsonl"
DEFAULT_LATEST = DEFAULT_OUTPUT_DIR / "slo-latest.json"
DEFAULT_RECALL_EVENTS = HERMES_HOME / "state" / "learning-index" / "recall-events.jsonl"
# 2026-06-26: recall_hit_rate is now computed from a STANDALONE recall-quality
# canary (recall-quality-canary.py) that probes a known in-pool lesson against the
# production recall(query, k=10) ranker and records a real target-in-top-k hit plus
# a separate off-domain discrimination gap. The old "hit = n_lessons>0" signal off
# recall-events.jsonl was structurally 1.0 (warm-recall always injects 5 lessons)
# and could never fire on degraded recall. recall-events.jsonl is still read, but
# only for the informational service_up sub-field (is the warm path producing
# injections at all), NOT for the SLO value.
DEFAULT_RECALL_CANARY = HERMES_HOME / "state" / "learning-index" / "recall-canary.jsonl"
# The in-domain/off-domain cosine gap is a separate, non-paging diagnostic. It
# must not redefine target-in-top-k recall hits: doing so turned 100% successful
# retrieval into a false-red recall_hit_rate when a static gap floor drifted.
# Kept in sync with recall-quality-canary.py's DISCRIMINATION_FLOOR.
RECALL_DISCRIMINATION_FLOOR = float(os.environ.get("HERMES_RECALL_CANARY_GAP", "0.20"))

WINDOW_SECONDS = 24 * 60 * 60
BUCKET_SECONDS = 5 * 60
# 2026-06-25: turns whose latency_ms spans a WSL2 suspend gap (the box sleeps) are
# multi-hour artifacts that inflated p95 to ~23min and made the SLO unarmable. Exclude
# per-turn latencies above this ceiling from p95 (the turn still counts toward turn_count).
MAX_PLAUSIBLE_LATENCY_MS = int(os.environ.get("SLO_MAX_PLAUSIBLE_LATENCY_MS", str(30 * 60 * 1000)))

SLO_DEFINITIONS: dict[str, dict[str, Any]] = {
    "gateway_turn_p95_latency_ms": {
        "target": "<=120000",
        "warn": 90000,
        "critical": 120000,
        "unit": "ms",
        # 2026-06-25: INFORMATIONAL (page=False). p95 mixes long lane/codex turns with
        # interactive turns, so a 120s budget over all turns is not a clean health signal
        # (it reads ~18min because ~40% of turns are multi-minute lane runs). Tracked in
        # the timeseries + dashboard; re-enable paging once turn_usage tags interactive
        # vs lane turns. >30min suspend-gap turns are already excluded from the value.
        "page": False,
        "source": "~/.hermes/state.db turn_usage.latency_ms (read-only; >30min suspend-gap turns excluded)",
    },
    "turn_error_rate": {
        "target": "<=0.05",
        "warn": 0.02,
        "critical": 0.05,
        "unit": "ratio",
        "source": "hermes-gateway.service journald error/failure lines divided by turn_usage turns",
    },
    "fallback_trigger_rate": {
        "target": "<=0.10",
        "warn": 0.05,
        "critical": 0.10,
        "unit": "ratio",
        "source": "gateway journald real provider/model fallback lines divided by turn_usage turns; ordinary retries and auxiliary title-generation fail-closed noise are tracked separately",
    },
    "recall_hit_rate": {
        "target": ">=0.80",
        "warn": 0.80,
        "critical": 0.65,
        "unit": "ratio",
        "source": "~/.hermes/state/learning-index/recall-canary.jsonl: fraction of recall-quality-canary runs where a known in-pool lesson appeared in production recall(query,k=10) top-k. service_up and discrimination_pass_rate are separate diagnostics. reports no_data when missing",
    },
    "recall_discrimination_pass_rate": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "ratio",
        "page": False,
        "source": "~/.hermes/state/learning-index/recall-canary.jsonl: fraction of measured in/off-domain cosine gaps clearing HERMES_RECALL_CANARY_GAP; tracked separately from target-in-top-k recall",
    },
    "watchdog_restart_count": {
        "target": "<=0 per 24h",
        "warn": 1,
        "critical": 1,
        "unit": "count/24h",
        "source": "hermes-gateway-watchdog.service journald start/restart/failure markers",
    },
    "cost_burn_rate_usd_24h": {
        "target": "<=10.00",
        "warn": 5.0,
        "critical": 10.0,
        "unit": "USD/24h",
        "source": "~/.hermes/state.db turn_usage.estimated_cost_usd sum over rolling 24h",
    },
}

_ERROR_RE = re.compile(r"\b(error|exception|traceback|failed|failure|timeout|crash)\b", re.I)
# 2026-06-25: tool-level noise that is NOT a gateway turn error — a lane's shell tool
# returning an error (e.g. missing module), title-generation hiccups, MCP memory-limit
# rejections. Normal agent operation, not gateway health failures; excluded from the rate.
# 2026-06-26: web/Firecrawl plugin config noise logged at ERROR level (Firecrawl client
# init failure + "web tools are not configured" fragments). ~88% of the live 24h numerator
# was ONE repeating firecrawl.provider init line, inflating turn_error_rate 0.04 -> 0.32 and
# making it un-armable for alerting. These are web-tool config gaps, not gateway turn
# failures; excluded so the rate reflects real turn health.
# 2026-06-26 (narrowed): the init line is matched by its message text
# ("firecrawl client initialization failed") rather than the bare "firecrawl.provider"
# logger namespace. A bare namespace match would also swallow GENUINE web-fetch failures
# logged under firecrawl.provider; the repeating init line (the ~88% noise) is still
# excluded via its message text, so noise reduction is preserved while real fetch
# failures still count toward turn_error_rate.
# 2026-07-03: expected disabled-tool/config failures (image generation without a
# backend, web tools without Firecrawl/managed auth) and tool guardrail denials
# are diagnostic events, not gateway/process health failures. Keep them visible
# in journal_counts, but do not let them page turn_error_rate.
_ERROR_EXCLUDE_RE = re.compile(
    r"returned error|tool_executor|title[_ ]generat|memory would be at"
    r"|Traceback \(most recent call last\):",
    re.I,
)
_TOOL_CONFIG_ERROR_RE = re.compile(
    r"firecrawl client initialization failed|web tools are not config"
    r"|image generation is unavailable in this environment"
    r"|error generating image: image generation is unavailable"
    r"|fal_key environment variable is not set",
    re.I,
)
_TOOL_BACKEND_ERROR_RE = re.compile(
    r"x_search failed: 429 Client Error: Too Many Requests"
    r"|requests\.exceptions\.HTTPError: 429 Client Error: Too Many Requests for url: https://api\.x\.ai/",
    re.I,
)
_TOOL_GUARDRAIL_DENIAL_RE = re.compile(
    r"WORKTREE_CONFINEMENT|route policy|approval denied|blocked by approval|blocked \(route policy\)",
    re.I,
)
_FALLBACK_RE = re.compile(r"\bfallback|fallback-trigger|provider fallback|model fallback\b", re.I)
# Auxiliary helpers can log fail-closed fallback-chain messages while preserving the
# primary turn. Those are useful diagnostics, but counting them as provider/model
# fallback pages made the SLO noisy during 2026-06-30 title-generation failures.
_AUXILIARY_FALLBACK_RE = re.compile(
    r"title[_ -]?generation|auxiliary.*fallback|fallback.*auxiliary",
    re.I,
)
_WATCHDOG_RE = re.compile(r"\b(started|starting|restart|failed|failure|watchdog)\b", re.I)
_TS_PREFIX_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<tz>Z|[+-]\d{2}:?\d{2})?")


@dataclass(frozen=True)
class JournalCounts:
    error_events: int = 0
    fallback_events: int = 0
    watchdog_restart_events: int = 0
    tool_config_error_events: int = 0
    tool_backend_error_events: int = 0
    tool_guardrail_denial_events: int = 0
    auxiliary_fallback_events: int = 0


def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc).isoformat()


def percentile(values: Sequence[float], pct: float) -> float | None:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = (len(clean) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(clean) - 1)
    frac = rank - low
    return clean[low] * (1 - frac) + clean[high] * frac


def open_state_db_readonly(path: Path = DEFAULT_STATE_DB) -> sqlite3.Connection:
    uri = f"file:{path.expanduser()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _fetch_turn_rows(con: sqlite3.Connection, since_epoch: float) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT ts, provider, model, latency_ms, retry_count, estimated_cost_usd,
               cost_status, total_tokens, input_tokens, output_tokens, tool_count
          FROM turn_usage
         WHERE ts >= ?
         ORDER BY ts ASC
        """,
        (since_epoch,),
    ).fetchall()
    return [dict(row) for row in rows]


def _run(cmd: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def journalctl_lines(unit: str, since_epoch: float, *, limit: int = 2000) -> list[str]:
    since = datetime.fromtimestamp(since_epoch, tz=timezone.utc).isoformat()
    proc = _run([
        "journalctl", "--user", "-u", unit, "--since", since,
        "-n", str(limit), "--no-pager", "-o", "short-iso",
    ], timeout=12.0)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _is_counted_gateway_error(line: str) -> bool:
    """True for gateway/process health failures that should page turn_error_rate."""
    return bool(
        _ERROR_RE.search(line)
        and not _ERROR_EXCLUDE_RE.search(line)
        and not _TOOL_CONFIG_ERROR_RE.search(line)
        and not _TOOL_BACKEND_ERROR_RE.search(line)
        and not _TOOL_GUARDRAIL_DENIAL_RE.search(line)
    )


def _is_counted_fallback(line: str) -> bool:
    """True for real provider/model fallback events that should page fallback SLO."""
    return bool(_FALLBACK_RE.search(line) and not _AUXILIARY_FALLBACK_RE.search(line))


def parse_journal_counts(gateway_lines: Iterable[str], watchdog_lines: Iterable[str] = ()) -> JournalCounts:
    errors = fallbacks = watchdogs = tool_config_errors = tool_backend_errors = tool_guardrail_denials = auxiliary_fallbacks = 0
    for line in gateway_lines:
        if _TOOL_CONFIG_ERROR_RE.search(line):
            tool_config_errors += 1
        if _TOOL_BACKEND_ERROR_RE.search(line):
            tool_backend_errors += 1
        if _TOOL_GUARDRAIL_DENIAL_RE.search(line):
            tool_guardrail_denials += 1
        if _is_counted_gateway_error(line):
            errors += 1
        if _FALLBACK_RE.search(line) and _AUXILIARY_FALLBACK_RE.search(line):
            auxiliary_fallbacks += 1
        if _is_counted_fallback(line):
            fallbacks += 1
    for line in watchdog_lines:
        text = line.lower()
        # 2026-06-25: this watchdog is ALERT-ONLY (its unit description literally says
        # "(no restart)") and gateway NRestarts=0. Skip its own start/finish heartbeats
        # (whose description contains the word "restart" -> 266 false positives/24h) and
        # count only genuine restart ACTIONS. A restarting watchdog logs
        # "restarting"/"restarted"/"scheduled restart".
        if "no restart" in text:
            continue
        if "restarted" in text or "restarting" in text or "scheduled restart" in text:
            watchdogs += 1
    return JournalCounts(errors, fallbacks, watchdogs, tool_config_errors, tool_backend_errors, tool_guardrail_denials, auxiliary_fallbacks)


def _line_epoch(line: str) -> float | None:
    match = _TS_PREFIX_RE.match(line)
    if not match:
        return None
    raw = match.group("ts") + (match.group("tz") or "+00:00")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", raw):
        raw = raw[:-2] + ":" + raw[-2:]
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _bucket_start(ts: float, bucket_seconds: int) -> int:
    return int(ts // bucket_seconds * bucket_seconds)


def build_bucket_series(rows: list[dict[str, Any]], gateway_lines: Iterable[str], *, bucket_seconds: int = BUCKET_SECONDS) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = defaultdict(lambda: {
        "turn_count": 0,
        "latencies_ms": [],
        "retry_turns": 0,
        "cost_usd": 0.0,
        "error_events": 0,
        "fallback_events": 0,
        "tool_config_error_events": 0,
        "tool_backend_error_events": 0,
        "tool_guardrail_denial_events": 0,
        "auxiliary_fallback_events": 0,
    })
    for row in rows:
        bucket = _bucket_start(float(row["ts"]), bucket_seconds)
        item = buckets[bucket]
        item["turn_count"] += 1
        if row.get("latency_ms") is not None and float(row["latency_ms"]) <= MAX_PLAUSIBLE_LATENCY_MS:
            item["latencies_ms"].append(float(row["latency_ms"]))
        if int(row.get("retry_count") or 0) > 0:
            item["retry_turns"] += 1
        item["cost_usd"] += float(row.get("estimated_cost_usd") or 0.0)
    for line in gateway_lines:
        epoch = _line_epoch(line)
        if epoch is None:
            continue
        bucket = _bucket_start(epoch, bucket_seconds)
        if _TOOL_CONFIG_ERROR_RE.search(line):
            buckets[bucket]["tool_config_error_events"] += 1
        if _TOOL_BACKEND_ERROR_RE.search(line):
            buckets[bucket]["tool_backend_error_events"] += 1
        if _TOOL_GUARDRAIL_DENIAL_RE.search(line):
            buckets[bucket]["tool_guardrail_denial_events"] += 1
        if _is_counted_gateway_error(line):
            buckets[bucket]["error_events"] += 1
        if _FALLBACK_RE.search(line) and _AUXILIARY_FALLBACK_RE.search(line):
            buckets[bucket]["auxiliary_fallback_events"] += 1
        if _is_counted_fallback(line):
            buckets[bucket]["fallback_events"] += 1
    series = []
    for bucket in sorted(buckets):
        item = buckets[bucket]
        turn_count = item["turn_count"]
        fallback_events = item["fallback_events"]  # retries are not fallbacks (2026-06-25)
        series.append({
            "bucket_start": utc_iso(bucket),
            "bucket_epoch": bucket,
            "turn_count": turn_count,
            "gateway_turn_p95_latency_ms": percentile(item["latencies_ms"], 0.95),
            "turn_error_rate": min(1.0, item["error_events"] / turn_count) if turn_count else None,
            "fallback_trigger_rate": min(1.0, fallback_events / turn_count) if turn_count else None,
            "cost_burn_rate_usd_bucket": round(item["cost_usd"], 6),
            "error_events": item["error_events"],
            "fallback_events": fallback_events,
            "tool_config_error_events": item["tool_config_error_events"],
            "tool_backend_error_events": item["tool_backend_error_events"],
            "tool_guardrail_denial_events": item["tool_guardrail_denial_events"],
            "auxiliary_fallback_events": item["auxiliary_fallback_events"],
        })
    return series


def _event_epoch(event: dict[str, Any]) -> float | None:
    ts = event.get("ts") or event.get("timestamp") or event.get("created_at")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _iter_jsonl(expanded: Path, *, since_epoch: float | None) -> Iterable[dict[str, Any]]:
    for raw in expanded.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        epoch = _event_epoch(event)
        if since_epoch is not None and epoch is not None and epoch < since_epoch:
            continue
        yield event


def _read_service_up(path: Path, *, since_epoch: float | None) -> dict[str, Any]:
    """Informational sub-field: is the warm-recall path producing injections at all.

    This is the OLD recall-events.jsonl signal (n_lessons>0 per dispatch). It is
    deliberately NOT the SLO value — a service that injects 5 lessons on every
    dispatch is structurally 'up' even if those lessons are irrelevant. We keep it
    only to distinguish 'recall down' from 'recall up but mis-ranking'.
    """
    expanded = path.expanduser()
    if not expanded.exists():
        return {"status": "no_data", "source_exists": False, "path": str(expanded), "rate": None, "total": 0, "up": 0}
    total = up = 0
    for event in _iter_jsonl(expanded, since_epoch=since_epoch):
        total += 1
        n_lessons = event.get("n_lessons")
        if bool(
            event.get("hit") or event.get("matched") or event.get("recalled") or event.get("success")
            or (isinstance(n_lessons, (int, float)) and not isinstance(n_lessons, bool) and n_lessons > 0)
        ):
            up += 1
    return {
        "status": "ok" if total else "no_events",
        "source_exists": True,
        "path": str(expanded),
        "rate": (up / total) if total else None,
        "total": total,
        "up": up,
    }


def read_recall_hit_rate(
    canary_path: Path = DEFAULT_RECALL_CANARY,
    *,
    since_epoch: float | None = None,
    service_events_path: Path = DEFAULT_RECALL_EVENTS,
    discrimination_floor: float = RECALL_DISCRIMINATION_FLOOR,
) -> dict[str, Any]:
    """Recall hit-rate from the recall-quality canary's target-in-top-k ledger.

    A canary record is a HIT when the known in-pool target appeared in the
    production recall(query, k=10) top-k. The in-domain/off-domain cosine gap is
    reported separately as ``discrimination_pass_rate``; it does not turn a real
    retrieval hit into a miss. Records with a null hit (the canary's never-raise
    setup_error path) are skipped. ``service_up`` is another separate diagnostic
    off the legacy recall-events.jsonl injection ledger.
    """
    expanded = canary_path.expanduser()
    service_up = _read_service_up(service_events_path, since_epoch=since_epoch)
    if not expanded.exists():
        return {
            "status": "no_data",
            "source_exists": False,
            "path": str(expanded),
            "hit_rate": None,
            "total": 0,
            "hits": 0,
            "target_misses": 0,
            "cosine_collapses": 0,
            "discrimination_samples": 0,
            "discrimination_passes": 0,
            "discrimination_pass_rate": None,
            "discrimination_floor": discrimination_floor,
            "service_up": service_up,
        }
    total = hits = target_misses = cosine_collapses = 0
    discrimination_samples = discrimination_passes = 0
    for event in _iter_jsonl(expanded, since_epoch=since_epoch):
        # Prefer the explicit canary fields; fall back to a bare `hit` flag so
        # legacy/synthetic hit-only records still score.
        raw_hit = event.get("target_hit")
        if raw_hit is None:
            raw_hit = event.get("hit")
        if raw_hit is None:
            # null hit -> canary setup_error / not a recall measurement; skip.
            continue
        total += 1
        # bool/int normalisation: 0/False -> miss, 1/True/non-zero -> candidate hit.
        target_hit = bool(raw_hit)
        gap = event.get("discrimination_gap")
        if gap is None:
            gap = event.get("gap")
        if gap is not None:
            try:
                numeric_gap = float(gap)
            except (TypeError, ValueError):
                numeric_gap = None
            if numeric_gap is not None and math.isfinite(numeric_gap):
                discrimination_samples += 1
                if numeric_gap >= discrimination_floor:
                    discrimination_passes += 1
                else:
                    cosine_collapses += 1
        if target_hit:
            hits += 1
        else:
            target_misses += 1
    return {
        "status": "ok" if total else "no_events",
        "source_exists": True,
        "path": str(expanded),
        "hit_rate": (hits / total) if total else None,
        "total": total,
        "hits": hits,
        "target_misses": target_misses,
        "cosine_collapses": cosine_collapses,
        "discrimination_samples": discrimination_samples,
        "discrimination_passes": discrimination_passes,
        "discrimination_pass_rate": (
            discrimination_passes / discrimination_samples
            if discrimination_samples
            else None
        ),
        "discrimination_floor": discrimination_floor,
        "service_up": service_up,
    }


def build_slo_snapshot(
    *,
    state_db: Path = DEFAULT_STATE_DB,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    now: float | None = None,
    window_seconds: int = WINDOW_SECONDS,
    gateway_lines: list[str] | None = None,
    watchdog_lines: list[str] | None = None,
    recall_canary_path: Path = DEFAULT_RECALL_CANARY,
    recall_service_path: Path = DEFAULT_RECALL_EVENTS,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    since = now - window_seconds
    with open_state_db_readonly(state_db) as con:
        rows = _fetch_turn_rows(con, since)
    if gateway_lines is None:
        gateway_lines = journalctl_lines("hermes-gateway.service", since)
    if watchdog_lines is None:
        watchdog_lines = journalctl_lines("hermes-gateway-watchdog.service", since)
    counts = parse_journal_counts(gateway_lines, watchdog_lines)
    latencies = [
        float(row["latency_ms"]) for row in rows
        if row.get("latency_ms") is not None and float(row["latency_ms"]) <= MAX_PLAUSIBLE_LATENCY_MS
    ]
    turn_count = len(rows)
    retry_turns = sum(1 for row in rows if int(row.get("retry_count") or 0) > 0)
    total_cost = sum(float(row.get("estimated_cost_usd") or 0.0) for row in rows)
    # 2026-06-25: provider fallbacks come from gateway journald only; ordinary turn
    # retries (retry_count>0) are NOT fallbacks and are reported separately as retry_turns.
    fallback_events = counts.fallback_events
    recall = read_recall_hit_rate(
        recall_canary_path, since_epoch=since, service_events_path=recall_service_path
    )
    metrics = {
        "gateway_turn_p95_latency_ms": percentile(latencies, 0.95),
        "turn_error_rate": min(1.0, counts.error_events / turn_count) if turn_count else None,
        "fallback_trigger_rate": min(1.0, fallback_events / turn_count) if turn_count else None,
        "recall_hit_rate": recall["hit_rate"],
        "recall_discrimination_pass_rate": recall["discrimination_pass_rate"],
        "watchdog_restart_count": counts.watchdog_restart_events,
        "cost_burn_rate_usd_24h": round(total_cost, 6),
    }
    sources = {
        "state_db": str(state_db.expanduser()),
        "state_db_mode": "ro",
        "gateway_journal_unit": "hermes-gateway.service",
        "watchdog_journal_unit": "hermes-gateway-watchdog.service",
        "recall_canary": str(recall_canary_path.expanduser()),
        "recall_events": str(recall_service_path.expanduser()),
        "output_dir": str(output_dir.expanduser()),
    }
    return {
        "generated_at": utc_iso(now),
        "window_seconds": window_seconds,
        "since": utc_iso(since),
        "turn_count": turn_count,
        "journal_counts": counts.__dict__,
        "retry_turns": retry_turns,
        "metrics": metrics,
        "recall": recall,
        "slo_definitions": SLO_DEFINITIONS,
        "series": build_bucket_series(rows, gateway_lines),
        "sources": sources,
    }


def write_snapshot(snapshot: dict[str, Any], *, timeseries_path: Path = DEFAULT_TIMESERIES, latest_path: Path = DEFAULT_LATEST) -> None:
    timeseries_path = timeseries_path.expanduser()
    latest_path = latest_path.expanduser()
    timeseries_path.parent.mkdir(parents=True, exist_ok=True)
    point = {
        "generated_at": snapshot["generated_at"],
        "turn_count": snapshot["turn_count"],
        **snapshot["metrics"],
    }
    with timeseries_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(point, sort_keys=True) + "\n")
    tmp = latest_path.with_suffix(latest_path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(latest_path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Hermes SLO metrics from state.db + journald")
    parser.add_argument("--state-db", default=str(DEFAULT_STATE_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--timeseries", default=str(DEFAULT_TIMESERIES))
    parser.add_argument("--latest", default=str(DEFAULT_LATEST))
    parser.add_argument("--window-seconds", type=int, default=WINDOW_SECONDS)
    parser.add_argument("--print", action="store_true", dest="print_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    snapshot = build_slo_snapshot(
        state_db=Path(args.state_db),
        output_dir=Path(args.output_dir),
        window_seconds=args.window_seconds,
    )
    write_snapshot(snapshot, timeseries_path=Path(args.timeseries), latest_path=Path(args.latest))
    if args.print_json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    else:
        metrics = snapshot["metrics"]
        print(
            "slo-export ok "
            f"turns={snapshot['turn_count']} "
            f"p95_ms={metrics['gateway_turn_p95_latency_ms']} "
            f"error_rate={metrics['turn_error_rate']} "
            f"fallback_rate={metrics['fallback_trigger_rate']} "
            f"cost24h={metrics['cost_burn_rate_usd_24h']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
