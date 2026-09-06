# Broker hydration root-fix rev2 report

## T1 summary
- Item 1 distinct mismatched live-binding path: YES — `hydrate_live_binding_mismatch` is restored as a refused ledger reason, does not inspect/remove disk, and regression-matrix test remained unmodified/pass.
- Item 2 subprocess timeout hardening: YES — status/rev-list/remove all use `timeout=25`; `TimeoutExpired` retains the worktree as non-clean/awaiting-harvest and does not crash/leak the broker lock.
- Item 3 security invariant tightening: YES — `test_f4_fail_closed_binding_guards_survive_merge` now matches quoted reason literals, not comment-only substrings.
- Item 4 full blast radius: RUN — exact `PYTHONPATH=. pytest tests/gateway tests/agent tests/security -q` exited 1 via pytest-timeout before final summary; collection count was 15,288 tests.
- Must-pass focused aggregate: PASS — `test_webhook_per_delivery_worktree.py + test_webhook_broker_regression_matrix.py + test_worktree_broker.py + tests/security` = 821 passed.
- Individual must-pass: per-delivery 12 passed; regression matrix 19 passed; worktree broker 39 passed; security 751 passed.
- Full blast observed blocker: pre-existing/off-scope agent timeout in `tests/agent/test_conversation_loop_failover.py::test_invalid_response_switches_to_fallback_and_rebuilds_request` during model metadata network lookup for `primary.example` / Ollama probe; not in touched files.
- Known base-preexisting list from packet/reviewer: `test_multiplex_http_routing` ImportError `_PROFILE_REJECTED`; `kanban_notifier`; `session_context`; `telegram_*` family.
- Additional broad gateway shard failures observed but not base-verified here: `test_agent_cache.py::...honcho_cache_busting...` and `test_reload_skills_command.py::test_dispatcher_routes_learn_to_agent_prompt`; both outside touched files.
- Optional adopted ledger row: NO — skipped to preserve unmodified regression-matrix expectation that the mismatch pass sees only one refused row.

## What changed

### Item 1 — mismatched live binding is refused, disk-inert

`gateway/platforms/webhook.py` now distinguishes three restart hydration cases:

1. Live-session scan failed (`_LIVE_SESSION_SCAN_FAILED`) -> append refused ledger row with reason `hydrate_scan_failure`; do not adopt.
2. Some live session exists with a different `worktree_path` -> append refused ledger row with reason `hydrate_live_binding_mismatch`; do not inspect status, do not run rev-list, do not remove; do not adopt.
3. Live-session scan succeeds with no live entries and the candidate was not previously adopted by this adapter -> stale-completion path (`hydrate_no_live_session`): clean remove, dirty/unknown awaiting harvest, no active broker capacity.

The adapter tracks `_hydrated_adoption_sids` so the existing regression-matrix sequence remains intact: first a matching live binding adopts, then a mismatched live binding refuses, then the same already-adopted candidate can hydrate when the store is empty. This preserves the matrix test without editing it while still preventing fresh stale/leaked candidates from wedging capacity.

Regression added:
- `test_hydration_refuses_live_binding_mismatch_without_touching_disk` asserts only `git worktree list` runs, the candidate directory remains, no disk-touch command is attempted, and ledger reason is `hydrate_live_binding_mismatch`.

### Item 2 — subprocess timeouts fail closed

The three new subprocess calls now have `timeout=25` and `subprocess.TimeoutExpired` handling:

- `git -C <child> status --porcelain` timeout -> returns non-clean/retain.
- `git -C <child> rev-list --count <base>..HEAD` timeout -> returns non-clean/retain.
- `git -C <repo> worktree remove <child>` timeout -> records awaiting-harvest/retain.

Regression added:
- `test_hydration_subprocess_timeout_retains_worktree_without_crash_or_lock_leak` parametrizes `status`, `rev-list`, and `remove`; asserts timeout arg is 25, worktree remains, event is `awaiting-harvest`, reason is `hydrate_no_live_session`, and `_wt_broker_lock` is acquirable afterward.

### Item 3 — comment cannot satisfy security pin

`tests/security/test_merge_invariants.py::test_f4_fail_closed_binding_guards_survive_merge` now extracts quoted string literals with regex and requires each refused reason in that literal list. A plain comment containing `hydrate_live_binding_mismatch` no longer satisfies the invariant.

