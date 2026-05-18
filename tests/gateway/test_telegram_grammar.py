"""Tests for gateway.telegram_grammar (Phase-1 /task command parser).

All functions under test are pure (no I/O, no DB, no network, no async),
so this file is entirely fixture-free. The parser promises totality —
it never raises — which means even degenerate inputs like None or "" must
return a well-formed dict. Tests are organised by concern; pytest.mark.parametrize
compresses repetitive cases.  No mocks are needed and none should be added.
"""

import pytest

from gateway.telegram_grammar import (
    DEFAULT_LANE,
    DEFAULT_PRIORITY,
    DEFAULT_PRIORITY_LABEL,
    LANE_ASSIGNEE,
    LANE_HEAVY,
    LANE_LIGHT,
    PRIORITY_BUCKETS,
    build_idempotency_key,
    format_ack,
    format_conflict,
    format_syntax_error,
    normalize_title,
    parse_task_command,
)


class TestLaneParsing:
    def test_heavy_colon_form(self):
        r = parse_task_command("/task heavy: build the new auth flow")
        assert r["lane"] == LANE_HEAVY

    def test_heavy_assignee(self):
        r = parse_task_command("/task heavy: build the new auth flow")
        assert r["assignee"] == LANE_ASSIGNEE[LANE_HEAVY]

    def test_light_colon_form(self):
        r = parse_task_command("/task light: fix typo in readme")
        assert r["lane"] == LANE_LIGHT

    def test_light_assignee(self):
        r = parse_task_command("/task light: fix typo in readme")
        assert r["assignee"] == LANE_ASSIGNEE[LANE_LIGHT]

    def test_default_lane_when_no_prefix(self):
        r = parse_task_command("/task no lane prefix here at all")
        assert r["lane"] == DEFAULT_LANE

    def test_default_assignee_when_no_prefix(self):
        r = parse_task_command("/task no lane prefix here at all")
        assert r["assignee"] == LANE_ASSIGNEE[DEFAULT_LANE]

    def test_light_case_insensitive(self):
        r = parse_task_command("/task LIGHT: case insensitive check")
        assert r["lane"] == LANE_LIGHT

    def test_heavy_case_insensitive(self):
        r = parse_task_command("/task HEAVY: case insensitive check")
        assert r["lane"] == LANE_HEAVY

    def test_heavy_bare_word_voice_input(self):
        r = parse_task_command("/task heavy build the widget")
        assert r["lane"] == LANE_HEAVY

    def test_light_bare_word_voice_input(self):
        r = parse_task_command("/task light fix the bug properly")
        assert r["lane"] == LANE_LIGHT

    def test_next_token_guard_colon_form_wins(self):
        # "heavy light: foo" — bare 'heavy' is suppressed because next token
        # is a lane word; then "light:" wins as the colon form.
        r = parse_task_command("/task heavy light: handle the new feature here")
        assert r["lane"] == LANE_LIGHT


class TestPriorityParsing:
    @pytest.mark.parametrize("tag,expected_label,expected_int", [
        ("#p0", "p0", 400),
        ("#p1", "p1", 300),
        ("#p2", "p2", 200),
        ("#p3", "p3", 100),
    ])
    def test_explicit_priority_tags(self, tag, expected_label, expected_int):
        r = parse_task_command(f"/task {tag} fix critical issue now")
        assert r["priority_label"] == expected_label
        assert r["priority"] == expected_int

    def test_default_priority_when_absent(self):
        r = parse_task_command("/task fix something minor today")
        assert r["priority_label"] == DEFAULT_PRIORITY_LABEL
        assert r["priority"] == DEFAULT_PRIORITY

    def test_urgent_alias_maps_to_p0(self):
        r = parse_task_command("/task #urgent fix production outage")
        assert r["priority_label"] == "p0"

    def test_low_alias_maps_to_p3(self):
        r = parse_task_command("/task #low cleanup old logs sometime")
        assert r["priority_label"] == "p3"

    def test_priority_tag_case_insensitive(self):
        r = parse_task_command("/task #P0 fix the deploy pipeline")
        assert r["priority_label"] == "p0"


