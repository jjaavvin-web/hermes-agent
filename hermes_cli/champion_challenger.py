#!/usr/bin/env python3
"""Champion/challenger promotion gate (Card 70 / E3).

The structural antidote to fake-green: never ship a new ranker/prompt config
unless it PROVABLY beats the incumbent on the PROTECTED HOLDOUT split of the
deterministic eval corpus. This module RECOMMENDS + RECORDS a promote / reject /
abstain decision and appends it to a decisions ledger; it NEVER auto-mutates any
live config — promotion is an explicit operator action.

It reuses ``hermes_cli.eval_runner`` end-to-end; NO scoring is duplicated here. A
"variant" is just a named ranker the corpus is scored through. The ``production``
variant injects nothing and runs through eval_runner's own production seam; a
challenger injects an alternate async ranker ``rank_fn(query, k) -> [content]``.
Both variants are scored ONLY on ``--split holdout`` (the ``train`` split is the
challenger's tuning set and is never gated on).

PROMOTION RULE (the heart of the gate):
    promote == challenger passes ALL its must-pass assertions on holdout
            AND (challenger.recall_at_k - champion.recall_at_k) >= margin
            AND challenger regresses NO must-pass assertion the champion passed

A challenger with HIGHER recall but a must-pass FAILURE is REJECTED — must-pass is
a HARD gate, never outweighed by aggregate recall. Ties / below-margin => REJECT.
A total stale-out (no resolved holdout signal on either side) => ABSTAIN, so the
gate can never promote on no signal.

CLI:
    python -m hermes_cli.champion_challenger \
        --corpus eval/recall_at_k.jsonl --champion production \
        --challenger <name> --margin 0.02 [--json]

Exit 0 == a decision was made (read ``promote``/``status`` to see the verdict);
non-zero ONLY on an infrastructure error. Never opens a browser; every text I/O
is explicit ``encoding='utf-8'`` (hermes_cli/ is ruff PLW1514-scoped).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from hermes_cli import eval_runner

# A challenger injects this: an async ranker returning ordered content strings.
RankFn = Callable[[str, int], Awaitable[list[str]]]

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/josep/.hermes"))
DECISIONS_LEDGER = (
    HERMES_HOME / "state" / "learning-index" / "champion-challenger-decisions.jsonl"
)

DEFAULT_MARGIN = 0.02

EXIT_OK = 0
EXIT_INFRA = 1


# --------------------------------------------------------------------------- #
# variants
# --------------------------------------------------------------------------- #
@dataclass
class Variant:
    """A named scorer. ``rank_fn=None`` => the eval_runner production ranker."""

    name: str
    rank_fn: RankFn | None = None


# Named variants resolvable from the CLI. Challengers are registered
# programmatically (or injected directly in tests); ``production`` is the
# always-present incumbent that runs through eval_runner's own production seam.
VARIANT_REGISTRY: dict[str, Variant] = {
    "production": Variant("production", rank_fn=None),
}


def get_variant(name: str) -> Variant:
    if name not in VARIANT_REGISTRY:
        raise KeyError(f"unknown variant {name!r}; known: {sorted(VARIANT_REGISTRY)}")
    return VARIANT_REGISTRY[name]


def _recall_fn_from_variant(
    variant: Variant,
) -> Callable[[str, int], Awaitable[list[dict[str, Any]]]] | None:
    """Adapt a variant's ``(query, k) -> [content]`` ranker to eval_runner's
    ``(query, k) -> [{'content': ...}]`` recall seam. ``None`` => production
    seam (the incumbent), so the production variant is byte-identical to a plain
    eval_runner run."""
    if variant.rank_fn is None:
        return None
    rank_fn = variant.rank_fn

    async def _adapter(query: str, k: int) -> list[dict[str, Any]]:
        contents = await rank_fn(query, k)
        return [{"content": c, "score": 0.0, "importance": 0} for c in list(contents)[:k]]

    return _adapter


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #
@dataclass
class PromotionDecision:
    promote: bool
    status: str  # 'promote' | 'reject' | 'abstain'
    reason: str
    margin: float
    champion: str
    challenger: str
    split: str
    champion_metrics: dict[str, Any]
    challenger_metrics: dict[str, Any]
    deltas: dict[str, Any]
    must_pass_champion: dict[str, Any]
    must_pass_challenger: dict[str, Any]
    regressed_qids: list[str]
    generated_at: str
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# scoring (reuses eval_runner's lane + aggregate; no duplication)
# --------------------------------------------------------------------------- #
async def _score_variant(
    rows: list[dict[str, Any]],
    variant: Variant,
    fetch_fn: Callable[[list[str]], Awaitable[dict[str, str]]] | None,
) -> list[dict[str, Any]]:
    return await eval_runner._run_recall_lane(
        rows, recall_fn=_recall_fn_from_variant(variant), fetch_fn=fetch_fn
    )


def _must_pass_summary(per: list[dict[str, Any]]) -> tuple[set[str], list[str], bool]:
    """Per-variant must-pass rollup over RESOLVED rows only (unresolved/stale
    rows carry no checkable signal — eval_runner already excludes them). Returns
    ``(passed_qids, failed_qids, all_pass)``."""
    passed: set[str] = set()
    failed: list[str] = []
    for p in per:
        if not p.get("target_resolved"):
            continue
        if p.get("must_pass_failed"):
            failed.append(p["qid"])
        else:
            passed.add(p["qid"])
    return passed, failed, (len(failed) == 0)


def _metrics(agg: dict[str, Any]) -> dict[str, Any]:
    return {
        "recall_at_k": agg["recall_at_k"],
        "mrr": agg["mrr"],
        "ndcg_at_k": agg["ndcg_at_k"],
        "k": agg["k"],
        "n": agg["n"],
        "n_target_resolved": agg["n_target_resolved"],
        "must_pass_failures": agg["must_pass_failures"],
        "errors": agg["errors"],
    }


def compare(
    corpus: str | Path,
    champion: Variant,
    challenger: Variant,
    *,
    split: str = "holdout",
    margin: float = DEFAULT_MARGIN,
    generated_at: str | None = None,
    fetch_fn: Callable[[list[str]], Awaitable[dict[str, str]]] | None = None,
) -> PromotionDecision:
    """Score ``champion`` and ``challenger`` on the ``split`` rows of ``corpus``
    and return a PromotionDecision. ``split`` defaults to (and should stay)
    ``holdout`` — train is the challenger's tuning set, never the gate.

    ``fetch_fn`` overrides ground-truth content resolution; it exists ONLY so the
    gate's hermetic tests can stay off the live DB. In production it is left None,
    which uses eval_runner's live resolver. The same fetch resolves BOTH variants,
    so the resolved row set is identical across champion/challenger and the
    per-row regression comparison is well defined."""
    if split != "holdout":
        raise ValueError(
            f"champion/challenger gate must use split='holdout'; got {split!r} "
            "(train is the challenger's tuning set, not a gating split)"
        )
    corpus_path = Path(corpus)
    rows = eval_runner.load_corpus(corpus_path, split=split)
    k = rows[0]["input"].get("k", eval_runner.DEFAULT_K) if rows else eval_runner.DEFAULT_K
    generated_at = generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    async def _both() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        champ = await _score_variant(rows, champion, fetch_fn)
        chall = await _score_variant(rows, challenger, fetch_fn)
        return champ, chall

    champ_per, chall_per = asyncio.run(_both())

    champ_agg = eval_runner.aggregate(champ_per, k)
    chall_agg = eval_runner.aggregate(chall_per, k)

    champ_passed, champ_failed, champ_all_pass = _must_pass_summary(champ_per)
    chall_passed, chall_failed, chall_all_pass = _must_pass_summary(chall_per)

    # A regression is a row the champion passed but the challenger now fails.
    regressed_qids = sorted(champ_passed & set(chall_failed))

    delta_recall = round(chall_agg["recall_at_k"] - champ_agg["recall_at_k"], 6)
    delta_mrr = round(chall_agg["mrr"] - champ_agg["mrr"], 6)

    # ABSTAIN if EITHER side has no resolved signal: the challenger can't be
    # gated, and with no champion baseline the delta is meaningless. Never
    # promote on no signal (mirrors eval_runner's total-stale-out -> AMBER).
    no_signal = (
        champ_agg["n_target_resolved"] == 0 or chall_agg["n_target_resolved"] == 0
    )
    below_margin = delta_recall < margin

    # Unequal/errored signal guard: a challenger with errors on holdout rows has a
    # SHRUNKEN denominator, making its recall@k look artificially inflated
    # (denominator-deflation gaming). Similarly, fewer resolved rows means the
    # champion and challenger are not measured on the same set — the delta is not
    # apples-to-apples. Either condition (fewer resolved rows OR any errors on the
    # challenger side) forces ABSTAIN; never promote on an unequal or errored signal.
    # NOTE: champion errors likewise cannot benefit anyone — ABSTAIN is still
    # correct when champion has errors, because we check challenger-side here and
    # the no_signal guard above already catches the zero-resolved edge.
    unequal_or_errored = (
        chall_agg["n_target_resolved"] < champ_agg["n_target_resolved"]
        or chall_agg["errors"] > 0
    )

    if no_signal:
        status, promote = "abstain", False
        reason = (
            "ABSTAIN: no resolved holdout signal "
            f"(champion_resolved={champ_agg['n_target_resolved']} "
            f"challenger_resolved={chall_agg['n_target_resolved']}); "
            "refusing to promote on no signal"
        )
    elif unequal_or_errored:
        status, promote = "abstain", False
        reason = (
            "ABSTAIN: challenger resolved fewer holdout rows than champion or has "
            f"errors (champion_resolved={champ_agg['n_target_resolved']} "
            f"challenger_resolved={chall_agg['n_target_resolved']} "
            f"challenger_errors={chall_agg['errors']}); refusing to promote on "
            "unequal or errored signal (denominator-deflation guard)"
        )
    elif regressed_qids:
        status, promote = "reject", False
        # Non-regression must-pass failures: challenger also failed these, but the
        # champion did not pass them either (so they are not regressions). Include
        # them in the reason so the ledger record is self-complete.
        other_failures = sorted(set(chall_failed) - set(regressed_qids))
        reason = (
            "REJECT: challenger regresses a must-pass assertion the champion "
            f"passed on row(s) {regressed_qids}"
        )
        if other_failures:
            reason += f"; other must-pass failures: {other_failures}"
        reason += "; must-pass is a hard gate"
    elif not chall_all_pass:
        status, promote = "reject", False
        reason = (
            f"REJECT: challenger fails {len(chall_failed)} must-pass assertion(s) "
            f"on resolved holdout row(s) {sorted(chall_failed)}; must-pass is a "
            f"hard gate, not outweighed by recall (challenger "
            f"recall@{k}={chall_agg['recall_at_k']:.4f} vs champion "
            f"{champ_agg['recall_at_k']:.4f})"
        )
    elif below_margin:
        status, promote = "reject", False
        reason = (
            f"REJECT: recall@{k} delta {delta_recall:+.4f} < margin {margin:.4f} "
            f"(champion {champ_agg['recall_at_k']:.4f} -> challenger "
            f"{chall_agg['recall_at_k']:.4f})"
        )
    else:
        status, promote = "promote", True
        reason = (
            "PROMOTE: challenger passes all must-pass and beats champion "
            f"recall@{k} by {delta_recall:+.4f} >= margin {margin:.4f} "
            f"(champion {champ_agg['recall_at_k']:.4f} -> challenger "
            f"{chall_agg['recall_at_k']:.4f})"
        )

    return PromotionDecision(
        promote=promote,
        status=status,
        reason=reason,
        margin=margin,
        champion=champion.name,
        challenger=challenger.name,
        split=split,
        champion_metrics=_metrics(champ_agg),
        challenger_metrics=_metrics(chall_agg),
        deltas={"recall_at_k": delta_recall, "mrr": delta_mrr},
        must_pass_champion={
            "all_pass": champ_all_pass,
            "failures": len(champ_failed),
            "failed_qids": sorted(champ_failed),
        },
        must_pass_challenger={
            "all_pass": chall_all_pass,
            "failures": len(chall_failed),
            "failed_qids": sorted(chall_failed),
        },
        regressed_qids=regressed_qids,
        generated_at=generated_at,
        provenance={
            "corpus": str(corpus_path),
            "split": split,
            "k": k,
            "n_rows": len(rows),
            "runner": "hermes_cli.eval_runner._run_recall_lane",
            "gate": "hermes_cli.champion_challenger.compare",
        },
    )


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def append_decision(decision: PromotionDecision, ledger: str | Path | None = None) -> Path:
    """Append the decision to the JSONL decisions ledger (additive; never
    overwrites). The gate RECORDS — it does not mutate any live config."""
    path = Path(ledger) if ledger is not None else DECISIONS_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(decision.to_dict()) + "\n")
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_human(d: PromotionDecision) -> None:
    cm, hm = d.champion_metrics, d.challenger_metrics
    print(
        f"champion={d.champion} challenger={d.challenger} "
        f"split={d.split} margin={d.margin}"
    )
    print(
        f"  champion : recall@{cm['k']}={cm['recall_at_k']} mrr={cm['mrr']} "
        f"resolved={cm['n_target_resolved']}/{cm['n']} "
        f"must_pass_failures={cm['must_pass_failures']}"
    )
    print(
        f"  challenger: recall@{hm['k']}={hm['recall_at_k']} mrr={hm['mrr']} "
        f"resolved={hm['n_target_resolved']}/{hm['n']} "
        f"must_pass_failures={hm['must_pass_failures']}"
    )
    print(
        f"  delta recall@k={d.deltas['recall_at_k']:+.4f} "
        f"mrr={d.deltas['mrr']:+.4f}"
    )
    print(f"PROMOTE={d.promote} STATUS={d.status.upper()}")
    print(d.reason)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Champion/challenger promotion gate (holdout-only)"
    )
    p.add_argument("--corpus", type=Path, required=True, help="corpus jsonl path")
    p.add_argument("--champion", default="production", help="incumbent variant name")
    p.add_argument("--challenger", required=True, help="candidate variant name")
    # holdout-only by construction: train is the challenger's tuning set.
    p.add_argument(
        "--split",
        choices=["holdout"],
        default="holdout",
        help="ONLY holdout is gateable (train is the challenger's tuning set)",
    )
    p.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                   help="minimum recall@k improvement to promote (default 0.02)")
    p.add_argument("--no-ledger", action="store_true",
                   help="do not append the decision to the ledger")
    p.add_argument("--json", action="store_true", help="print the full JSON decision")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        champion = get_variant(args.champion)
        challenger = get_variant(args.challenger)
        decision = compare(
            args.corpus, champion, challenger, split=args.split, margin=args.margin
        )
        if not args.no_ledger:
            append_decision(decision)
    except Exception as exc:  # infra error only (corpus missing, ranker import, DB)
        print(f"INFRA-ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INFRA
    if args.json:
        print(json.dumps(decision.to_dict(), indent=2))
    else:
        _print_human(decision)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
