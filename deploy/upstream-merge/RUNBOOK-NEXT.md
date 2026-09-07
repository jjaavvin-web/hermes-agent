# Next upstream absorption — the harness that made v0.21 a program instead of a week
Everything here was used for the v2026.8.31 merge (audit 20260903T202249Z-v021-candidate). Paths inside the
workflow scripts are absolute and dated — search/replace the AUD dir, the worktree path, BASE/FORK/UP commits.

0. Size it first (10 s, no worktree): `git merge-tree --write-tree --no-messages <serving> <tag>` → conflicted
   file list + per-file hunk counts; `fork_line_survival.py <merged-tree> out.json conflicts.txt` on the dry-run tree.
1. Preflight: quick backup, tag hash assert vs origin, serving deployment byte-identical to git (hash every tracked file).
2. Worktree at the SERVING commit (never the operator checkout); `git merge --no-ff <tag>`.
3. Analyze (cheap models): `workflows/conflict-analysis.js` (one analyzer + one refuter per conflicted file, RULES.md
   binding) and `workflows/clean-merge-review.js` (both-touched clean files; tier A = 3 lenses). Run ≤6 concurrent
   agents; NEVER "resume" a big workflow (it re-runs finished agents) — launch fresh scoped ones.
4. Lead adjudicates: needs_lead + every refuter amend → `lead-overrides.json`; write LEAD-DECISIONS.md; rule 16
   (behavior-preserving merge) governs upstream policy drift → DIVERGENCES.md for josep after cutover.
5. Apply: `make_apply_packets.py` → `workflows/apply-resolutions.js` (sonnet-coder per file; agents never touch git;
   killed coder → `git checkout -m -- <file>`); then deterministic `verify_apply.py` (markers, compile, must-survive
   identifiers, hard assertions) + AST duplicate-def scan + `fork_line_survival.py <worktree>` count-aware.
6. Non-hunk review of the resolved conflict files (`workflows/nonhunk-review.js`) — the silent-reversion zone.
7. Parity manifest: `workflows/manifest-extension.js` proposes pins from the custody ledger; `build_manifest_extension.py`
   verifies every anchor deterministically; counts + in-repo docket + guard-test literals updated in lockstep.
8. Locks: `p4_locks.sh` (UV_PYTHON = the pinned runtime; diff vs BOTH parents; check CVE-fixed versions were not
   downgraded by `uv lock` preserving upstream's older pins → `uv lock --upgrade-package`).
9. Certify: security suite (collected ≥ floor), parity guard Git mode, merge invariants, redteam, preflight (5 checks),
   targeted rows, dashboard smoke with keys forced empty, ruff, full suite + `triage_against_baseline.py`,
   G1/G2 migration rehearsal on a COPY (old binary must read the migrated copy), mutation evidence (throwaway
   detached worktree; each mutation must turn a named pin RED), tombstone-inert proof, F821 fork-vs-candidate diff.
10. Build: `p6_build.sh <HEAD>` — deployment WITHOUT `--activate`; reproducible external TUI bundle (not activated);
    the `current` symlink, both HERMES_TUI_DIR pins, unit pins and restarts belong to the cutover gate.
