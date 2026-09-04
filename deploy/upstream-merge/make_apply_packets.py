#!/usr/bin/env python3
"""Build per-file APPLY packets from analysis + refutation + lead overrides.
Usage: make_apply_packets.py <AUD> [wave]   (wave 1|2|all; default all)
Reads  conflict-analysis/out/<slug>.json, conflict-analysis/refute/<slug>.json, conflict-analysis/lead-overrides.json
Writes conflict-analysis/apply-packets/<slug>.md and INDEX-apply.json
"""
import json,sys,os,glob
AUD=sys.argv[1]; WAVE=sys.argv[2] if len(sys.argv)>2 else 'all'
idx=json.load(open(f'{AUD}/conflict-analysis/INDEX.json'))
ovr=json.load(open(f'{AUD}/conflict-analysis/lead-overrides.json')) if os.path.exists(f'{AUD}/conflict-analysis/lead-overrides.json') else {}
os.makedirs(f'{AUD}/conflict-analysis/apply-packets',exist_ok=True)
out=[]
for it in idx:
    if WAVE!='all' and str(it['wave'])!=WAVE: continue
    p=it['path']; slug=p.replace('/','__')
    a=json.load(open(f'{AUD}/conflict-analysis/out/{slug}.json')) if os.path.exists(f'{AUD}/conflict-analysis/out/{slug}.json') else None
    r=json.load(open(f'{AUD}/conflict-analysis/refute/{slug}.json')) if os.path.exists(f'{AUD}/conflict-analysis/refute/{slug}.json') else None
    o=ovr.get(p,{})
    if not a: out.append({'path':p,'status':'NO_ANALYSIS'}); continue
    L=[f"# APPLY PACKET — {p} (wave {it['wave']})",'',
       "Binding rules: read conflict-analysis/RULES.md first. Resolve ONLY this file. Never touch other files. Never run any git command that writes (no add/commit/checkout/stash/reset/merge); the lead stages files. Read-only git show/diff/log/grep are fine.",
       f"Worktree: /home/josep/.local/share/hermes-agent-worktrees/v021-fork-merge (the file is in conflicted state there; markers <<<<<<< HEAD / ======= / >>>>>>> v2026.8.31).",
       f"Reference versions: git -C /home/josep/.local/share/hermes-agent show f80f453ae067:{p} (BASE) | 938b676d7ab8:{p} (FORK/ours) | 29112bef0992:{p} (UPSTREAM/theirs)",
       '', f"## File strategy (analysis)", a.get('file_strategy',''), '',
       f"## Identifiers that MUST survive in the resolved file", *[f"- {x}" for x in a.get('identifiers_must_survive',[])], '']
    amends={}
    if r:
        for am in r.get('amendments',[]): amends.setdefault(am['hunk_index'],[]).append(am)
        L+= [f"## Refuter verdict: {r.get('verdict')} (confidence {r.get('confidence')})", r.get('notes',''), '']
        if r.get('missed_rails'): L+=["### Rails the refuter says the analysis missed — HONOR THESE:",*[f"- {m}" for m in r['missed_rails']],'']
    if o.get('note'): L+=["## LEAD OVERRIDE (binding, supersedes analysis and refuter where they conflict)", o['note'], '']
    L+=["## Hunk-by-hunk instructions"]
    for h in a.get('hunks',[]):
        i=h['index']; kind=o.get('hunks',{}).get(str(i),{}).get('resolution_kind',h['resolution_kind'])
        L+=[f"### Hunk {i} (marker lines ~{h.get('marker_lines')}) — {kind.upper()}", f"Fork intent: {h.get('fork_intent','')}", f"Upstream intent: {h.get('upstream_intent','')}", f"Resolution: {o.get('hunks',{}).get(str(i),{}).get('resolution',h.get('resolution',''))}"]
        if h.get('rails_at_risk'): L.append(f"Rails at risk: {', '.join(h['rails_at_risk'])}")
        for am in amends.get(i,[]): L.append(f"REFUTER AMENDMENT — problem: {am['problem']} → fix: {am['fix']}")
        L.append('')
    L+=["## Done criteria (all mandatory)",
        "1. No conflict markers remain in the file (grep -c '^<<<<<<<\\|^=======\\|^>>>>>>>' must be 0).",
        "2. Every identifier in the MUST-survive list is present (grep each).",
        "3. For .py: `python3 -m py_compile <file>` succeeds. For .json/.toml/.yml: a parse succeeds. For .ts/.tsx: no markers only.",
        "4. Do NOT run any git command that writes (no add/commit/checkout/stash/reset). The lead stages files. Read-only git show/diff/log are fine.",
        "5. Return a JSON report: {path, hunks_resolved, kind_per_hunk, identifiers_checked: [..], deviations_from_packet: [..], notes}."]
    open(f'{AUD}/conflict-analysis/apply-packets/{slug}.md','w').write('\n'.join(L))
    out.append({'path':p,'wave':it['wave'],'status':'READY','needs_lead':a.get('needs_lead'),'refuter':r.get('verdict') if r else 'NONE','override':bool(o)})
json.dump(out,open(f'{AUD}/conflict-analysis/INDEX-apply.json','w'),indent=1)
print(json.dumps({'packets':len([x for x in out if x['status']=='READY']),'no_analysis':[x['path'] for x in out if x['status']=='NO_ANALYSIS'],'needs_lead':[x['path'] for x in out if x.get('needs_lead')],'refuter_not_accept':[x['path'] for x in out if x.get('refuter') not in ('accept',None)]},indent=1))
