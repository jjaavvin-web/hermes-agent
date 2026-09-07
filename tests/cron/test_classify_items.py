"""Tests for the proactive-monitor item classifier script.

Covers JSON input normalization, stable item ids, prompt shaping, tolerant score
parsing, and end-to-end surfaced/silent output paths with the LLM mocked.
"""

import json
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from cron.scripts import classify_items as ci


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _write_text(path, value):
    path.write_text(value, encoding="utf-8")
    return path


def _fake_response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _run_main(monkeypatch, tmp_path, items, *, classifier_content, fmt="text", threshold=7):
    input_file = _write_json(tmp_path / "items.json", items)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "classify_items.py",
            "--criteria",
            "reply today if urgent",
            "--threshold",
            str(threshold),
            "--format",
            fmt,
            "--input-file",
            str(input_file),
        ],
    )
    with mock.patch("agent.auxiliary_client.call_llm") as mocked:
        mocked.return_value = _fake_response(classifier_content)
        result = ci.main()
    return result, mocked


def test_load_items_accepts_lists_filters_non_dicts_and_quiet_empty_input(tmp_path):
    assert ci._load_items(str(_write_json(tmp_path / "list.json", [{"id": 1}, "drop", 3, {"id": 2}]))) == [
        {"id": 1},
        {"id": 2},
    ]
    assert ci._load_items(str(_write_text(tmp_path / "empty.json", "  \n\t  "))) == []


def test_load_items_accepts_items_wrapper_and_single_dict(tmp_path):
    wrapped = {"items": [{"title": "one"}, {"title": "two"}]}
    assert ci._load_items(str(_write_json(tmp_path / "wrapped.json", wrapped))) == wrapped[
        "items"
    ]

    single = {"title": "only", "body": "standalone object"}
    assert ci._load_items(str(_write_json(tmp_path / "single.json", single))) == [single]


def test_load_items_invalid_json_exits_with_code_2(tmp_path):
    bad_file = _write_text(tmp_path / "bad.json", "{not valid json")

    with pytest.raises(SystemExit) as exc:
        ci._load_items(str(bad_file))

    assert exc.value.code == 2


def test_item_id_prefers_stable_keys_in_order_and_falls_back_to_index():
    item = {
        "link": "link-value",
        "url": "url-value",
        "message_id": "message-value",
        "guid": "guid-value",
        "id": "id-value",
    }
    assert ci._item_id(item, 4) == "id-value"
    assert ci._item_id({"guid": "guid-value", "message_id": "message-value"}, 4) == "guid-value"
    assert ci._item_id({"message_id": "message-value", "url": "url-value"}, 4) == "message-value"
    assert ci._item_id({"url": "url-value", "link": "link-value"}, 4) == "url-value"
    assert ci._item_id({"link": "link-value"}, 4) == "link-value"
    assert ci._item_id({"title": "no stable id"}, 4) == "item-4"


def test_build_prompt_includes_criteria_salient_fields_and_whole_object_fallback():
    prompt = ci._build_prompt(
        [
            {
                "title": "Urgent note",
                "summary": "Needs action",
                "from": "boss@example.com",
                "url": "https://example.com/urgent",
                "irrelevant": "hidden from compact view",
            },
            {"custom": "fallback shown", "count": 3},
        ],
        "Only interrupt for manager requests.",
    )

    assert "USER IMPORTANCE CRITERIA:\nOnly interrupt for manager requests." in prompt
    assert '[0] {"title": "Urgent note"' in prompt
    assert '"summary": "Needs action"' in prompt
    assert '"from": "boss@example.com"' in prompt
    assert '"url": "https://example.com/urgent"' in prompt
    assert "hidden from compact view" not in prompt
    assert '[1] {"custom": "fallback shown", "count": 3}' in prompt


def test_parse_scores_handles_json_fences_prose_and_drops_invalid_entries():
    clean = ci._parse_scores('[{"index":0,"score":8,"reason":"clean"}]', 2)
    assert clean == {0: {"index": 0, "score": 8, "reason": "clean"}}

    fenced = ci._parse_scores(
        '```json\n[{"index":1,"score":7,"reason":"fenced"}]\n```', 2
    )
    assert fenced[1]["reason"] == "fenced"

    prose = ci._parse_scores(
        'Sure, here it is: [{"index":0,"score":9,"reason":"prose"}] thanks', 2
    )
    assert prose[0]["score"] == 9

    mixed = ci._parse_scores(
        json.dumps(
            [
                {"index": 0, "score": 6, "reason": "kept"},
                {"index": 2, "score": 10, "reason": "out of range"},
                {"index": "1", "score": 8, "reason": "non-int index"},
                "not a dict",
            ]
        ),
        2,
    )
    assert mixed == {0: {"index": 0, "score": 6, "reason": "kept"}}
    assert ci._parse_scores("total garbage", 2) == {}


def test_main_text_surfaces_only_at_or_above_threshold_with_mocked_llm(monkeypatch, capsys, tmp_path):
    result, mocked = _run_main(
        monkeypatch,
        tmp_path,
        [
            {"id": "urgent-1", "title": "Production fire", "url": "https://example.com/fire"},
            {"id": "noise-1", "title": "FYI newsletter", "url": "https://example.com/fyi"},
        ],
        classifier_content='[{"index":0,"score":9,"reason":"Needs same-day action"},{"index":1,"score":2,"reason":"Low priority"}]',
        fmt="text",
        threshold=7,
    )

    assert result == 0
    out = capsys.readouterr().out
    assert "## [9/10] Production fire" in out
    assert "https://example.com/fire" in out
    assert "_Needs same-day action_" in out
    assert "FYI newsletter" not in out
    mocked.assert_called_once()
    assert mocked.call_args.kwargs["task"] == "monitor"


def test_main_json_surfaces_exact_item_with_id_score_reason_and_payload(monkeypatch, capsys, tmp_path):
    urgent = {"guid": "message-123", "subject": "Call back now", "link": "https://example.com/call"}
    result, _mocked = _run_main(
        monkeypatch,
        tmp_path,
        [urgent, {"id": "low", "title": "Later"}],
        classifier_content='[{"index":0,"score":8,"reason":"Meets criteria"},{"index":1,"score":1,"reason":"Ignore"}]',
        fmt="json",
        threshold=7,
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "id": "message-123",
            "score": 8,
            "reason": "Meets criteria",
            "item": urgent,
        }
    ]


def test_main_silent_paths_return_zero_with_empty_stdout(monkeypatch, capsys, tmp_path):
    empty_file = _write_json(tmp_path / "empty-items.json", [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "classify_items.py",
            "--criteria",
            "anything urgent",
            "--input-file",
            str(empty_file),
        ],
    )
    assert ci.main() == 0
    assert capsys.readouterr().out == ""

    result, mocked = _run_main(
        monkeypatch,
        tmp_path,
        [{"title": "Not urgent"}],
        classifier_content='[{"index":0,"score":3,"reason":"below threshold"}]',
        fmt="text",
        threshold=7,
    )
    assert result == 0
    assert capsys.readouterr().out == ""
    mocked.assert_called_once()
