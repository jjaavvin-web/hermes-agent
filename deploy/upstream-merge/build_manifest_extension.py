#!/usr/bin/env python3
"""Deterministically verify pin proposals and build the extended parity manifest.

Reads  <AUD>/manifest-extension/pins/*.json  (agent proposals, schema PIN)
Reads  git show <REV>:<path> for every anchor (never the working tree)
Writes <AUD>/manifest-extension/extension-items.json   (schema-legal items, ids assigned)
       <AUD>/manifest-extension/extension-report.md    (per-proposal verdict)
       <AUD>/manifest-extension/docket-v021.txt         (one docket line per new item)
Usage: build_extension.py <AUD> <REV> [--start-id 72]
"""
import ast, hashlib, json, subprocess, sys, glob, os, re
AUD, REV = sys.argv[1], sys.argv[2]
START = int(sys.argv[sys.argv.index('--start-id')+1]) if '--start-id' in sys.argv else 72
REPO = '/home/josep/.local/share/hermes-agent'
_cache = {}
def show(path):
    if path not in _cache:
        r = subprocess.run(['git','-C',REPO,'show',f'{REV}:{path}'],capture_output=True)
        _cache[path] = r.stdout.decode('utf-8','replace') if r.returncode==0 else None
    return _cache[path]
def module_symbols(src):
    try: tree = ast.parse(src)
    except Exception: return set()
    out=set()
    for n in tree.body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): out.add(n.name)
        elif isinstance(n,ast.Assign):
            for t in n.targets:
                if isinstance(t,ast.Name): out.add(t.id)
        elif isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name): out.add(n.target.id)
    return out
def call_edge(src, caller, callee):
    try: tree = ast.parse(src)
    except Exception: return False
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==caller:
            for c in ast.walk(n):
                if isinstance(c,ast.Call):
                    f=c.func; name = f.id if isinstance(f,ast.Name) else (f.attr if isinstance(f,ast.Attribute) else None)
                    if name==callee: return True
    return False
def check_anchor(a):
    src = show(a['path']); mode=a['mode']
    if mode=='exists': return (src is not None, 'exists' if src is not None else 'MISSING file')
    if src is None: return (mode=='absent_or_repurposed', 'MISSING file')
    if mode=='contains':
        if a.get('symbol'):
            if a['symbol'] in module_symbols(src) or a['symbol'] in src: return (True,'symbol ok')
            return (False,f"symbol {a['symbol']!r} not found")
        t=a.get('text','')
        return (bool(t) and t in src, 'text ok' if t and t in src else f'text {t[:60]!r} not found')
    if mode=='call_edge': ok=call_edge(src,a.get('caller',''),a.get('callee','')); return (ok,'edge ok' if ok else 'edge missing')
    if mode=='absent_or_repurposed':
        m=a.get('retired_marker'); return ((m not in src) if m else True, 'ok')
    return (False,f'unknown mode {mode}')
def proof_exists(node):
    if '::' not in node: return False
    f,t = node.split('::',1); t=t.split('[')[0].split('::')[-1]
    src=show(f); return bool(src) and re.search(rf'^\s*(async\s+)?def\s+{re.escape(t)}\s*\(',src,re.M) is not None
items=[]; report=[]; seen=set(); nid=START
for fn in sorted(glob.glob(f'{AUD}/manifest-extension/pins/*.json')):
    try: p=json.load(open(fn))
    except Exception as e: report.append(f'- {os.path.basename(fn)}: UNREADABLE {e}'); continue
    # accept both shapes: PIN wrapper {decision,item,...} OR a bare item {verdict,kind,anchors,...}
    if 'decision' not in p and 'anchors' in p:
        p={'ledger_index':int(os.path.basename(fn)[:3]),'decision':'new_item','item':p,'surface':p.get('docket_item','')}
    if 'decision' in p and p.get('decision')=='new_item' and not p.get('item') and 'anchors' in p:
        p['item']=p
    tag=f"{os.path.basename(fn)} idx={p.get('ledger_index')}"
    if p.get('decision')!='new_item' or not p.get('item'):
        report.append(f"- {tag}: {p.get('decision')} {p.get('covered_by','')}"); continue
    it=p['item']; good=[]; bad=[]
    for a in it.get('anchors',[]):
        ok,why=check_anchor(a); (good if ok else bad).append((a,why))
    proofs=[pr for pr in it.get('proofs',[]) if proof_exists(pr.get('test',''))]
    badproofs=[pr['test'] for pr in it.get('proofs',[]) if not proof_exists(pr.get('test',''))]
    kind=it.get('kind','structural')
    if kind=='security_invariant' and not proofs: kind='structural'; report.append(f"- {tag}: demoted to structural (no verified proof)")
    if not good: report.append(f"- {tag}: REJECTED — no anchor verified ({[w for _,w in bad]})"); continue
    key=tuple(sorted(json.dumps(a,sort_keys=True) for a,_ in good))
    if key in seen: report.append(f"- {tag}: DUPLICATE anchors of an earlier item — skipped"); continue
    seen.add(key)
    docket=it['docket_item'].strip().replace('\n',' ')
    items.append({'id':f'fp-{nid:03d}','verdict':it.get('verdict','PRESERVED_EQUIVALENT'),'severity':'NONE','kind':kind,'phase':'in_repo','introduced_by':[],
        'docket_item':docket,'docket_item_sha256_16':hashlib.sha256(docket.encode()).hexdigest()[:16],
        'anchors':[a for a,_ in good],'proofs':proofs,'notes':f"v021 extension from custody ledger row idx {p.get('ledger_index')}: {it.get('notes','')}"[:400]})
    report.append(f"- {tag}: fp-{nid:03d} {kind} anchors={len(good)} dropped_anchors={len(bad)} proofs={len(proofs)} badproofs={badproofs}"); nid+=1
json.dump(items,open(f'{AUD}/manifest-extension/extension-items.json','w'),indent=1)
open(f'{AUD}/manifest-extension/docket-v021.txt','w').write(''.join(f"{i['id']}\t{i['docket_item']}\n" for i in items))
open(f'{AUD}/manifest-extension/extension-report.md','w').write(f"# extension report — {len(items)} items built from {len(report)} proposals at {REV}\n\n"+'\n'.join(report)+'\n')
print(f"built {len(items)} items; security_invariant={sum(1 for i in items if i['kind']=='security_invariant')}; report lines={len(report)}")
