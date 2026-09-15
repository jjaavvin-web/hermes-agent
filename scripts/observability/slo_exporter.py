#!/usr/bin/env python3
"""Hermes SLO exporter entrypoint with calibrated K4 journal counting.

The shared :mod:`hermes_cli.observability_slo` module still provides the state-db,
recall, snapshot write, and CLI plumbing. This script owns the timer-executed K4
calibration so ``turn_error_rate`` is a failed-turn fraction instead of a broad
"error-looking journal lines / turns" ratio, while preserving diagnostic split
counters for restart/watchdog/reconnect noise.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from hermes_cli import observability_slo as _base

SLO_DEFINITIONS: dict[str, dict[str, Any]] = {
    **_base.SLO_DEFINITIONS,
    "turn_error_rate": {
        **_base.SLO_DEFINITIONS["turn_error_rate"],
        "source": "failed-event estimate per recorded turn (not a turn-ID join); paired console/logger errors count once; process exits/reconnect/watchdog remain diagnostic",
    },
    "fallback_trigger_rate": {
        **_base.SLO_DEFINITIONS["fallback_trigger_rate"],
        "source": "confirmed INFO Fallback activated logger records per recorded turn; advice, attempts, recovery chatter and duplicate UI records excluded",
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

# Every alternative in the first group must correspond to a REAL logger call
# that fires only when a user turn fails to produce a normal response:
#   "Agent error in session"            gateway/run.py — outermost per-turn
#       handler; the try's success path returns the response, so this fires
#       only after every tool/provider recovery layer deeper in the loop
#   "Outer loop error in API call #"    agent/conversation_loop.py
#   "Non-retryable client error"        agent/conversation_loop.py (terminal,
#       logged when no fallback rescues the call)
#   "Invalid API response after N retries."  agent/conversation_loop.py
# Patterns that can also match tool-level or expected-backend noise belong in
# the diagnostic buckets, never here (this regex feeds the page-bearing rate).
_FAILED_TURN_RE = re.compile(
    r"Agent error in session |Outer loop error in API call #"
    r"|Non-retryable client error|Invalid API response after \d+ retries"
    r"|\b(?:TURN_FAILED|turn failed|failed turn|terminal turn failure|terminal_error=.*after retries exhausted|returned error to user)\b",
    re.I,
)
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
    return _base._line_epoch(line)  # type: ignore[attr-defined]


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
    return bool(_FAILED_TURN_RE.search(line))


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


# Count confirmed activation logger records, not advice, attempts, UI echoes or
# the second "activated after empty responses" log emitted by the same switch.
_ACTIVATED_FALLBACK_RE = re.compile(
    r"\bINFO\s+[\w.]+:\s*(?:\[[^\]]+\]\s*)?Fallback activated: .+ (?:→|->) .+ \(.+\)"
)
_CONSOLE_CLIENT_ERROR_RE = re.compile(r"❌ Non-retryable client error \(HTTP [^)]*\)\. Aborting\.")
_LOGGER_CLIENT_ERROR_RE = re.compile(r"\bERROR\s+agent\.conversation_loop:\s*(?:\[[^\]]+\]\s*)?Non-retryable client error:")
_PID_RE = re.compile(r"\b(?:python[\w.-]*|hermes)\[(\d+)\]:")


def _paired_error_identity(line: str) -> tuple[str, str] | None:
    pid = _PID_RE.search(line)
    if not pid:
        return None  # no identity = no speculative time-only collapsing
    body = line[pid.end():].strip()
    body = re.sub(r"^(?:\d{4}-\d\d-\d\d \S+ )?ERROR agent\.conversation_loop:\s*", "", body)
    prefix = re.match(r"\[([^]]+)\]", body)
    return pid[1], prefix[1] if prefix else ""


def _event_indexes(lines: Sequence[str]) -> tuple[set[int], set[int]]:
    """Remove only proven console/logger duplicate pairs, not adjacent failures.

    Legacy records lack turn IDs: this remains a failed-event/recorded-turn
    estimate, NOT an exact join to unique turns. One-to-one pairing conserves
    two same-process failures in the same second. Unpaired lines stay counted.
    """
    errors = {i for i, line in enumerate(lines) if _is_failed_turn_marker(line)}
    logger_rows = [i for i in errors if _LOGGER_CLIENT_ERROR_RE.search(lines[i])]
    unused = set(logger_rows)
    for i in sorted(errors):
        if _LOGGER_CLIENT_ERROR_RE.search(lines[i]) or not _CONSOLE_CLIENT_ERROR_RE.search(lines[i]):
            continue
        identity, ts = _paired_error_identity(lines[i]), _line_epoch(lines[i])
        if identity is None or ts is None:
            continue
        for j in sorted(unused):
            other_ts = _line_epoch(lines[j])
            if (j > i and other_ts is not None and 0 <= other_ts - ts <= 2
                    and identity == _paired_error_identity(lines[j])):
                errors.remove(i)
                unused.remove(j)
                break
    fallbacks = {i for i, line in enumerate(lines) if _ACTIVATED_FALLBACK_RE.search(line)}
    return errors, fallbacks


def event_evidence(lines: Sequence[str]) -> dict[str, list[str]]:
    """Stable, content-free IDs; retain repeated equal logger records separately."""
    errors, fallbacks = _event_indexes(lines)
    result = {}
    for name, indexes in (("turn_error_rate", errors), ("fallback_trigger_rate", fallbacks)):
        seen: dict[str, int] = {}
        ids = []
        for i in sorted(indexes):
            line = lines[i].strip()
            occurrence = seen.get(line, 0)
            seen[line] = occurrence + 1
            ids.append(hashlib.sha256(f"{name}\n{line}\n{occurrence}".encode()).hexdigest())
        result[name] = ids
    return result


def parse_journal_counts(gateway_lines: Iterable[str], watchdog_lines: Iterable[str] = ()) -> CalibratedJournalCounts:
    gateway = list(gateway_lines)
    watchdog = list(watchdog_lines)
    fallback_events = 0
    tool_config_errors = 0
    tool_backend_errors = 0
    tool_guardrail_denials = 0
    auxiliary_fallbacks = 0
    turn_errors = 0
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
    all_lines = [*gateway, *watchdog]
    error_indexes, fallback_indexes = _event_indexes(all_lines)
    for idx, line in enumerate(all_lines):
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
        if idx in fallback_indexes:
            fallback_events += 1

        if idx in error_indexes:
            turn_errors += 1
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
        fallback_events=fallback_events,
        watchdog_restart_events=base_watchdogs,
        tool_config_error_events=tool_config_errors,
        tool_backend_error_events=tool_backend_errors,
        tool_guardrail_denial_events=tool_guardrail_denials,
        auxiliary_fallback_events=auxiliary_fallbacks,
        turn_error_events=turn_errors,
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
        item["turn_count"] += 1
        if row.get("latency_ms") is not None and float(row["latency_ms"]) <= _base.MAX_PLAUSIBLE_LATENCY_MS:
            item["latencies_ms"].append(float(row["latency_ms"]))
        if int(row.get("retry_count") or 0) > 0:
            item["retry_turns"] += 1
        item["cost_usd"] += float(row.get("estimated_cost_usd") or 0.0)
    gateway_lines = list(gateway_lines)
    error_indexes, fallback_indexes = _event_indexes(gateway_lines)
    for idx, line in enumerate(gateway_lines):
        epoch = _line_epoch(line)
        if epoch is None:
            continue
        bucket = _base._bucket_start(epoch, bucket_seconds)  # type: ignore[attr-defined]
        buckets[bucket]["lines"].append(line)
        buckets[bucket]["selected_errors"] = buckets[bucket].get("selected_errors", 0) + (idx in error_indexes)
        buckets[bucket]["selected_fallbacks"] = buckets[bucket].get("selected_fallbacks", 0) + (idx in fallback_indexes)
    series = []
    for bucket in sorted(buckets):
        item = buckets[bucket]
        turn_count = item["turn_count"]
        counts = parse_journal_counts(item["lines"])
        counts = dataclasses.replace(counts, turn_error_events=item.get("selected_errors", 0), fallback_events=item.get("selected_fallbacks", 0))
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
        rows = _base._fetch_turn_rows(con, since)  # type: ignore[attr-defined]
    if gateway_lines is None:
        gateway_lines = _base.journalctl_lines("hermes-gateway.service", since)
    if watchdog_lines is None:
        watchdog_lines = _base.journalctl_lines("hermes-gateway-watchdog.service", since)
    counts = parse_journal_counts(gateway_lines, watchdog_lines)
    latencies = [
        float(row["latency_ms"]) for row in rows
        if row.get("latency_ms") is not None and float(row["latency_ms"]) <= _base.MAX_PLAUSIBLE_LATENCY_MS
    ]
    turn_count = len(rows)
    retry_turns = sum(1 for row in rows if int(row.get("retry_count") or 0) > 0)
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
        "event_evidence": event_evidence([*gateway_lines, *watchdog_lines]),
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
