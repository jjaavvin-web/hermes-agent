"""Codex sessions dashboard API — live read of the Codex parallel workflow.

Mount point: add to hermes_cli/web_server.py:

    from hermes_cli.dashboard_codex_sessions import router as codex_router
    app.include_router(codex_router)

All endpoints require ``X-Hermes-Session-Token`` validated by existing
middleware (same auth as ``dashboard_health.py``).

Routes (all under ``/api/dashboard/codex-sessions``):

- ``GET ./``                — snapshot of all tracked sessions + counts
- ``GET ./{sid}``           — detail (ISA verbatim, diff, review history)
- ``GET ./{sid}/log``       — agent.log tail filtered to this thread
- ``POST ./{sid}/pause``    — set ``paused`` flag (non-destructive)
- ``POST ./{sid}/resume``   — clear ``paused`` flag (non-destructive)
- ``POST ./{sid}/kill``     — release worktree + drop row.  Requires
                              ``{"confirm": "KILL_CODEX_SESSION"}``.
- ``POST ./{sid}/force-merge``  Requires ``{"confirm":
                              "FORCE_MERGE_CODEX_SESSION"}``.

Cache: 15s TTL on the snapshot endpoint, mirroring
``dashboard_health.py``'s ``_HIVES_TTL`` (same operator surface
shape).
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-codex-sessions"])

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
_SESSIONS_PATH = HERMES_HOME / "codex_sessions.json"
_REVIEW_STATE_PATH = HERMES_HOME / "codex-review-state.json"
_PORTS_PATH = HERMES_HOME / "codex-ports.json"
_AGENT_LOG_PATH = HERMES_HOME / "logs" / "agent.log"

_SNAPSHOT_TTL = 15.0
_SNAPSHOT_CACHE: tuple[dict, float] | None = None
_SNAPSHOT_LOCK = threading.Lock()

_KILL_TOKEN = "KILL_CODEX_SESSION"
_FORCE_MERGE_TOKEN = "FORCE_MERGE_CODEX_SESSION"
_LOG_TAIL_DEFAULT = 200
_DIFF_MAX_BYTES = 200 * 1024


# ── helpers ────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _safe_int(value: Optional[str]) -> int:
    """Parse git's count output; 0 on None / non-numeric."""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _build_snapshot() -> dict:
    """One snapshot of all tracked codex sessions + counts."""
    sessions_file = _load_json(_SESSIONS_PATH)
    rows = sessions_file.get("sessions", {})
    review_state = _load_json(_REVIEW_STATE_PATH).get("sessions", {})
    ports = _load_json(_PORTS_PATH)
    claimed_ports = sum(1 for v in ports.values() if v)

    sessions = []
    state_counts: dict[str, int] = {}
    for thread_id, row in rows.items():
        sid = row.get("session_id", "")
        review = review_state.get(sid, {})
        wt_path = row.get("worktree_path", "")
        wt_alive = Path(wt_path).is_dir() if wt_path else False
        sessions.append({
            "thread_id": thread_id,
            "session_id": sid,
            "state": row.get("state"),
            "paused": bool(row.get("paused")),
            "isa_id": row.get("isa_id"),
            "isa_phase": row.get("isa_phase"),
            "worktree_path": wt_path,
            "worktree_alive": wt_alive,
            "port": row.get("port"),
            "channel_id": row.get("channel_id"),
            "last_message_at": row.get("last_message_at"),
            "review_iterations": int(review.get("iterations", 0)),
            "reviews_today": int(review.get("reviews_today", 0)),
            "last_verdict": review.get("last_verdict"),
            "last_review_at": review.get("last_review_at"),
            "created_at": row.get("created_at"),
            # P3.5 PR meta surfaced for the SPA tab.
            "pr_number": row.get("pr_number"),
            "pr_url": row.get("pr_url"),
            "pr_state": row.get("pr_state"),
            "head_branch": row.get("head_branch"),
            "merge_label": row.get("merge_label"),
            "merge_requested_at": row.get("merge_requested_at"),
            "merged_at": row.get("merged_at"),
            "merge_commit_oid": row.get("merge_commit_oid"),
            "closed_at": row.get("closed_at"),
        })
        state = row.get("state") or "UNKNOWN"
        state_counts[state] = state_counts.get(state, 0) + 1

    return {
        "scanned_at": _now_iso(),
        "sessions": sessions,
        "counts": {
            "total": len(sessions),
            "by_state": state_counts,
            "ports_claimed": claimed_ports,
            "ports_free": 8 - claimed_ports,
        },
        "review_pool": {
            "size": 2,                # P2 default
            "daily_cap_per_sid": 10,  # P2 default
            "iteration_cap": 3,       # P2 default
        },
    }


