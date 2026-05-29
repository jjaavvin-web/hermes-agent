#!/usr/bin/env python3
"""Silent-on-green health check for Hermes Codex parallel workflow.

Checks the Discord closed-loop Codex pipeline signals and appends every tick to
~/.hermes/health-checks/codex-parallel.jsonl. Stdout is intentionally empty on
all-green so Hermes cron does not spam Discord. On failure, stdout contains one
compact alert suitable for no_agent cron delivery.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

HOME = Path.home()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", HOME / ".hermes")).expanduser()
LIVE_SOURCE = Path(os.environ.get("HERMES_AGENT_SOURCE", HOME / ".local/share/hermes-agent"))
LIVE_VENV = LIVE_SOURCE / "venv"
JSONL_PATH = HERMES_HOME / "health-checks" / "codex-parallel.jsonl"
CODEX_SESSIONS = HERMES_HOME / "codex_sessions.json"
DEFAULT_EXPECTED_MINUTES = int(os.environ.get("CODEX_PARALLEL_EXPECTED_MINUTES", "120"))
STUCK_THRESHOLD_MINUTES = int(os.environ.get("CODEX_PARALLEL_STUCK_THRESHOLD_MINUTES", "15"))
DISCOVERY_MAX_AGE_MINUTES = int(os.environ.get("CODEX_PARALLEL_DISCOVERY_MAX_AGE_MINUTES", "40"))
CODEX_MODEL = os.environ.get("CODEX_PARALLEL_PROBE_MODEL", "gpt-5.5")
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_PARALLEL_PROBE_TIMEOUT_SECONDS", "120"))

OK = "ok"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def one_line(text: str, limit: int = 360) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def signal_gateway_active() -> tuple[str, str]:
    try:
        cp = run(["systemctl", "--user", "is-active", "hermes-gateway"], timeout=10)
    except Exception as exc:
        return f"systemctl check failed: {type(exc).__name__}: {exc}", "systemctl --user is-active hermes-gateway"
    status = (cp.stdout or cp.stderr or "").strip()
    if cp.returncode == 0 and status == "active":
        return OK, "systemctl --user is-active hermes-gateway -> active"
    return f"hermes-gateway not active: rc={cp.returncode} status={status!r}", "systemctl --user status hermes-gateway"


def journal_json(unit: str, since: str, timeout: int = 30) -> list[dict[str, Any]]:
    try:
        cp = run(["journalctl", "--user", "-u", unit, "--since", since, "--no-pager", "-o", "json"], timeout=timeout)
    except Exception:
        return []
    entries: list[dict[str, Any]] = []
    for line in (cp.stdout or "").splitlines():
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def entry_dt(entry: dict[str, Any]) -> datetime | None:
    raw = entry.get("__REALTIME_TIMESTAMP")
    if raw is not None:
        try:
            return datetime.fromtimestamp(int(raw) / 1_000_000, timezone.utc)
        except Exception:
            pass
    return None


def signal_discord_discovery() -> tuple[str, str]:
    entries = journal_json("hermes-gateway", "2 hours ago")
    candidates: list[tuple[datetime, str]] = []
    for entry in entries:
        msg = str(entry.get("MESSAGE") or "")
        low = msg.lower()
        if any(token in low for token in ("thread auto-discovery", "codex p1.4", "discover_threads", "codex_dispatcher.discover_threads")):
            dt = entry_dt(entry) or now_utc()
            candidates.append((dt, msg))
    if not candidates:
        return (
            f"no P1.4 thread auto-discovery/discover_threads journal line in last 2h; expected <{DISCOVERY_MAX_AGE_MINUTES}m",
            "journalctl --user -u hermes-gateway --since '2 hours ago' | grep -Ei 'thread auto-discovery|codex P1.4|discover_threads'",
        )
    dt, msg = max(candidates, key=lambda x: x[0])
    age_min = (now_utc() - dt).total_seconds() / 60
    low = msg.lower()
    if any(bad in low for bad in (" failed", " error", "exception", "allocation failed", "per-thread error")):
        return f"latest P1.4/discover_threads line is failure: {one_line(msg)}", one_line(msg)
    if age_min > DISCOVERY_MAX_AGE_MINUTES:
        return (
            f"latest P1.4/discover_threads OK line is stale: {age_min:.1f}m old > {DISCOVERY_MAX_AGE_MINUTES}m; line: {one_line(msg)}",
            one_line(msg),
        )
    return OK, one_line(msg)


def signal_codex_probe() -> tuple[str, str]:
    # Import from the live Hermes source so this probes the deployed provider path,
    # not the current worker checkout.
    if str(LIVE_SOURCE) not in sys.path:
        sys.path.insert(0, str(LIVE_SOURCE))
    try:
        from agent.auxiliary_client import resolve_provider_client  # type: ignore
    except Exception as exc:
        return f"could not import live Hermes Codex provider: {type(exc).__name__}: {exc}", str(LIVE_SOURCE)

    last_error = ""
    for attempt in range(1, 4):
        started = time.monotonic()
        client = None
        try:
            client, model = resolve_provider_client(
                provider="openai-codex",
                model=CODEX_MODEL,
                raw_codex=True,
            )
            if client is None:
                return "openai-codex provider unavailable: no OAuth client/token resolved", "~/.hermes/auth.json presence only; run hermes auth status openai-codex"
            resp = client.responses.create(
                model=model or CODEX_MODEL,
                instructions="Reply with exactly: ok",
                input="reply with 'ok'",
                store=False,
                timeout=CODEX_TIMEOUT_SECONDS,
            )
            output = getattr(resp, "output", None)
            if output is None:
                raise RuntimeError("response.output is null")
            if isinstance(output, list) and len(output) == 0:
                raise RuntimeError("response.output is empty")
            text = (getattr(resp, "output_text", None) or "").strip()
            if not text:
                # Fallback extraction for SDK/model variants.
                parts: list[str] = []
                for item in output or []:
                    content = getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else None) or []
                    for part in content:
                        ptype = getattr(part, "type", None) or (part.get("type") if isinstance(part, dict) else None)
                        if ptype in {"output_text", "text"}:
                            parts.append(getattr(part, "text", "") or (part.get("text") if isinstance(part, dict) else "") or "")
                text = "".join(parts).strip()
            if not text:
                raise RuntimeError("response text is empty")
            elapsed = time.monotonic() - started
            if "ok" not in text.lower():
                raise RuntimeError(f"unexpected response text: {one_line(text, 80)!r}")
            return OK, f"openai-codex {model or CODEX_MODEL} probe ok in {elapsed:.1f}s"
        except Exception as exc:
            last_error = f"attempt {attempt}/3 {type(exc).__name__}: {one_line(exc)}"
            if attempt < 3:
                time.sleep(min(2 * attempt, 5))
        finally:
            try:
                if client is not None and hasattr(client, "close"):
                    client.close()
            except Exception:
                pass
    return f"Codex probe exhausted 3 attempts: {last_error}", "provider=openai-codex model=%s prompt=reply-with-ok" % CODEX_MODEL


def codex_session_stuck_rows(threshold: timedelta) -> list[str]:
    if not CODEX_SESSIONS.exists():
        return []
    try:
        data = json.loads(CODEX_SESSIONS.read_text())
    except Exception as exc:
        return [f"codex_sessions.json unreadable: {type(exc).__name__}: {exc}"]
    now = now_utc()
    stuck: list[str] = []
    active_states = {"CLAIMED", "EXECUTING", "REVIEWING", "MERGING"}
    for thread_id, row in (data.get("sessions") or {}).items():
        state = str(row.get("state") or "").upper()
        if state not in active_states:
            continue
        basis = parse_dt(row.get("last_message_at")) or parse_dt(row.get("created_at"))
        if basis is None:
            continue
        age = now - basis
        if age > threshold:
            slug = row.get("isa_slug") or row.get("isa_id") or "unknown"
            stuck.append(f"{slug}:{thread_id}:{state}:{age.total_seconds()/3600:.1f}h")
    return stuck


def iter_kanban_dbs() -> list[Path]:
    dbs: list[Path] = []
    root_db = HERMES_HOME / "kanban.db"
    if root_db.exists():
        dbs.append(root_db)
    boards = HERMES_HOME / "kanban/boards"
    if boards.exists():
        dbs.extend(sorted(p for p in boards.glob("*/kanban.db") if p.is_file()))
    return dbs


def kanban_stuck_rows(threshold: timedelta) -> list[str]:
    now = now_utc()
    stuck: list[str] = []
    for db in iter_kanban_dbs():
        board = db.parent.name if db.parent.name != ".hermes" else "default"
        try:
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
            task_cols = {r[1] for r in con.execute("pragma table_info(tasks)")}
            if {"id", "status"}.issubset(task_cols):
                for row in con.execute("select id,title,status,started_at,created_at from tasks where lower(status) in ('executing','running')"):
                    basis = parse_dt(row["started_at"]) or parse_dt(row["created_at"])
                    if basis and now - basis > threshold:
                        stuck.append(f"{board}/{row['id']}:{row['status']}:{(now-basis).total_seconds()/3600:.1f}h")
            run_cols = {r[1] for r in con.execute("pragma table_info(task_runs)")}
            if {"id", "task_id", "status"}.issubset(run_cols):
                for row in con.execute("select id,task_id,status,started_at,last_heartbeat_at from task_runs where lower(status) in ('executing','running')"):
                    basis = parse_dt(row["last_heartbeat_at"]) or parse_dt(row["started_at"])
                    if basis and now - basis > threshold:
                        stuck.append(f"{board}/{row['task_id']}/run{row['id']}:{row['status']}:{(now-basis).total_seconds()/3600:.1f}h")
            con.close()
        except Exception as exc:
            stuck.append(f"{board}:kanban-db-error:{type(exc).__name__}:{one_line(exc, 80)}")
    return stuck


def signal_stuck_sessions() -> tuple[str, str]:
    threshold = timedelta(minutes=STUCK_THRESHOLD_MINUTES)
    stuck = codex_session_stuck_rows(threshold) + kanban_stuck_rows(threshold)
    if not stuck:
        return OK, f"no codex/kanban executing rows older than {STUCK_THRESHOLD_MINUTES}m"
    shown = "; ".join(stuck[:12])
    extra = f" (+{len(stuck)-12} more)" if len(stuck) > 12 else ""
    return f"{len(stuck)} stuck row(s) >{STUCK_THRESHOLD_MINUTES}m: {shown}{extra}", f"{CODEX_SESSIONS}; ~/.hermes/kanban/boards/*/kanban.db"


def signal_sandbox_refused() -> tuple[str, str]:
    cp = run(["journalctl", "--user", "-u", "hermes-gateway", "--since", "1 hour ago", "--no-pager", "-o", "short-iso"], timeout=30)
    lines = []
    for line in (cp.stdout or "").splitlines():
        low = line.lower()
        if "sandbox" in low and "refused" in low:
            lines.append(line)
    if not lines:
        return OK, "no sandbox.*refused lines in hermes-gateway journal last hour"
    return f"sandbox guard violation(s) in last hour: {one_line(lines[-1])}", one_line(lines[-1])


def main() -> int:
    checks = [
        ("1", "gateway", signal_gateway_active),
        ("2", "discord_p1_4", signal_discord_discovery),
        ("3", "codex_probe", signal_codex_probe),
        ("4", "stuck_sessions", signal_stuck_sessions),
        ("5", "sandbox_guard", signal_sandbox_refused),
    ]
    signals: dict[str, str] = {}
    evidence: dict[str, str] = {}
    failures: list[tuple[str, str, str, str]] = []
    for sid, name, fn in checks:
        try:
            result, ev = fn()
        except Exception as exc:
            result, ev = f"check crashed: {type(exc).__name__}: {one_line(exc)}", "script exception"
        signals[sid] = OK if result == OK else result
        evidence[sid] = ev
        if result != OK:
            failures.append((sid, name, result, ev))
    overall = OK if not failures else "fail"
    record = {"ts": iso_now(), "signals": signals, "overall": overall, "evidence": evidence}
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    if failures:
        print(f"⚠️ Codex parallel health check FAIL — {record['ts']}")
        for sid, name, reason, ev in failures:
            print(f"S{sid} {name}: {one_line(reason)} | evidence: {one_line(ev)}")
        print(f"log: {JSONL_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
