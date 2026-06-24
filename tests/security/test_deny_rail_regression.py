"""Permanent regression gate for webhook/Discord terminal deny rails.

This converts the one-shot red-team artifact into a hermetic pytest suite:
no model calls, no network, no database, and no cross-repo imports.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.webhook import DEFAULT_WEBHOOK_DENY_PATTERNS
from gateway.session import SessionSource, build_session_key
from plugins.platforms.discord.adapter import _register_discord_session_deny_patterns
from tools.approval import (
    check_session_deny_patterns,
    clear_session,
    register_session_deny_patterns,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "redteam_cases.jsonl"
EXPECTED_CASE_COUNT = 23
EXPECTED_DENIED_CASE_COUNT = 19
WEBHOOK_SESSION_KEY = "test:webhook:redteam"
ALLOWED_CONTROL = "git status"

# The frozen source fixture still marks RT-14 as expect_denied=false, but the
# current production CREDENTIAL_EXFIL_DENY_PATTERNS correctly denies it.
CURRENT_DENIED_OVERRIDES = {"RT-14": True}

# Canonical source of truth for the production SQL filter:
# /home/josep/.hermes/mcp/recall/recall_at_dispatch.py::CLEAN_POOL_WHERE.
# Keep this hermetic main-repo test semantics-only; full SQL equivalence belongs
# in /home/josep/.hermes/mcp/recall/tests/test_recall_at_dispatch.py.


def _load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                cases.append(json.loads(line))
    return cases


ALL_CASES = _load_cases()
DENIED_CASES = [case for case in ALL_CASES if case["expect_denied"] is True]


def _current_expected_denied(case: dict[str, Any]) -> bool:
    return CURRENT_DENIED_OVERRIDES.get(case["id"], bool(case["expect_denied"]))


def _assert_case_current_verdict(path: str, session_key: str, case: dict[str, Any]) -> None:
    denied, matched_pattern = check_session_deny_patterns(case["attack"], session_key)
    expected_denied = _current_expected_denied(case)
    assert denied is expected_denied, (
        f"{path}:{case['id']} expected denied={expected_denied}, got denied={denied}; "
        f"matched_pattern={matched_pattern!r}; attack={case['attack']!r}"
    )


def _collect_denial_breaches(path: str, session_key: str) -> list[dict[str, str]]:
    breaches: list[dict[str, str]] = []
    for case in DENIED_CASES:
        denied, matched_pattern = check_session_deny_patterns(case["attack"], session_key)
        if not denied:
            breaches.append(
                {
                    "path": path,
                    "id": case["id"],
                    "attack": case["attack"],
                    "matched_pattern": str(matched_pattern),
                }
            )
    return breaches


@pytest.fixture(scope="module", autouse=True)
def fixture_integrity() -> None:
    assert len(ALL_CASES) == EXPECTED_CASE_COUNT
    assert len(DENIED_CASES) == EXPECTED_DENIED_CASE_COUNT
    assert {case["id"] for case in ALL_CASES} == {
        "RT-01",
        "RT-02",
        "RT-03",
        "RT-04",
        "RT-05",
        "RT-06",
        "RT-07",
        "RT-08",
        "RT-09",
        "RT-10",
        "RT-11",
        "RT-12",
        "RT-13",
        "RT-14",
        "RT-15",
        "RT-16",
        "RT-17",
        "RT-18",
        "RT-19",
        "RT-20",
        "RT-21",
        "RT-22",
        "RT-25",
    }


@pytest.fixture
def webhook_session_key() -> Generator[str]:
    register_session_deny_patterns(WEBHOOK_SESSION_KEY, list(DEFAULT_WEBHOOK_DENY_PATTERNS))
    try:
        yield WEBHOOK_SESSION_KEY
    finally:
        clear_session(WEBHOOK_SESSION_KEY)


@pytest.fixture
def discord_session_key() -> Generator[str]:
    source = _discord_source()
    config = PlatformConfig(extra={})
    _register_discord_session_deny_patterns(source, config)
    session_key = _discord_session_key(source, config)
    try:
        yield session_key
    finally:
        clear_session(session_key)


def _discord_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="discord-redteam-dm",
        chat_type="dm",
        user_id="discord-redteam-user",
        user_name="Discord Redteam",
    )


def _discord_session_key(source: SessionSource, config: PlatformConfig) -> str:
    extra = config.extra or {}
    return build_session_key(
        source,
        group_sessions_per_user=extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
    )


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case["id"])
def test_webhook_outbound_redteam_case_current_verdict(
    case: dict[str, Any], webhook_session_key: str
) -> None:
    _assert_case_current_verdict("webhook", webhook_session_key, case)


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case["id"])
def test_discord_inbound_redteam_case_current_verdict(
    case: dict[str, Any], discord_session_key: str
) -> None:
    _assert_case_current_verdict("discord", discord_session_key, case)


def test_matcher_discriminates_known_allowed_control(webhook_session_key: str) -> None:
    denied, matched_pattern = check_session_deny_patterns(ALLOWED_CONTROL, webhook_session_key)
    assert denied is False
    assert matched_pattern is None


def test_no_breaches() -> None:
    webhook_session = WEBHOOK_SESSION_KEY
    discord_source = _discord_source()
    discord_config = PlatformConfig(extra={})
    discord_session = _discord_session_key(discord_source, discord_config)
    try:
        register_session_deny_patterns(webhook_session, list(DEFAULT_WEBHOOK_DENY_PATTERNS))
        _register_discord_session_deny_patterns(discord_source, discord_config)
        breaches = _collect_denial_breaches("webhook", webhook_session)
        breaches.extend(_collect_denial_breaches("discord", discord_session))
        breach_count = len(breaches)
        assert breach_count == 0, "breach_count != 0: " + "; ".join(
            f"{breach['path']}:{breach['id']} attack={breach['attack']!r}"
            for breach in breaches
        )
    finally:
        clear_session(webhook_session)
        clear_session(discord_session)


def _recall_poison_filter_keeps(source: str, deprecated_at: str | None) -> bool:
    if source == "ict-brain":
        return False
    if re.search(r":(gave_up|crashed|blocked)$", source):
        return False
    if re.search(r"compactor|superseder|reflect-promote|curator", source, re.IGNORECASE):
        return False
    if source.startswith("kanban-mvms-bridge:"):
        return False
    return deprecated_at is None


@pytest.mark.parametrize(
    ("row", "expected_kept"),
    [
        ({"source": "ict-brain", "deprecated_at": None}, False),
        ({"source": "loki7:gave_up", "deprecated_at": None}, False),
        ({"source": "loki3:crashed", "deprecated_at": None}, False),
        ({"source": "loki2:blocked", "deprecated_at": None}, False),
        ({"source": "daily-compactor", "deprecated_at": None}, False),
        ({"source": "manual-superseder", "deprecated_at": None}, False),
        ({"source": "loki5:reflect-promote", "deprecated_at": None}, False),
        ({"source": "curator", "deprecated_at": None}, False),
        ({"source": "kanban-mvms-bridge:t_9e51b3df", "deprecated_at": None}, False),
        ({"source": "loki:lane7:done", "deprecated_at": "2026-06-01T00:00:00Z"}, False),
        ({"source": "loki:lane7:done", "deprecated_at": None}, True),
    ],
)
def test_recall_poison_filter_semantics(row: dict[str, str | None], expected_kept: bool) -> None:
    assert _recall_poison_filter_keeps(row["source"] or "", row["deprecated_at"]) is expected_kept
