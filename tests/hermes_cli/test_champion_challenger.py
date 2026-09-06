"""Hermetic unit tests for hermes_cli.champion_challenger (E3 promotion gate).

Every variant is a synthetic in-memory ranker and ground-truth resolution is a
synthetic ``fetch_fn`` — so NO test touches the live DB, the network, or a
browser. The gate reuses eval_runner's lane/aggregate machinery; here we drive it
end-to-end through a real on-disk corpus + injected variants, exercising the
exact promotion rule:

    promote == challenger passes ALL must-pass on holdout
            AND recall@k delta >= margin
            AND no regression of a must-pass the champion passed

Cases: (a) beats champion by >= margin + clean must-pass => promote; (b) tie /
below-margin => reject; (c) HIGHER recall but a must-pass FAILURE => REJECT (the
critical hard-gate case); (d) regresses a must-pass the champion passed =>
reject; (e) total stale-out => abstain (never promote on no signal).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import champion_challenger as cc
from hermes_cli import eval_runner


# --------------------------------------------------------------------------- #
# synthetic corpus + injected variants/fetch (never touch DB/net/browser)
# --------------------------------------------------------------------------- #
def _row(qid: str, target_id: str, *, split: str = "holdout",
         distractors=None, assertions=None) -> dict:
    return {
        "qid": qid,
        "lane": "recall_at_k",
        "split": split,
        "input": {"query": f"q-{qid}", "k": 10},
        "target_id": target_id,
        "distractor_ids": distractors or [],
        "assertions": assertions or [{"type": "target_in_topk", "k": 10}],
        "tags": ["test"],
        "notes": "",
    }


def _write_corpus(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "# test corpus\n" + "".join(json.dumps(r) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


def _variant(name: str, content_by_id: dict[str, str],
             rankings: dict[str, list[str]]) -> cc.Variant:
    """A variant whose ranker maps a query to ordered *ids*, translated to the
    content strings a ranker actually returns (matches eval_runner's stub)."""

    async def rank_fn(query: str, k: int) -> list[str]:
        ids = rankings.get(query, [])
        return [content_by_id.get(i, f"phantom-{i}") for i in ids[:k]]

    return cc.Variant(name, rank_fn=rank_fn)


def _fetch_fn(content_by_id: dict[str, str]):
    """Ground-truth resolver shared by both variants. Ids absent from
    content_by_id simulate stale/unresolved targets (excluded, never failed)."""

    async def fetch(ids: list[str]) -> dict[str, str]:
        return {i: eval_runner._norm(content_by_id[i]) for i in ids if i in content_by_id}

    return fetch


# --------------------------------------------------------------------------- #
# (a) challenger beats champion by >= margin AND passes must-pass => PROMOTE
# --------------------------------------------------------------------------- #
def test_challenger_beats_margin_and_passes_promotes(tmp_path) -> None:
    rows = [_row(f"h{i}", f"t{i}") for i in range(5)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {f"t{i}": f"target {i}" for i in range(5)}
    # champion hits 3/5 (recall 0.6); challenger hits 5/5 (recall 1.0); delta 0.4.
    champ = _variant("prod", content,
                     {f"q-h{i}": ([f"t{i}"] if i < 3 else ["miss"]) for i in range(5)})
    chall = _variant("cand", content, {f"q-h{i}": [f"t{i}"] for i in range(5)})

    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.02,
                   fetch_fn=_fetch_fn(content))

    assert d.promote is True
    assert d.status == "promote"
    assert d.deltas["recall_at_k"] == pytest.approx(0.4)
    assert d.must_pass_challenger["all_pass"] is True
    assert d.regressed_qids == []


# --------------------------------------------------------------------------- #
# (b) tie / below-margin => REJECT
# --------------------------------------------------------------------------- #
def test_tie_rejects(tmp_path) -> None:
    rows = [_row(f"h{i}", f"t{i}") for i in range(5)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {f"t{i}": f"target {i}" for i in range(5)}
    rankings = {f"q-h{i}": [f"t{i}"] for i in range(5)}  # identical => delta 0
    champ = _variant("prod", content, rankings)
    chall = _variant("cand", content, rankings)

    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.02,
                   fetch_fn=_fetch_fn(content))

    assert d.promote is False
    assert d.status == "reject"
    assert d.deltas["recall_at_k"] == pytest.approx(0.0)
    assert "margin" in d.reason


def test_positive_but_below_margin_rejects(tmp_path) -> None:
    rows = [_row(f"h{i}", f"t{i}") for i in range(5)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {f"t{i}": f"target {i}" for i in range(5)}
    champ = _variant("prod", content,
                     {f"q-h{i}": ([f"t{i}"] if i < 4 else ["miss"]) for i in range(5)})
    chall = _variant("cand", content, {f"q-h{i}": [f"t{i}"] for i in range(5)})
    # real +0.2 gain, but a high margin makes it insufficient => reject.
    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.5,
                   fetch_fn=_fetch_fn(content))

    assert d.promote is False
    assert d.status == "reject"
    assert d.deltas["recall_at_k"] == pytest.approx(0.2)
    assert d.must_pass_challenger["all_pass"] is True
    assert "margin" in d.reason


# --------------------------------------------------------------------------- #
# (c) CRITICAL: HIGHER recall but a must-pass FAILURE => REJECT
# --------------------------------------------------------------------------- #
def test_higher_recall_but_must_pass_failure_rejects(tmp_path) -> None:
    # r0 carries a distractor; BOTH variants rank it above the target, so r0 is a
    # must-pass FAILURE for both (not a regression). The challenger additionally
    # hits r1+r2 the champion misses => challenger recall (1.0) > champion (0.33),
    # yet the hard must-pass gate must REJECT it.
    rows = [
        _row("r0", "t0", distractors=["d0"],
             assertions=[{"type": "target_in_topk", "k": 10},
                         {"type": "no_distractor_above_target"}]),
        _row("r1", "t1"),
        _row("r2", "t2"),
    ]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t0": "target 0", "d0": "distractor 0",
               "t1": "target 1", "t2": "target 2"}
    champ = _variant("prod", content,
                     {"q-r0": ["d0", "t0"], "q-r1": ["miss"], "q-r2": ["miss"]})
    chall = _variant("cand", content,
                     {"q-r0": ["d0", "t0"], "q-r1": ["t1"], "q-r2": ["t2"]})

    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.02,
                   fetch_fn=_fetch_fn(content))

    # challenger genuinely has the higher aggregate recall ...
    assert d.challenger_metrics["recall_at_k"] > d.champion_metrics["recall_at_k"]
    # ... but a must-pass failure is a HARD gate => REJECT, never promote.
    assert d.promote is False
    assert d.status == "reject"
    assert d.must_pass_challenger["all_pass"] is False
    assert "r0" in d.must_pass_challenger["failed_qids"]
    assert d.regressed_qids == []  # champion also failed r0 => not a regression
    assert "must-pass" in d.reason


