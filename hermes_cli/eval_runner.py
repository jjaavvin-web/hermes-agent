#!/usr/bin/env python3
"""Lane-agnostic deterministic eval runner + regression gate (Card 70 / E1).

Loads a version-controlled corpus (eval/<lane>.jsonl), executes the lane against
each row's input, evaluates per-row MUST-PASS assertions, aggregates recall@k/
MRR/nDCG over the RESOLVED rows, appends a dashboard-compatible history row, and
exits with score_holdout-compatible codes so the nightly verifier lane
(learning-verify.sh) color logic is unchanged:

    exit 0  ok
    exit 2  AMBER  — per-case errors and/or unresolved (stale) target ids
    exit 3  RED    — >regression_pt recall@k/MRR drop vs the previous run for the
                     same lane, OR any must-pass assertion failure on a RESOLVED
                     row, OR a --neg-control discrimination failure

Design contracts (mirroring ~/.hermes/evals/recall/score_holdout.py):
  * STALE-ID DISCIPLINE — if a target_id does not resolve in memory.observations
    it is WARNed and EXCLUDED, never counted as a must-pass failure, so a DB
    rebuild can never produce a false RED. With NO trustworthy resolved signal
    at all (every target stale/unresolved, e.g. a full DB rebuild) the
    regression comparison is SUPPRESSED and the run degrades to AMBER (exit 2),
    never RED — a rebuild must never fake a recall regression.
  * The production recall ranker is imported lazily BY PATH inside the lane (never
    at module import) and is never invoked by the hermetic test, which
    monkeypatches `_recall` / `_fetch_contents`. No module imported here
    auto-connects to a DB at import time; nothing here opens a browser.
  * recall() returns no id, so targets/distractors are matched by NORMALIZED
    content (exactly like score_holdout.py).

The recall@k lane is deterministic but NOT hermetic: it requires the live
Supabase Postgres (:5434) + a CPU embedder + the out-of-repo production ranker,
so it runs in the verifier lane on the box, NOT in repo CI. pytest testpaths
exclude eval/ and the hermetic test stays fully monkeypatched.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

# --------------------------------------------------------------------------- #
# module-level config (all monkeypatchable from the hermetic test)
# --------------------------------------------------------------------------- #
RECALL_MODULE_PATH = Path("/home/josep/.hermes/mcp/recall/recall_at_dispatch.py")
HISTORY = Path("/home/josep/.hermes/state/learning-index/eval-runner-history.jsonl")

DEFAULT_K = 10
REGRESSION_PT = 0.05  # >5pt drop in recall@k or MRR vs the previous run => RED
NEG_CONTROL_MIN = 0.20
# Minimum resolved rows required to TRUST the gate. Below this floor there is no
# trustworthy resolved signal (a total stale-out, e.g. after a DB rebuild): the
# regression comparison is suppressed and the run is capped at AMBER, never RED.
RESOLVED_FLOOR = 1

EXIT_OK = 0
EXIT_AMBER = 2
EXIT_RED = 3

# Negative-control discrimination probes (reused from score_holdout.py): a
# degenerate/seeded index would score off-domain queries about as high as
# in-domain ones. Cheap canary, only run with --neg-control.
_IN_DOMAIN_PROBES = [
    "how should backups exclude credential files like auth.json and .env",
    "loki lane concurrency ceiling and the gateway async event-loop watchdog",
    "SSE dashboard endpoint authentication with a query token",
]
_OFF_DOMAIN_PROBES = [
    "best sourdough bread proofing time and temperature",
    "tomorrow's weekend weather forecast",
    "how to cast on stitches when knitting a wool scarf",
]


def _norm(s: str) -> str:
    return " ".join((s or "").split())


# --------------------------------------------------------------------------- #
# corpus loading
# --------------------------------------------------------------------------- #
def load_corpus(path: Path, split: str = "all") -> list[dict[str, Any]]:
    """Load eval/<lane>.jsonl. '#'-prefixed and blank lines are skipped. Filters
    by split ('train' | 'holdout' | 'all'). Validates the required keys."""
    if not path.exists():
        raise FileNotFoundError(f"corpus not found: {path}")
    rows: list[dict[str, Any]] = []
    for ln, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corpus line {ln} is not valid JSON: {exc}") from exc
        if "input" not in obj or "query" not in (obj.get("input") or {}):
            raise ValueError(f"corpus line {ln} missing input.query")
        if "target_id" not in obj:
            raise ValueError(f"corpus line {ln} missing target_id")
        obj.setdefault("qid", f"row-{ln:03d}")
        obj.setdefault("lane", "recall_at_k")
        obj.setdefault("split", "train")
        obj.setdefault("distractor_ids", [])
        obj.setdefault("assertions", [])
        if split != "all" and obj.get("split") != split:
            continue
        rows.append(obj)
    return rows


def load_meta(corpus_path: Path) -> dict[str, Any]:
    """Load the sidecar <corpus_stem>.meta.json gate config if present."""
    meta_path = corpus_path.with_name(corpus_path.stem + ".meta.json")
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# production ranker access (lazy; never imported at module scope; monkeypatched
# in tests so the live DB/embedder/network is never touched)
# --------------------------------------------------------------------------- #
def _load_ranker() -> Any:
    """Dynamic-import the production recall ranker BY PATH (same recipe as
    learning-recall-eval.py:127-139). Lazy so the hermetic test never triggers
    it. The module defines functions only; it does not connect at import."""
    if not RECALL_MODULE_PATH.exists():
        raise FileNotFoundError(f"production ranker not found: {RECALL_MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("recall_at_dispatch_eval", RECALL_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import ranker from {RECALL_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "recall") or not hasattr(module, "_run_readonly"):
        raise RuntimeError("recall_at_dispatch missing recall()/_run_readonly — wrong module")
    return module


async def _recall(query: str, k: int) -> list[dict[str, Any]]:
    """Call the production ranker read-only. Monkeypatched in tests."""
    module = _load_ranker()
    return await module.recall(query, k=k)


async def _fetch_contents(ids: list[str]) -> dict[str, str]:
    """Resolve observation ids -> normalized content (SELECT id=ANY). Returns
    only ids that exist. Monkeypatched in tests."""
    ids = [i for i in ids if i]
    if not ids:
        return {}
    module = _load_ranker()

    async def inner(conn: Any) -> dict[str, str]:
        rows = await conn.fetch(
            "SELECT id::text AS id, content FROM memory.observations WHERE id = ANY($1::uuid[])",
            ids,
        )
        return {r["id"]: _norm(str(r["content"] or "")) for r in rows}

    return await module._run_readonly(inner)


# --------------------------------------------------------------------------- #
# assertions
# --------------------------------------------------------------------------- #
def evaluate_assertions(
    row: dict[str, Any],
    ranked_contents: list[str],
    content_by_id: dict[str, str],
    rank: int | None,
) -> list[dict[str, Any]]:
    """Evaluate this row's must-pass assertions against the produced ranking.

    Only called for RESOLVED rows (target content known). `rank` is the 1-based
    position of the target in ranked_contents (None => not in top-k)."""
    target = content_by_id.get(row["target_id"])
    distractor_contents = {
        content_by_id[d] for d in row.get("distractor_ids", []) if content_by_id.get(d)
    }
    results: list[dict[str, Any]] = []
    for a in row.get("assertions", []):
        atype = a.get("type")
        if atype == "target_in_topk":
            k = int(a.get("k", DEFAULT_K))
            passed = rank is not None and rank <= k
            detail = f"rank={rank} k={k}"
        elif atype == "no_distractor_above_target":
            if rank is None:
                # target not surfaced: cannot claim a distractor is "above" it;
                # the target_in_topk assertion already carries the miss.
                passed, detail = True, "target not in top-k (deferred to target_in_topk)"
            else:
                above = [c for c in ranked_contents[: rank - 1] if c in distractor_contents]
                passed = not above
                detail = f"distractors above target={len(above)}"
        elif atype == "max_rank":
            value = int(a.get("value", DEFAULT_K))
            passed = rank is not None and rank <= value
            detail = f"rank={rank} max={value}"
        else:
            # unknown assertion type: FAIL CLOSED by design (passed=False => a
            # must-pass failure => RED). An unrecognized assertion must never
            # silently pass and fake green; surface it as a hard gate failure so
            # the corpus or the runner gets fixed.
            passed, detail = False, f"unknown assertion type {atype!r}"
        results.append({"type": atype, "passed": bool(passed), "detail": detail})
    return results


# --------------------------------------------------------------------------- #
# lane: recall_at_k
# --------------------------------------------------------------------------- #
async def _run_recall_lane(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score each row with the production ranker. Per-row result carries enough
    to aggregate + gate. Stale (unresolved) targets are flagged, not failed."""
    per: list[dict[str, Any]] = []
    for row in rows:
        qid = row.get("qid")
        query = row["input"]["query"]
        k = int(row["input"].get("k", DEFAULT_K))
        target = row["target_id"]
        distractors = list(row.get("distractor_ids", []))
        try:
            results = await _recall(query, k)
            ranked = [_norm(r.get("content", "")) for r in results]
            wanted = await _fetch_contents([target, *distractors])
            tgt = wanted.get(target)
            # Treat empty/whitespace-only resolved content as UNRESOLVED: a
            # content-less observation carries no checkable signal, so exclude it
            # (stale-id discipline => AMBER) rather than scoring it a must-pass
            # miss (false RED). tgt is already normalized by _fetch_contents, so
            # bool(tgt) is False for "" / whitespace-only content.
            resolved = bool(tgt)
            rank = None
            if resolved:
                for i, c in enumerate(ranked, 1):
                    if c == tgt:
                        rank = i
                        break
            assertions = (
                evaluate_assertions(row, ranked, wanted, rank) if resolved else []
            )
            must_pass_failed = [a for a in assertions if not a["passed"]]
            per.append({
                "qid": qid,
                "split": row.get("split"),
                "target_resolved": resolved,
                "rank": rank,
                "hit": 1 if rank else 0,
                "mrr": (1.0 / rank) if rank else 0.0,
                "ndcg": (1.0 / math.log2(rank + 1)) if rank else 0.0,
                "assertions": assertions,
                "must_pass_failed": [a["type"] for a in must_pass_failed],
                "error": None,
            })
        except Exception as exc:  # keep per-case failures inspectable (AMBER)
            per.append({
                "qid": qid,
                "split": row.get("split"),
                "target_resolved": False,
                "rank": None,
                "hit": 0,
                "mrr": 0.0,
                "ndcg": 0.0,
                "assertions": [],
                "must_pass_failed": [],
                "error": f"{type(exc).__name__}: {exc}",
            })
    return per


