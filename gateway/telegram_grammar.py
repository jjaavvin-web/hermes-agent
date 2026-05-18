"""Telegram `/task` command grammar parser (Phase 1).

Pure-function module: no side effects, no I/O, no `gateway.*` imports.
Implements the Phase-1 grammar from TELEGRAM-COMMAND-GRAMMAR-V2.md:

    /task [heavy:|light:] [#p0|#p1|#p2|#p3] DESCRIPTION

`LANE` before `PRIORITY` is canonical but either order is accepted.
Unknown `#tags` are treated as labels (not priority). `+dep:...`,
`@deadline:...`, `--attach`, `--dry-run` are recognised as Phase-3+
tokens and produce a NOT-IMPLEMENTED-YET error rather than a generic
syntax error. Voice-input tolerance: when the first token after
`/task` is the bare word `heavy` or `light` (no trailing colon) and
the next token is not also a lane word, treat it as the lane prefix.
"""

from __future__ import annotations

import re
from typing import Optional

# Lane → assignee mapping. Heavy lane currently routes to
# claude-code-coder rather than claude-code-queen (the queen profile is
# reserved for orchestration). Anthropic profile dispatch (claude-code-*)
# is known broken via the OAuth bug Joseph is tracking — that's expected;
# this mapping documents the intended target so routing is correct the
# moment OAuth is fixed.
LANE_HEAVY = "heavy"
LANE_LIGHT = "light"
DEFAULT_LANE = LANE_LIGHT

LANE_ASSIGNEE = {
    LANE_HEAVY: "claude-code-coder",
    LANE_LIGHT: "h2coder",
}

# Priority bucket → kanban_db priority int. Higher numbers sort first
# (kanban_db.py ORDER BY priority DESC). Buckets leave room for future
# fine-grained ordering inside a bucket without colliding.
PRIORITY_BUCKETS = {
    "p0": 400,
    "p1": 300,
    "p2": 200,
    "p3": 100,
}
PRIORITY_ALIASES = {
    "urgent": "p0",
    "low": "p3",
}
DEFAULT_PRIORITY_LABEL = "p2"
DEFAULT_PRIORITY = PRIORITY_BUCKETS[DEFAULT_PRIORITY_LABEL]

# Phase-3+ token prefixes that must produce a NOT-IMPLEMENTED-YET error
# rather than being silently swallowed into the description.
PHASE3_TOKEN_PREFIXES = ("+dep:", "@deadline:", "--attach", "--dry-run", "--profile-pin")

# Minimum description length (spec §Defaults). Anything shorter is
# treated as a syntax error rather than creating an empty-titled card.
MIN_DESCRIPTION_LEN = 3

# Recognised lane keywords (case-insensitive). Used by both colon-form
# (`heavy:`) and voice-input bare-word form (`heavy`).
_LANE_WORDS = (LANE_HEAVY, LANE_LIGHT)

# `#word` token regex. `#fakepriority` becomes a label; `#p0` becomes
# the priority. Underscores and dashes inside labels are allowed; we
# stop at whitespace and the next `#`.
_HASHTAG_RE = re.compile(r"^#([A-Za-z][A-Za-z0-9_\-]*)$")


def _strip_command_prefix(text: str) -> str:
    """Strip a leading ``/task`` (and bot suffix) from ``text``."""
    stripped = text.strip()
    if not stripped:
        return ""
    # Accept "/task", "/task@botname", "task" (no slash) for tolerance.
    head, _, rest = stripped.partition(" ")
    head_lower = head.lower()
    if head_lower.startswith("/"):
        head_lower = head_lower[1:]
    # Drop "@botname" suffix on the command.
    head_lower = head_lower.split("@", 1)[0]
    if head_lower == "task":
        return rest.strip()
    return stripped


def _is_priority_token(tok: str) -> Optional[str]:
    """Return canonical priority label (``p0``..``p3``) or None."""
    m = _HASHTAG_RE.match(tok)
    if not m:
        return None
    word = m.group(1).lower()
    if word in PRIORITY_BUCKETS:
        return word
    if word in PRIORITY_ALIASES:
        return PRIORITY_ALIASES[word]
    return None


