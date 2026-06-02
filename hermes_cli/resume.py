"""Live project resume command backed by Honcho session state.

``hermes resume`` answers "where did we leave off?" from the latest Honcho
session uploaded for the current project directory.  Honcho sessions are not
project-aware, so selection is anchored by the Claude/Honcho bridge log
(``upload_complete`` rows carrying cwd + honcho_session) instead of Honcho's
session list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

DEFAULT_HONCHO_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_WORKSPACE = "hermes"
DEFAULT_BRIDGE_LOG = get_hermes_home() / "state" / "claude-honcho-bridge.log"
HONCHO_UNREACHABLE = "Honcho unreachable - cannot read live state"


class ResumeError(RuntimeError):
    """User-facing resume command failure."""


class HonchoUnreachableError(ResumeError):
    """Honcho could not be contacted or returned an unhealthy response."""


@dataclass(frozen=True)
class BridgeSelection:
    """Latest project-specific upload_complete record from the bridge log."""

    session_id: str
    ai_peer: str
    cwd: str
    ts: str
    messages_sent: int


@dataclass
class ResumeState:
    """Rendered live state payload."""

    cwd: str
    workspace: str
    session_id: str
    ai_peer: str
    bridge_ts: str
    messages_sent: int
    session_created_at: str | None
    source: str
    freshness: str
    short_summary: str | None
    long_summary: str | None = None
    recent_messages: list[dict[str, str]] | None = None


class HonchoHTTP:
    """Small stdlib JSON HTTP client for Honcho v3 read endpoints."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request_json(self, method: str, path: str, *, body: object | None = None) -> Any:
        data: bytes | None = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # HTTPError subclasses URLError but exposes response body/status.
            detail = exc.read().decode("utf-8", "replace")[:500]
            if path == "/health":
                raise HonchoUnreachableError(f"health HTTP {exc.code}: {detail}") from exc
            raise ResumeError(f"Honcho request failed ({exc.code}) for {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HonchoUnreachableError(str(exc)) from exc

        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ResumeError(f"Honcho returned invalid JSON for {path}: {exc}") from exc


def _parse_ts(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_cwd(cwd: str | Path) -> str:
    try:
        return str(Path(cwd).expanduser().resolve())
    except OSError:
        return str(Path(cwd).expanduser().absolute())


def select_latest_session_from_bridge_log(
    *,
    log_path: str | Path = DEFAULT_BRIDGE_LOG,
    cwd: str | Path | None = None,
) -> BridgeSelection:
    """Select the newest non-empty Honcho upload for ``cwd`` from bridge log.

    Honcho's own session list is project-blind, so this function intentionally
    ignores it.  Only JSONL rows with ``msg == upload_complete``, matching cwd,
    a honcho_session id, and ``messages_sent > 0`` are eligible.
    """

    target_cwd = _normalize_cwd(cwd or os.getcwd())
    path = Path(log_path).expanduser()
    if not path.exists():
        raise ResumeError(f"Bridge log not found: {path}")

    best: dict[str, Any] | None = None
    best_ts = datetime.min.replace(tzinfo=timezone.utc)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ResumeError(f"Could not read bridge log {path}: {exc}") from exc

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("msg") != "upload_complete":
            continue
        if _normalize_cwd(row.get("cwd") or "") != target_cwd:
            continue
        try:
            messages_sent = int(row.get("messages_sent") or 0)
        except (TypeError, ValueError):
            messages_sent = 0
        if messages_sent <= 0:
            continue
        if not row.get("honcho_session"):
            continue
        row_ts = _parse_ts(str(row.get("ts") or ""))
        if best is None or row_ts > best_ts:
            best = row
            best_ts = row_ts

    if best is None:
        raise ResumeError(
            f"No Honcho upload_complete entry found for cwd {target_cwd} "
            f"with messages_sent > 0 in {path}"
        )

    return BridgeSelection(
        session_id=str(best["honcho_session"]),
        ai_peer=str(best.get("ai_peer") or ""),
        cwd=target_cwd,
        ts=str(best.get("ts") or ""),
        messages_sent=int(best.get("messages_sent") or 0),
    )


def _content(value: Any) -> str | None:
    if isinstance(value, dict):
        content = value.get("content")
    else:
        content = getattr(value, "content", None)
    if content is None:
        return None
    text = str(content).strip()
    return text or None


def _created_at_from_summary(value: Any) -> str | None:
    if isinstance(value, dict):
        created_at = value.get("created_at")
    else:
        created_at = getattr(value, "created_at", None)
    return str(created_at) if created_at else None


def _latest_message_created_at(context: dict[str, Any]) -> str | None:
    messages = context.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    stamped = [str(m.get("created_at")) for m in messages if isinstance(m, dict) and m.get("created_at")]
    if not stamped:
        return None
    return max(stamped, key=lambda s: _parse_ts(s))


def _session_created_at(summaries: dict[str, Any], context: dict[str, Any] | None = None) -> str | None:
    candidates = [
        summaries.get("created_at"),
        _created_at_from_summary(summaries.get("short_summary")),
        _created_at_from_summary(summaries.get("long_summary")),
    ]
    if context:
        messages = context.get("messages")
        if isinstance(messages, list):
            candidates.extend(
                m.get("created_at")
                for m in messages
                if isinstance(m, dict) and m.get("created_at")
            )
    parsed = [str(c) for c in candidates if c]
    if not parsed:
        return None
    return min(parsed, key=lambda s: _parse_ts(s))


def _clip(text: str, limit: int = 500) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _recent_messages(context: dict[str, Any], limit: int = 10) -> list[dict[str, str]]:
    messages = context.get("messages")
    if not isinstance(messages, list):
        return []
    sortable: list[dict[str, Any]] = [m for m in messages if isinstance(m, dict)]
    sortable.sort(key=lambda m: _parse_ts(str(m.get("created_at") or "")))
    recent = sortable[-limit:]
    rendered = []
    for message in recent:
        content = _clip(str(message.get("content") or ""), 500)
        if not content:
            continue
        rendered.append(
            {
                "created_at": str(message.get("created_at") or ""),
                "peer_id": str(message.get("peer_id") or "unknown"),
                "content": content,
            }
        )
    return rendered


def _session_path(workspace: str, session_id: str, suffix: str) -> str:
    encoded_session = urllib.parse.quote(session_id, safe="")
    encoded_workspace = urllib.parse.quote(workspace, safe="")
    return f"/v3/workspaces/{encoded_workspace}/sessions/{encoded_session}/{suffix}"


def read_resume_state(
    *,
    cwd: str | Path | None = None,
    log_path: str | Path | None = None,
    base_url: str | None = None,
    workspace: str = DEFAULT_WORKSPACE,
    full: bool = False,
    http: HonchoHTTP | None = None,
) -> ResumeState:
    """Read the latest project state from Honcho without mutating state."""

    selected = select_latest_session_from_bridge_log(log_path=log_path or DEFAULT_BRIDGE_LOG, cwd=cwd)
    client = http or HonchoHTTP(base_url or os.environ.get("HONCHO_BASE_URL") or DEFAULT_HONCHO_BASE_URL)

    # Health is a hard precondition.  Do not fall back to local memory/scratch.
    health = client.request_json("GET", "/health")
    if isinstance(health, dict) and health.get("status") not in {"ok", "healthy"}:
        raise HonchoUnreachableError(f"health returned {health!r}")

    summaries = client.request_json(
        "GET",
        _session_path(workspace, selected.session_id, "summaries"),
    )
    if not isinstance(summaries, dict):
        summaries = {}

    short_summary = _content(summaries.get("short_summary"))
    long_summary = _content(summaries.get("long_summary"))
    context_data = client.request_json(
        "GET",
        _session_path(workspace, selected.session_id, "context"),
    )
    context: dict[str, Any] = context_data if isinstance(context_data, dict) else {}

    if short_summary:
        created_at = _session_created_at({}, context) or _session_created_at(summaries)
        return ResumeState(
            cwd=selected.cwd,
            workspace=workspace,
            session_id=selected.session_id,
            ai_peer=selected.ai_peer,
            bridge_ts=selected.ts,
            messages_sent=selected.messages_sent,
            session_created_at=created_at,
            source="summary",
            freshness="summary",
            short_summary=short_summary,
            long_summary=long_summary if full else None,
            recent_messages=None,
        )

    # Deriver lag: visible fallback to raw latest session messages, never blank.
    recent = _recent_messages(context)
    if not recent:
        raise ResumeError(
            f"Selected Honcho session {selected.session_id} has no summary and no raw messages to display"
        )
    return ResumeState(
        cwd=selected.cwd,
        workspace=workspace,
        session_id=selected.session_id,
        ai_peer=selected.ai_peer,
        bridge_ts=selected.ts,
        messages_sent=selected.messages_sent,
        session_created_at=_session_created_at(summaries, context) or _latest_message_created_at(context),
        source="raw_messages",
        freshness="summaries not ready; showing raw latest session messages",
        short_summary=None,
        long_summary=None,
        recent_messages=recent,
    )


def render_text(state: ResumeState, *, full: bool = False) -> str:
    """Render a concise human-readable resume summary."""

    lines = [
        f"Project: {state.cwd}",
        f"Session: {state.session_id}",
        f"Created: {state.session_created_at or 'unknown'}",
        f"Bridge upload: {state.bridge_ts} ({state.messages_sent} messages)",
    ]
    if state.ai_peer:
        lines.append(f"AI peer: {state.ai_peer}")

    if state.source == "summary" and state.short_summary:
        lines.append("")
        lines.append(state.short_summary)
        if full and state.long_summary:
            lines.append("")
            lines.append("Long summary:")
            lines.append(state.long_summary)
        return "\n".join(lines)

    lines.append(f"Freshness: {state.freshness}")
    lines.append("")
    lines.append("Recent messages:")
    for message in state.recent_messages or []:
        stamp = message.get("created_at") or "unknown-time"
        peer = message.get("peer_id") or "unknown"
        content = message.get("content") or ""
        lines.append(f"- [{stamp}] {peer}: {content}")
    return "\n".join(lines)


def render_json(state: ResumeState, *, full: bool = False) -> str:
    payload = asdict(state)
    if not full:
        payload.pop("long_summary", None)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def run(args: argparse.Namespace) -> int:
    """Execute the resume command and return an exit code."""

    try:
        state = read_resume_state(
            cwd=getattr(args, "cwd", None) or os.getcwd(),
            base_url=(
                getattr(args, "honcho_base_url", None)
                or os.environ.get("HONCHO_BASE_URL")
                or DEFAULT_HONCHO_BASE_URL
            ),
            full=bool(getattr(args, "full", False)),
        )
    except (HonchoUnreachableError, urllib.error.URLError, TimeoutError, OSError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": HONCHO_UNREACHABLE}), file=sys.stderr)
        else:
            print(f"{HONCHO_UNREACHABLE}: {exc}", file=sys.stderr)
        return 2
    except ResumeError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(render_json(state, full=bool(getattr(args, "full", False))))
    else:
        print(render_text(state, full=bool(getattr(args, "full", False))))
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``hermes resume`` on the top-level argparse parser."""

    parser = subparsers.add_parser(
        "resume",
        help="Print the latest live project state from Honcho",
        description=(
            "Read the latest Honcho session uploaded for this project cwd via "
            "~/.hermes/state/claude-honcho-bridge.log and print a concise "
            "where-did-we-leave-off summary. Fails loudly if Honcho is unreachable."
        ),
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Project directory to select from the bridge log (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include Honcho long_summary when available",
    )
    parser.add_argument(
        "--honcho-base-url",
        default=None,
        help="Honcho base URL (default: HONCHO_BASE_URL or http://127.0.0.1:8000)",
    )

    def _cmd(args: argparse.Namespace) -> None:
        raise SystemExit(run(args))

    parser.set_defaults(func=_cmd)
    return parser