LANE_REGISTRY: dict[str, Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]] = {
    "recall_at_k": _run_recall_lane,
}


# --------------------------------------------------------------------------- #
# negative control (only with --neg-control)
# --------------------------------------------------------------------------- #
def _top1_cosine(results: list[dict[str, Any]]) -> float | None:
    """Recover top-1 cosine from recall()'s score (= cosine + bounded importance boost)."""
    if not results:
        return None
    row = results[0]
    if "score" not in row:
        return None
    try:
        imp = min(max(int(row.get("importance", 0) or 0), 0), 5)
        return float(row["score"]) - imp * 0.02
    except (TypeError, ValueError):
        return None


async def _neg_control_gap(threshold: float) -> dict[str, Any]:
    async def _max_top1(probes: list[str]) -> float | None:
        best = None
        for q in probes:
            cos = _top1_cosine(await _recall(q, 1))
            if cos is not None and (best is None or cos > best):
                best = cos
        return best

    in_max = await _max_top1(_IN_DOMAIN_PROBES)
    off_max = await _max_top1(_OFF_DOMAIN_PROBES)
    if in_max is None or off_max is None:
        return {"status": "skipped", "gap": None, "pass": True, "threshold": threshold}
    gap = round(in_max - off_max, 4)
    return {
        "status": "ok",
        "in_domain_top1": round(in_max, 4),
        "off_domain_top1": round(off_max, 4),
        "gap": gap,
        "threshold": threshold,
        "pass": gap >= threshold,
    }


