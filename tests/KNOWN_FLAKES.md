# Known flaky tests (fork main)

Purpose: stop lanes and review passes from re-triaging failures that are
already known flakes on a clean tree. Before owning a failure, check this
registry first — if it's listed here, cite the entry instead of re-deriving
the diagnosis.

## Ordering pollution warning

`-k` sweeps and partial selections pollute test ordering: fixtures and
module-level state leak differently than in a full run, producing failures
that do not reproduce (or extra passes that hide real failures). Before
declaring a test flaky or broken:

1. Reproduce against a **clean tree** (stash/branch aside your diff), or use
   `/test-triage`.
2. Run the failing test file **alone** (`venv/bin/pytest tests/path/test_x.py`)
   and as part of the full default selection — a failure that only appears in
   a `-k` sweep is ordering pollution, not a flake in the test itself.

## Adding an entry

An entry requires **evidence** — no evidence, no entry:

- Two failure occurrences on a clean tree (or one clean-tree failure plus a
  clean-tree pass of the same commit, proving nondeterminism).
- A link/path to the evidence: pytest output capture, CI run, or an audit
  report under `~/.hermes/audits/`.
- The suspected mechanism if known (timing, port reuse, tmp dir collision,
  ordering, external service).

Format: add a row to the inventory table below. Remove rows when the flake is
fixed (link the fixing commit in the removal commit message).

## Inventory

~7 known flakes on fork main, enumeration pending next full triage run — see
CLAUDE.local.md lore.

| Test | Symptom | Mechanism (suspected) | Evidence | First seen |
|------|---------|----------------------|----------|------------|
| _pending enumeration_ | — | — | — | — |
