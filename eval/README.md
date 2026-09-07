# eval/ — deterministic eval corpora + regression gate

Version-controlled, deterministic eval corpora with explicit **per-row must-pass
assertions** and a **train/holdout split**, run by a lane-agnostic runner
(`hermes_cli/eval_runner.py`) as a regression gate in the nightly verifier lane.

This is the in-repo home for Card 70 / E1. The recall@k substrate already existed
end-to-end **outside** this repo (`~/.hermes/evals/recall/`, scored by
`score_holdout.py`, wired into `~/.hermes/scripts/learning-verify.sh`, surfaced on
the :9119 Learning panel). What is net-new here: a portable corpus + explicit
assertions + a train/holdout split living in `hermes-agent`, plus a generic runner
that exits with `score_holdout`-compatible codes (0 / 2 / 3) so the verifier
lane's color logic is unchanged.

## Deterministic-lanes-ONLY policy

Only **deterministic** lanes belong here as a **hard gate**. A lane qualifies if a
fixed corpus has a stable, checkable expected output (a known target id to rank, a
known classification, a known route-deny pattern).

- IN (hard gate): `recall_at_k` — fixed gold targets, deterministic ranker.
- OUT (advisory only, never a hard gate): generative / model-judged work —
  daily-digest, dream-reflect, and **fact-QA** (`~/.hermes/evals/recall/behavioral_factqa.py`
  is already tiered ADVISORY/AMBER with a McNemar test). Generative quality drifts
  with the model and must not RED a regression gate. Keep that boundary:
  **deterministic = hard gate; generative = advisory.**

Do **not** seed the gate from the operational recall canary
(`~/.hermes/scripts/recall-quality-canary.py`): it AUTO-SELECTS its target from
the live pool (a 10-min liveness probe), which reintroduces the structural-1.0
tautology its own docstring warns about. This corpus is a **fixed** gold set.

## recall@k is deterministic but NOT hermetic

The `recall_at_k` lane requires the live Supabase Postgres (`:5434`), a CPU
embedder, and the out-of-repo production ranker
(`~/.hermes/mcp/recall/recall_at_dispatch.py`); its `target_id`s are live MVMS
UUIDs. It therefore **cannot** run in `hermes-agent` pytest/ruff CI — it runs only
in the **verifier lane** on the box (`learning-verify.sh`). `pytest testpaths`
excludes `eval/` from collection; the corpus files are data, not packaged.

## Schema — `recall_at_k.jsonl`

One JSON object per line. `#`-prefixed and blank lines are ignored (matches
`load_corpus` and the upstream `load_holdout`).

```json
{
  "qid": "hold-01",
  "lane": "recall_at_k",
  "split": "holdout",
  "input": {"query": "<blind, operator-phrased query>", "k": 10},
  "target_id": "<live MVMS observation uuid>",
  "distractor_ids": ["<uuid>", "..."],
  "assertions": [
    {"type": "target_in_topk", "k": 10},
    {"type": "no_distractor_above_target"},
    {"type": "max_rank", "value": 3}
  ],
  "tags": ["wave1"],
  "notes": "leak-linted; distractor overlaps on 'source of truth'"
}
```

Queries are **blind** (no answer-token leakage) and pass the Jaccard leak linter
at `leak_threshold = 0.30`. Re-run the linter after any edit (read-only SELECT):

```
venv/bin/python ~/.hermes/scripts/learning-recall-eval.py --holdout <flat.jsonl> --lint-only
```

(The linter reads a flat top-level `query` field; flatten `input.query -> query`
first. The migrated rows are byte-identical to the proven
`~/.hermes/evals/recall/{holdout,holdout_wave2}.jsonl` and inherit leak-clean.)

### Assertion types (the must-pass contract)

- `target_in_topk` (`k`): the target is ranked within the top `k`. **Hard floor.**
- `no_distractor_above_target`: no planted hard-distractor outranks the target.
- `max_rank` (`value`): optional per-row tightness for very-easy rows (rank `<= value`).

An assertion that fails on a **RESOLVED** row is a must-pass failure => the gate
exits 3 (RED). A failing assertion on an **unresolved** (stale-id) row never
counts — see below.

## Train / holdout split (E3-ready)