# --------------------------------------------------------------------------- #
# aggregation + history + gate
# --------------------------------------------------------------------------- #
def aggregate(per: list[dict[str, Any]], k: int) -> dict[str, Any]:
    n = len(per)
    resolved = [p for p in per if p["target_resolved"]]
    nr = len(resolved) or 1  # avoid div0; n_target_resolved reported separately
    return {
        "k": k,
        "n": n,
        "n_target_resolved": len(resolved),
        "recall_at_k": round(sum(p["hit"] for p in resolved) / nr, 4),
        "mrr": round(sum(p["mrr"] for p in resolved) / nr, 4),
        "ndcg_at_k": round(sum(p["ndcg"] for p in resolved) / nr, 4),
        "must_pass_failures": sum(1 for p in resolved if p["must_pass_failed"]),
        "errors": sum(1 for p in per if p.get("error")),
    }


def _load_history() -> list[dict[str, Any]]:
    if not HISTORY.exists():
        return []
    return [
        json.loads(line)
        for line in HISTORY.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _find_previous(history_rows: list[dict[str, Any]], lane: str, split: str) -> dict[str, Any] | None:
    for row in reversed(history_rows):
        if row.get("lane") == lane and row.get("split") == split:
            return row
    return None


def _regression_alarms(agg: dict[str, Any], prev: dict[str, Any] | None, regression_pt: float) -> list[str]:
    if not prev:
        return []
    prev_agg = prev.get("agg", {})
    alarms = []
    for m in ("recall_at_k", "mrr"):
        if m in prev_agg and agg[m] < prev_agg[m] - regression_pt:
            alarms.append(f"{m} {prev_agg[m]:.3f}->{agg[m]:.3f}")
    return alarms


def _append_history(record: dict[str, Any]) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def run(
    *,
    lane: str,
    corpus: Path,
    split: str,
    no_history: bool,
    neg_control: bool,
    regression_pt: float,
    neg_control_gap: float,
    k_override: int | None,
) -> dict[str, Any]:
    if lane not in LANE_REGISTRY:
        raise SystemExit(f"unknown lane {lane!r}; known: {sorted(LANE_REGISTRY)}")
    rows = load_corpus(corpus, split=split)
    if k_override is not None:
        for row in rows:
            row["input"]["k"] = k_override
    k = k_override or (rows[0]["input"].get("k", DEFAULT_K) if rows else DEFAULT_K)

    executor = LANE_REGISTRY[lane]
    per = asyncio.run(executor(rows))
    agg = aggregate(per, k)

    history_rows = [] if no_history else _load_history()
    prev = None if no_history else _find_previous(history_rows, lane, split)
    # FALSE-RED guard (core stale-id invariant): with no trustworthy resolved
    # signal — every target unresolved/stale, e.g. after a DB rebuild — there is
    # nothing to regress against. Suppress the regression comparison; the exit
    # logic below then caps the run at AMBER so a rebuild can NEVER fake a RED.
    no_resolved_signal = agg["n"] > 0 and agg["n_target_resolved"] < RESOLVED_FLOOR
    alarms = [] if no_resolved_signal else _regression_alarms(agg, prev, regression_pt)
    if agg["must_pass_failures"]:
        failed = [p["qid"] for p in per if p.get("must_pass_failed")]
        alarms.append(
            f"must-pass assertion failure on {agg['must_pass_failures']} resolved row(s): "
            + ", ".join(str(q) for q in failed)
        )

    neg = None
    if neg_control:
        neg = asyncio.run(_neg_control_gap(neg_control_gap))

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lane": lane,
        "split": split,
        # holdout_file kept for dashboard_learning._classify_eval compatibility
        "holdout_file": str(corpus),
        "agg": agg,
        "alarms": alarms,
        "neg_control": neg,
        "per_query": per,
    }
    if not no_history:
        _append_history(record)

    n_unresolved = agg["n"] - agg["n_target_resolved"]
    neg_fail = bool(neg and not neg.get("pass", True))
    if no_resolved_signal:
        # Total stale-out: no trustworthy resolved signal to regress or gate on.
        # Cap at AMBER (must_pass_failures is 0 by construction here; alarms were
        # already suppressed). A rebuild can never produce a RED.
        exit_code = EXIT_AMBER
    elif alarms or neg_fail:
        exit_code = EXIT_RED
    elif agg["errors"] or n_unresolved:
        exit_code = EXIT_AMBER
    else:
        exit_code = EXIT_OK

    return {"record": record, "agg": agg, "alarms": alarms, "neg": neg,
            "n_unresolved": n_unresolved, "exit_code": exit_code}


