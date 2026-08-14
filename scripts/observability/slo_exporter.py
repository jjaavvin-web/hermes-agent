#!/usr/bin/env python3
"""Hermes SLO exporter entrypoint with calibrated K4 journal counting.

The shared :mod:`hermes_cli.observability_slo` module still provides the state-db,
recall, snapshot write, and CLI plumbing. This script owns the timer-executed K4
calibration so ``turn_error_rate`` is a failed-turn fraction instead of a broad
"error-looking journal lines / turns" ratio, while preserving diagnostic split
counters for restart/watchdog/reconnect noise.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from hermes_cli import observability_slo as _base

SLO_DEFINITIONS: dict[str, dict[str, Any]] = {
    **_base.SLO_DEFINITIONS,
    "turn_error_rate": {
        **_base.SLO_DEFINITIONS["turn_error_rate"],
        "source": "explicit failed-turn journal markers divided by turn_usage turns; process exits, reconnect churn, and watchdog kills are split into diagnostic counters",
    },
    "fallback_trigger_rate": {
        **_base.SLO_DEFINITIONS["fallback_trigger_rate"],
        "source": "distinct successful provider/model failover transitions logged as 'Fallback activated:', divided by turn_usage turns; fallback advice and CLI help text are excluded",
    },
    "gateway_restart_count": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "count/24h",
        "page": False,
        "source": "hermes-gateway.service systemd process-exit/result lines de-duped into restart/exit events",
    },
    "watchdog_kill_count": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "count/24h",
        "page": False,
        "source": "hermes-gateway.service watchdog timeout/SIGABRT/result-watchdog lines collapsed into availability incidents",
    },
    "reconnect_burst_count": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "count/24h",
        "page": False,
        "source": "MCP/Discord reconnect-family journal lines collapsed into 120s bursts",
    },
    "diagnostic_error_line_count": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "count/24h",
        "page": False,
        "source": "error-looking diagnostic lines that are not explicit failed-turn markers and not classified restart/watchdog/reconnect noise",
    },
    "mcp_reconnect_burst_count": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "count/24h",
        "page": False,
        "source": "MCP-family reconnect lines collapsed into 120s bursts",
    },
    "discord_reconnect_burst_count": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "count/24h",
        "page": False,
        "source": "Discord-family reconnect lines collapsed into 120s bursts",
    },
    "mcp_reconnect_line_count": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "count/24h",
        "page": False,
        "source": "raw MCP-family reconnect line count before burst collapse",
    },
    "discord_reconnect_line_count": {
        "target": "diagnostic",
        "warn": None,
        "critical": None,
        "unit": "count/24h",
        "page": False,
        "source": "raw Discord-family reconnect line count before burst collapse",
    },
}

# Every alternative must correspond to a REAL logger call that fires only when
# a user turn fails to produce a normal response:
#   "Agent error in session"            gateway/run.py — outermost per-turn
#       handler; the try's success path returns the response, so this fires
#       only after every tool/provider recovery layer deeper in the loop
#   "Non-retryable client error"        agent/conversation_loop.py (terminal,
#       logged when no fallback rescues the call)
#   "Invalid API response after N retries."  agent/conversation_loop.py
# "Outer loop error in API call #" is deliberately excluded: that handler
# normally repairs message state and continues to a later API call.
# Patterns that can also match tool-level or expected-backend noise belong in
# the diagnostic buckets, never here (this regex feeds the page-bearing rate).
_FAILED_TURN_RE = re.compile(
    r"(?:^|:\s+)ERROR\s+(?:"
    r"gateway\.run:\s+Agent error in session\s+"
    r"|agent\.conversation_loop:\s+(?!\[subagent-)(?:\[[^\]]+\]\s*)?"
    r"(?:Non-retryable client error|Invalid API response after \d+ retries)"
    r"|agent\.turn:.*\b(?:TURN_FAILED|turn failed|failed turn|terminal turn failure|terminal_error=.*after retries exhausted|returned error to user)\b"
    r")",
    re.I,
)
_REAL_FALLBACK_RE = re.compile(r"\bFallback activated:\s+.+?\s+(?:→|->)\s+.+?\s+\([^)]+\)", re.I)
_GATEWAY_UNIT_RE = re.compile(r"hermes-gateway\.service", re.I)
_GATEWAY_EXIT_RE = re.compile(
    r"Main process exited, code=.*status=.*(?:FAILURE|ABRT)|Failed with result ['\"](?:exit-code|signal)['\"]|Scheduled restart job",
    re.I,
)
_WATCHDOG_KILL_RE = re.compile(
    r"Watchdog timeout|Failed with result ['\"]watchdog['\"]|status=6/ABRT|code=killed, status=6/ABRT|SIGABRT|Failed to kill control group|watchdog.*kill",
    re.I,
)
_RECONNECT_RE = re.compile(r"reconnect|reconnecting|connection lost|keepalive failed", re.I)
_MCP_RECONNECT_RE = re.compile(r"tools\.mcp_tool|MCP server", re.I)
_DISCORD_RECONNECT_RE = re.compile(r"discord\.client|Discord", re.I)
_PRECISE_TS_PREFIX_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)


@dataclass(frozen=True)
class CalibratedJournalCounts:
    # error_events keeps its PRE-K4 broad semantics (every line matching the
    # base _is_counted_gateway_error filter, before K4 classification) so the
    # field name never silently narrows; turn_error_events below is the
    # page-bearing calibrated numerator. Scope caveat: counted over gateway AND
    # watchdog lines (this module's combined loop), while the pre-K4 base
    # scanned gateway lines only — can read slightly HIGH, never quiet.
    error_events: int = 0
    fallback_events: int = 0
    watchdog_restart_events: int = 0
    tool_config_error_events: int = 0
    tool_backend_error_events: int = 0
    tool_guardrail_denial_events: int = 0
    auxiliary_fallback_events: int = 0
    # K4 split counters.
    turn_error_events: int = 0
    gateway_restart_events: int = 0
    gateway_restart_line_events: int = 0
    watchdog_kill_events: int = 0
    watchdog_kill_line_events: int = 0
    reconnect_burst_events: int = 0
    reconnect_line_events: int = 0
    mcp_reconnect_burst_events: int = 0
    discord_reconnect_burst_events: int = 0
    mcp_reconnect_line_events: int = 0
    discord_reconnect_line_events: int = 0
    diagnostic_error_line_events: int = 0


def _line_epoch(line: str) -> float | None:
    match = _PRECISE_TS_PREFIX_RE.match(line)
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


def _journalctl_lines_precise(unit: str, since_epoch: float, *, limit: int = 2000) -> list[str]:
    """Read journald without discarding event identity below one second."""
    since = datetime.fromtimestamp(since_epoch, tz=timezone.utc).isoformat()
    proc = _base._run([  # type: ignore[attr-defined]
        "journalctl", "--user", "-u", unit, "--since", since,
        "-n", str(limit), "--no-pager", "-o", "short-iso-precise",
    ], timeout=12.0)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _collapse_times(times: Sequence[float], *, window_seconds: int) -> int:
    if not times:
        return 0
    bursts = 0
    last: float | None = None
    for ts in sorted(times):
        if last is None or ts - last > window_seconds:
            bursts += 1
        last = ts
    return bursts


def _is_failed_turn_marker(line: str) -> bool:
    # Subagent failures are measured by their own delegation/run contracts; do
    # not charge the parent gateway turn for a leaf's terminal event.  The
    # prefix appears before the structured logger token in journal output, so
    # this exclusion must apply to the whole line rather than one regex branch.
    if re.search(r"\[subagent-[^\]]+\]", line, re.I):
        return False
    return bool(_FAILED_TURN_RE.search(line))


def _event_key(line: str, *, family: str) -> tuple[str, float | None, str]:
    """Collapse duplicate renderings of one structured journal event.

    Buffered status output can replay the exact same journal event. Preserve
    sub-second identity so distinct turns/failovers in one second are not merged.
    """
    epoch = _line_epoch(line)
    message = line.split(": ", 1)[-1].strip()
    message = re.sub(r"\s+", " ", message)
    return family, epoch, message


def _is_real_fallback_event(line: str) -> bool:
    return bool(_REAL_FALLBACK_RE.search(line))


def _is_gateway_exit_line(line: str) -> bool:
    return bool(_GATEWAY_UNIT_RE.search(line) and _GATEWAY_EXIT_RE.search(line) and not _WATCHDOG_KILL_RE.search(line))


def _is_watchdog_kill_line(line: str) -> bool:
    return bool(_GATEWAY_UNIT_RE.search(line) and _WATCHDOG_KILL_RE.search(line))


def _is_reconnect_line(line: str) -> bool:
    return bool(_RECONNECT_RE.search(line) and (_MCP_RECONNECT_RE.search(line) or _DISCORD_RECONNECT_RE.search(line)))


def _is_mcp_reconnect_line(line: str) -> bool:
    return bool(_is_reconnect_line(line) and _MCP_RECONNECT_RE.search(line))


def _is_discord_reconnect_line(line: str) -> bool:
    return bool(_is_reconnect_line(line) and _DISCORD_RECONNECT_RE.search(line))


def _event_time(line: str, fallback_index: int) -> float:
    epoch = _line_epoch(line)
    return epoch if epoch is not None else float(fallback_index)


def parse_journal_counts(gateway_lines: Iterable[str], watchdog_lines: Iterable[str] = ()) -> CalibratedJournalCounts:
    gateway = list(gateway_lines)
    watchdog = list(watchdog_lines)
    fallback_event_keys: set[tuple[str, float | None, str]] = set()
    tool_config_errors = 0
    tool_backend_errors = 0
    tool_guardrail_denials = 0
    auxiliary_fallbacks = 0
    turn_error_keys: set[tuple[str, float | None, str]] = set()
    diagnostic_errors = 0
    gateway_exit_times: list[float] = []
    watchdog_kill_times: list[float] = []
    reconnect_times: list[float] = []
    mcp_reconnect_times: list[float] = []
    discord_reconnect_times: list[float] = []
    gateway_exit_lines = 0
    watchdog_kill_lines = 0
    reconnect_lines = 0
    mcp_reconnect_lines = 0
    discord_reconnect_lines = 0

    legacy_error_lines = 0
    for idx, line in enumerate([*gateway, *watchdog]):
        if _base._is_counted_gateway_error(line):  # type: ignore[attr-defined]
            legacy_error_lines += 1
        if _base._TOOL_CONFIG_ERROR_RE.search(line):  # type: ignore[attr-defined]
            tool_config_errors += 1
        if _base._TOOL_BACKEND_ERROR_RE.search(line):  # type: ignore[attr-defined]
            tool_backend_errors += 1
        if _base._TOOL_GUARDRAIL_DENIAL_RE.search(line):  # type: ignore[attr-defined]
            tool_guardrail_denials += 1
        if _base._FALLBACK_RE.search(line) and _base._AUXILIARY_FALLBACK_RE.search(line):  # type: ignore[attr-defined]
            auxiliary_fallbacks += 1
        if _is_real_fallback_event(line):
            fallback_event_keys.add(_event_key(line, family="fallback"))

        if _is_failed_turn_marker(line):
            turn_error_keys.add(_event_key(line, family="turn_error"))
            continue
        if _is_gateway_exit_line(line):
            gateway_exit_lines += 1
            gateway_exit_times.append(_event_time(line, idx))
            continue
        if _is_watchdog_kill_line(line):
            watchdog_kill_lines += 1
            watchdog_kill_times.append(_event_time(line, idx))
            continue
        if _is_reconnect_line(line):
            reconnect_lines += 1
            reconnect_times.append(_event_time(line, idx))
            if _is_mcp_reconnect_line(line):
                mcp_reconnect_lines += 1
                mcp_reconnect_times.append(_event_time(line, idx))
            if _is_discord_reconnect_line(line):
                discord_reconnect_lines += 1
                discord_reconnect_times.append(_event_time(line, idx))
            continue
        if _base._is_counted_gateway_error(line):  # type: ignore[attr-defined]
            diagnostic_errors += 1

    base_watchdogs = _base.parse_journal_counts([], watchdog).watchdog_restart_events
    return CalibratedJournalCounts(
        error_events=legacy_error_lines,
        fallback_events=len(fallback_event_keys),
        watchdog_restart_events=base_watchdogs,
        tool_config_error_events=tool_config_errors,
        tool_backend_error_events=tool_backend_errors,
        tool_guardrail_denial_events=tool_guardrail_denials,
        auxiliary_fallback_events=auxiliary_fallbacks,
        turn_error_events=len(turn_error_keys),
        gateway_restart_events=_collapse_times(gateway_exit_times, window_seconds=5),
        gateway_restart_line_events=gateway_exit_lines,
        watchdog_kill_events=_collapse_times(watchdog_kill_times, window_seconds=120),
        watchdog_kill_line_events=watchdog_kill_lines,
        reconnect_burst_events=_collapse_times(reconnect_times, window_seconds=120),
        reconnect_line_events=reconnect_lines,
        mcp_reconnect_burst_events=_collapse_times(mcp_reconnect_times, window_seconds=120),
        discord_reconnect_burst_events=_collapse_times(discord_reconnect_times, window_seconds=120),
        mcp_reconnect_line_events=mcp_reconnect_lines,
        discord_reconnect_line_events=discord_reconnect_lines,
        diagnostic_error_line_events=diagnostic_errors,
    )


def _fetch_turn_rows_with_source(con: Any, since_epoch: float) -> list[dict[str, Any]]:
    """Fetch turns with session source for coherent primary-turn rates."""
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT t.ts, t.provider, t.model, t.latency_ms, t.retry_count,
                   t.estimated_cost_usd, t.cost_status, t.total_tokens,
                   t.input_tokens, t.output_tokens, t.tool_count,
                   t.turn_id, t.session_id, s.source AS session_source
              FROM turn_usage AS t
              LEFT JOIN sessions AS s ON s.id = t.session_id
             WHERE t.ts >= ?
             ORDER BY t.ts ASC
            """,
            (since_epoch,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # Compatibility for old fixture/legacy stores that predate sessions.
        if "no such table: sessions" not in str(exc).lower():
            raise
        return _base._fetch_turn_rows(con, since_epoch)  # type: ignore[attr-defined]
    return [dict(row) for row in rows]


def _is_primary_turn_row(row: dict[str, Any]) -> bool:
    # Keep unmatched legacy rows: only explicit source='subagent' is excluded.
    return str(row.get("session_source") or "").lower() != "subagent"


def build_bucket_series(rows: list[dict[str, Any]], gateway_lines: Iterable[str], *, bucket_seconds: int = _base.BUCKET_SECONDS) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = defaultdict(lambda: {
        "turn_count": 0,
        "latencies_ms": [],
        "retry_turns": 0,
        "cost_usd": 0.0,
        "lines": [],
    })
    for row in rows:
        bucket = _base._bucket_start(float(row["ts"]), bucket_seconds)  # type: ignore[attr-defined]
        item = buckets[bucket]
        # Cost is workforce-wide; primary-turn reliability is not. Preserve
        # leaf spend while excluding leaf turns from gateway rate denominators.
        item["cost_usd"] += float(row.get("estimated_cost_usd") or 0.0)
        if not _is_primary_turn_row(row):
            continue
        item["turn_count"] += 1
        if row.get("latency_ms") is not None and float(row["latency_ms"]) <= _base.MAX_PLAUSIBLE_LATENCY_MS:
            item["latencies_ms"].append(float(row["latency_ms"]))
        if int(row.get("retry_count") or 0) > 0:
            item["retry_turns"] += 1
    for line in gateway_lines:
        epoch = _line_epoch(line)
        if epoch is None:
            continue
        bucket = _base._bucket_start(epoch, bucket_seconds)  # type: ignore[attr-defined]
        buckets[bucket]["lines"].append(line)
    series = []
    for bucket in sorted(buckets):
        item = buckets[bucket]
        turn_count = item["turn_count"]
        counts = parse_journal_counts(item["lines"])
        series.append({
            "bucket_start": _base.utc_iso(bucket),
            "bucket_epoch": bucket,
            "turn_count": turn_count,
            "gateway_turn_p95_latency_ms": _base.percentile(item["latencies_ms"], 0.95),
            "turn_error_rate": min(1.0, counts.turn_error_events / turn_count) if turn_count else None,
            "fallback_trigger_rate": min(1.0, counts.fallback_events / turn_count) if turn_count else None,
            "cost_burn_rate_usd_bucket": round(item["cost_usd"], 6),
            **counts.__dict__,
        })
    return series


def build_slo_snapshot(
    *,
    state_db: Path = _base.DEFAULT_STATE_DB,
    output_dir: Path = _base.DEFAULT_OUTPUT_DIR,
    now: float | None = None,
    window_seconds: int = _base.WINDOW_SECONDS,
    gateway_lines: list[str] | None = None,
    watchdog_lines: list[str] | None = None,
    recall_canary_path: Path = _base.DEFAULT_RECALL_CANARY,
    recall_service_path: Path = _base.DEFAULT_RECALL_EVENTS,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    since = now - window_seconds
    with _base.open_state_db_readonly(state_db) as con:
        rows = _fetch_turn_rows_with_source(con, since)
    primary_rows = [row for row in rows if _is_primary_turn_row(row)]
    if gateway_lines is None:
        gateway_lines = _journalctl_lines_precise("hermes-gateway.service", since)
    if watchdog_lines is None:
        watchdog_lines = _journalctl_lines_precise("hermes-gateway-watchdog.service", since)
    counts = parse_journal_counts(gateway_lines, watchdog_lines)
    latencies = [
        float(row["latency_ms"]) for row in primary_rows
        if row.get("latency_ms") is not None and float(row["latency_ms"]) <= _base.MAX_PLAUSIBLE_LATENCY_MS
    ]
    turn_count = len(primary_rows)
    retry_turns = sum(1 for row in primary_rows if int(row.get("retry_count") or 0) > 0)
    # Cost burn remains workforce-wide even though reliability rates are scoped
    # to primary gateway turns.
    total_cost = sum(float(row.get("estimated_cost_usd") or 0.0) for row in rows)
    recall = _base.read_recall_hit_rate(
        recall_canary_path, since_epoch=since, service_events_path=recall_service_path
    )
    metrics = {
        "gateway_turn_p95_latency_ms": _base.percentile(latencies, 0.95),
        "turn_error_rate": min(1.0, counts.turn_error_events / turn_count) if turn_count else None,
        "fallback_trigger_rate": min(1.0, counts.fallback_events / turn_count) if turn_count else None,
        "recall_hit_rate": recall["hit_rate"],
        "watchdog_restart_count": counts.watchdog_restart_events,
        "cost_burn_rate_usd_24h": round(total_cost, 6),
        "gateway_restart_count": counts.gateway_restart_events,
        "watchdog_kill_count": counts.watchdog_kill_events,
        "reconnect_burst_count": counts.reconnect_burst_events,
        "diagnostic_error_line_count": counts.diagnostic_error_line_events,
        "mcp_reconnect_burst_count": counts.mcp_reconnect_burst_events,
        "discord_reconnect_burst_count": counts.discord_reconnect_burst_events,
        "mcp_reconnect_line_count": counts.mcp_reconnect_line_events,
        "discord_reconnect_line_count": counts.discord_reconnect_line_events,
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
        "generated_at": _base.utc_iso(now),
        "window_seconds": window_seconds,
        "since": _base.utc_iso(since),
        "turn_count": turn_count,
        "journal_counts": counts.__dict__,
        "retry_turns": retry_turns,
        "metrics": metrics,
        "recall": recall,
        "slo_definitions": SLO_DEFINITIONS,
        "series": build_bucket_series(rows, gateway_lines),
        "sources": sources,
    }


def build_arg_parser():
    return _base.build_arg_parser()


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    snapshot = build_slo_snapshot(
        state_db=Path(args.state_db),
        output_dir=Path(args.output_dir),
        window_seconds=args.window_seconds,
    )
    _base.write_snapshot(snapshot, timeseries_path=Path(args.timeseries), latest_path=Path(args.latest))
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
            f"cost24h={metrics['cost_burn_rate_usd_24h']} "
            f"gateway_restarts={metrics['gateway_restart_count']} "
            f"watchdog_kills={metrics['watchdog_kill_count']} "
            f"reconnect_bursts={metrics['reconnect_burst_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
