export const meta = {
  name: 'v021-nonhunk-review-throttled',
  description: 'B8-style semantic review of the auto-merged (non-hunk) regions of the 47 resolved conflict files, throttled to 6 concurrent agents to avoid server rate limits',
  phases: [{ title: 'Review' }, { title: 'Refute' }],
}
const AUD = '/home/josep/.hermes/audits/20260903T202249Z-v021-candidate'
const WT = '/home/josep/.local/share/hermes-agent-worktrees/v021-fork-merge'
const REPO = '/home/josep/.local/share/hermes-agent'
const BASE = 'f80f453ae067', FORK = '938b676d7ab8', UP = '29112bef0992'
const A = ['tools/approval.py','tools/terminal_tool.py','tools/file_tools.py','hermes_cli/backup.py','hermes_cli/auth.py','hermes_cli/web_server.py','hermes_cli/config_defaults.py','hermes_cli/runtime_provider.py','agent/codex_runtime.py','agent/codex_responses_adapter.py','hermes_cli/active_sessions.py','hermes_cli/main.py','gateway/run.py','cron/scheduler.py','tools/delegate_tool.py','agent/auxiliary_client.py','gateway/platforms/webhook.py']
const C = ['.github/workflows/tests.yml','agent/agent_init.py','agent/anthropic_adapter.py','agent/conversation_loop.py','agent/skill_commands.py','gateway/platforms/base.py','gateway/status.py','hermes_cli/commands.py','hermes_cli/cron.py','hermes_cli/curator.py','hermes_cli/models.py','hermes_cli/plugins.py','hermes_cli/setup.py','hermes_cli/tools_config.py','scripts/run_tests_parallel.py','tests/cli/test_cli_init.py','tests/cron/test_run_one_job.py','tests/hermes_cli/test_backup.py','tests/hermes_cli/test_gateway.py','tests/hermes_cli/test_runtime_provider_resolution.py','tests/hermes_cli/test_skills_hub.py','tests/hermes_cli/test_web_server_host_header.py','tests/tools/test_code_execution_modes.py','tests/tools/test_x_search_tool.py','tests/tui_gateway/test_protocol.py','tools/lazy_deps.py','tui_gateway/methods_tools.py','web/src/pages/CronPage.tsx','web/vite.config.ts','web/package.json','pyproject.toml']
const FILES = [...A.map((p) => ({ path: p, tier: 'A' })), ...C.map((p) => ({ path: p, tier: 'C' }))].map((f) => ({ ...f, slug: f.path.replace(/\//g, '__') }))
const REVIEW = { type: 'object', required: ['path', 'verdict', 'fork_edges', 'findings', 'upstream_default_changes', 'summary'], properties: {
  path: { type: 'string' }, verdict: { type: 'string', enum: ['clean', 'needs_fix', 'needs_lead'] },
  fork_edges: { type: 'array', items: { type: 'object', required: ['desc', 'present', 'effective', 'evidence'], properties: { desc: { type: 'string' }, present: { type: 'boolean' }, effective: { type: 'boolean' }, evidence: { type: 'string' } } } },
  findings: { type: 'array', items: { type: 'object', required: ['severity', 'desc', 'fix'], properties: { severity: { type: 'string', enum: ['P0', 'P1', 'P2', 'P3'] }, desc: { type: 'string' }, fix: { type: 'string' } } } },
  upstream_default_changes: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' } } }
const REFUTE = { type: 'object', required: ['path', 'verdict', 'disputed', 'missed', 'notes'], properties: { path: { type: 'string' }, verdict: { type: 'string', enum: ['accept', 'amend'] },
  disputed: { type: 'array', items: { type: 'object', required: ['claim', 'why'], properties: { claim: { type: 'string' }, why: { type: 'string' } } } },
  missed: { type: 'array', items: { type: 'object', required: ['severity', 'desc', 'fix'], properties: { severity: { type: 'string', enum: ['P0', 'P1', 'P2', 'P3'] }, desc: { type: 'string' }, fix: { type: 'string' } } } }, notes: { type: 'string' } } }
const common = (f) => `
READ-ONLY RULES (absolute): never run git checkout/stash/reset/clean/fetch/merge/commit/add; never edit ANY file (you are a reviewer); never open any *.db. The operator checkout ${REPO} is a stale branch — use it only for git show/diff/log.
The RESOLVED candidate file is committed in the worktree ${WT} (read ${WT}/${f.path} directly). Reference versions: git -C ${REPO} show ${BASE}:${f.path} | ${FORK}:${f.path} | ${UP}:${f.path}. Fork edges since base: git -C ${REPO} diff ${BASE} ${FORK} -- ${f.path}. What the candidate changed relative to the fork: git -C ${WT} diff ${FORK} HEAD -- ${f.path}. Fork commits: git -C ${REPO} log --oneline ${BASE}..${FORK} -- ${f.path}.
Program rules: ${AUD}/conflict-analysis/RULES.md (rule 16 = behavior-preserving merge). Hunk-level resolution already adjudicated (do NOT re-litigate): ${AUD}/conflict-analysis/out/${f.slug}.json + ${AUD}/conflict-analysis/LEAD-DECISIONS.md. Ledger rails: ${AUD}/conflict-analysis/packets/${f.slug}.json.
Be efficient: at most ~20 tool calls.`
const reviewOne = (f) => agent(`Review the AUTO-MERGED (non-hunk) regions of a resolved conflict file in the Hermes v0.21 fork merge: ${f.path} (tier ${f.tier}). Hunks were adjudicated separately; your scope is everything ELSE git merged silently.
${common(f)}
TASK: (1) enumerate every fork behavioral edge from the BASE..FORK diff (group trivial ones); for each: present in the resolved file AND still effective (caller intact, check still ahead of the bypass it guards)? cite line numbers. (2) each ledger row: intact / weakened / missing. (3) upstream changes to defaults touching security, providers/spend, lifecycle, state schema, kanban, dispatch caps. (4) findings with severity (P0 rail gone/reordered; P1 present but dead; P2 default drift; P3 cosmetic) + concrete fix. ${f.tier === 'A' ? 'Tier A: exhaustive, security lens, confirm check ORDER.' : 'Tier C: efficient; trivial edges → verdict=clean with evidence.'}
Write JSON to ${AUD}/nonhunk-review/out/${f.slug}.json (create dirs) and return it as structured output.`,
  { label: `review:${f.path}`, phase: 'Review', schema: REVIEW, model: 'sonnet', effort: f.tier === 'A' ? 'high' : 'low' })
const refuteOne = (f, review) => agent(`Adversarial skeptic: refute this review of the auto-merged regions of ${f.path} (tier ${f.tier}). Re-trace "present/effective" claims; are P0/P1 findings real? Did the reviewer MISS a dead or reordered fork edge? Default to amend when uncertain; be concrete.
${common(f)}
REVIEW UNDER TEST:
${JSON.stringify(review).slice(0, 50000)}
Write JSON to ${AUD}/nonhunk-review/refute/${f.slug}.json (create dirs) and return it as structured output.`,
  { label: `refute:${f.path}`, phase: 'Refute', schema: REFUTE, model: 'sonnet', effort: 'high' })

const results = []
const CHUNK = 6
for (let i = 0; i < FILES.length; i += CHUNK) {
  const chunk = FILES.slice(i, i + CHUNK)
  log(`chunk ${i / CHUNK + 1}/${Math.ceil(FILES.length / CHUNK)}: ${chunk.map((f) => f.path).join(', ')}`)
  const part = await parallel(chunk.map((f) => async () => {
    const review = await reviewOne(f)
    if (!review) return null
    const must = f.tier === 'A' || (review.findings || []).length > 0 || review.verdict !== 'clean'
    const refutation = must ? await refuteOne(f, review) : null
    return { path: f.path, tier: f.tier, review, refutation }
  }))
  results.push(...part.filter(Boolean))
}
const notClean = results.filter((r) => r.review.verdict !== 'clean' || (r.review.findings || []).some((x) => x.severity === 'P0' || x.severity === 'P1'))
log(`reviewed ${results.length}/${FILES.length}; not-clean=${notClean.length}`)
if (results.length < FILES.length) log(`DROPPED: ${FILES.filter((f) => !results.some((d) => d.path === f.path)).map((f) => f.path).join(', ')}`)
return { reviewed: results.length, total: FILES.length,
  not_clean: notClean.map((r) => ({ path: r.path, verdict: r.review.verdict, findings: (r.review.findings || []).filter((x) => x.severity !== 'P3').map((x) => `${x.severity}: ${x.desc.slice(0, 200)}`) })),
  refuter_amend: results.filter((r) => r.refutation && r.refutation.verdict === 'amend').map((r) => ({ path: r.path, missed: (r.refutation.missed || []).map((m) => `${m.severity}: ${m.desc.slice(0, 200)}`) })),
  default_changes: results.flatMap((r) => (r.review.upstream_default_changes || []).map((c) => `${r.path}: ${c.slice(0, 160)}`)) }