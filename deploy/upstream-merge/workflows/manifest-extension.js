export const meta = {
  name: 'v021-manifest-extension',
  description: 'Propose parity-manifest pins for every PORT ledger surface (anchors verified at the serving commit) and dispose the WEAKENED/OVERTURNED/INVESTIGATE ledger rows',
  phases: [
    { title: 'Pins', detail: 'one sonnet agent per PORT ledger row: manifest item proposal with verified anchors' },
    { title: 'Dispose', detail: 'one sonnet agent per weakened/overturned/investigate row' },
  ],
}
const AUD = '/home/josep/.hermes/audits/20260903T202249Z-v021-candidate'
const REPO = '/home/josep/.local/share/hermes-agent'
const FORK = '938b676d7ab8', UP = '29112bef0992', BASE = 'f80f453ae067'

const N_PORT = 135, N_WEAK = 11, N_INV = 6
const portItems = Array.from({ length: N_PORT }, (_, i) => i)
const weakItems = Array.from({ length: N_WEAK }, (_, i) => ({ file: 'weakened-overturned-rows.json', i }))
const invItems = Array.from({ length: N_INV }, (_, i) => ({ file: 'investigate-rows.json', i }))

const ANCHOR = { type: 'object', required: ['path', 'mode'], properties: { path: { type: 'string' }, mode: { type: 'string', enum: ['exists', 'contains', 'call_edge', 'absent_or_repurposed'] }, text: { type: 'string' }, symbol: { type: 'string' }, caller: { type: 'string' }, callee: { type: 'string' } } }
const PIN = {
  type: 'object',
  required: ['ledger_index', 'surface', 'decision', 'anchor_evidence', 'notes'],
  properties: {
    ledger_index: { type: 'integer' }, surface: { type: 'string' },
    decision: { type: 'string', enum: ['new_item', 'covered_by_existing', 'not_pinnable'] },
    covered_by: { type: 'string', description: 'existing fp-0NN id when decision=covered_by_existing' },
    item: { type: 'object', required: ['verdict', 'kind', 'docket_item', 'anchors', 'proofs'], properties: {
      verdict: { type: 'string', enum: ['PRESERVED_EQUIVALENT', 'RELOCATED', 'SUPERSEDED_LEGIT'] },
      kind: { type: 'string', enum: ['security_invariant', 'structural', 'superseded', 'meta'] },
      docket_item: { type: 'string', description: 'one-line docket text naming file + behavior' },
      anchors: { type: 'array', items: ANCHOR, minItems: 1, maxItems: 4 },
      proofs: { type: 'array', items: { type: 'object', required: ['test'], properties: { test: { type: 'string' } } } },
      notes: { type: 'string' },
    } },
    anchor_evidence: { type: 'array', items: { type: 'object', required: ['anchor', 'command', 'result'], properties: { anchor: { type: 'string' }, command: { type: 'string' }, result: { type: 'string', enum: ['hit', 'miss'] } } } },
    notes: { type: 'string' },
  },
}
const DISPOSE = {
  type: 'object',
  required: ['file', 'index', 'surface', 'original_disposition', 'verifier_verdict', 'final_disposition', 'evidence', 'follow_up'],
  properties: {
    file: { type: 'string' }, index: { type: 'integer' }, surface: { type: 'string' },
    original_disposition: { type: 'string' }, verifier_verdict: { type: 'string' },
    final_disposition: { type: 'string', enum: ['PORT', 'ALREADY_UPSTREAM', 'RETIRE', 'RESOLVED_NO_ACTION', 'NEEDS_LEAD'] },
    evidence: { type: 'string' }, follow_up: { type: 'string', description: 'what the merge executor must do for this row, or none' },
    proposed_anchors: { type: 'array', items: ANCHOR },
  },
}

const RO = `READ-ONLY RULES (absolute): never run git checkout/stash/reset/clean/fetch/merge/commit/add; never edit anything under ${REPO}; never open ~/.hermes/state.db or any *.db. The repo working tree is a STALE branch — read file versions ONLY via: git -C ${REPO} show ${FORK}:<path> (serving fork), git -C ${REPO} show ${UP}:<path> (upstream v0.21), git -C ${REPO} show ${BASE}:<path> (merge base). To test a contains-anchor: git -C ${REPO} show ${FORK}:<path> | grep -nF '<text>' . To check a test proof exists: git -C ${REPO} show ${FORK}:<testfile> | grep -n 'def <test_name>'. The existing 71-item manifest (read it to avoid duplicates): git -C ${REPO} show ${FORK}:tests/security/fork_parity_manifest.json`

