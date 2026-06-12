# h2reviewer post-2026-06-15 billing-split spec

Kanban card: `t_81faf967` (`infra-audit-2026-05`) — **PROVIDER-STACK h2reviewer workaround self-defeats after 2026-06-15 with no replacement path**.

## Problem statement

The current documented h2reviewer workaround is self-defeating after the Anthropic 2026-06-15 billing split.

`/home/josep/.hermes/PROVIDER-STACK.md:7-16` says Anthropic moves programmatic usage — `claude -p` / `claude --print`, the Agent SDK, Claude Code GitHub Actions, and third-party agents — off the Max subscription onto a separate API-priced "Agent SDK credit" pool on 2026-06-15. Only **interactive** Claude Code remains Max-subscription-safe. The same file currently records the h2reviewer workaround at `PROVIDER-STACK.md:76-79`: h2reviewer misroutes to paid Anthropic API, tracked as `t_b1719e96`, and the workaround was to route review work through a `claude -p` subprocess. That workaround becomes a metered route on 2026-06-15, so it no longer solves the paid-path problem.

The immediate broken path is not a Python source symbol called `h2reviewer`: a repo search for `h2reviewer` in `.py` source is expected to return zero hits. `h2reviewer` is selected as a Kanban dispatch **profile** by live config routing:

- `/home/josep/.hermes/config.yaml:721` lists `h2reviewer: code review, security audit, quality gate` in the triage profile list.
- `/home/josep/.hermes/config.yaml:742-745` routes `review|audit|validate|quality|security` to `profile: h2reviewer` with `confidence_threshold: 0.7`.
- `/home/josep/.hermes/profiles/h2reviewer/config.yaml:1-3` sets `model.default: claude-via-cli` and `provider: claude-cli-subprocess`.
- `/home/josep/.hermes/scripts/h2_claude_profile_runner.py:4-8` says `claude-cli-subprocess` is currently only a Kanban dispatch override label, not a normal `hermes chat` provider, and that the runner calls `claude --print`.
- `/home/josep/.hermes/scripts/h2_claude_profile_runner.py:81-104` builds the actual command: `claude --print --output-format text --no-session-persistence ...` plus optional `--model` / `--effort`.
- `/home/josep/.hermes/scripts/h2_claude_profile_runner.py:131-139` executes that command through `subprocess.run(...)`.

That runner did remove legacy Anthropic key fallback env vars (`h2_claude_profile_runner.py:22-38`) and instructs the model not to suggest `ANTHROPIC_API_KEY` (`h2_claude_profile_runner.py:57-66`). Those were good pre-split safety controls, but they do **not** make `claude --print` Max-safe after 2026-06-15. The subprocess itself becomes the metered surface.

## Three distinct review surfaces

### A. Adversarial critic / Codex reviewer — already migrated, not the problem

This is the OpenAI/Codex review lane, not h2reviewer.

Evidence:

- `/home/josep/.hermes/PROVIDER-STACK.md:46` assigns **Adversarial-diversity critic** to `gpt-5.5` via ChatGPT OAuth / Codex CLI.
- `/home/josep/.hermes/PROVIDER-STACK.md:56` assigns **Code review when Max quota exhausted** to `gpt-5.5 via codex CLI`.
- `agent/role_defaults.py:16-26` pins accepted Codex gpt-5.5 names and `ADVERSARIAL_CRITIC = "openai-codex/gpt-5.5"`.

Status: **not affected by the 2026-06-15 Anthropic split**. It does not call `claude -p`, `claude --print`, the Agent SDK, or the h2reviewer profile. It is already on the non-Anthropic, already-paid Codex lane and should remain available as the safe fallback/replacement reviewer when h2reviewer cannot run on a non-metered Anthropic path.

### B. h2reviewer Kanban profile path — broken path

This is the path that `t_81faf967` is about.

Evidence:

