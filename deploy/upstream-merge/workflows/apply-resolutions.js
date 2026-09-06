export const meta = {
  name: 'v021-apply-batch1',
  description: 'Apply the adjudicated conflict resolutions (35 files with analysis + refutation + lead overrides) inside the isolated candidate worktree; agents never touch git',
  phases: [{ title: 'Apply', detail: 'one sonnet-coder per conflicted file, packet-driven' }],
}
const AUD = '/home/josep/.hermes/audits/20260903T202249Z-v021-candidate'
const WT = '/home/josep/.local/share/hermes-agent-worktrees/v021-fork-merge'
const FILES = [{"path": ".github/workflows/tests.yml", "wave": 2}, {"path": "agent/agent_init.py", "wave": 2}, {"path": "agent/auxiliary_client.py", "wave": 2}, {"path": "agent/codex_responses_adapter.py", "wave": 1}, {"path": "agent/codex_runtime.py", "wave": 1}, {"path": "agent/skill_commands.py", "wave": 2}, {"path": "cron/scheduler.py", "wave": 2}, {"path": "gateway/platforms/base.py", "wave": 2}, {"path": "gateway/run.py", "wave": 2}, {"path": "gateway/status.py", "wave": 2}, {"path": "hermes_cli/active_sessions.py", "wave": 1}, {"path": "hermes_cli/backup.py", "wave": 1}, {"path": "hermes_cli/commands.py", "wave": 2}, {"path": "hermes_cli/cron.py", "wave": 2}, {"path": "hermes_cli/curator.py", "wave": 2}, {"path": "hermes_cli/main.py", "wave": 1}, {"path": "hermes_cli/models.py", "wave": 2}, {"path": "hermes_cli/plugins.py", "wave": 2}, {"path": "hermes_cli/runtime_provider.py", "wave": 1}, {"path": "hermes_cli/tools_config.py", "wave": 2}, {"path": "hermes_cli/web_server.py", "wave": 1}, {"path": "package-lock.json", "wave": 1}, {"path": "pyproject.toml", "wave": 1}, {"path": "tests/cli/test_cli_init.py", "wave": 2}, {"path": "tests/hermes_cli/test_runtime_provider_resolution.py", "wave": 2}, {"path": "tests/hermes_cli/test_web_server_host_header.py", "wave": 2}, {"path": "tests/tools/test_x_search_tool.py", "wave": 2}, {"path": "tests/tui_gateway/test_protocol.py", "wave": 2}, {"path": "tools/approval.py", "wave": 1}, {"path": "tools/delegate_tool.py", "wave": 2}, {"path": "tools/file_tools.py", "wave": 1}, {"path": "tools/terminal_tool.py", "wave": 1}, {"path": "tui_gateway/methods_tools.py", "wave": 2}, {"path": "web/src/pages/CronPage.tsx", "wave": 2}, {"path": "web/vite.config.ts", "wave": 2}]
  .map((f) => ({ ...f, slug: f.path.replace(/\//g, '__') }))

const REPORT = {
  type: 'object',
  required: ['path', 'hunks_resolved', 'kind_per_hunk', 'markers_remaining', 'compiled', 'identifiers_checked', 'identifiers_missing', 'deviations_from_packet', 'notes'],
  properties: {
    path: { type: 'string' }, hunks_resolved: { type: 'integer' },
    kind_per_hunk: { type: 'array', items: { type: 'string' } },
    markers_remaining: { type: 'integer' }, compiled: { type: 'boolean' },
    identifiers_checked: { type: 'array', items: { type: 'string' } },
    identifiers_missing: { type: 'array', items: { type: 'string' } },
    deviations_from_packet: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}

phase('Apply')
const results = await pipeline(FILES, (f) => agent(`You are resolving ONE merge-conflicted file inside an isolated git worktree of the Hermes fork (v0.21 absorption). File: ${f.path} (wave ${f.wave}${f.wave === 1 ? ' — SECURITY-CRITICAL: follow the packet exactly; when in doubt keep the fork rail and record a deviation' : ''}).

ABSOLUTE RULES
- Edit ONLY ${WT}/${f.path}. Never touch any other file. Never edit anything under /home/josep/.local/share/hermes-agent (that is a different checkout).
- NEVER run a git command that writes: no add, commit, checkout, stash, reset, merge, restore, clean. Read-only git show/diff/log/grep are fine. The lead stages files.
- Never open ~/.hermes/state.db or any *.db. Never run the test suite; never install packages.
- Conflict markers in the worktree file are "<<<<<<< HEAD" (fork = ours), "=======", ">>>>>>> v2026.8.31" (upstream = theirs). Line numbers in the packet refer to the same content.

YOUR PACKET (binding; read it first, then RULES.md): ${AUD}/conflict-analysis/apply-packets/${f.slug}.md and ${AUD}/conflict-analysis/RULES.md
The packet contains: the file strategy, the per-hunk resolution (fork intent, upstream intent, resolution text or precise description), the refuter's amendments (HONOR them), and the LEAD OVERRIDE (binding over everything else where they conflict). Reference versions: git -C ${WT} show f80f453ae067:${f.path} (BASE) | 938b676d7ab8:${f.path} (FORK) | 29112bef0992:${f.path} (UPSTREAM).

PROCEDURE
1. Read the packet and RULES.md. Read the conflicted file region by region (grep -n '^<<<<<<<\\|^=======\\|^>>>>>>>' to locate every hunk; count them).
2. Resolve every hunk per the packet (use the Edit tool; for large deletions or seeding from one side use bash with python/sed carefully). Apply every OUT-OF-HUNK instruction the packet's LEAD OVERRIDE names (e.g. a constant restored outside the markers).
3. Verify: grep -c '^<<<<<<<\\|^=======\\|^>>>>>>>' must be 0 — careful: a legitimate line of exactly seven '=' characters can exist in Markdown/docstrings; report if so. For .py run: python3 -m py_compile ${WT}/${f.path}. For .json: python3 -c "import json;json.load(open('${WT}/${f.path}'))". For .toml: python3 -c "import tomllib;tomllib.load(open('${WT}/${f.path}','rb'))". For .yml: check indentation consistency by eye and that every KEEP identifier is present. For .ts/.tsx: markers only.
4. grep every identifier in the packet's MUST-survive list; list any missing.
5. Return the JSON report (schema enforced). deviations_from_packet must name every place you did something the packet did not literally say, and every out-of-hunk edit you made.
Do NOT return until markers_remaining is 0 and compiled is true, unless a genuine blocker exists — then explain it in notes and leave the file with markers (do not half-resolve).`,
  { label: `apply:${f.path}`, phase: 'Apply', schema: REPORT, agentType: 'sonnet-coder', model: 'sonnet', effort: f.wave === 1 ? 'high' : 'medium' }))

const done = results.filter(Boolean)
const bad = done.filter((r) => r.markers_remaining !== 0 || !r.compiled || (r.identifiers_missing || []).length)
log(`applied ${done.length}/${FILES.length}; problems=${bad.length}`)
if (done.length < FILES.length) log(`DROPPED: ${FILES.filter((f) => !done.some((d) => d.path === f.path)).map((f) => f.path).join(', ')}`)
return {
  applied: done.length, total: FILES.length,
  problems: bad.map((r) => ({ path: r.path, markers: r.markers_remaining, compiled: r.compiled, missing: r.identifiers_missing, notes: r.notes.slice(0, 300) })),
  deviations: done.filter((r) => (r.deviations_from_packet || []).length).map((r) => ({ path: r.path, deviations: r.deviations_from_packet })),
}