class TestLabelParsing:
    def test_unknown_hashtag_becomes_label(self):
        r = parse_task_command("/task #fakepriority test that unknown tags become labels")
        assert r["priority_label"] == DEFAULT_PRIORITY_LABEL
        assert "fakepriority" in r["labels"]

    def test_multiple_label_tags(self):
        r = parse_task_command("/task #docs #refactor improve the kanban module")
        assert "docs" in r["labels"]
        assert "refactor" in r["labels"]

    def test_duplicate_priority_tags_second_becomes_label(self):
        r = parse_task_command("/task #p0 #urgent #docs duplicate priority tags here")
        assert r["priority_label"] == "p0"
        # #urgent is also a priority alias — second priority collapses to its canonical label
        assert "p0" in r["labels"]
        assert "docs" in r["labels"]


class TestTokenOrder:
    def test_lane_before_priority(self):
        r = parse_task_command("/task heavy: #p0 build auth service")
        assert r["lane"] == LANE_HEAVY
        assert r["priority_label"] == "p0"

    def test_priority_before_lane(self):
        r = parse_task_command("/task #p0 heavy: build auth service")
        assert r["lane"] == LANE_HEAVY
        assert r["priority_label"] == "p0"

    def test_priority_label_and_lane_mixed(self):
        r = parse_task_command("/task #p1 #docs heavy: build auth service")
        assert r["lane"] == LANE_HEAVY
        assert r["priority_label"] == "p1"
        assert "docs" in r["labels"]


class TestErrors:
    def test_empty_command_is_error(self):
        r = parse_task_command("/task")
        assert r["ok"] is False

    def test_empty_command_error_chip(self):
        r = parse_task_command("/task")
        assert r["error_chip"] == "⚠️ SYNTAX"

    def test_whitespace_only_is_error(self):
        r = parse_task_command("/task ")
        assert r["ok"] is False

    def test_description_too_short(self):
        r = parse_task_command("/task ab")
        assert r["ok"] is False

    def test_heavy_lane_with_short_description_is_error(self):
        r = parse_task_command("/task heavy: hi")
        assert r["ok"] is False

    def test_heavy_lane_still_parsed_on_short_description_error(self):
        r = parse_task_command("/task heavy: hi")
        assert r["lane"] == LANE_HEAVY

    def test_dep_token_not_implemented(self):
        r = parse_task_command("/task +dep:t_abc123 fix it all up")
        assert r["ok"] is False
        assert r["error_chip"] == "⚠️ NOT IMPLEMENTED YET"

    def test_deadline_token_not_implemented(self):
        r = parse_task_command("/task @deadline:tomorrow fix the release")
        assert r["ok"] is False
        assert r["error_chip"] == "⚠️ NOT IMPLEMENTED YET"

    def test_dry_run_token_not_implemented(self):
        r = parse_task_command("/task --dry-run fix it all up")
        assert r["ok"] is False
        assert r["error_chip"] == "⚠️ NOT IMPLEMENTED YET"

    def test_attach_token_not_implemented(self):
        r = parse_task_command("/task --attach /path/to/file fix it")
        assert r["ok"] is False
        assert r["error_chip"] == "⚠️ NOT IMPLEMENTED YET"


class TestPrefixHandling:
    def test_with_slash_prefix(self):
        r = parse_task_command("/task fix the kanban bug now")
        assert r["ok"] is True

    def test_without_slash_prefix(self):
        r = parse_task_command("task fix the kanban bug now")
        assert r["ok"] is True

    def test_with_bot_suffix(self):
        r = parse_task_command("/task@hermesbot fix the kanban bug now")
        assert r["ok"] is True

    def test_all_three_prefixes_produce_same_result(self):
        a = parse_task_command("/task fix the kanban bug now")
        b = parse_task_command("task fix the kanban bug now")
        c = parse_task_command("/task@hermesbot fix the kanban bug now")
        assert a["description"] == b["description"] == c["description"]
        assert a["lane"] == b["lane"] == c["lane"]

    def test_empty_string_returns_error_without_raising(self):
        r = parse_task_command("")
        assert r["ok"] is False

    def test_none_does_not_raise(self):
        r = parse_task_command(None)
        assert r["ok"] is False