# --------------------------------------------------------------------------- #
# (d) challenger REGRESSES a must-pass the champion passed => REJECT
# --------------------------------------------------------------------------- #
def test_regression_of_champion_pass_rejects(tmp_path) -> None:
    # r0: champion ranks target above distractor (PASS); challenger ranks the
    # distractor above the target (FAIL) => a regression on a row the champion
    # passed. Even though the challenger has higher overall recall, it's rejected.
    rows = [
        _row("r0", "t0", distractors=["d0"],
             assertions=[{"type": "target_in_topk", "k": 10},
                         {"type": "no_distractor_above_target"}]),
        _row("r1", "t1"),
        _row("r2", "t2"),
    ]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t0": "target 0", "d0": "distractor 0",
               "t1": "target 1", "t2": "target 2"}
    champ = _variant("prod", content,
                     {"q-r0": ["t0", "d0"], "q-r1": ["t1"], "q-r2": ["miss"]})
    chall = _variant("cand", content,
                     {"q-r0": ["d0", "t0"], "q-r1": ["t1"], "q-r2": ["t2"]})

    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.02,
                   fetch_fn=_fetch_fn(content))

    assert d.promote is False
    assert d.status == "reject"
    assert d.regressed_qids == ["r0"]
    assert d.challenger_metrics["recall_at_k"] > d.champion_metrics["recall_at_k"]
    assert "regress" in d.reason