- `split: "holdout"` — the **gate** set. The verifier runs `--split holdout`.
  These rows are hand-picked to be **currently clean** (every target hits @10 and
  no distractor outranks it per the live score), so the gate does not false-RED on
  the first run. Treat these as frozen ground truth; do not tune against them.
- `split: "train"` — the E3 champion/challenger tuning set. Includes deliberately
  **hard / currently-failing** rows (misses, distractor-above). They carry the
  same ideal assertions; making them pass is the E3 objective. A diagnostic
  `--split all` run is therefore EXPECTED to surface train-row must-pass failures
  (exit 3) — that is honest signal, **not** the gate. The gate is `--split holdout`.

Current corpus: 48 rows (9 holdout / 39 train), migrated from the 30-row
`holdout.jsonl` + 18-row `holdout_wave2.jsonl`.

## E3 — champion/challenger promotion gate (`hermes_cli/champion_challenger.py`)

The structural antidote to fake-green: **never ship a new ranker/prompt config
unless it provably beats the incumbent on the protected holdout split.** The gate
**recommends + records** a decision; it does **not** auto-mutate any live config
(promotion stays an explicit operator action).

### The variant abstraction (reuses the runner — no duplicated scoring)

A **variant** is a named scorer the corpus is run through. It reuses
`eval_runner._run_recall_lane` via a thin seam — the lane grew two optional,
keyword-only, default-`None` params (`recall_fn`, `fetch_fn`); with nothing
injected the runner's standalone behavior (E1) is byte-identical:

