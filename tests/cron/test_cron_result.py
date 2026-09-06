"""Unit tests for cron.cron_result — the self-reporting contract helper (Card 64).

Hermetic: no network, no agent, no subprocess. All ledger I/O is redirected to a
pytest ``tmp_path`` so the real ``HERMES_HOME/observability/cron-contracts.jsonl``
is never touched.
"""
import json

import pytest

from cron import cron_result


@pytest.fixture()
def ledger(tmp_path):
    return tmp_path / "cron-contracts.jsonl"


def test_record_contract_roundtrips_valid_jsonl(ledger):
    c = cron_result.record_contract(
        name="board-shepherd",
        quota=3,
        achieved=2,
        gaps=["one card stuck"],
        retries=1,
        status="miss",
        ledger_path=ledger,
    )
    # record_contract returns the persisted contract.
    assert c.name == "board-shepherd"
    assert c.achieved == 2
    assert c.generated_at  # populated with a UTC ISO timestamp

    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["name"] == "board-shepherd"
    assert rec["quota"] == 3
    assert rec["achieved"] == 2
    assert rec["gaps"] == ["one card stuck"]
    assert rec["retries"] == 1
    assert rec["status"] == "miss"
    assert "generated_at" in rec


def test_record_contract_appends(ledger):
    cron_result.record_contract("c", 1, 1, [], 0, "ok", ledger_path=ledger)
    cron_result.record_contract("c", 1, 0, ["nope"], 0, "miss", ledger_path=ledger)
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["achieved"] == 1
    assert json.loads(lines[1])["achieved"] == 0


def test_record_contract_creates_parent_dir(tmp_path):
    nested = tmp_path / "observability" / "cron-contracts.jsonl"
    assert not nested.parent.exists()
    cron_result.record_contract("c", None, 1, None, 0, "ok", ledger_path=nested)
    assert nested.exists()
    rec = json.loads(nested.read_text(encoding="utf-8").splitlines()[0])
    assert rec["quota"] is None
    assert rec["gaps"] == []


def test_gap_line_quota_present_and_gaps():
    c = cron_result.CronContract(
        name="weekly-457-market-scan", quota=5, achieved=3, gaps=["A", "B"], retries=2, status="miss"
    )
    line = cron_result.gap_line(c)
    assert line == "[contract] weekly-457-market-scan: achieved 3/5; gaps: A, B; retries: 2"


def test_gap_line_quota_absent_and_no_gaps():
    c = cron_result.CronContract(
        name="daily-morning-briefing", quota=None, achieved=1, gaps=[], retries=0, status="ok"
    )
    line = cron_result.gap_line(c)
    assert line == "[contract] daily-morning-briefing: achieved 1/?; gaps: none; retries: 0"


def test_parse_contract_trailer_parses_last_line():
    text = "Here is the report.\nLots of content.\nCONTRACT: {\"quota\": 5, \"achieved\": 4, \"gaps\": [\"x\"]}"
    data = cron_result.parse_contract_trailer(text)
    assert data == {"quota": 5, "achieved": 4, "gaps": ["x"]}


def test_parse_contract_trailer_ignores_when_not_last_line():
    # A CONTRACT line that is NOT the last non-blank line is not a trailer.
    text = "CONTRACT: {\"quota\": 1}\nActual report body follows."
    assert cron_result.parse_contract_trailer(text) is None


def test_parse_contract_trailer_tolerates_trailing_blank_lines():
    text = "body\nCONTRACT: {\"achieved\": 2}\n\n   \n"
    assert cron_result.parse_contract_trailer(text) == {"achieved": 2}


def test_parse_contract_trailer_bad_json_returns_none():
    assert cron_result.parse_contract_trailer("body\nCONTRACT: {not json}") is None


def test_parse_contract_trailer_none_and_empty():
    assert cron_result.parse_contract_trailer(None) is None
    assert cron_result.parse_contract_trailer("") is None
    assert cron_result.parse_contract_trailer("just a normal response") is None


