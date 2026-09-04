# Resolution rules for the v0.21 fork merge (binding for every analysis and resolution agent)

Identities: BASE (merge-base) = f80f453ae067 (tag v2026.8.13) · FORK (ours, serving) = 938b676d7ab8 · UPSTREAM (theirs) = 29112bef0992 (tag v2026.8.31, Hermes v0.21.0). Dry-run merged tree = see merge-dry-run/TREE. Read any version with: git -C /home/josep/.local/share/hermes-agent show <commit>:<path>. NEVER touch the working tree of that repo (stale branch). NEVER run checkout/stash/reset/clean/fetch/merge/commit.

General law
1. Never resolve a file wholesale "ours" or "theirs". Every hunk is resolved on intent: keep every fork behavioral edge (the ledger rows in your packet are the fork's rails for this file) AND take upstream's mechanics/fixes where they do not remove a rail.
2. A fork rail = any security/no-spend/isolation/lifecycle behavior the ledger marks PORT. Dropping one = FAIL. Weakening its ordering (e.g. a deny/taint check moved after a bypass) = FAIL.
3. Never-port commit 0e038425db (upstream loosened the gateway-lifecycle guard from env-marker to PID-file-ownership) lands via hermes_cli/gateway.py (clean merge) AND tools/terminal_tool.py (conflict). Fork semantics must win: ANY gateway-descendant process is blocked from gateway stop/restart. Flag any hunk that touches this.
4. hermes resume vs hermes unpause: fork law says ESTOP lift = hermes unpause. Any upstream string pointing users to "hermes resume" for estop is wrong and must be re-pointed (agent/estop.py has 5 such strings upstream).
5. tools/delegate_tool.py: keep FORK defaults 3 concurrent children / 50 iterations (upstream bumps to 10/250). Take upstream mechanics only.
6. Provider policy: no new upstream provider may be enabled by default; nothing may re-enable the metered anthropic provider or openrouter; disable_paid_api_fallback and select_anthropic_oauth_only stay; lane provider stays claude-cli-subprocess; never a paid API fallback.
7. Backup: fork secret exclusions (_should_exclude component walk; .env/auth.json/relay.secret/.key/.pem never in any backup) win. tests/hermes_cli/test_backup.py keeps the fork EXCLUSION assertions; upstream's test asserting .env/auth.json ROUND-TRIP (test_restores_secret_files_with_0600_perms or similar) is DROPPED, never adopted.
8. .github/workflows/tests.yml keeps the fork dashboard-smoke gate with API keys forced EMPTY (it hangs otherwise) and its 6-slice full-suite run.
9. Kanban is fully RETIRED live (tombstone dir at ~/.hermes/kanban.db, plugin disabled, no units/crons). In the merge: carry fork kanban code through unchanged where conflicts arise, never activate upstream's new kanban dispatch/swarm/PR/decompose surfaces by default, and flag any hunk that would open the board path unconditionally at startup (must be inert against a directory tombstone).
10. Discord adapter / web_server: re-anchor the fork Codex-lane pipeline seams and the dashboard truth/auth routes (GitNexus family, codex SSE, session-token checks) onto upstream's preserved anchors — never lose the auth posture of a route.
11. Security rail order inside check_all_command_guards (tools/approval.py): route-deny and credential-taint checks come BEFORE the yolo/mode-off bypass. The order IS the property.
12. Lock files (uv.lock, package-lock.json): do not hand-merge. Resolution = take upstream's file as the seed, then regenerate (uv lock / npm install --package-lock-only) in P4 and diff against both parents. Your job for these is only to list what pins changed on each side and flag anything non-registry or range-widening.
13. pyproject.toml / web/package.json: exact pins only (supply-chain policy); keep fork extras and fork devDeps; take upstream version bumps; flag any range operator.
14. Test files: treat each test-file conflict as a possible harness defect; an assertion is adopted only if it pins a behavior we want. Prefer keeping BOTH estates' tests when they do not contradict.
15. When a hunk cannot preserve both a fork rail and a needed upstream behavior, do NOT improvise a weakening: mark needs_lead=true and describe the exact edge.

Output discipline: cite line numbers in the marker file and in the fork/upstream versions; quote the exact identifiers (function names, constants) that must survive; propose the resolved text at hunk granularity when it is short (<40 lines), otherwise describe it precisely.

16. GOVERNING PRINCIPLE — BEHAVIOR-PRESERVING MERGE (lead, 2026-09-03): where upstream changed a DEFAULT or POLICY that alters live behavior on this install (memory writes from cron, cross-profile write guard, self-repo guard scope, dispatch caps, provider enablement, toolset denylists), the candidate keeps TODAY's live behavior unless the upstream change is a security fix or a bug fix. Every such divergence is listed in DIVERGENCES.md (what upstream changed, what we kept, how to flip it later) for josep to accept or reject AFTER cutover. A merge never changes live policy silently.

## Rule 17 — investigation agents are denied production ENTRY POINTS, not just production paths (added 2026-09-03 after a live incident)
A debugger that was told "never touch ~/.hermes" still ran a real in-place update against the live install: it called `cmd_update()` in-process and the code resolved the real home itself. Every dispatch prompt (debugger, tester, reviewer) carries this clause verbatim:
*"Never call any CLI/gateway entry point in-process or via a scratch script (no `cmd_*`, no `_cmd_*_impl`, no `gateway run/install`, no `hermes update`). Reproduce only through pytest with the repo's hermetic conftest. Any scratch script sets `HERMES_HOME` to a temp dir under the scratchpad and monkeypatches `Path.home`."*
Pure text generators (e.g. `generate_systemd_unit`) may be called only under those temp-home conditions. The P0 pins snapshot (`pins-before/`) is mandatory for the same reason: it made the staged restore a `cp`.

## Rule 18 — expect the stable class of red upstream tests, adapt the TEST never the rail
After every absorption: upstream tests asserting upstream POLICY (paid fallback for `provider=auto`, free-tier rings on by default) and upstream HOSTS (no WSL2 probe mock, Chrome present, port 9119 free, drain floors not shortened). Fork tests go stale against upstream refactors silently when a swallow-all `except` hides the mismatch — add a `logger.debug` at every swallow the merge touches. Provision the CI extras in the certification venv before G6 (`uv sync --locked --extra daytona --extra hindsight --extra mistral --extra parallel-web`).