phase('Pins')
const pins = await pipeline(portItems, (i) => agent(`You are proposing ONE fork-parity manifest pin for a Hermes fork surface, so that future upstream merges machine-verify it survived.
${RO}
Read your ledger row: python3 -c "import json;print(json.dumps(json.load(open('${AUD}/ledger/port-rows.json'))[${i}],indent=1))"
Manifest item rules (from tests/security/fork_parity_lib.py at ${FORK}): anchors modes = exists | contains (path + text, or path + symbol for a module-level def/class/assign) | call_edge (path + caller + callee) | absent_or_repurposed. kind=security_invariant REQUIRES at least one proof = an existing pytest node id (file::test_function) that exists at ${FORK} and is not skipped; otherwise use kind=structural (anchors only). verdict is normally PRESERVED_EQUIVALENT.
TASK: (1) If an existing fp-0NN item already pins this exact surface (same file + same behavior), return decision=covered_by_existing with covered_by. (2) Otherwise design 1-4 anchors that would go RED if the fork behavior were silently reverted: prefer distinctive identifiers (function names, constants, env keys, route strings) over prose; each anchor text must be a literal substring present at ${FORK}. RUN the grep for every anchor and record the command + hit/miss in anchor_evidence; only emit anchors that HIT. (3) Choose proofs from the row's behavioral_tests column when they exist at ${FORK} (verify the def exists); if none exist, kind=structural. (4) docket_item = one line: "<path>: <behavior>". (5) If the surface is not pinnable by file content (pure config/runtime), return decision=not_pinnable with notes.
Write your JSON to ${AUD}/manifest-extension/pins/${String(i).padStart(3, '0')}.json (create dirs) and return it as structured output.`,
  { label: `pin:${i}`, phase: 'Pins', schema: PIN, model: 'sonnet', effort: 'medium' }))

phase('Dispose')
const disposals = await pipeline([...weakItems, ...invItems], (it) => agent(`You are adjudicating ONE ledger row whose adversarial verifier marked it WEAKENED/OVERTURNED or whose disposition is INVESTIGATE, for the Hermes v0.21 fork merge. The merge must not certify until every such row is disposed.
${RO}
Read the row: python3 -c "import json;print(json.dumps(json.load(open('${AUD}/ledger/${it.file}'))[${it.i}],indent=1))"
Context: Kanban was FULLY RETIRED live on 2026-09-01 (tombstone dir at ~/.hermes/kanban.db, plugin disabled, all kanban units/crons removed, gateway restarted) — any row whose open question was about live kanban openers/consumers must be re-read in that light (the CODE still exists in the fork and merges through; only live authority is gone). Honcho retirement APPLY has NOT happened.
TASK: re-check the cited facts yourself at ${FORK} and ${UP} (file:line), then decide final_disposition: PORT (fork behavior must survive the merge; give proposed_anchors), ALREADY_UPSTREAM (upstream v0.21 has an equivalent; cite it), RETIRE (dead/inoperable; cite proof of zero live callers), RESOLVED_NO_ACTION (the verifier's concern is answered; say how), or NEEDS_LEAD (a genuine judgment call; state the exact question). follow_up = the concrete instruction for the merge executor.
Write your JSON to ${AUD}/manifest-extension/dispose/${it.file.replace('.json', '')}-${it.i}.json (create dirs) and return it as structured output.`,
  { label: `dispose:${it.file}:${it.i}`, phase: 'Dispose', schema: DISPOSE, model: 'sonnet', effort: 'high' }))

const p = pins.filter(Boolean), d = disposals.filter(Boolean)
const newItems = p.filter((x) => x.decision === 'new_item')
log(`pins: ${p.length}/${N_PORT} returned; new_item=${newItems.length} covered=${p.filter((x) => x.decision === 'covered_by_existing').length} not_pinnable=${p.filter((x) => x.decision === 'not_pinnable').length}; disposals ${d.length}/${N_WEAK + N_INV}`)
if (p.length < N_PORT) log(`DROPPED pins: ${portItems.filter((i) => !p.some((x) => x.ledger_index === i)).join(',')}`)
return {
  pins_returned: p.length, new_items: newItems.length,
  covered: p.filter((x) => x.decision === 'covered_by_existing').map((x) => `${x.ledger_index}->${x.covered_by}`),
  not_pinnable: p.filter((x) => x.decision === 'not_pinnable').map((x) => `${x.ledger_index}: ${x.surface.slice(0, 80)}`),
  security_invariants: newItems.filter((x) => x.item && x.item.kind === 'security_invariant').length,
  disposals: d.map((x) => `${x.file}#${x.index} ${x.original_disposition}/${x.verifier_verdict} -> ${x.final_disposition}`),
  needs_lead: d.filter((x) => x.final_disposition === 'NEEDS_LEAD').map((x) => `${x.file}#${x.index}: ${x.surface.slice(0, 100)}`),
}