- Live triage profile list and routing: `/home/josep/.hermes/config.yaml:721` and `/home/josep/.hermes/config.yaml:742-745`.
- h2reviewer profile config: `/home/josep/.hermes/profiles/h2reviewer/config.yaml:1-3` sets `provider: claude-cli-subprocess`.
- Runner disclaimer: `/home/josep/.hermes/scripts/h2_claude_profile_runner.py:4-8` says the pseudo-provider is not a normal `hermes chat` provider and the runner calls `claude --print`.
- Runner command: `/home/josep/.hermes/scripts/h2_claude_profile_runner.py:81-104` builds `claude --print ...`; `/home/josep/.hermes/scripts/h2_claude_profile_runner.py:131-139` executes it.
- Kanban worker launch in repo: `hermes_cli/kanban_db.py:5089-5106` injects profile-scoped `HERMES_HOME`, and `hermes_cli/kanban_db.py:5134-5161` launches `hermes -p <assignee> --skills kanban-worker chat -q "work kanban task <id>"`. For tasks routed to `h2reviewer`, that means the h2reviewer profile config must resolve to a real Hermes runtime provider.
- Current repo provider support does not include `claude-cli-subprocess`: repo search for `claude-cli-subprocess` returned zero hits. `hermes_cli/providers.py:46-100` defines built-in Hermes overlays such as `openai-codex`, `google-gemini-cli`, `copilot-acp`, and `anthropic`; no `claude-cli-subprocess` overlay exists. `hermes_cli/auth.py (copilot-acp ProviderConfig registration, auth_type=external_process)` registers only `copilot-acp` as an external-process provider, and `hermes_cli/auth.py get_external_process_provider_status()` / `hermes_cli/auth.py resolve_external_process_provider_credentials()` resolve external-process providers through Copilot-specific env vars and error text. `agent/auxiliary_client.py (the `if pconfig.auth_type == "external_process"` branch handling `provider == "copilot-acp"`)` directly supports only `provider == "copilot-acp"` for external-process clients and warns unsupported for other external-process providers.
- `run_agent.py (RunAgent.__init__ acp_command/acp_args/api_mode params)` accepts `acp_command` / `acp_args` and known `api_mode` values, but the accepted API modes do not include a generic Claude CLI subprocess transport. `agent/transports/__init__.py:49-68` discovers only anthropic, codex, chat-completions, and bedrock transports.

Status: **affected and currently broken as a post-0615 replacement**. Before 2026-06-15, the standalone runner could appear to avoid paid Anthropic API keys by stripping key env vars, but after the split its `claude --print` invocation is itself metered. Meanwhile the profile points at a provider name that is not implemented as a real Hermes chat provider in the inspected repo. Therefore h2reviewer cannot be considered a safe Max-only review path until `claude-cli-subprocess` is productionized in Hermes and explicitly avoids `--print` / SDK surfaces.

### C. `agent/peer_review.py` in-pipeline Opus reviewer — separate interactive path, not the h2reviewer problem

This is the Codex pipeline auto-reviewer that runs an interactive Claude Code pane pool. It is separate from h2reviewer and from the `h2_claude_profile_runner.py` subprocess.

Evidence:

- `agent/peer_review.py:1-18` states the orchestrator uses warm tmux panes running interactive `claude`, and explicitly forbids `claude -p`, `claude --print`, and the Agent SDK because of the post-2026-06-15 billing constraint.
- `agent/peer_review.py:215-243` spawns `tmux new-session ... claude --model REVIEWER_MODEL --allowed-tools Read,Bash,Write --add-dir /tmp`.
- `agent/peer_review.py:355-370` sends a prompt to the interactive pane with `tmux send-keys`, not a `--print` subprocess.
- `agent/peer_review.py:385-421` polls for a verdict JSON file created by the interactive reviewer.
- `agent/role_defaults.py:13-14` pins `REVIEWER_MODEL = "opus"`.

Status: **not affected by the dated 2026-06-15 `claude --print` billing split**, because it is interactive Claude Code. It remains dependent on Max availability/quota and could still fail if interactive Max lapses or the pane cannot start, but it is not converted to the new Agent SDK credit pool by the June 15 split. Do not confuse this in-pipeline reviewer with h2reviewer profile dispatch.

## Required replacement design

The replacement must remove h2reviewer's dependency on `claude --print` / `claude -p` / Agent SDK and must not fall back to paid Anthropic API keys.

### Recommended concrete replacement