def _is_label_token(tok: str) -> Optional[str]:
    """Return the non-priority label slug from a ``#tag`` token or None."""
    m = _HASHTAG_RE.match(tok)
    if not m:
        return None
    word = m.group(1).lower()
    if word in PRIORITY_BUCKETS or word in PRIORITY_ALIASES:
        return None
    return word


def _is_phase3_token(tok: str) -> Optional[str]:
    """Return the offending Phase-3+ prefix if ``tok`` matches one."""
    for prefix in PHASE3_TOKEN_PREFIXES:
        if tok.startswith(prefix):
            return prefix
    return None


def _detect_lane(tok: str, *, next_tok: Optional[str]) -> Optional[str]:
    """Detect a lane prefix on ``tok``.

    Accepts the colon form (``heavy:``, ``LIGHT:``) and, for voice
    input tolerance, the bare-word form (``heavy``, ``light``) provided
    the following token is not itself a lane word. The next-token guard
    prevents ``/task heavy light: foo`` from silently selecting heavy
    when the user intended light.
    """
    lower = tok.lower()
    if lower.endswith(":"):
        bare = lower[:-1]
        if bare in _LANE_WORDS:
            return bare
        return None
    # Bare word — only accept when the next token is not also a lane.
    if lower in _LANE_WORDS:
        if next_tok is not None:
            next_lower = next_tok.lower().rstrip(":")
            if next_lower in _LANE_WORDS:
                return None
        return lower
    return None


def parse_task_command(text: str) -> dict:
    """Parse a `/task` invocation per the Phase-1 grammar.

    Args:
        text: Raw message text. ``/task`` prefix is optional; it is
              stripped if present.

    Returns:
        A dict with these keys:

        - ``ok`` (bool): True if a card should be created.
        - ``lane`` (str): ``"heavy"`` or ``"light"``.
        - ``assignee`` (str): Profile name from ``LANE_ASSIGNEE``.
        - ``priority`` (int): Kanban priority integer.
        - ``priority_label`` (str): ``"p0"``..``"p3"``.
        - ``description`` (str): Trimmed description with all
          recognised tokens consumed.
        - ``labels`` (list[str]): Non-priority ``#tag`` slugs.
        - ``error_chip`` (str | None): One of ``"⚠️ SYNTAX"``,
          ``"⚠️ NOT IMPLEMENTED YET"``, or None.
        - ``error_message`` (str | None): Human-readable explanation
          when ``ok`` is False.
        - ``raw`` (str): The original input (for audit logging).

    The function is total: it never raises, and always returns a
    fully-populated dict. Callers route on ``ok``.
    """
    raw = text or ""
    body = _strip_command_prefix(raw)
    base = {
        "ok": False,
        "lane": DEFAULT_LANE,
        "assignee": LANE_ASSIGNEE[DEFAULT_LANE],
        "priority": DEFAULT_PRIORITY,
        "priority_label": DEFAULT_PRIORITY_LABEL,
        "description": "",
        "labels": [],
        "error_chip": None,
        "error_message": None,
        "raw": raw,
    }

    if not body:
        base["error_chip"] = "⚠️ SYNTAX"
        base["error_message"] = "description required (min 3 chars)"
        return base

    tokens = body.split()
    lane: Optional[str] = None
    priority_label: Optional[str] = None
    labels: list[str] = []
    consumed = 0

    # Phase-1 grammar permits the two optional prefix tokens in either
    # order. We greedily consume lane/priority/label/Phase-3-error
    # tokens from the front, stopping at the first un-recognised token
    # which begins the description.
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        next_tok = tokens[i + 1] if i + 1 < len(tokens) else None
        # Phase-3+ token? Fail fast with the dedicated chip so users
        # don't get a confusing generic syntax error.
        phase3 = _is_phase3_token(tok)
        if phase3:
            base["error_chip"] = "⚠️ NOT IMPLEMENTED YET"
            base["error_message"] = (
                f"'{phase3}...' is a Phase 3+ token. "
                "Use: /task [heavy:|light:] [#p0-#p3] DESCRIPTION"
            )
            return base
        # Lane?
        if lane is None:
            detected = _detect_lane(tok, next_tok=next_tok)
            if detected is not None:
                lane = detected
                i += 1
                consumed = i
                continue
        # Priority?
        prio = _is_priority_token(tok)
        if prio is not None:
            if priority_label is None:
                priority_label = prio
            # If multiple priority tags appear, the first wins. Subsequent
            # priority tags collapse to labels so we don't silently drop
            # input the user typed.
            else:
                labels.append(prio)
            i += 1
            consumed = i
            continue
        # Non-priority #tag?
        label = _is_label_token(tok)
        if label is not None:
            labels.append(label)
            i += 1
            consumed = i
            continue
        # Anything else marks the start of the description.
        break

    description = " ".join(tokens[consumed:]).strip()
    if len(description) < MIN_DESCRIPTION_LEN:
        base["error_chip"] = "⚠️ SYNTAX"
        base["error_message"] = "description required (min 3 chars)"
        if lane is not None:
            base["lane"] = lane
            base["assignee"] = LANE_ASSIGNEE[lane]
        if priority_label is not None:
            base["priority_label"] = priority_label
            base["priority"] = PRIORITY_BUCKETS[priority_label]
        base["labels"] = labels
        return base

    resolved_lane = lane if lane is not None else DEFAULT_LANE
    resolved_label = priority_label if priority_label is not None else DEFAULT_PRIORITY_LABEL
    return {
        "ok": True,
        "lane": resolved_lane,
        "assignee": LANE_ASSIGNEE[resolved_lane],
        "priority": PRIORITY_BUCKETS[resolved_label],
        "priority_label": resolved_label,
        "description": description,
        "labels": labels,
        "error_chip": None,
        "error_message": None,
        "raw": raw,
    }