def _cached_snapshot() -> dict:
    global _SNAPSHOT_CACHE
    now = time.monotonic()
    with _SNAPSHOT_LOCK:
        if _SNAPSHOT_CACHE is not None:
            value, expires_at = _SNAPSHOT_CACHE
            if now < expires_at:
                return value
        snap = _build_snapshot()
        _SNAPSHOT_CACHE = (snap, now + _SNAPSHOT_TTL)
        return snap


def _invalidate_snapshot() -> None:
    global _SNAPSHOT_CACHE
    with _SNAPSHOT_LOCK:
        _SNAPSHOT_CACHE = None


def _find_thread_for_sid(sid: str) -> tuple[Optional[str], Optional[dict]]:
    sessions_file = _load_json(_SESSIONS_PATH)
    for thread_id, row in sessions_file.get("sessions", {}).items():
        if row.get("session_id") == sid:
            return thread_id, row
    return None, None


def _persist_sessions(state: dict) -> None:
    _SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SESSIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_SESSIONS_PATH)
    _invalidate_snapshot()


def _collect_diff(worktree_path: str) -> tuple[str, bool]:
    """git diff origin/main...HEAD inside the worktree, truncated to 200KB."""
    if not worktree_path or not Path(worktree_path).is_dir():
        return "<worktree missing>", False
    try:
        result = subprocess.run(
            ["git", "-C", worktree_path, "diff", "origin/main...HEAD"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "<git diff timed out>", False
    out = result.stdout or ""
    truncated = False
    if len(out.encode("utf-8")) > _DIFF_MAX_BYTES:
        out = out.encode("utf-8")[:_DIFF_MAX_BYTES].decode("utf-8", errors="replace")
        truncated = True
    return out, truncated


# ── routes ─────────────────────────────────────────────────────────────


@router.get("/git-health", summary="Per-session git readiness + recommended next move")
def git_health() -> dict:
    """Per tracked codex session: uncommitted file count, reviewable diff size
    (3-dot vs fork/main), and a plain-English recommended next move. Powers the
    dashboard Git Health tab."""
    import os as _os
    import re as _re
    import subprocess as _sp
    from datetime import datetime as _dt, timezone as _tz

    state = _load_json(_SESSIONS_PATH)
    sessions = state.get("sessions", state) if isinstance(state, dict) else {}
    if not isinstance(sessions, dict):
        sessions = {}

    def _git(wt: str, *args: str):
        try:
            r = _sp.run(["git", "-C", wt, *args], capture_output=True, text=True, timeout=15)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    # Lifecycle the operator cares about: Work → Commit → Review → PR → Merge.
    # ``stage`` is the *current* step for a session; the SPA lights everything
    # before it as done and everything after as to-do.
    STAGE_ORDER = ["work", "commit", "review", "pr", "merge"]

    def _stage_for(uncommitted: int, files_changed: int, pr_number, merged: bool) -> str:
        if merged:
            return "merge"
        if pr_number:
            return "pr"
        if uncommitted and uncommitted > 0:
            return "commit"
        if files_changed and files_changed > 0:
            return "review"
        return "work"

    rows: list[dict] = []
    for _sid, s in sessions.items():
        if not isinstance(s, dict):
            continue
        slug = s.get("isa_slug") or "?"
        thread_id = s.get("thread_id")
        pr_number = s.get("pr_number")
        pr_url = s.get("pr_url")
        pr_state = s.get("pr_state")
        merged = bool(s.get("merged_at"))
        wt = s.get("worktree_path") or ""
        if not wt or not _os.path.isdir(wt):
            stage = _stage_for(0, 0, pr_number, merged)
            rows.append({"slug": slug, "thread_id": thread_id, "worktree": False,
                         "uncommitted": None, "files_changed": None,
                         "insertions": None, "deletions": None, "commits_ahead": None,
                         "diverged": False, "stage": stage,
                         "stage_index": STAGE_ORDER.index(stage),
                         "pr_number": pr_number, "pr_url": pr_url,
                         "pr_state": pr_state, "merged": merged,
                         "recommendation": "No worktree on disk — nothing to review",
                         "severity": "idle"})
            continue
        st = _git(wt, "status", "--porcelain")
        uncommitted = len([ln for ln in (st.splitlines() if st else []) if ln.strip()])
        shortstat = _git(wt, "diff", "--shortstat", "fork/main...HEAD") or ""
        m = _re.search(r"(\d+)\s+files?\s+changed", shortstat)
        files_changed = int(m.group(1)) if m else 0
        mi = _re.search(r"(\d+)\s+insertions?\(\+\)", shortstat)
        md = _re.search(r"(\d+)\s+deletions?\(-\)", shortstat)
        insertions = int(mi.group(1)) if mi else 0
        deletions = int(md.group(1)) if md else 0
        ahead_raw = _git(wt, "rev-list", "--count", "fork/main..HEAD")
        try:
            commits_ahead = int(ahead_raw) if ahead_raw is not None else None
        except ValueError:
            commits_ahead = None
        diverged = files_changed > 400
        if uncommitted > 0:
            rec = f"{uncommitted} uncommitted file(s) — the worker must commit before /review"
            sev = "warn"
        elif files_changed == 0:
            rec, sev = "No new work yet — nothing to review", "idle"
        elif diverged:
            rec = f"Base looks diverged ({files_changed} files) — re-base on fork/main, don't /review"
            sev = "bad"
        else:
            rec = f"Ready — run /review in this thread ({files_changed} file{'s' if files_changed != 1 else ''})"
            sev = "ready"
        stage = _stage_for(uncommitted, files_changed, pr_number, merged)
        rows.append({"slug": slug, "thread_id": thread_id, "worktree": True,
                     "uncommitted": uncommitted, "files_changed": files_changed,
                     "insertions": insertions, "deletions": deletions,
                     "commits_ahead": commits_ahead, "diverged": diverged,
                     "stage": stage, "stage_index": STAGE_ORDER.index(stage),
                     "pr_number": pr_number, "pr_url": pr_url,
                     "pr_state": pr_state, "merged": merged,
                     "recommendation": rec, "severity": sev})

    rank = {"ready": 0, "warn": 1, "bad": 2, "idle": 3}
    actionable = [r for r in rows if r["severity"] != "idle"]
    by_sev = {"ready": 0, "warn": 0, "bad": 0, "idle": 0}
    for r in rows:
        by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + 1
    if not rows:
        best = {"text": "No tracked codex sessions.", "severity": "idle",
                "slug": None, "thread_id": None}
    elif not actionable:
        best = {"text": "All clear — nothing is waiting on you.", "severity": "ready",
                "slug": None, "thread_id": None}
    else:
        top = sorted(actionable, key=lambda r: rank[r["severity"]])[0]
        best = {"text": f"{top['slug']}: {top['recommendation']}",
                "severity": top["severity"], "slug": top["slug"],
                "thread_id": top.get("thread_id")}
    summary = {
        "total": len(rows),
        "by_severity": by_sev,
        "ready": by_sev["ready"],
        "actionable": len(actionable),
        "total_uncommitted": sum(r["uncommitted"] or 0 for r in rows),
        "total_files_changed": sum(r["files_changed"] or 0 for r in rows),
    }
    return {"scanned_at": _dt.now(_tz.utc).isoformat(), "best_move": best,
            "summary": summary, "rows": rows}


@router.get("/git-graph", summary="Railroad graph: trunk + per-session diverging lanes")
def git_graph() -> dict:
    """A commit-graph (railroad) view of the repo. Returns the ``fork/main``
    trunk plus one *lane* per tracked unit of work — every codex-session
    worktree that exists on disk, **and** the local checkout where Claude works
    for you. Each lane reports its merge-base with the trunk, how far ahead /
    behind it is, a capped list of its lead commits, whether its base has
    diverged, and an owner tag (``claude`` | ``codex``). Powers the Git Health
    tab's visualization. Iterates ``codex_sessions.json`` (not ``git worktree
    list``) so throwaway/abandoned worktrees never appear."""
    import os as _os
    import re as _re
    import subprocess as _sp
    from datetime import datetime as _dt, timezone as _tz

    REPO = str(Path(__file__).resolve().parent.parent)
    AHEAD_CAP = 6           # lead commits surfaced per lane before "⋯"
    TRUNK_CAP = 6           # trunk commits surfaced
    DIVERGED_FILES = 400    # same threshold as git-health

    def _git(wt: str, *args: str):
        try:
            r = _sp.run(["git", "-C", wt, *args], capture_output=True, text=True, timeout=15)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    # Resolve the trunk ref once (prefer the user's fork, then upstream).
    base_ref = None
    for cand in ("fork/main", "origin/main", "main"):
        if _git(REPO, "rev-parse", "--verify", cand) is not None:
            base_ref = cand
            break
    if base_ref is None:
        return {"scanned_at": _dt.now(_tz.utc).isoformat(),
                "base": {"ref": None, "commits": []}, "lanes": [], "counts": {},
                "error": "no trunk ref (fork/main, origin/main, main) found"}

    def _commits(wt: str, rng: str, cap: int) -> list[dict]:
        out = _git(wt, "log", f"--max-count={cap}", "--pretty=%h\x1f%s\x1f%cI", rng)
        out_rows = []
        for ln in (out.splitlines() if out else []):
            parts = ln.split("\x1f")
            if len(parts) == 3:
                out_rows.append({"sha": parts[0], "subject": parts[1], "date": parts[2]})
        return out_rows

    trunk_commits = _commits(REPO, base_ref, TRUNK_CAP)
    base_sha = _git(REPO, "rev-parse", "--short", base_ref)
    # Is the fork itself behind upstream? (informational chip in the UI)
    upstream_behind = None
    if base_ref == "fork/main":
        for up in ("origin/main", "upstream/main"):
            if _git(REPO, "rev-parse", "--verify", up) is not None:
                upstream_behind = {"ref": up,
                                   "behind": _safe_int(_git(REPO, "rev-list", "--count", f"{base_ref}..{up}"))}
                break

    def _lane_for(wt: str, label: str, owner: str, meta: dict | None) -> dict | None:
        if not wt or not _os.path.isdir(wt):
            return None
        branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD") or "?"
        head = _git(wt, "rev-parse", "--short", "HEAD")
        mb = _git(wt, "merge-base", base_ref, "HEAD")
        mb_short = mb[:9] if mb else None
        ahead = _safe_int(_git(wt, "rev-list", "--count", f"{base_ref}..HEAD"))
        behind = _safe_int(_git(wt, "rev-list", "--count", f"HEAD..{base_ref}"))
        shortstat = _git(wt, "diff", "--shortstat", f"{base_ref}...HEAD") or ""
        m = _re.search(r"(\d+)\s+files?\s+changed", shortstat)
        files_changed = int(m.group(1)) if m else 0
        mi = _re.search(r"(\d+)\s+insertions?\(\+\)", shortstat)
        md = _re.search(r"(\d+)\s+deletions?\(-\)", shortstat)
        insertions = int(mi.group(1)) if mi else 0
        deletions = int(md.group(1)) if md else 0
        diverged = files_changed > DIVERGED_FILES or ahead > DIVERGED_FILES
        st = _git(wt, "status", "--porcelain")
        uncommitted = len([l for l in (st.splitlines() if st else []) if l.strip()])
        lead = _commits(wt, f"{base_ref}..HEAD", AHEAD_CAP)
        # Severity mirrors git-health so colors are consistent across the tab.
        if uncommitted > 0:
            severity = "warn"
        elif ahead == 0 and files_changed == 0:
            severity = "idle"
        elif diverged:
            severity = "bad"
        else:
            severity = "ready"
        meta = meta or {}
        sess_state = meta.get("state")
        # "active" drives the lit/pulsing node in the tree. A codex row may sit in
        # EXECUTING for hours while having produced zero git output, so state
        # alone is misleading — require the session to be live *and* to have
        # actual git movement (commits ahead or uncommitted edits). The local
        # Claude lane is active whenever it has work in flight.
        live_states = {"EXECUTING", "REVIEWING", "MERGING", "SCAFFOLD", "CLAIMED"}
        is_live = bool(sess_state and str(sess_state).upper() in live_states)
        has_git_movement = uncommitted > 0 or ahead > 0
        if owner == "claude":
            active = has_git_movement
        else:
            active = is_live and has_git_movement
        return {
            "id": label, "label": label, "owner": owner, "branch": branch,
            "head": head, "merge_base": mb_short,
            "on_trunk_tip": bool(mb_short and base_sha and mb_short.startswith(base_sha[:7])),
            "ahead": ahead, "behind": behind, "diverged": diverged,
            "uncommitted": uncommitted, "files_changed": files_changed,
            "insertions": insertions, "deletions": deletions,
            "lead_commits": lead, "lead_truncated": ahead > len(lead),
            "severity": severity, "thread_id": meta.get("thread_id"),
            "pr_number": meta.get("pr_number"), "pr_url": meta.get("pr_url"),
            "pr_state": meta.get("pr_state"), "merged": bool(meta.get("merged_at")),
            "state": sess_state, "active": active,
            "isa_phase": meta.get("isa_phase"),
            "last_activity": meta.get("last_message_at"),
        }

    lanes: list[dict] = []
    seen = {_os.path.realpath(REPO)}
    # Claude's lane: the local checkout (this is where Claude works for you).
    cur_branch = _git(REPO, "rev-parse", "--abbrev-ref", "HEAD") or "local"
    local = _lane_for(REPO, f"local · {cur_branch}", "claude", None)
    if local:
        lanes.append(local)
    # One lane per codex session worktree that actually exists on disk.
    state = _load_json(_SESSIONS_PATH)
    sess = state.get("sessions", state) if isinstance(state, dict) else {}
    if isinstance(sess, dict):
        for _key, s in sess.items():
            if not isinstance(s, dict):
                continue
            wt = s.get("worktree_path") or ""
            if not wt:
                continue
            real = _os.path.realpath(wt)
            if real in seen:
                continue
            seen.add(real)
            slug = s.get("isa_slug") or Path(real).name[:8]
            lane = _lane_for(wt, slug, "codex", s)
            if lane:
                lanes.append(lane)

    # Sort: attention first (bad → warn → ready → idle); within a tier the
    # more-diverged lane leads so runaway branches are obvious. Claude's lane
    # is pinned first within its tier so "your" work is easy to spot.
    sev_rank = {"bad": 0, "warn": 1, "ready": 2, "idle": 3}
    lanes.sort(key=lambda l: (sev_rank.get(l["severity"], 9),
                              0 if l["owner"] == "claude" else 1,
                              -l["ahead"]))

    return {
        "scanned_at": _dt.now(_tz.utc).isoformat(),
        "base": {"ref": base_ref, "sha": base_sha, "commits": trunk_commits,
                 "upstream_behind": upstream_behind},
        "lanes": lanes,
        "counts": {
            "lanes": len(lanes),
            "diverged": sum(1 for l in lanes if l["diverged"]),
            "ahead_total": sum(l["ahead"] for l in lanes),
            "max_ahead": max((l["ahead"] for l in lanes), default=0),
        },
    }


_RIVER_TTL = 30.0
_RIVER_CACHE: tuple[dict, float] | None = None
_RIVER_LOCK = threading.Lock()


def _git_out(repo: str, *args: str, timeout: float = 20.0):
    import subprocess as _sp
    try:
        r = _sp.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _build_river(trunk_n: int = 56, branch_n: int = 30) -> dict:
    """Real git-history 'river': the mainline as a time-ordered spine (oldest →
    newest) plus real branches that fork from it (and, where detectable, merge
    back). Powers the Git Health river visualization. Only real branch names and
    commit metadata are used — no fabricated labels."""
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _P
    import re as _re

    repo = str(_P(__file__).resolve().parent.parent)

    base_ref = None
    for cand in ("fork/main", "origin/main", "main"):
        if _git_out(repo, "rev-parse", "--verify", cand) is not None:
            base_ref = cand
            break
    if base_ref is None:
        return {"scanned_at": _dt.now(_tz.utc).isoformat(), "base": None,
                "trunk": [], "branches": [], "error": "no trunk ref found"}

    # ── trunk: newest `trunk_n` first-parent commits (newest first) ──
    # %H|%h|%ct|%an|%s  with merge PR number parsed from the subject when present
    raw = _git_out(repo, "log", "--first-parent", f"--max-count={trunk_n}",
                   "--pretty=%H%x1f%h%x1f%ct%x1f%an%x1f%s", base_ref) or ""
    trunk: list[dict] = []
    sha_to_trunkidx: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        full, short, ct, author, subj = parts
        m = _re.search(r"#(\d+)", subj)
        trunk.append({
            "sha": short, "full": full, "ts": int(ct) if ct.isdigit() else 0,
            "author": author, "subject": subj,
            "pr": int(m.group(1)) if m else None,
        })
        sha_to_trunkidx[full] = len(trunk) - 1
    # order index: 0 = newest (top), len-1 = oldest (bottom)
    for i, c in enumerate(trunk):
        c["age_rank"] = i  # 0 newest

    tip_ts = trunk[0]["ts"] if trunk else 0
    base_sha = trunk[0]["sha"] if trunk else None

    # ── branches: real local branches that are ahead of the trunk ──
    names_with_ts: list[tuple[str, int]] = []
    refs_raw = _git_out(
        repo,
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)\t%(committerdate:unix)",
        "refs/heads",
    ) or ""
    for line in refs_raw.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ref_name, ref_ts = parts
        ref_name = ref_name.strip()
        if not ref_name or ref_name in ("main", base_ref):
            continue
        names_with_ts.append((ref_name, int(ref_ts) if ref_ts.isdigit() else 0))
    # Only probe enough recent refs to fill the canopy; this endpoint feeds a
    # visual, and walking every local branch's history can take tens of seconds.
    names_with_ts = names_with_ts[:max(branch_n * 3, branch_n + 18)]
    # codex session worktrees → which are live, by branch name
    state = _load_json(_SESSIONS_PATH)
    sess = state.get("sessions", state) if isinstance(state, dict) else {}
    live_branch_meta: dict[str, dict] = {}
    if isinstance(sess, dict):
        for _k, s in sess.items():
            if not isinstance(s, dict):
                continue
            hb = s.get("head_branch")
            if hb:
                live_branch_meta[hb] = s

    cur_branch = (_git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()

    cand: list[dict] = []
    for name, lc_ts in names_with_ts:
        ahead_raw = _git_out(repo, "rev-list", "--count", f"{base_ref}..{name}")
        ahead = _safe_int(ahead_raw.strip() if ahead_raw else None)
        if ahead <= 0:
            continue
        mb = (_git_out(repo, "merge-base", base_ref, name) or "").strip()
        cand.append({
            "name": name, "ahead": ahead, "merge_base": mb,
            "ts": lc_ts,
        })

    # keep the most-recently-active branches (these render bright near the top)
    cand.sort(key=lambda b: b["ts"], reverse=True)
    cand = cand[:branch_n]

    branches: list[dict] = []
    for b in cand:
        mb_full = b["merge_base"]
        # where on the trunk does this branch fork? (age_rank of the merge-base)
        fork_rank = sha_to_trunkidx.get(mb_full)
        # a few lead commits along the branch (newest first), capped
        lead_raw = _git_out(repo, "log", "--max-count=6",
                            "--pretty=%h%x1f%ct%x1f%s", f"{base_ref}..{b['name']}") or ""
        lead = []
        for ln in lead_raw.splitlines():
            p = ln.split("\x1f")
            if len(p) == 3:
                lead.append({"sha": p[0], "ts": int(p[1]) if p[1].isdigit() else 0, "subject": p[2]})
        meta = live_branch_meta.get(b["name"], {})
        # recency 0..1 (1 = freshest) relative to the trunk tip
        age_days = max(0.0, (tip_ts - b["ts"]) / 86400.0) if tip_ts else 0.0
        recency = max(0.0, min(1.0, 1.0 - age_days / 21.0))  # 3-week falloff
        branches.append({
            "name": b["name"],
            "short": b["name"].split("/")[-1][:28],
            "ahead": b["ahead"],
            "fork_rank": fork_rank,        # None if fork point older than trunk window
            "lead_commits": lead,
            "ts": b["ts"],
            "recency": round(recency, 3),
            "active": bool(meta) or b["name"] == cur_branch,
            "is_current": b["name"] == cur_branch,
            "thread_id": meta.get("thread_id"),
            "pr_number": meta.get("pr_number"),
            "pr_url": meta.get("pr_url"),
            "merged": bool(meta.get("merged_at")),
        })

    return {
        "scanned_at": _dt.now(_tz.utc).isoformat(),
        "base": {"ref": base_ref, "sha": base_sha,
                 "total_commits": _safe_int((_git_out(repo, "rev-list", "--count", base_ref) or "0").strip())},
        "trunk": trunk,        # newest-first
        "branches": branches,  # most-recent-first
        "counts": {"trunk": len(trunk), "branches": len(branches),
                   "active": sum(1 for b in branches if b["active"])},
    }


@router.get("/git-river", summary="Real git-history river: time-ordered mainline + branches")
def git_river() -> dict:
    global _RIVER_CACHE
    now = time.monotonic()
    with _RIVER_LOCK:
        if _RIVER_CACHE is not None:
            value, exp = _RIVER_CACHE
            if now < exp:
                return value
        snap = _build_river()
        _RIVER_CACHE = (snap, now + _RIVER_TTL)
        return snap


@router.get("/codex-sessions", summary="Snapshot of all codex sessions")
def get_snapshot():
    return _cached_snapshot()


@router.get("/codex-sessions/{sid}", summary="Detail for one codex session")
def get_detail(sid: str):
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    isa_path = Path(row.get("isa_path", ""))
    try:
        isa_text = isa_path.read_text(encoding="utf-8") if isa_path.exists() else ""
    except OSError as exc:
        isa_text = f"<could not read ISA: {exc}>"
    diff, diff_truncated = _collect_diff(row.get("worktree_path", ""))
    review_state = _load_json(_REVIEW_STATE_PATH).get("sessions", {}).get(sid, {})
    return {
        "thread_id": thread_id,
        "session_id": sid,
        "row": row,
        "isa_verbatim": isa_text,
        "current_diff": diff,
        "diff_truncated": diff_truncated,
        "review_state": review_state,
    }


@router.get("/codex-sessions/{sid}/log", summary="Recent agent.log lines for this thread")
def get_log(sid: str, tail: int = _LOG_TAIL_DEFAULT):
    thread_id, _ = _find_thread_for_sid(sid)
    if thread_id is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    if not _AGENT_LOG_PATH.exists():
        return {"sid": sid, "lines": []}
    needle = f"chat={thread_id}"
    try:
        with open(_AGENT_LOG_PATH, "r", encoding="utf-8", errors="replace") as fd:
            matching = [line.rstrip("\n") for line in fd if needle in line]
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"log read failed: {exc}") from exc
    return {"sid": sid, "lines": matching[-tail:]}


@router.post("/codex-sessions/{sid}/pause", summary="Pause a session (non-destructive)")
def post_pause(sid: str):
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    sessions_file = _load_json(_SESSIONS_PATH)
    sessions_file["sessions"][thread_id]["paused"] = True
    _persist_sessions(sessions_file)
    return {"ok": True, "sid": sid, "paused": True}


@router.post("/codex-sessions/{sid}/resume", summary="Resume a paused session")
def post_resume(sid: str):
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    sessions_file = _load_json(_SESSIONS_PATH)
    sessions_file["sessions"][thread_id]["paused"] = False
    _persist_sessions(sessions_file)
    return {"ok": True, "sid": sid, "paused": False}


@router.post("/codex-sessions/{sid}/kill", summary="Release worktree + drop row (destructive)")
def post_kill(sid: str, body: dict = Body(default_factory=dict)):
    confirm = (body or {}).get("confirm", "")
    if confirm != _KILL_TOKEN:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "confirm token required",
                "expected": _KILL_TOKEN,
                "example": {"confirm": _KILL_TOKEN},
            },
        )
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")

    # Best-effort worktree removal — use git worktree remove --force so
    # untracked files don't block the cleanup.
    wt_path = row.get("worktree_path", "")
    if wt_path and Path(wt_path).exists():
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt_path],
                capture_output=True, text=True, check=False, timeout=30,
            )
        except subprocess.TimeoutExpired:
            log.warning("post_kill: worktree remove timed out for %s", sid)

    sessions_file = _load_json(_SESSIONS_PATH)
    sessions_file["sessions"].pop(thread_id, None)
    _persist_sessions(sessions_file)
    return {"ok": True, "sid": sid, "released_worktree": wt_path}


@router.post("/codex-sessions/{sid}/force-merge", summary="Force-merge a session (destructive)")
def post_force_merge(sid: str, body: dict = Body(default_factory=dict)):
    confirm = (body or {}).get("confirm", "")
    if confirm != _FORCE_MERGE_TOKEN:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "confirm token required",
                "expected": _FORCE_MERGE_TOKEN,
                "example": {"confirm": _FORCE_MERGE_TOKEN},
            },
        )
    thread_id, row = _find_thread_for_sid(sid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    # P4 emits an "intent" record; the actual merge runs out-of-band via
    # the same MergeBroker the dispatcher uses on APPROVE.  Putting the
    # merge in the request path would let a slow GitHub or rebase
    # conflict block the dashboard; instead the operator's intent is
    # logged + the row's state flips to MERGING for the dispatcher's
    # next tick to action.
    sessions_file = _load_json(_SESSIONS_PATH)
    sessions_file["sessions"][thread_id]["state"] = "MERGING"
    sessions_file["sessions"][thread_id]["force_merge_requested_at"] = _now_iso()
    _persist_sessions(sessions_file)
    return {
        "ok": True,
        "sid": sid,
        "state": "MERGING",
        "note": "dispatcher will pick up next tick",
    }