1. **Productionize `claude-cli-subprocess` as a real Hermes chat provider before reusing the h2reviewer profile.** This is the prerequisite sibling card `t_adf44216`.

   Exact repo surfaces to edit in that card:

   - `hermes_cli/providers.py`
     - Add a `HERMES_OVERLAYS["claude-cli-subprocess"]` entry with `auth_type="external_process"` and a new transport identifier, e.g. `transport="claude_cli_interactive"` or equivalent.
     - Add the transport mapping to `TRANSPORT_TO_API_MODE` if the provider is represented by an `api_mode`, or deliberately route it through a provider-specific client path if not.
     - Make `resolve_provider_full(...)` return this provider from the built-in overlay path, not only from live profile config.
   - `hermes_cli/auth.py`
     - Add `PROVIDER_REGISTRY["claude-cli-subprocess"]` with `auth_type="external_process"`.
     - Generalize `get_external_process_provider_status(...)` and `resolve_external_process_provider_credentials(...)` so they are not Copilot-only. Required config/env keys should be Claude-specific, for example `HERMES_CLAUDE_CLI_COMMAND` / `CLAUDE_CLI_PATH` and `HERMES_CLAUDE_CLI_ARGS`, while preserving existing Copilot keys for `copilot-acp`.
     - For this provider, default command should be `claude` and args must be **interactive-safe**; they must not include `--print`, `-p`, Agent SDK usage, or `ANTHROPIC_API_KEY` fallback.
   - `hermes_cli/runtime_provider.py`
     - Add a branch next to the current `provider == "copilot-acp"` handling (`runtime_provider.py:1261-1272`) that resolves `claude-cli-subprocess` credentials and returns provider/runtime details for the new provider.
   - `agent/auxiliary_client.py`
     - Extend the external-process branch (`agent/auxiliary_client.py (the `if pconfig.auth_type == "external_process"` branch handling `provider == "copilot-acp"`)`) so `claude-cli-subprocess` is directly supported instead of logging unsupported.
     - The client must interact with an interactive Claude CLI session/pane or other Max-safe interactive mechanism. It must not shell out to `claude --print` per request.
   - `run_agent.py` and `agent/transports/`
     - If the provider is exposed as an `api_mode`, add an explicit supported mode in `run_agent.py:1289-1320`, add a registered transport in `agent/transports/__init__.py:49-68`, and implement the transport/client handoff.
     - If the provider is implemented as a provider-specific client wrapper instead, document why no new `api_mode` is needed and add tests proving the normal `hermes chat` path can run with `--provider claude-cli-subprocess`.

   Exact live config keys to keep after the provider exists:

   ```yaml
   # ~/.hermes/profiles/h2reviewer/config.yaml
   model:
     default: claude-via-cli
     provider: claude-cli-subprocess
   ```

   Exact routing keys that should continue to select the profile:

   ```yaml
   # ~/.hermes/config.yaml
   triage:
     llm_classifier:
       prompt: |
         ...
         - h2reviewer: code review, security audit, quality gate
       routing_rules:
       - pattern: review|audit|validate|quality|security
         profile: h2reviewer
         confidence_threshold: 0.7
   ```

   The critical change is that `provider: claude-cli-subprocess` must resolve inside Hermes itself and must not depend on `h2_claude_profile_runner.py`'s `subprocess.run(["claude", "--print", ...])` call.

2. **Until that provider exists, change h2reviewer-dispatchable work to the already migrated Codex lane or loud escalation.**

   Safe interim choices:

   - Route review work to `openai-codex/gpt-5.5` / Codex CLI where lineage diversity is acceptable or where the goal is to keep a quality gate operational. This is already doctrine-sanctioned by `PROVIDER-STACK.md:46` and `PROVIDER-STACK.md:56`.
   - If the review specifically requires Anthropic/Opus semantics and no interactive Max-safe provider is available, return `ESCALATE` / needs-operator-review loudly instead of silently using `claude --print` or paid Anthropic API.

   Do **not** implement a fallback to `provider: anthropic`, `ANTHROPIC_API_KEY`, `claude -p`, `claude --print`, Agent SDK, GitHub Actions, or third-party-agent Anthropic paths. `agent/role_defaults.py:31-41` already encodes the doctrine: Anthropic rail is Max OAuth only, Anthropic API keys are disallowed, and plain `claude -p` is disallowed.

## Acceptance checks for the replacement implementation

When `t_adf44216` or a sibling implementation card lands, require these checks before treating h2reviewer as fixed:

1. `hermes --profile h2reviewer chat -q "ack" -Q` resolves `provider: claude-cli-subprocess` as a real Hermes provider and does not fall through to `provider: anthropic` or any API-key path.
2. Runtime logs and command capture show no `claude --print`, no `claude -p`, no Agent SDK call, and no `ANTHROPIC_API_KEY` / `ANTHROPIC_TOKEN` fallback.
3. A test review routed by `/home/josep/.hermes/config.yaml:742-745` completes through either the productionized Max-safe provider or the explicit Codex fallback / loud `ESCALATE` path.
4. Regression tests cover provider resolution for `claude-cli-subprocess`, external-process credential resolution that is not Copilot-specific, and a failure path that refuses paid Anthropic API fallback.
5. The old standalone runner is either deleted, made a wrapper around `hermes --profile h2reviewer chat`, or hard-blocked after 2026-06-15 with an error explaining that `claude --print` is metered.

## Cross-card notes

