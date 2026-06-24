"""Tests for shared Hermes CLI and gateway session-listing policy."""
from __future__ import annotations

from typing import Any

from hermes_cli.session_listing import (
    format_gateway_session_listing,
    parse_session_listing_args,
    query_session_listing,
)


class _FakeSessionDB:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def list_sessions_rich(
        self,
        *,
        source: str | None,
        exclude_sources: list[str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {"source": source, "exclude_sources": exclude_sources, "limit": limit}
        )
        return list(self.rows)


def test_parse_strips_display_aliases() -> None:
    assert parse_session_listing_args("list") == (False, False, "")
    assert parse_session_listing_args("ls") == (False, False, "")
    assert parse_session_listing_args("browse") == (False, False, "")


def test_parse_recognizes_all_and_full_flags_case_insensitively() -> None:
    assert parse_session_listing_args("ALL") == (True, False, "")
    assert parse_session_listing_args("--full") == (False, True, "")
    assert parse_session_listing_args("all full") == (True, True, "")
    assert parse_session_listing_args("--all FULL") == (True, True, "")


def test_parse_preserves_non_flag_text_as_target() -> None:
    assert parse_session_listing_args("My Session") == (False, False, "My Session")
    assert parse_session_listing_args("ls My Session") == (False, False, "My Session")


def test_query_scopes_to_source_by_default() -> None:
    db = _FakeSessionDB([{"id": "s1", "title": "Session 1", "source": "discord"}])

    result = query_session_listing(db, source="discord")

    assert result == [{"id": "s1", "title": "Session 1", "source": "discord"}]
    assert db.calls == [{"source": "discord", "exclude_sources": None, "limit": 40}]


def test_query_all_sources_passes_none_source_and_exclude_sources() -> None:
    db = _FakeSessionDB([{"id": "s1", "title": "Session 1", "source": "web"}])

    query_session_listing(
        db,
        source="discord",
        include_all_sources=True,
        exclude_sources=["local"],
        limit=3,
    )

    assert db.calls == [{"source": None, "exclude_sources": ["local"], "limit": 12}]


def test_query_excludes_current_session() -> None:
    db = _FakeSessionDB(
        [
            {"id": "current", "title": "Current chat"},
            {"id": "older", "title": "Older chat"},
        ]
    )

    result = query_session_listing(db, source="discord", current_session_id="current")

    assert result == [{"id": "older", "title": "Older chat"}]


def test_query_hides_unnamed_caps_limit_and_overfetches() -> None:
    db = _FakeSessionDB(
        [
            {"id": "named-1", "title": "Named 1"},
            {"id": "missing-title"},
            {"id": "blank-title", "title": ""},
            {"id": "named-2", "title": "Named 2"},
            {"id": "named-3", "title": "Named 3"},
        ]
    )

    result = query_session_listing(db, source="discord", limit=2)

    assert result == [
        {"id": "named-1", "title": "Named 1"},
        {"id": "named-2", "title": "Named 2"},
    ]
    assert db.calls == [{"source": "discord", "exclude_sources": None, "limit": 8}]


def test_query_include_unnamed_keeps_titleless_rows() -> None:
    db = _FakeSessionDB(
        [
            {"id": "missing-title"},
            {"id": "blank-title", "title": ""},
            {"id": "named", "title": "Named"},
        ]
    )

    result = query_session_listing(
        db, source="discord", include_unnamed=True, limit=3
    )

    assert result == [
        {"id": "missing-title"},
        {"id": "blank-title", "title": ""},
        {"id": "named", "title": "Named"},
    ]


def test_format_empty_rows_shows_no_sessions_help() -> None:
    rendered = format_gateway_session_listing([])

    assert "No sessions found." in rendered
    assert "/sessions full" in rendered


def test_format_non_empty_rows_renders_title_ids_footer_and_optional_source() -> None:
    rows = [
        {
            "id": "abc123",
            "title": "Build Review",
            "preview": "Review the latest source changes",
            "source": "discord",
        }
    ]

    with_source = format_gateway_session_listing(
        rows, include_source=True, title="Recent Sessions"
    )
    without_source = format_gateway_session_listing(
        rows, include_source=False, title="Recent Sessions"
    )

    assert "📋 **Recent Sessions**" in with_source
    assert "**Build Review**" in with_source
    assert "`abc123`" in with_source
    assert "Resume:" in with_source
    assert "`discord`" in with_source
    assert "`discord`" not in without_source
