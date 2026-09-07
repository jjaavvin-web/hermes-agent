export const meta = {
  name: 'v021-conflict-analysis',
  description: 'Analyze all 50 v0.21 merge conflicts hunk-by-hunk from the read-only dry-run tree, then adversarially refute each proposed resolution',
  phases: [
    { title: 'Analyze', detail: 'one sonnet agent per conflicted file: every hunk, both intents, proposed resolution' },
    { title: 'Refute', detail: 'independent sonnet skeptic per file: does any resolution drop a fork rail?' },
  ],
}

const AUD = '/home/josep/.hermes/audits/20260903T202249Z-v021-candidate'
const REPO = '/home/josep/.local/share/hermes-agent'
const BASE = 'f80f453ae067', FORK = '938b676d7ab8', UP = '29112bef0992'
const TREE = '70449d37f08f0e759f67ec77739c2a693131fbee'

// wave 1 = security-critical (plan P2 step 2); wave 2 = mechanical
const FILES = [
  ['tools/approval.py',1],['tools/terminal_tool.py',1],['tools/file_tools.py',1],['hermes_cli/backup.py',1],
  ['hermes_cli/auth.py',1],['hermes_cli/web_server.py',1],['hermes_cli/config_defaults.py',1],['hermes_cli/runtime_provider.py',1],
  ['gateway/platforms/webhook.py',1],['agent/codex_runtime.py',1],['agent/codex_responses_adapter.py',1],['hermes_cli/active_sessions.py',1],
  ['hermes_cli/main.py',1],['pyproject.toml',1],['uv.lock',1],['package-lock.json',1],['web/package.json',1],
  ['.github/workflows/tests.yml',2],['agent/agent_init.py',2],['agent/anthropic_adapter.py',2],['agent/auxiliary_client.py',2],
  ['agent/conversation_loop.py',2],['agent/skill_commands.py',2],['cron/scheduler.py',2],['gateway/platforms/base.py',2],
  ['gateway/run.py',2],['gateway/status.py',2],['hermes_cli/commands.py',2],['hermes_cli/cron.py',2],['hermes_cli/curator.py',2],
  ['hermes_cli/models.py',2],['hermes_cli/plugins.py',2],['hermes_cli/setup.py',2],['hermes_cli/tools_config.py',2],
  ['scripts/run_tests_parallel.py',2],['tests/cli/test_cli_init.py',2],['tests/cron/test_run_one_job.py',2],['tests/hermes_cli/test_backup.py',2],
  ['tests/hermes_cli/test_gateway.py',2],['tests/hermes_cli/test_runtime_provider_resolution.py',2],['tests/hermes_cli/test_skills_hub.py',2],
  ['tests/hermes_cli/test_web_server_host_header.py',2],['tests/tools/test_code_execution_modes.py',2],['tests/tools/test_x_search_tool.py',2],
  ['tests/tui_gateway/test_protocol.py',2],['tools/delegate_tool.py',2],['tools/lazy_deps.py',2],['tui_gateway/methods_tools.py',2],
  ['web/src/pages/CronPage.tsx',2],['web/vite.config.ts',2],
].map(([path, wave]) => ({ path, wave, slug: path.replace(/\//g, '__') }))

const LOCKS = new Set(['uv.lock', 'package-lock.json'])

const HUNK = {
  type: 'object',
  required: ['index', 'marker_lines', 'fork_intent', 'upstream_intent', 'resolution_kind', 'resolution', 'rails_at_risk', 'risk', 'rationale'],
  properties: {
    index: { type: 'integer' },
    marker_lines: { type: 'string', description: 'line range of this hunk in the marker file, e.g. 120-168' },
    fork_intent: { type: 'string' },
    upstream_intent: { type: 'string' },
    resolution_kind: { type: 'string', enum: ['keep_fork', 'take_upstream', 'combine', 'regenerate', 'needs_lead'] },
    resolution: { type: 'string', description: 'exact resolved text if under ~40 lines, else a precise description naming every identifier that must survive' },
    rails_at_risk: { type: 'array', items: { type: 'string' } },
    risk: { type: 'string', enum: ['low', 'medium', 'high'] },
    rationale: { type: 'string' },
  },
}
const ANALYSIS = {
  type: 'object',
  required: ['path', 'file_strategy', 'needs_lead', 'summary', 'hunks', 'identifiers_must_survive'],
  properties: {
    path: { type: 'string' },
    file_strategy: { type: 'string' },
    needs_lead: { type: 'boolean' },
    summary: { type: 'string' },
    identifiers_must_survive: { type: 'array', items: { type: 'string' } },
    hunks: { type: 'array', items: HUNK },
  },
}
const REFUTE = {
  type: 'object',
  required: ['path', 'verdict', 'amendments', 'missed_rails', 'confidence', 'notes'],
  properties: {
    path: { type: 'string' },
    verdict: { type: 'string', enum: ['accept', 'amend', 'reject'] },
    amendments: { type: 'array', items: { type: 'object', required: ['hunk_index', 'problem', 'fix'], properties: { hunk_index: { type: 'integer' }, problem: { type: 'string' }, fix: { type: 'string' } } } },
    missed_rails: { type: 'array', items: { type: 'string' } },
    confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
    notes: { type: 'string' },
  },
}

const common = (f) => `
READ-ONLY RULES (absolute): never run git checkout/stash/reset/clean/fetch/merge/commit/add, never edit anything under ${REPO}, never open ~/.hermes/state.db or any *.db. The repo working tree is a STALE branch — never read files from it directly; read versions only via:
  git -C ${REPO} show ${BASE}:${f.path}     (merge base, v2026.8.13)
  git -C ${REPO} show ${FORK}:${f.path}     (fork / serving side = "ours")
  git -C ${REPO} show ${UP}:${f.path}       (upstream v0.21 = "theirs")
  git -C ${REPO} log --oneline ${BASE}..${FORK} -- ${f.path}   and   git -C ${REPO} log --oneline ${BASE}..${UP} -- ${f.path}   (who changed what; use git show <sha> -- ${f.path} for a specific commit)
The conflict-marked merge result (markers <<<<<<< ======= >>>>>>>) is at: ${AUD}/merge-dry-run/files/${f.slug}
Binding rules: ${AUD}/conflict-analysis/RULES.md (read it first, every time).
Fork rails for THIS file (ledger rows; each names a behavior that must survive): ${AUD}/conflict-analysis/packets/${f.slug}.json
`

phase('Analyze')
const results = await pipeline(
  FILES,
  (f) => agent(`You are analyzing ONE conflicted file of the Hermes v0.21 fork merge: ${f.path} (wave ${f.wave}${f.wave === 1 ? ' = SECURITY-CRITICAL' : ''}).
${common(f)}
TASK: for EVERY conflict hunk in the marker file (count the <<<<<<< markers; analyze all of them, in order), determine: what the fork side is doing and why (use git log/show on the fork commits), what upstream is doing and why, and the correct resolution per RULES.md — keep every fork rail, take upstream mechanics where they do not remove a rail. ${LOCKS.has(f.path) ? 'THIS IS A LOCK FILE: per rule 12, do not hand-merge. List which pins differ per side, flag any non-registry or range-widening pin, and set every hunk resolution_kind=regenerate.' : ''}
Also list identifiers_must_survive: every function/constant/route/env name from the ledger rows and fork hunks that must exist in the resolved file. If any hunk cannot preserve both a rail and a needed upstream behavior, set resolution_kind=needs_lead and needs_lead=true, describing the exact edge. Never propose wholesale ours/theirs for the file.
When done, Write your JSON result to ${AUD}/conflict-analysis/out/${f.slug}.json (create the directory if needed) and ALSO return it as structured output.`,
    { label: `analyze:${f.path}`, phase: 'Analyze', schema: ANALYSIS, model: 'sonnet', effort: LOCKS.has(f.path) ? 'low' : (f.wave === 1 ? 'high' : 'medium') }),
  (analysis, f) => {
    if (!analysis) return null
    return agent(`You are an adversarial skeptic reviewing a proposed merge resolution for ${f.path} (wave ${f.wave}${f.wave === 1 ? ' = SECURITY-CRITICAL' : ''}). Your job is to REFUTE it.
${common(f)}
The proposed analysis/resolution is at ${AUD}/conflict-analysis/out/${f.slug}.json (also inline below). Independently re-read the marker file and both sides. For each hunk ask: does the resolution drop or reorder a fork rail (ledger rows)? Does it reintroduce never-port 0e038425db semantics (gateway lifecycle guard loosening)? Does it enable a paid provider, raise delegation quotas, weaken backup secret exclusion, adopt an insecure test assertion, point users at "hermes resume" for estop, or activate an upstream kanban surface? Did the analysis MISS a hunk or a rail entirely? Is any "take_upstream" hiding a fork edge that lived inside that hunk?
Default to verdict=amend when uncertain; use reject only if the whole strategy is wrong. Be concrete: every amendment names the hunk index, the problem, and the exact fix.
Write your JSON to ${AUD}/conflict-analysis/refute/${f.slug}.json (create dir if needed) and return it as structured output.

PROPOSED ANALYSIS:
${JSON.stringify(analysis).slice(0, 60000)}`,
      { label: `refute:${f.path}`, phase: 'Refute', schema: REFUTE, model: 'sonnet', effort: 'high' })
      .then((r) => ({ path: f.path, wave: f.wave, analysis, refutation: r }))
  },
)

const done = results.filter(Boolean)
const needsLead = done.filter((r) => r.analysis.needs_lead || (r.analysis.hunks || []).some((h) => h.resolution_kind === 'needs_lead'))
const amended = done.filter((r) => r.refutation && r.refutation.verdict !== 'accept')
log(`analyzed ${done.length}/${FILES.length}; needs_lead=${needsLead.length}; refuter amend/reject=${amended.length}`)
if (done.length < FILES.length) log(`DROPPED (agent returned null): ${FILES.filter((f) => !done.some((d) => d.path === f.path)).map((f) => f.path).join(', ')}`)
return {
  analyzed: done.length,
  total: FILES.length,
  needs_lead: needsLead.map((r) => r.path),
  amend_or_reject: amended.map((r) => ({ path: r.path, verdict: r.refutation.verdict, n_amend: (r.refutation.amendments || []).length, missed: r.refutation.missed_rails })),
  high_risk_hunks: done.flatMap((r) => (r.analysis.hunks || []).filter((h) => h.risk === 'high').map((h) => `${r.path}#${h.index}`)),
}