- `t_adf44216` (`infra-audit-2026-05`, status `ready`) is the sibling/prerequisite implementation card: **Migrate h2_claude_profile_runner.py to Hermes provider stack (off claude --print before 2026-06-15)**. It should productionize `claude-cli-subprocess` before h2reviewer is considered safe.
- `t_b1719e96` (`hermes-kanban-control`, status `done`) added `auth.disable_paid_api_fallback` and is not an open h2reviewer replacement-path card. `PROVIDER-STACK.md:91` still says `P7: Fix h2reviewer paid-API misroute (t_b1719e96)`, but that card is stale as a tracker for this work: it already exists as a completed credential-layer guard, and it is not present in `infra-audit-2026-05`. Treat `PROVIDER-STACK.md:91` as a stale/nonexistent tracker reference for the current h2reviewer post-0615 replacement path; use `t_81faf967` + `t_adf44216` instead.

## Paste-ready card update for `t_81faf967`

### Summary

Documented why the h2reviewer workaround self-defeats after the 2026-06-15 Anthropic split: `claude --print` / `claude -p` / Agent SDK usage moves to a separate paid API-priced pool while only interactive Claude Code remains Max-safe. The live h2reviewer route is a Kanban profile selected by `/home/josep/.hermes/config.yaml:721` and `:742-745`, with `/home/josep/.hermes/profiles/h2reviewer/config.yaml:1-3` pointing at `provider: claude-cli-subprocess`; the inspected runner `/home/josep/.hermes/scripts/h2_claude_profile_runner.py:81-104` still builds `claude --print`, so the workaround becomes metered after Jun 15.

### Recommended-action

Do not use h2reviewer / `h2_claude_profile_runner.py` as a post-0615 hedge until sibling card `t_adf44216` productionizes `claude-cli-subprocess` as a real Hermes chat provider. The concrete replacement is to implement `claude-cli-subprocess` across `hermes_cli/providers.py`, `hermes_cli/auth.py`, `hermes_cli/runtime_provider.py`, `agent/auxiliary_client.py`, and if needed `run_agent.py` / `agent/transports/`, then keep `~/.hermes/profiles/h2reviewer/config.yaml` on `model.default: claude-via-cli` + `provider: claude-cli-subprocess` and preserve the existing triage routing keys in `~/.hermes/config.yaml`. Until that lands, route review work to the already migrated `gpt-5.5` Codex lane (`PROVIDER-STACK.md:46`, `:56`) or loud `ESCALATE`; never fallback to `provider: anthropic`, `ANTHROPIC_API_KEY`, `claude -p`, `claude --print`, Agent SDK, GitHub Actions, or third-party Anthropic agent paths.

### Evidence

Primary evidence: `/home/josep/.hermes/PROVIDER-STACK.md:7-16` defines the Jun-15 split and `:76-79` names h2reviewer's paid-API misroute plus the now-stale `claude -p` workaround; `/home/josep/.hermes/PROVIDER-STACK.md:46` and `:56` show the distinct Codex/gpt-5.5 critic/reviewer lane is already migrated and not the problem; `/home/josep/.hermes/config.yaml:721` and `:742-745` show h2reviewer profile routing; `/home/josep/.hermes/profiles/h2reviewer/config.yaml:1-3` points h2reviewer at `claude-cli-subprocess`; `/home/josep/.hermes/scripts/h2_claude_profile_runner.py:4-8`, `:81-104`, and `:131-139` show the pseudo-provider disclaimer and `claude --print` subprocess call; `agent/peer_review.py:1-18`, `:215-243`, and `:355-421` show the separate in-pipeline reviewer uses interactive Claude/tmux rather than `--print`; `agent/role_defaults.py:31-41` forbids paid Anthropic fallback and plain `claude -p`; repo provider evidence (`hermes_cli/providers.py:46-100`, `hermes_cli/auth.py (copilot-acp ProviderConfig registration, auth_type=external_process)`, `hermes_cli/auth.py get_external_process_provider_status()`, `hermes_cli/auth.py resolve_external_process_provider_credentials()`, `agent/auxiliary_client.py (the `if pconfig.auth_type == "external_process"` branch handling `provider == "copilot-acp"`)`, `run_agent.py (RunAgent.__init__ acp_command/acp_args/api_mode params)`, `agent/transports/__init__.py:49-68`) shows `claude-cli-subprocess` is not yet a real Hermes provider. Cross refs: `t_adf44216` is the ready sibling migration card; `PROVIDER-STACK.md:91` cites stale tracker `t_b1719e96`, which is already a done credential-layer guard rather than the current h2reviewer replacement-path tracker.
