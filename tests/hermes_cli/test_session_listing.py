"""Tests for the shared session-listing helpers (hermes_cli/session_listing.py).

Covers both the fork's CLI/gateway listing-policy suite and upstream v0.20's
search / lane-scope suite.
"""
from __future__ import annotations

from typing import Any

import pytest

from hermes_cli.session_listing import (
    format_gateway_session_listing,
    parse_session_listing_args,
    query_session_listing,
)


class _FakeSessionDB:
    """Fork listing-policy double.

    Re-anchored for the v0.20 merge: ``query_session_listing`` now also passes
    ``session_key``/``search_query``/``order_by_last_active``, so accept (and
    ignore) them while still recording the three fields the fork asserts on.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def list_sessions_rich(
        self,
        *,
        source: str | None,
        exclude_sources: list[str] | None,
        limit: int,
        session_key: str | None = None,
        search_query: str | None = None,
        order_by_last_active: bool = False,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {"source": source, "exclude_sources": exclude_sources, "limit": limit}
        )
        return list(self.rows)


def test_parse_strips_display_aliases() -> None:
    assert parse_session_listing_args("list") == (False, False, "", None)
    assert parse_session_listing_args("ls") == (False, False, "", None)
    assert parse_session_listing_args("browse") == (False, False, "", None)


def test_parse_recognizes_all_and_full_flags_case_insensitively() -> None:
    assert parse_session_listing_args("ALL") == (True, False, "", None)
    assert parse_session_listing_args("--full") == (False, True, "", None)
    assert parse_session_listing_args("all full") == (True, True, "", None)
    assert parse_session_listing_args("--all FULL") == (True, True, "", None)


def test_parse_preserves_non_flag_text_as_target() -> None:
    assert parse_session_listing_args("My Session") == (False, False, "My Session", None)
    assert parse_session_listing_args("ls My Session") == (False, False, "My Session", None)


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


class TestParseSessionListingArgs:
    def test_plain_listing(self):
        assert parse_session_listing_args("") == (False, False, "", None)




class TestQuerySessionListingSearch:
    @pytest.fixture
    def db(self, tmp_path):
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("sess_an94", "telegram", user_id="1", chat_id="2")
        db.set_session_title("sess_an94", "AN-94 Prestige Barrel Build #2")
        db.create_session("sess_winton", "whatsapp", user_id="1", chat_id="2")
        db.set_session_title("sess_winton", "Winton Email Sheet Update #3")
        db.create_session("sess_untitled", "telegram", user_id="1", chat_id="2")
        yield db
        db.close()

    def _ids(self, db, **kw):
        return [r["id"] for r in query_session_listing(db, **kw)]



    def test_source_scoping(self, db):
        assert self._ids(db, source="telegram", search_query="winton") == []
        assert self._ids(db, source="whatsapp", search_query="winton") == ["sess_winton"]


    def test_search_matches_compression_root_title(self, tmp_path):
        """Searching an old (compressed-away) title surfaces the live tip."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "chain.db")
        db.create_session("root_1", "telegram", user_id="1", chat_id="2")
        db.set_session_title("root_1", "Old Chat")
        db.end_session("root_1", end_reason="compression")
        db.create_session(
            "tip_1", "telegram", user_id="1", chat_id="2", parent_session_id="root_1"
        )
        db.set_session_title("tip_1", "AN-94 Build")
        try:
            for query in ("old chat", "root_1", "an94"):
                rows = query_session_listing(db, source="telegram", search_query=query)
                assert [r["id"] for r in rows] == ["tip_1"], query
        finally:
            db.close()


class TestQuerySessionListingLaneScope:
    @pytest.fixture
    def db(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        lane_key = "agent:main:telegram:dm:lane"
        db.create_session(
            "lane_current", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        db.set_session_title("lane_current", "Current lane")
        db.create_session(
            "lane_named", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        db.set_session_title("lane_named", "Needle lane")
        db.create_session(
            "lane_unnamed", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        for i in range(60):
            db.create_session(
                f"foreign_{i}", "telegram",
                session_key=f"agent:main:telegram:dm:foreign-{i}",
                user_id=f"foreign-user-{i}", chat_id=f"foreign-{i}",
            )
            db.set_session_title(f"foreign_{i}", f"Needle foreign {i}")
        yield db, lane_key
        db.close()

    def test_exact_lane_precedes_limit_and_current_session_exclusion(self, db):
        session_db, lane_key = db

        rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            current_session_id="lane_current",
            limit=1,
        )

        assert [row["id"] for row in rows] == ["lane_named"]

    def test_exact_lane_preserves_full_and_search_modes(self, db):
        session_db, lane_key = db

        full_rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            include_unnamed=True,
            limit=10,
        )
        search_rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            search_query="needle",
            limit=10,
        )

        assert {row["id"] for row in full_rows} == {
            "lane_current", "lane_named", "lane_unnamed",
        }
        assert [row["id"] for row in search_rows] == ["lane_named"]

    def test_omitted_session_key_keeps_source_scope(self, db):
        session_db, _lane_key = db

        rows = query_session_listing(
            session_db,
            source="telegram",
            search_query="needle foreign 59",
            limit=10,
        )

        assert [row["id"] for row in rows] == ["foreign_59"]