def _print_human(result: dict[str, Any], lane: str, split: str) -> None:
    agg = result["agg"]
    print(
        f"eval[{lane}/{split}]: recall@{agg['k']}={agg['recall_at_k']} mrr={agg['mrr']} "
        f"ndcg@{agg['k']}={agg['ndcg_at_k']} resolved={agg['n_target_resolved']}/{agg['n']} "
        f"must_pass_failures={agg['must_pass_failures']} errors={agg['errors']}"
    )
    if result["n_unresolved"]:
        print(
            f"WARN: {result['n_unresolved']}/{agg['n']} target id(s) not in memory.observations "
            "(stale ids — excluded from metrics, not counted as misses)",
            file=sys.stderr,
        )
    neg = result["neg"]
    if neg is not None:
        print(
            f"NEG-CONTROL: status={neg['status']} gap={neg.get('gap')} "
            f"threshold={neg.get('threshold')} pass={neg.get('pass')}"
        )
    if result["alarms"]:
        print("ALARM: " + "; ".join(result["alarms"]), file=sys.stderr)
    print({EXIT_OK: "OK", EXIT_AMBER: "AMBER", EXIT_RED: "RED"}[result["exit_code"]])


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic eval runner + regression gate")
    parser.add_argument("--lane", default="recall_at_k", choices=sorted(LANE_REGISTRY),
                        help="lane to run (default: recall_at_k)")
    parser.add_argument("--corpus", type=Path, required=True, help="corpus jsonl path")
    parser.add_argument("--split", choices=["train", "holdout", "all"], default="holdout",
                        help="row split to score (gate default: holdout)")
    parser.add_argument("--neg-control", action="store_true",
                        help="also run the in/off-domain top-1 cosine discrimination gate")
    parser.add_argument("--no-history", action="store_true",
                        help="do not append to history and do not alarm against the previous run")
    parser.add_argument("--k", type=int, default=None, help="override top-k for every row")
    parser.add_argument("--json", action="store_true", help="print the full JSON record")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    meta = load_meta(args.corpus)
    regression_pt = float(meta.get("regression_pt", REGRESSION_PT))
    neg_gap = float(meta.get("neg_control_gap", NEG_CONTROL_MIN))
    result = run(
        lane=args.lane,
        corpus=args.corpus,
        split=args.split,
        no_history=args.no_history,
        neg_control=args.neg_control,
        regression_pt=regression_pt,
        neg_control_gap=neg_gap,
        k_override=args.k,
    )
    if args.json:
        print(json.dumps(result["record"], indent=2))
    else:
        _print_human(result, args.lane, args.split)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