# --------------------------------------------------------------------------- #
# (e) total stale-out => ABSTAIN (never promote on no signal)
# --------------------------------------------------------------------------- #
def test_total_stale_out_abstains(tmp_path) -> None:
    rows = [_row(f"h{i}", f"stale{i}") for i in range(4)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    # content_by_id EMPTY => no target resolves for either variant.
    champ = _variant("prod", {}, {f"q-h{i}": ["whatever"] for i in range(4)})
    chall = _variant("cand", {}, {f"q-h{i}": ["whatever"] for i in range(4)})

    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.02,
                   fetch_fn=_fetch_fn({}))

    assert d.promote is False
    assert d.status == "abstain"
    assert d.champion_metrics["n_target_resolved"] == 0
    assert d.challenger_metrics["n_target_resolved"] == 0
    assert "no signal" in d.reason or "no resolved" in d.reason


# --------------------------------------------------------------------------- #
# gate scores ONLY the holdout split (train is never gated)
# --------------------------------------------------------------------------- #
def test_only_holdout_split_is_scored(tmp_path) -> None:
    rows = [
        _row("h0", "t0", split="holdout"),
        _row("tr0", "t1", split="train"),
        _row("tr1", "t2", split="train"),
    ]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t0": "target 0", "t1": "target 1", "t2": "target 2"}
    rankings = {"q-h0": ["t0"], "q-tr0": ["t1"], "q-tr1": ["t2"]}
    champ = _variant("prod", content, rankings)
    chall = _variant("cand", content, rankings)

    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.02,
                   fetch_fn=_fetch_fn(content))

    assert d.champion_metrics["n"] == 1  # only the holdout row was scored
    assert d.provenance["n_rows"] == 1
    assert d.provenance["split"] == "holdout"


# --------------------------------------------------------------------------- #
# ledger append is additive JSONL, utf-8, round-trippable
# --------------------------------------------------------------------------- #
def test_append_decision_writes_jsonl(tmp_path, monkeypatch) -> None:
    ledger = tmp_path / "champion-challenger-decisions.jsonl"
    monkeypatch.setattr(cc, "DECISIONS_LEDGER", ledger)
    rows = [_row(f"h{i}", f"t{i}") for i in range(3)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {f"t{i}": f"target {i}" for i in range(3)}
    rankings = {f"q-h{i}": [f"t{i}"] for i in range(3)}
    champ = _variant("prod", content, rankings)
    chall = _variant("cand", content, rankings)

    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.02,
                   fetch_fn=_fetch_fn(content))
    path = cc.append_decision(d)
    assert path == ledger

    lines = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["promote"] is False  # tie => reject
    assert rec["status"] == "reject"
    assert set(("champion_metrics", "challenger_metrics", "deltas",
                "must_pass_champion", "must_pass_challenger",
                "generated_at", "provenance")) <= set(rec)
    # a second decision appends, never overwrites
    cc.append_decision(d)
    assert len([ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]) == 2


