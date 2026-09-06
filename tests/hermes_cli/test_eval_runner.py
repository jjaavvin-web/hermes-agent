"""Hermetic unit tests for hermes_cli.eval_runner.

Mirrors the isolation shape of ~/.hermes/evals/recall/test_score_holdout.py:
the lane's recall()/content-fetch primitives are monkeypatched so the runner
NEVER touches the live DB, the network, or a browser. The conftest browser
guard also neuters webbrowser; the runner additionally only imports the
production ranker lazily inside _recall/_fetch_contents, which we replace.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import eval_runner


def _row(qid: str, target_id: str, *, split: str, distractors=None, assertions=None) -> dict:
    return {
        "qid": qid,
        "lane": "recall_at_k",
        "split": split,
        "input": {"query": f"q-{qid}", "k": 10},
        "target_id": target_id,
        "distractor_ids": distractors or [],
        "assertions": assertions or [
            {"type": "target_in_topk", "k": 10},
            {"type": "no_distractor_above_target"},
        ],
        "tags": ["test"],
        "notes": "",
    }


def _write_corpus(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "# test corpus\n" + "".join(json.dumps(r) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def isolate_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    history = tmp_path / "eval-runner-history.jsonl"
    monkeypatch.setattr(eval_runner, "HISTORY", history)
    return history


def _stub_lane(
    monkeypatch: pytest.MonkeyPatch,
    *,
    content_by_id: dict[str, str],
    rankings: dict[str, list[str]],
) -> None:
    """Stub the two production-ranker seams. `rankings` maps a query to the
    ordered list of *ids* the ranker would surface; we translate to the
    content strings recall() actually returns (it has no id). content_by_id
    holds ONLY resolvable ids — anything absent simulates a stale/unresolved id.
    Replacing these guarantees _load_ranker() (the live import) is never called.
    """

    async def fake_recall(query: str, k: int):
        ids = rankings.get(query, [])
        return [{"content": content_by_id.get(i, f"phantom-{i}"), "score": 0.9, "importance": 0}
                for i in ids[:k]]

    async def fake_fetch(ids: list[str]):
        return {i: eval_runner._norm(content_by_id[i]) for i in ids if i in content_by_id}

    monkeypatch.setattr(eval_runner, "_recall", fake_recall)
    monkeypatch.setattr(eval_runner, "_fetch_contents", fake_fetch)


def test_all_hit_corpus_exits_0(isolate_history, monkeypatch, tmp_path) -> None:
    rows = [_row("h1", "t1", split="holdout"), _row("h2", "t2", split="holdout")]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t1": "target one", "t2": "target two"}
    _stub_lane(monkeypatch, content_by_id=content,
               rankings={"q-h1": ["t1"], "q-h2": ["t2"]})

    rc = eval_runner.main(["--lane", "recall_at_k", "--corpus", str(corpus), "--split", "all"])
    assert rc == eval_runner.EXIT_OK


def test_target_in_topk_miss_on_resolved_row_exits_3(isolate_history, monkeypatch, tmp_path) -> None:
    rows = [_row("h1", "t1", split="holdout")]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t1": "target one"}
    # target resolves but never appears in the ranking => must-pass miss => RED
    _stub_lane(monkeypatch, content_by_id=content,
               rankings={"q-h1": ["other-a", "other-b"]})

    rc = eval_runner.main(["--corpus", str(corpus), "--split", "all"])
    assert rc == eval_runner.EXIT_RED


def test_distractor_above_target_exits_3(isolate_history, monkeypatch, tmp_path) -> None:
    rows = [_row("h1", "t1", split="holdout", distractors=["d1"])]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t1": "target one", "d1": "distractor one"}
    # distractor ranked ABOVE the target => no_distractor_above_target fails
    _stub_lane(monkeypatch, content_by_id=content,
               rankings={"q-h1": ["d1", "t1"]})

    rc = eval_runner.main(["--corpus", str(corpus), "--split", "all"])
    assert rc == eval_runner.EXIT_RED


def test_unresolved_target_excluded_not_failed(isolate_history, monkeypatch, tmp_path) -> None:
    # one resolved hit row + one row whose target id is NOT in content_by_id
    rows = [_row("h1", "t1", split="holdout"), _row("h2", "stale", split="holdout")]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t1": "target one"}  # 'stale' deliberately absent
    _stub_lane(monkeypatch, content_by_id=content,
               rankings={"q-h1": ["t1"], "q-h2": ["whatever"]})

    rc = eval_runner.main(["--corpus", str(corpus), "--split", "all", "--json", "--no-history"])
    # AMBER (stale id present), NOT RED: stale row excluded, never a must-pass fail
    assert rc == eval_runner.EXIT_AMBER

    # the resolved row drove the metric; the stale row is excluded from it
    result = eval_runner.run(
        lane="recall_at_k", corpus=corpus, split="all", no_history=True,
        neg_control=False, regression_pt=0.05, neg_control_gap=0.20, k_override=None,
    )
    agg = result["agg"]
    assert agg["n"] == 2
    assert agg["n_target_resolved"] == 1
    assert agg["recall_at_k"] == 1.0  # 1 hit / 1 resolved
    assert agg["must_pass_failures"] == 0
    stale = next(p for p in result["record"]["per_query"] if p["qid"] == "h2")
    assert stale["target_resolved"] is False
    assert stale["must_pass_failed"] == []


def test_total_stale_out_is_amber_not_red(isolate_history, monkeypatch, tmp_path) -> None:
    # FALSE-RED guard: a DB rebuild makes EVERY target unresolved. Even with a
    # strong previous run on record, the gate MUST degrade to AMBER (exit 2),
    # never RED (exit 3) — there is no trustworthy resolved signal to regress
    # against, so a rebuild can never fake a recall regression.
    history = isolate_history
    history.write_text(
        json.dumps({
            "ts": "prev", "lane": "recall_at_k", "split": "holdout",
            "agg": {"k": 10, "recall_at_k": 1.0, "mrr": 1.0},
        }) + "\n",
        encoding="utf-8",
    )
    rows = [_row(f"h{i}", f"stale{i}", split="holdout") for i in range(5)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    # content_by_id EMPTY => no target resolves (total stale-out)
    _stub_lane(monkeypatch, content_by_id={},
               rankings={f"q-h{i}": ["whatever"] for i in range(5)})

    rc = eval_runner.main(["--corpus", str(corpus), "--split", "holdout"])
    assert rc == eval_runner.EXIT_AMBER

    result = eval_runner.run(
        lane="recall_at_k", corpus=corpus, split="holdout", no_history=True,
        neg_control=False, regression_pt=0.05, neg_control_gap=0.20, k_override=None,
    )
    assert result["agg"]["n_target_resolved"] == 0
    assert result["agg"]["must_pass_failures"] == 0
    assert result["alarms"] == []  # regression comparison suppressed
    assert result["exit_code"] == eval_runner.EXIT_AMBER


def test_empty_content_target_treated_as_unresolved(isolate_history, monkeypatch, tmp_path) -> None:
    # one solid hit + one row whose target resolves to whitespace-only content.
    # The content-less observation must be EXCLUDED (unresolved/AMBER), not
    # scored a must-pass miss (which would be a false RED).
    rows = [_row("h1", "t1", split="holdout"), _row("h2", "blank", split="holdout")]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t1": "target one", "blank": "   "}  # 'blank' => content-less
    _stub_lane(monkeypatch, content_by_id=content,
               rankings={"q-h1": ["t1"], "q-h2": ["t1"]})

    result = eval_runner.run(
        lane="recall_at_k", corpus=corpus, split="all", no_history=True,
        neg_control=False, regression_pt=0.05, neg_control_gap=0.20, k_override=None,
    )
    assert result["agg"]["n"] == 2
    assert result["agg"]["n_target_resolved"] == 1  # blank excluded
    assert result["agg"]["must_pass_failures"] == 0
    assert result["exit_code"] == eval_runner.EXIT_AMBER
    blank = next(p for p in result["record"]["per_query"] if p["qid"] == "h2")
    assert blank["target_resolved"] is False
    assert blank["must_pass_failed"] == []


def test_split_holdout_scores_only_holdout_rows(isolate_history, monkeypatch, tmp_path) -> None:
    rows = [
        _row("h1", "t1", split="holdout"),
        _row("tr1", "t2", split="train"),
        _row("tr2", "t3", split="train"),
    ]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t1": "target one", "t2": "target two", "t3": "target three"}
    _stub_lane(monkeypatch, content_by_id=content,
               rankings={"q-h1": ["t1"], "q-tr1": ["t2"], "q-tr2": ["t3"]})

    result = eval_runner.run(
        lane="recall_at_k", corpus=corpus, split="holdout", no_history=True,
        neg_control=False, regression_pt=0.05, neg_control_gap=0.20, k_override=None,
    )
    assert result["agg"]["n"] == 1
    qids = {p["qid"] for p in result["record"]["per_query"]}
    assert qids == {"h1"}


def test_regression_vs_previous_run_exits_3(isolate_history, monkeypatch, tmp_path) -> None:
    history = isolate_history
    # seed a strong previous run for the same lane/split
    history.write_text(
        json.dumps({
            "ts": "prev", "lane": "recall_at_k", "split": "holdout",
            "agg": {"k": 10, "recall_at_k": 1.0, "mrr": 1.0},
        }) + "\n",
        encoding="utf-8",
    )
    rows = [_row(f"h{i}", f"t{i}", split="holdout",
                 assertions=[{"type": "target_in_topk", "k": 10}]) for i in range(10)]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {f"t{i}": f"target {i}" for i in range(10)}
    # only 5/10 now hit => recall 0.5, a >5pt drop from 1.0 => regression RED
    rankings = {f"q-h{i}": ([f"t{i}"] if i < 5 else ["miss"]) for i in range(10)}
    _stub_lane(monkeypatch, content_by_id=content, rankings=rankings)

    rc = eval_runner.main(["--corpus", str(corpus), "--split", "holdout"])
    assert rc == eval_runner.EXIT_RED


def test_max_rank_assertion(isolate_history, monkeypatch, tmp_path) -> None:
    rows = [_row("h1", "t1", split="holdout",
                 assertions=[{"type": "max_rank", "value": 1}])]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    content = {"t1": "target one", "x": "filler"}
    # target at rank 2 but max_rank requires <=1 => fail => RED
    _stub_lane(monkeypatch, content_by_id=content, rankings={"q-h1": ["x", "t1"]})
    rc = eval_runner.main(["--corpus", str(corpus), "--split", "all", "--no-history"])
    assert rc == eval_runner.EXIT_RED


def test_history_row_is_dashboard_compatible(isolate_history, monkeypatch, tmp_path) -> None:
    history = isolate_history
    rows = [_row("h1", "t1", split="holdout")]
    corpus = _write_corpus(tmp_path / "c.jsonl", rows)
    _stub_lane(monkeypatch, content_by_id={"t1": "target one"}, rankings={"q-h1": ["t1"]})

    eval_runner.main(["--corpus", str(corpus), "--split", "holdout"])
    written = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(written) == 1
    rec = written[0]
    # dashboard_learning._record_metric keys on agg.recall_at_k; _classify_eval on holdout_file
    assert isinstance(rec["agg"]["recall_at_k"], (int, float))
    assert rec["holdout_file"].endswith("c.jsonl")
    assert rec["lane"] == "recall_at_k"