def test_parse_contract_trailer_non_dict_payload_returns_none():
    assert cron_result.parse_contract_trailer("body\nCONTRACT: [1, 2, 3]") is None


def test_strip_contract_trailer_removes_trailer():
    text = "report body\nmore body\nCONTRACT: {\"achieved\": 1}\n"
    assert cron_result.strip_contract_trailer(text) == "report body\nmore body"


def test_strip_contract_trailer_noop_without_trailer():
    text = "report body\nmore body"
    assert cron_result.strip_contract_trailer(text) == text


def test_strip_contract_trailer_empty():
    assert cron_result.strip_contract_trailer("") == ""
    assert cron_result.strip_contract_trailer(None) == ""


def test_consecutive_misses_counts_only_trailing_zeros_for_name(ledger):
    # board-shepherd: ok, miss, miss  -> trailing streak 2
    cron_result.record_contract("board-shepherd", 1, 1, [], 0, "ok", ledger_path=ledger)
    cron_result.record_contract("other", 1, 0, [], 0, "miss", ledger_path=ledger)
    cron_result.record_contract("board-shepherd", 1, 0, [], 0, "miss", ledger_path=ledger)
    cron_result.record_contract("board-shepherd", 1, 0, [], 0, "miss", ledger_path=ledger)
    assert cron_result.consecutive_misses("board-shepherd", ledger) == 2
    assert cron_result.consecutive_misses("other", ledger) == 1


def test_consecutive_misses_breaks_on_success(ledger):
    cron_result.record_contract("c", 1, 0, [], 0, "miss", ledger_path=ledger)
    cron_result.record_contract("c", 1, 1, [], 0, "ok", ledger_path=ledger)  # breaks streak
    cron_result.record_contract("c", 1, 0, [], 0, "miss", ledger_path=ledger)
    assert cron_result.consecutive_misses("c", ledger) == 1


def test_consecutive_misses_missing_ledger_is_zero(tmp_path):
    assert cron_result.consecutive_misses("c", tmp_path / "absent.jsonl") == 0


def test_hard_floor_breaches_zero_achieved(ledger):
    cron_result.record_contract("a", 1, 1, [], 0, "ok", ledger_path=ledger)
    cron_result.record_contract("b", 1, 0, ["nothing"], 0, "miss", ledger_path=ledger)
    breaches = cron_result.hard_floor_breaches(ledger)
    names = {b["name"] for b in breaches}
    assert names == {"b"}
    assert breaches[0]["reason"] == "zero-achieved"


def test_hard_floor_breaches_consecutive_misses_threshold(ledger):
    # 3 consecutive misses but the LATEST run achieved>0 should still breach via streak? No:
    # latest achieved>0 resets the trailing streak. Build a true 3-miss streak instead.
    for _ in range(3):
        cron_result.record_contract("c", 5, 0, [], 0, "miss", ledger_path=ledger)
    breaches = cron_result.hard_floor_breaches(ledger, n=3)
    assert any(b["name"] == "c" and b["streak"] >= 3 for b in breaches)


def test_hard_floor_breaches_below_threshold_not_flagged(ledger):
    cron_result.record_contract("c", 5, 4, [], 0, "ok", ledger_path=ledger)
    cron_result.record_contract("c", 5, 0, [], 0, "miss", ledger_path=ledger)
    cron_result.record_contract("c", 5, 3, [], 0, "ok", ledger_path=ledger)  # latest ok, streak 0
    assert cron_result.hard_floor_breaches(ledger, n=3) == []


def test_iter_ledger_skips_corrupt_lines(ledger):
    cron_result.record_contract("c", 1, 1, [], 0, "ok", ledger_path=ledger)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write("\n")
    cron_result.record_contract("c", 1, 0, [], 0, "miss", ledger_path=ledger)
    # corrupt + blank lines are skipped; the two valid records survive
    recs = cron_result._iter_ledger_records(ledger)
    assert len(recs) == 2
    assert cron_result.consecutive_misses("c", ledger) == 1
