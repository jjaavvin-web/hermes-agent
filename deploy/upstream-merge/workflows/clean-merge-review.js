export const meta = {
  name: 'v021-clean-merge-review',
  description: 'B8: semantic review of the 114 files that merge cleanly but were touched on both sides — confirm every fork behavioral edge survives and still executes',
  phases: [
    { title: 'Review', detail: 'tier A: 3 lenses per file; tier B/C: one reviewer' },
    { title: 'Refute', detail: 'tier A/B always; tier C only when findings exist' },
  ],
}

const AUD = '/home/josep/.hermes/audits/20260903T202249Z-v021-candidate'
const REPO = '/home/josep/.local/share/hermes-agent'
const BASE = 'f80f453ae067', FORK = '938b676d7ab8', UP = '29112bef0992'
const TREE = '70449d37f08f0e759f67ec77739c2a693131fbee'

const A = ['hermes_state.py', 'hermes_cli/gateway.py', 'gateway/session.py', 'agent/credential_pool.py', 'gateway/kanban_watchers.py']
const B = ['gateway/slash_commands.py', 'gateway/config.py', 'hermes_cli/providers.py', 'agent/estop.py', 'agent/file_safety.py', 'tools/registry.py', 'tools/subagent_worktree.py', 'hermes_cli/kanban_db.py']
const C = ['.gitignore','AGENTS.md','acp_adapter/server.py','agent/agent_runtime_helpers.py','agent/context_compressor.py','agent/conversation_compression.py','agent/error_classifier.py','agent/prompt_builder.py','agent/title_generator.py','agent/tool_executor.py','agent/trace_upload.py','agent/turn_finalizer.py','cli-config.yaml.example','cli.py','cron/jobs.py','hermes_cli/banner.py','hermes_cli/cli_agent_setup_mixin.py','hermes_cli/config.py','hermes_cli/goals.py','hermes_cli/kanban.py','hermes_cli/mcp_config.py','hermes_cli/model_normalize.py','hermes_cli/slash_exec.py','hermes_cli/status.py','hermes_state_common.py','model_tools.py','plugins/platforms/discord/adapter.py','plugins/platforms/matrix/adapter.py','plugins/platforms/telegram/adapter.py','run_agent.py','scripts/release.py','tests/agent/test_anthropic_adapter.py','tests/agent/test_auxiliary_client.py','tests/agent/test_credential_pool.py','tests/agent/test_error_classifier.py','tests/agent/test_model_metadata.py','tests/agent/test_skill_commands.py','tests/agent/test_skip_memory_store_65429.py','tests/agent/test_title_generator.py','tests/conftest.py','tests/gateway/test_config.py','tests/gateway/test_restart_resume_pending.py','tests/gateway/test_update_command.py','tests/gateway/test_webhook_adapter.py','tests/hermes_cli/test_active_sessions.py','tests/hermes_cli/test_api_key_providers.py','tests/hermes_cli/test_commands.py','tests/hermes_cli/test_doctor.py','tests/hermes_cli/test_gateway_service.py','tests/hermes_cli/test_kanban_db.py','tests/hermes_cli/test_profiles.py','tests/hermes_cli/test_update_autostash.py','tests/hermes_cli/test_update_hangup_protection.py','tests/hermes_cli/test_web_server.py','tests/run_agent/test_file_mutation_verifier.py','tests/run_agent/test_pre_compress_memory_context.py','tests/run_agent/test_run_agent_codex_responses.py','tests/test_estop.py','tests/test_mcp_serve.py','tests/test_packaging_metadata.py','tests/tools/test_delegate.py','tests/tui_gateway/test_goal_command.py','tools/async_delegation.py','tools/code_execution_tool.py','tools/environments/base.py','tools/environments/local.py','tools/mcp_tool.py','tools/x_search_tool.py','tools/xai_http.py','toolsets.py','trajectory_compressor.py','tui_gateway/server.py','web/src/App.tsx','web/src/i18n/af.ts','web/src/i18n/de.ts','web/src/i18n/en.ts','web/src/i18n/es.ts','web/src/i18n/fr.ts','web/src/i18n/ga.ts','web/src/i18n/hu.ts','web/src/i18n/it.ts','web/src/i18n/ja.ts','web/src/i18n/ko.ts','web/src/i18n/pt.ts','web/src/i18n/ru.ts','web/src/i18n/tr.ts','web/src/i18n/types.ts','web/src/i18n/uk.ts','web/src/i18n/zh-hant.ts','web/src/i18n/zh.ts','web/src/lib/api.ts','web/src/pages/EnvPage.tsx','web/src/pages/ModelsPage.tsx','web/src/pages/ProfilesPage.tsx','web/src/pages/SessionsPage.tsx','web/src/pages/SystemPage.tsx','web/src/pages/WebhooksPage.tsx','web/src/plugins/registry.ts','web/vitest.config.ts','website/docs/user-guide/configuration.md','website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/skills/bundled/research/research-research-paper-writing.md']
const FILES = [...A.map((p) => ({ path: p, tier: 'A' })), ...B.map((p) => ({ path: p, tier: 'B' })), ...C.map((p) => ({ path: p, tier: 'C' }))].map((f) => ({ ...f, slug: f.path.replace(/\//g, '__') }))
log(`files: A=${A.length} B=${B.length} C=${C.length} total=${FILES.length}`)

const REVIEW = {
  type: 'object',
  required: ['path', 'tier', 'verdict', 'fork_edges', 'rails', 'upstream_default_changes', 'findings', 'summary'],
  properties: {
    path: { type: 'string' }, tier: { type: 'string' },
    verdict: { type: 'string', enum: ['clean', 'needs_fix', 'needs_lead'] },
    fork_edges: { type: 'array', items: { type: 'object', required: ['desc', 'present', 'effective', 'evidence'], properties: { desc: { type: 'string' }, present: { type: 'boolean' }, effective: { type: 'boolean', description: 'still reachable/executed in the merged file (caller intact, not dead code)' }, evidence: { type: 'string' } } } },
    rails: { type: 'array', items: { type: 'object', required: ['row', 'status', 'evidence'], properties: { row: { type: 'integer' }, status: { type: 'string', enum: ['intact', 'weakened', 'missing', 'not_in_this_file'] }, evidence: { type: 'string' } } } },
    upstream_default_changes: { type: 'array', items: { type: 'string' }, description: 'any upstream change to a default that touches security, providers, spend, lifecycle, state schema, kanban' },
    findings: { type: 'array', items: { type: 'object', required: ['severity', 'desc', 'fix'], properties: { severity: { type: 'string', enum: ['P0', 'P1', 'P2', 'P3'] }, desc: { type: 'string' }, fix: { type: 'string' } } } },
    summary: { type: 'string' },
  },
}
const REFUTE = {
  type: 'object',
  required: ['path', 'verdict', 'disputed', 'missed', 'notes'],
  properties: {
    path: { type: 'string' },
    verdict: { type: 'string', enum: ['accept', 'amend'] },
    disputed: { type: 'array', items: { type: 'object', required: ['claim', 'why'], properties: { claim: { type: 'string' }, why: { type: 'string' } } } },
    missed: { type: 'array', items: { type: 'object', required: ['severity', 'desc', 'fix'], properties: { severity: { type: 'string', enum: ['P0', 'P1', 'P2', 'P3'] }, desc: { type: 'string' }, fix: { type: 'string' } } } },
    notes: { type: 'string' },
  },
}

const common = (f) => `
READ-ONLY RULES (absolute): never run git checkout/stash/reset/clean/fetch/merge/commit/add; never edit anything under ${REPO}; never open ~/.hermes/state.db or any *.db. The repo working tree is a STALE branch — never read files from it directly. Use only:
  git -C ${REPO} diff ${BASE} ${FORK} -- ${f.path}      (the FORK's own changes since merge base = the fork behavioral edges to protect)
  git -C ${REPO} diff ${BASE} ${UP} -- ${f.path}        (upstream v0.21 changes)
  git -C ${REPO} diff ${FORK} ${TREE} -- ${f.path}      (what the merge changes relative to serving — this is what will ship)
  git -C ${REPO} show ${TREE}:${f.path}                 (the merged result; this file merged CLEANLY so git flagged nothing)
  git -C ${REPO} show ${FORK}:${f.path} / ${UP}:${f.path} / ${BASE}:${f.path}
  git -C ${REPO} log --oneline ${BASE}..${FORK} -- ${f.path} (fork commits; git show <sha> for intent)
Rules of the program: ${AUD}/conflict-analysis/RULES.md (read first). Ledger rails for THIS file: ${AUD}/clean-merge-review/packets/${f.slug}.json (may have zero rows — then the fork edges from the diff are the rails).
`

const reviewPrompt = (f, lens) => `You are reviewing ONE file that merged CLEANLY in the Hermes v0.21 fork merge: ${f.path} (tier ${f.tier}${lens ? `, lens = ${lens}` : ''}). Clean merges are the silent-reversion zone: git kept both sides textually, but a fork edge can be dead (its caller rewritten), reordered, or semantically undone by upstream's surrounding rewrite.
${common(f)}
TASK: (1) Enumerate EVERY fork behavioral edge from the BASE..FORK diff (one entry each; for files with many trivial edges group by behavior). For each: is it present in the merged result AND still effective — i.e. the function is still called from the same place, the constant is still read, the check still runs before the bypass it guards? Cite merged-file line numbers. (2) For each ledger row in the packet: intact / weakened / missing with evidence. (3) List every upstream change to a default that touches security, providers/spend, lifecycle, state schema, kanban, dispatch caps. (4) Findings with severity and a concrete fix (P0 = a rail is gone or reordered; P1 = a rail is present but dead/unreachable; P2 = default drift needing a decision; P3 = cosmetic).
${lens === 'security' ? 'LENS: security — deny/taint/approval ordering, secret handling, auth boundaries, never-port 0e038425db (in hermes_cli/gateway.py locate exactly where the gateway lifecycle guard changed from env-marker to PID-file ownership and specify the counter-patch: any gateway-descendant process must be blocked from gateway stop/restart).' : ''}
${lens === 'state' ? 'LENS: state and lifecycle — SQLite open/close paths, WAL/checkpoint, migrations (are they additive-only? can the OLD binary still read the migrated DB?), FD/reader limits, repair loops, restart/resume, session persistence.' : ''}
${lens === 'liveness' ? 'LENS: does the fork edge still execute — trace every fork-added call from its entry point in the merged file; find callers renamed/removed by upstream; find fork code that became unreachable or shadowed by a new upstream definition of the same name.' : ''}
${f.tier === 'C' ? 'Tier C: be efficient — if the fork edges are trivial (i18n keys, doc lines, cosmetic), say so and return verdict=clean quickly with the evidence.' : ''}
Write your JSON to ${AUD}/clean-merge-review/out/${f.slug}${lens ? '.' + lens : ''}.json (create dirs if needed) and return it as structured output.`

phase('Review')
const results = await pipeline(
  FILES,
  async (f) => {
    if (f.tier === 'A') {
      const lenses = await parallel(['security', 'state', 'liveness'].map((lens) => () =>
        agent(reviewPrompt(f, lens), { label: `review:${f.path}:${lens}`, phase: 'Review', schema: REVIEW, model: 'sonnet', effort: 'high' })))
      const ok = lenses.filter(Boolean)
      if (!ok.length) return null
      return {
        path: f.path, tier: f.tier,
        verdict: ok.some((r) => r.verdict === 'needs_lead') ? 'needs_lead' : (ok.some((r) => r.verdict === 'needs_fix') ? 'needs_fix' : 'clean'),
        fork_edges: ok.flatMap((r) => r.fork_edges), rails: ok.flatMap((r) => r.rails),
        upstream_default_changes: [...new Set(ok.flatMap((r) => r.upstream_default_changes))],
        findings: ok.flatMap((r) => r.findings), summary: ok.map((r) => r.summary).join('\n---\n'),
      }
    }
    return agent(reviewPrompt(f, null), { label: `review:${f.path}`, phase: 'Review', schema: REVIEW, model: 'sonnet', effort: f.tier === 'B' ? 'high' : 'low' })
  },
  (review, f) => {
    if (!review) return null
    const must = f.tier !== 'C' || (review.findings || []).length > 0 || review.verdict !== 'clean'
    if (!must) return { path: f.path, tier: f.tier, review, refutation: null }
    return agent(`You are an adversarial skeptic. A reviewer claims the following about the cleanly-merged file ${f.path} (tier ${f.tier}). Refute it: are the "present/effective" claims true (re-trace the callers yourself)? Are the findings real, or is a P0/P1 overstated? Did the reviewer MISS a fork edge that is dead or reordered in the merged result? Default to verdict=amend when uncertain and be concrete.
${common(f)}
REVIEW UNDER TEST:
${JSON.stringify(review).slice(0, 60000)}
Write your JSON to ${AUD}/clean-merge-review/refute/${f.slug}.json (create dir if needed) and return it as structured output.`,
      { label: `refute:${f.path}`, phase: 'Refute', schema: REFUTE, model: 'sonnet', effort: 'high' })
      .then((r) => ({ path: f.path, tier: f.tier, review, refutation: r }))
  },
)

const done = results.filter(Boolean)
const notClean = done.filter((r) => r.review.verdict !== 'clean' || (r.review.findings || []).some((x) => x.severity === 'P0' || x.severity === 'P1'))
const amended = done.filter((r) => r.refutation && r.refutation.verdict === 'amend')
log(`reviewed ${done.length}/${FILES.length}; not-clean=${notClean.length}; refuter amendments=${amended.length}`)
if (done.length < FILES.length) log(`DROPPED: ${FILES.filter((f) => !done.some((d) => d.path === f.path)).map((f) => f.path).join(', ')}`)
return {
  reviewed: done.length, total: FILES.length,
  not_clean: notClean.map((r) => ({ path: r.path, tier: r.tier, verdict: r.review.verdict, findings: (r.review.findings || []).filter((x) => x.severity !== 'P3').map((x) => `${x.severity}: ${x.desc}`) })),
  refuter_amend: amended.map((r) => ({ path: r.path, missed: (r.refutation.missed || []).map((m) => `${m.severity}: ${m.desc}`), disputed: (r.refutation.disputed || []).map((d) => d.claim) })),
  upstream_default_changes: done.flatMap((r) => (r.review.upstream_default_changes || []).map((c) => `${r.path}: ${c}`)),
}