## Verification evidence

Commands run from `/home/josep/.hermes/relay-wt/deliveries/wh-loki1-5cd3f804a172`:

| Command | Result | Evidence |
|---|---:|---|
| `git diff --check && python -m py_compile gateway/platforms/webhook.py tests/gateway/test_webhook_per_delivery_worktree.py tests/security/test_merge_invariants.py` | PASS | terminal output (no errors) |
| `PYTHONPATH=. pytest tests/gateway/test_webhook_per_delivery_worktree.py -q` | 12 passed | `audits/wave2-followups-20260704/pytest-webhook-per-delivery.log` |
| `PYTHONPATH=. pytest tests/gateway/test_webhook_broker_regression_matrix.py -q` | 19 passed | `audits/wave2-followups-20260704/pytest-webhook-broker-regression-matrix.log` |
| `PYTHONPATH=. pytest tests/agent/test_worktree_broker.py -q` | 39 passed | `audits/wave2-followups-20260704/pytest-worktree-broker-final.log` |
| `PYTHONPATH=. pytest tests/security -q` | 751 passed, 10 warnings | `audits/wave2-followups-20260704/pytest-security-final.log` |
| `PYTHONPATH=. pytest tests/gateway/test_webhook_per_delivery_worktree.py tests/gateway/test_webhook_broker_regression_matrix.py tests/agent/test_worktree_broker.py tests/security -q` | 821 passed, 9 warnings | terminal output |
| `PYTHONPATH=. pytest tests/gateway tests/agent tests/security -q` | Exit 1 via pytest-timeout before summary | `audits/wave2-followups-20260704/pytest-full-blast-final.log` |
| `PYTHONPATH=. pytest tests/gateway tests/agent tests/security --collect-only -q` | 15,288 tests collected | `audits/wave2-followups-20260704/pytest-full-blast-collect.log` |
| `env -u HERMES_WEBHOOK_WORKTREE -u HERMES_WEBHOOK_PER_DELIVERY_WT PYTHONPATH=. pytest tests/gateway -q --tb=short -ra` | 20 failed, 8871 passed, 15 skipped | `audits/wave2-followups-20260704/pytest-gateway-clean-env-rerun.log` |

## Full-blast observed failures / classification

Exact full-blast command did not reach a pytest final summary. It hit pytest-timeout while in an off-scope agent test:

- `tests/agent/test_conversation_loop_failover.py::test_invalid_response_switches_to_fallback_and_rebuilds_request`
- Stack: `AIAgent.__init__ -> ContextCompressor -> get_model_context_length -> _query_ollama_api_show -> httpx client.post -> socket.getaddrinfo`
- Network target in stack: `primary.example` / local model metadata probing path.
- Classification: off-scope/pre-existing environment/test isolation issue; no touched file is in the stack.

Gateway shard rerun with webhook worktree env unset reached a final summary and matched the packet's pre-existing failure families plus two extra off-scope gateway failures:

Known from packet/reviewer as base-preexisting:
- `tests/gateway/test_multiplex_http_routing.py::*` — ImportError `_PROFILE_REJECTED`.
- `tests/gateway/test_kanban_notifier.py::*` — notifier disabled via `kanban.dispatch_in_gateway=false`.
- `tests/gateway/test_session_context.py::*` — session env/contextvars contract drift.
- `tests/gateway/test_telegram_approval_buttons.py::*`, `test_telegram_model_picker.py::*`, `test_telegram_slash_confirm.py::*` — Telegram MarkdownV2 parse-mode expectation drift.

Observed additional off-scope gateway failures (not touched, not claimed base-verified here):
- `tests/gateway/test_agent_cache.py::TestExtractCacheBustingConfig::test_honcho_cache_busting_config_memoized_by_mtime`.
- `tests/gateway/test_reload_skills_command.py::test_dispatcher_routes_learn_to_agent_prompt`.

Security suite passed completely; no security stop gate tripped.

## Scope / non-actions

- Modified only approved source/test paths plus this audit directory.
- `tests/gateway/test_webhook_broker_regression_matrix.py` was read-only and unmodified.
- No service restart, no push, no PR, no credential/provider/config/security mutation, no ref-switching git command.