- `Variant(name, rank_fn=None)` — `rank_fn=None` is the **`production`** incumbent
  (eval_runner's own production ranker seam).
- A challenger injects `rank_fn(query, k) -> [content, …]` (an async ranker
  returning ordered content strings). The gate adapts it to the lane's
  `recall()`-shaped seam, so resolution, the must-pass assertions, and the
  recall@k/MRR/nDCG aggregation are the **same** code the nightly gate runs.
- `fetch_fn` (ground-truth id→content resolution) is the **same for both
  variants** and exists only so the gate's hermetic tests stay off the live DB.
  Because resolution is shared, the resolved-row set is identical across
  champion/challenger and the per-row regression check is well defined.

### The promotion rule (the heart of the deliverable)

Both variants are scored **ONLY on `--split holdout`** (train is the challenger's
tuning set — never gate on it). Then:

```
promote == challenger passes ALL its must-pass assertions on holdout
        AND (challenger.recall_at_k - champion.recall_at_k) >= margin
        AND challenger regresses NO must-pass assertion the champion passed
```

- **must-pass is a HARD gate.** A challenger with *higher* recall@k but *any*
  must-pass failure on a resolved holdout row is **REJECTED** — aggregate recall
  never outweighs a must-pass miss.
- **No regression.** If the challenger fails a must-pass on a row the champion
  passed, REJECT (reported with the offending qids), even if recall improved.
- **Margin.** Ties and below-margin gains REJECT (`>=`, so `delta == margin`
  promotes).
- **Abstain on no signal.** If *either* side has zero resolved holdout rows
  (total stale-out, e.g. a DB rebuild), the gate **ABSTAINs** — it can never
  promote on no signal (mirrors the runner's total-stale-out → AMBER).

### Decision record + ledger

`compare(...)` returns a `PromotionDecision` — `{promote, status (promote |
reject | abstain), reason, margin, champion_metrics, challenger_metrics, deltas,
must_pass_champion, must_pass_challenger, regressed_qids, generated_at,
provenance}` — and `append_decision(...)` appends it as one JSONL line
(`encoding='utf-8'`, additive, never overwrites) to the decisions ledger:

```
$HERMES_HOME/state/learning-index/champion-challenger-decisions.jsonl
```

A **verifier lane** records decisions by invoking the CLI (below) on a schedule
or on each candidate; the appended ledger row is the durable, auditable record of
every promote/reject/abstain (dashboards key off `promote`/`status`/`deltas`).

### CLI + exit codes

```
venv/bin/python -m hermes_cli.champion_challenger \
  --corpus eval/recall_at_k.jsonl --champion production \
  --challenger <name> --margin 0.02 [--json] [--no-ledger]
```

`--split` is **holdout-only** by construction. **Exit `0` == a decision was
made** (read `promote`/`status` for the verdict — `0` does *not* mean "promote");
non-zero (`1`) is an **infrastructure error only** (corpus missing, ranker import,
DB). Named challengers register in `VARIANT_REGISTRY` (or are injected directly in
tests); an unknown name is an infra error, never a silent pass.

### Conservatism smoke (champion vs champion)

Running `--champion production --challenger production` on holdout scores the
incumbent against itself: `delta == 0 < margin` ⇒ the gate outputs **REJECT** (it
cannot promote a no-op). On the live box this resolves 9/9 holdout rows at
recall@10 = 1.0 on both sides → `delta +0.0000 < 0.0200` → REJECT, exit 0 —
proof the gate is conservative.

### Local checks (no live DB)

```
venv/bin/pytest tests/hermes_cli/test_champion_challenger.py -p no:cacheprovider -q
venv/bin/ruff check hermes_cli/champion_challenger.py
```

The tests inject synthetic rankers + a synthetic `fetch_fn`, so they never touch
the DB, the network, or a browser.

## Gate thresholds — `recall_at_k.meta.json`

```json
{"k": 10, "recall_at_k_floor": 0.80, "regression_pt": 0.05, "neg_control_gap": 0.20, "gate_split": "holdout"}
```

- `recall_at_k_floor` (0.80) mirrors the `observability_slo` `recall_hit_rate` SLO
  target; surfaced for context. The **hard** floor is the per-row
  `target_in_topk` assertions, not this aggregate.
- `regression_pt` (0.05): a `> 5pt` drop in recall@k or MRR vs the previous run
  for the same lane/split => RED.
- `neg_control_gap` (0.20): minimum in-domain vs off-domain top-1 cosine
  separation when `--neg-control` is passed.

## Stale-id discipline (never blanket-RED)

If a `target_id` no longer resolves in `memory.observations` (e.g. a DB rebuild),
the row is **WARNed and EXCLUDED** — never counted as a must-pass failure (mirrors
`score_holdout.py`). Metrics aggregate over **resolved** rows only. A run with
unresolved targets is **AMBER (exit 2)**, not RED, so a rebuild cannot fake a
regression. This is the diff-attributable-only contract: the gate goes RED only on
a real recall regression or a real must-pass failure on a resolved row.

## Exit codes (score_holdout-compatible)

| code | meaning |
|------|---------|
| 0 | OK |
| 2 | AMBER — per-case errors and/or unresolved (stale) target ids |
| 3 | RED — `>regression_pt` recall@k/MRR drop, OR a must-pass failure on a resolved row, OR a `--neg-control` discrimination failure |

## How the verifier lane invokes the runner

`~/.hermes/scripts/learning-verify.sh` (a **separate repo**) runs the gate via the
same `$VENV_PY` it already uses, alongside the existing `score_holdout` step during
the transition window (so the gate is never weaker than today):

```sh
run_step recall_eval "$VENV_PY" -m hermes_cli.eval_runner \
  --lane recall_at_k \
  --corpus /home/josep/.local/share/hermes-agent/eval/recall_at_k.jsonl \
  --split holdout --neg-control
eval_rc=$?
# Fold eval_rc into the color logic at BOTH ends, mirroring recall_rc EXACTLY —
# wiring only one end lets an AMBER (exit 2) silently slip through to GREEN:
#   * add  eval_rc -eq 0  to the GREEN &&-chain  (GREEN requires eval OK)
#   * add  eval_rc -eq 3  to the RED   ||-chain  (RED on an eval regression)
# e.g.:
#   [ "$recall_rc" -eq 0 ] && [ "$eval_rc" -eq 0 ] && color=GREEN
#   { [ "$recall_rc" -eq 3 ] || [ "$eval_rc" -eq 3 ]; } && color=RED
# exit 2 is neither 0 nor 3, so it correctly lands as AMBER (never GREEN, never RED).
```

Wiring `learning-verify.sh` is a cross-repo apply step (done by the orchestrator),
not part of this repo's diff.

## Local checks (no live DB)

```
venv/bin/pytest tests/hermes_cli/test_eval_runner.py -p no:cacheprovider -q   # hermetic
venv/bin/ruff check hermes_cli/eval_runner.py                                  # PLW1514
```

The hermetic test monkeypatches the ranker seams (`_recall`, `_fetch_contents`),
so it never touches the DB, the network, or a browser. The live ranker is imported
lazily by path inside the lane and is never reached under test.
