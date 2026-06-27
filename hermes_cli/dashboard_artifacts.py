"""HTML Artifacts Gallery — list and serve Hermes-generated HTML files.

GET /api/dashboard/artifacts
    Returns a JSON listing of report and session-replay HTML files under
    ~/.hermes, sorted newest-first.  Replays are capped to the 500 most
    recent to avoid a 4,745-item firehose.

GET /api/dashboard/artifacts/raw?id=<opaque>
    Serve a single HTML file.  The id is a url-safe base64 encoding of
    the path relative to ~/.hermes; the handler validates that the resolved
    path stays inside HERMES_HOME and is a .html file (path-traversal guard).

Auth: web_server.py auth_middleware gates everything under /api/ automatically.
      fetchJSON on the frontend injects the session-token header.
      These are plain GETs, NOT SSE — no _QUERY_TOKEN_PATHS entry needed.
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-artifacts"])

HERMES_HOME = Path.home() / ".hermes"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_PEEK_BYTES = 4096


def _read_title(path: Path) -> str | None:
    """Read the first 4KB of an HTML file and extract the <title> text."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(_PEEK_BYTES)
        text = raw.decode("utf-8", errors="replace")
        m = _TITLE_RE.search(text)
        if m:
            title = m.group(1).strip()
            if title:
                return title
    except Exception:
        pass
    return None


def _encode_id(rel: str) -> str:
    """Encode a relative path to a url-safe opaque id (no padding)."""
    return base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")


def _decode_id(opaque: str) -> str:
    """Decode an opaque id back to a relative path string."""
    # Re-add base64 padding.
    padding = (4 - len(opaque) % 4) % 4
    padded = opaque + "=" * padding
    return base64.urlsafe_b64decode(padded).decode()


def _safe_resolve(rel: str) -> Path:
    """Resolve rel relative to HERMES_HOME and validate it stays inside.

    Raises HTTPException(404) on any path-traversal or bad-extension attempt.
    The three checks are:
      1. Resolved path is inside HERMES_HOME (is_relative_to).
      2. File has a .html extension.
      3. File exists and is a regular file.
    """
    resolved_home = HERMES_HOME.resolve()
    try:
        candidate = (HERMES_HOME / rel).resolve()
    except Exception:
        raise HTTPException(status_code=404, detail="not found")

    if not candidate.is_relative_to(resolved_home):
        raise HTTPException(status_code=404, detail="not found")
    if candidate.suffix.lower() != ".html":
        raise HTTPException(status_code=404, detail="not found")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return candidate


def _collect_reports() -> list[dict[str, Any]]:
    """Glob audits/**/*.html — these are deliverable reports / mockups."""
    audits_root = HERMES_HOME / "audits"
    items: list[dict[str, Any]] = []
    if not audits_root.exists():
        return items
    try:
        for path in audits_root.rglob("*.html"):
            if not path.is_file():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(HERMES_HOME))
            # group = first directory component under audits/
            parts = path.relative_to(audits_root).parts
            group = parts[0] if len(parts) > 1 else "audits"
            raw_title = _read_title(path)
            title = raw_title or path.name
            items.append({
                "id": _encode_id(rel),
                "title": title,
                "kind": "report",
                "group": group,
                "mtime": st.st_mtime,
                "size": st.st_size,
            })
    except Exception:
        pass
    return items


_REPLAY_CAP = 500


def _collect_replays(cap: int = _REPLAY_CAP) -> tuple[list[dict[str, Any]], int]:
    """Glob sessions/artifacts/*/*.html — session replay files.

    Returns (items_list, total_found_before_cap).
    """
    replays_root = HERMES_HOME / "sessions" / "artifacts"
    all_items: list[dict[str, Any]] = []
    if not replays_root.exists():
        return [], 0
    try:
        for path in replays_root.rglob("*.html"):
            if not path.is_file():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            rel = str(path.relative_to(HERMES_HOME))
            # Use parent uuid short-form as the title since all replays share
            # "Replay — hermes-agent" as their <title>.
            parent_uuid = path.parent.name
            short_id = parent_uuid[:8] if len(parent_uuid) >= 8 else parent_uuid
            # Format mtime as a readable label for the title.
            mtime = st.st_mtime
            dt_str = _fmt_mtime(mtime)
            title = f"Replay {dt_str} ({short_id})"
            all_items.append({
                "id": _encode_id(rel),
                "title": title,
                "kind": "replay",
                "group": "Session replays",
                "mtime": mtime,
                "size": st.st_size,
            })
    except Exception:
        pass

    total = len(all_items)
    # Sort by mtime descending so the cap keeps the newest.
    all_items.sort(key=lambda x: x["mtime"], reverse=True)
    return all_items[:cap], total


def _fmt_mtime(mtime: float) -> str:
    """Format epoch float as 'YYYY-MM-DD HH:MM' UTC for display."""
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(int(mtime))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/artifacts")
def list_artifacts() -> dict[str, Any]:
    """Return a listing of all Hermes-generated HTML files.

    Never 500s: any exception is caught and returned as an error field.
    """
    try:
        reports = _collect_reports()
        replays, replay_total = _collect_replays()

        # Sort each group newest-first independently, then merge.
        reports.sort(key=lambda x: x["mtime"], reverse=True)
        # replays already sorted newest-first from _collect_replays.

        items = reports + replays
        replays_truncated = max(0, replay_total - len(replays))

        counts = {
            "reports": len(reports),
            "replays": len(replays),
            "total": len(items),
        }

        return {
            "items": items,
            "counts": counts,
            "replays_truncated": replays_truncated,
            "replay_total": replay_total,
        }
    except Exception as exc:
        return {"items": [], "counts": {}, "error": str(exc), "replays_truncated": 0}


@router.get("/artifacts/raw")
def serve_artifact(id: str = Query(..., description="Opaque artifact id")) -> FileResponse:
    """Serve a single HTML artifact by its opaque id.

    Security: decodes the id to a relative path, resolves it, and rejects
    (404) if the result is outside HERMES_HOME, is not .html, or does not
    exist.  Absolute path is NEVER exposed to the client.
    """
    try:
        rel = _decode_id(id)
    except Exception:
        raise HTTPException(status_code=404, detail="not found")

    path = _safe_resolve(rel)
    response = FileResponse(path, media_type="text/html")
    # Defense-in-depth: artifacts are agent-written HTML. The dashboard UI renders
    # them via a sandboxed srcdoc iframe, but the raw endpoint can also be loaded
    # as a same-origin document. The `sandbox` CSP directive neuters any embedded
    # script so it cannot read the session token from the URL or call auth-gated
    # APIs. Inline styles + data/blob images still render so reports display normally.
    response.headers["Content-Security-Policy"] = (
        "sandbox; default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'"
    )
    return response