# --------------------------------------------------------------------------- #
# CLI: production-vs-production is identical => delta 0 < margin => REJECT,
# exit 0 (a decision was made), ledger written. Hermetic via monkeypatched seams.
# --------------------------------------------------------------------------- #
def test_cli_identical_variants_reject_exit_0(tmp_path, monkeypatch, capsys) -> None:
    ledger = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(cc, "DECISIONS_LEDGER", ledger)
    rows = [_row(f"h{i}", f"t{i}") for i in range(3)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {f"t{i}": f"target {i}" for i in range(3)}

    async def fake_recall(query: str, k: int):
        # query "q-hN" -> rank target "tN" first (a clean hit)
        qid = query.split("q-", 1)[-1]
        target = qid.replace("h", "t")
        return [{"content": content[target], "score": 0.9, "importance": 0}]

    async def fake_fetch(ids):
        return {i: eval_runner._norm(content[i]) for i in ids if i in content}

    # both variants are 'production' (rank_fn=None) => both use these seams =>
    # identical scores => delta 0 < margin(0.02) => the gate MUST reject.
    monkeypatch.setattr(eval_runner, "_recall", fake_recall)
    monkeypatch.setattr(eval_runner, "_fetch_contents", fake_fetch)

    rc = cc.main([
        "--corpus", str(corpus), "--champion", "production",
        "--challenger", "production", "--margin", "0.02", "--json",
    ])
    assert rc == cc.EXIT_OK  # a decision was made; non-zero is infra-only

    out = json.loads(capsys.readouterr().out)
    assert out["promote"] is False
    assert out["status"] == "reject"
    assert out["deltas"]["recall_at_k"] == 0.0

    written = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(written) == 1
    assert json.loads(written[0])["promote"] is False


# --------------------------------------------------------------------------- #
# (f) denominator-deflation guard: challenger errors => ABSTAIN, never promote
# --------------------------------------------------------------------------- #
def test_denominator_deflation_errors_abstains(tmp_path) -> None:
    """A challenger whose rank_fn raises on some holdout rows gets errors > 0 and
    resolves FEWER rows than the champion — its recall is inflated on the shrunken
    denominator. The guard must ABSTAIN even though challenger raw recall > champion
    recall, and must never PROMOTE."""
    rows = [_row(f"h{i}", f"t{i}") for i in range(5)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {f"t{i}": f"target {i}" for i in range(5)}

    # Champion: hits h0, h1 only (recall 2/5 = 0.40); all 5 rows resolve cleanly.
    champ = _variant("prod", content,
                     {f"q-h{i}": ([f"t{i}"] if i < 2 else ["miss"]) for i in range(5)})

    # Challenger: raises on h3 and h4 (errors=2, n_target_resolved=3).
    # On the 3 rows it CAN answer it hits all 3 (recall 3/3 = 1.0 — inflated).
    error_qids = {"q-h3", "q-h4"}

    async def erroring_rank_fn(query: str, k: int) -> list[str]:
        if query in error_qids:
            raise RuntimeError(f"simulated hard-query failure: {query!r}")
        idx = int(query.split("q-h")[1])
        return [content[f"t{idx}"]]

    chall = cc.Variant("cand-err", rank_fn=erroring_rank_fn)

    d = cc.compare(corpus, champ, chall, split="holdout", margin=0.02,
                   fetch_fn=_fetch_fn(content))

    # Challenger's measured recall (1.0) exceeds champion's (0.40) — the whole
    # point of the guard: a naive check would wrongly promote.
    assert d.challenger_metrics["recall_at_k"] > d.champion_metrics["recall_at_k"]
    # The guard must block promotion.
    assert d.promote is False
    assert d.status == "abstain"
    assert d.challenger_metrics["n_target_resolved"] < d.champion_metrics["n_target_resolved"]
    assert d.challenger_metrics["errors"] > 0
    assert "denominator-deflation" in d.reason or "unequal" in d.reason


# --------------------------------------------------------------------------- #
# (g) split guard: compare() on non-holdout split raises ValueError
# --------------------------------------------------------------------------- #
def test_compare_raises_on_non_holdout_split(tmp_path) -> None:
    """compare() must reject split='train' at the Python API layer so importers
    cannot accidentally gate on the challenger's tuning set."""
    rows = [_row("h0", "t0")]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t0": "target 0"}
    champ = _variant("prod", content, {"q-h0": ["t0"]})
    chall = _variant("cand", content, {"q-h0": ["t0"]})

    with pytest.raises(ValueError, match="split='holdout'"):
        cc.compare(corpus, champ, chall, split="train",
                   fetch_fn=_fetch_fn(content))


def test_cli_unknown_variant_is_infra_error(tmp_path, capsys) -> None:
    rows = [_row("h0", "t0")]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    rc = cc.main([
        "--corpus", str(corpus), "--champion", "production",
        "--challenger", "does-not-exist", "--no-ledger",
    ])
    assert rc == cc.EXIT_INFRA
    assert "INFRA-ERROR" in capsys.readouterr().err