def normalize_title(title: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace.

    Used to build the dedup idempotency key (spec §Defaults). Two
    descriptions that differ only in punctuation, case, or whitespace
    map to the same normalised string and therefore the same key.
    """
    cleaned = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return " ".join(cleaned.split())


def build_idempotency_key(*, normalized_title: str, chat_id: str, now_epoch: int) -> str:
    """Compose the 30s-window dedup key.

    The kanban `idempotency_key` column is a free-form string. Within
    a single 30-second bucket the same chat + same normalised title
    yield the same key, so `create_task` returns the existing row id
    instead of inserting a duplicate.
    """
    bucket = int(now_epoch) // 30
    return f"telegram:{chat_id}:{bucket}:{normalized_title}"


def format_syntax_error(parsed: dict) -> str:
    """Render a single-line ``⚠️ SYNTAX``/``NOT IMPLEMENTED YET`` reply."""
    chip = parsed.get("error_chip") or "⚠️ SYNTAX"
    msg = parsed.get("error_message") or "unknown parse error"
    return f"{chip} — {msg}"


def format_ack(*, task_id: str, parsed: dict) -> str:
    """Render the intake acknowledgment chip per H1 grammar style.

    The description is wrapped in backticks so any Markdown
    meta-characters (``*``, ``_``, ``[``, ``` ` ```) in user text are
    rendered literally rather than as italic/bold/link markup. Backtick
    chars inside the description itself are stripped, since nesting a
    backtick inside a code-span closes it prematurely.
    """
    lane = parsed.get("lane", DEFAULT_LANE)
    label = parsed.get("priority_label", DEFAULT_PRIORITY_LABEL)
    assignee = parsed.get("assignee", LANE_ASSIGNEE[DEFAULT_LANE])
    desc = (parsed.get("description", "") or "").replace("`", "")
    if len(desc) > 80:
        desc = desc[:77] + "..."
    return f"📥 {task_id} created — {lane}/{label} / {assignee} / `{desc}`"


def format_conflict(*, existing_task_id: str, parsed: dict) -> str:
    """Render the dedup-conflict chip per spec §Error chips."""
    assignee = parsed.get("assignee", LANE_ASSIGNEE[DEFAULT_LANE])
    desc = (parsed.get("description", "") or "").replace("`", "")
    if len(desc) > 60:
        desc = desc[:57] + "..."
    return (
        f"↪ CONFLICT — duplicate detected (within 30s window). "
        f"Existing: {existing_task_id} ({assignee}) — `{desc}`. "
        f"No new card created."
    )