class TestNormalisation:
    def test_lowercase_strip_punct_collapse_whitespace(self):
        assert normalize_title("Fix THE  bug, please!") == "fix the bug please"

    def test_empty_string(self):
        assert normalize_title("") == ""

    def test_tabs_and_newlines_collapsed(self):
        assert normalize_title("a\tb\nc") == "a b c"


class TestIdempotencyKey:
    def test_same_window_same_key(self):
        k1 = build_idempotency_key(normalized_title="fix the bug", chat_id="c1", now_epoch=0)
        k2 = build_idempotency_key(normalized_title="fix the bug", chat_id="c1", now_epoch=29)
        assert k1 == k2

    def test_different_window_different_key(self):
        k1 = build_idempotency_key(normalized_title="fix the bug", chat_id="c1", now_epoch=0)
        k2 = build_idempotency_key(normalized_title="fix the bug", chat_id="c1", now_epoch=30)
        assert k1 != k2

    def test_different_chat_different_key(self):
        k1 = build_idempotency_key(normalized_title="fix the bug", chat_id="c1", now_epoch=0)
        k2 = build_idempotency_key(normalized_title="fix the bug", chat_id="c2", now_epoch=0)
        assert k1 != k2

    def test_key_starts_with_telegram_prefix(self):
        k = build_idempotency_key(normalized_title="fix the bug", chat_id="c1", now_epoch=0)
        assert k.startswith("telegram:")


class TestFormatters:
    def test_format_syntax_error_starts_with_chip(self):
        parsed = {
            "error_chip": "⚠️ SYNTAX",
            "error_message": "description required (min 3 chars)",
        }
        result = format_syntax_error(parsed)
        assert result.startswith("⚠️ SYNTAX")

    def test_format_ack_contains_task_id(self):
        parsed = {
            "lane": "light",
            "priority_label": "p2",
            "assignee": "h2coder",
            "description": "fix the kanban dispatch crash",
        }
        result = format_ack(task_id="t_abc123", parsed=parsed)
        assert "t_abc123" in result

    def test_format_ack_contains_inbox_emoji(self):
        parsed = {
            "lane": "light",
            "priority_label": "p2",
            "assignee": "h2coder",
            "description": "fix the kanban dispatch crash",
        }
        result = format_ack(task_id="t_abc123", parsed=parsed)
        assert "📥" in result

    def test_format_ack_contains_lane_priority(self):
        parsed = {
            "lane": "light",
            "priority_label": "p2",
            "assignee": "h2coder",
            "description": "fix the kanban dispatch crash",
        }
        result = format_ack(task_id="t_abc123", parsed=parsed)
        assert "light/p2" in result

    def test_format_ack_contains_assignee(self):
        parsed = {
            "lane": "light",
            "priority_label": "p2",
            "assignee": "h2coder",
            "description": "fix the kanban dispatch crash",
        }
        result = format_ack(task_id="t_abc123", parsed=parsed)
        assert "h2coder" in result

    def test_format_ack_contains_description(self):
        parsed = {
            "lane": "light",
            "priority_label": "p2",
            "assignee": "h2coder",
            "description": "fix the kanban dispatch crash",
        }
        result = format_ack(task_id="t_abc123", parsed=parsed)
        assert "fix the kanban dispatch crash" in result

    def test_format_ack_truncates_long_description(self):
        long_desc = "x" * 100
        parsed = {
            "lane": "light",
            "priority_label": "p2",
            "assignee": "h2coder",
            "description": long_desc,
        }
        result = format_ack(task_id="t_abc123", parsed=parsed)
        assert "..." in result

    def test_format_conflict_contains_conflict_chip(self):
        parsed = {"assignee": "h2coder", "description": "x" * 100}
        result = format_conflict(existing_task_id="t_old1", parsed=parsed)
        assert "↪ CONFLICT" in result

    def test_format_conflict_contains_existing_task_id(self):
        parsed = {"assignee": "h2coder", "description": "x" * 100}
        result = format_conflict(existing_task_id="t_old1", parsed=parsed)
        assert "t_old1" in result

    def test_format_conflict_truncates_long_description(self):
        parsed = {"assignee": "h2coder", "description": "x" * 100}
        result = format_conflict(existing_task_id="t_old1", parsed=parsed)
        assert "..." in result
