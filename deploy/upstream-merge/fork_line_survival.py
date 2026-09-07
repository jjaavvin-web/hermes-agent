#!/usr/bin/env python3
"""Fork-line survival scan v2 (count-aware). For every line the FORK added since BASE (per file),
compare its occurrence count in the FORK file vs the merged TREE file. A drop = a fork line git dropped
without a conflict marker (clean-merge silent loss) or a resolution dropped (conflict files, post-resolution).
Usage: fork_line_survival.py <TREE-ish> <out.json> [conflict-list-file] [--min-len 8]
"""
import subprocess, sys, json, re, collections
REPO='/home/josep/.local/share/hermes-agent'; BASE='f80f453ae067'; FORK='938b676d7ab8'
TREE=sys.argv[1]; OUT=sys.argv[2]
conflicts=set(l.strip() for l in open(sys.argv[3], encoding='utf-8')) if len(sys.argv)>3 and not sys.argv[3].startswith('--') else set()
MINLEN=int(sys.argv[sys.argv.index('--min-len')+1]) if '--min-len' in sys.argv else 8
GENERIC=set(['return','return True','return False','return None','pass','continue','break','else:','try:','finally:','raise','return result','return out','return {}','return []','})','],',')','}',']','),','}),','])','"""','if not ok:','except Exception:','except Exception as e:','except Exception as exc:','except Exception as exc:  # noqa: BLE001','yield','return data','return None  # noqa'])
def git(*a):
    r=subprocess.run(['git','-C',REPO,*a],capture_output=True); return r.stdout
import os
def text(rev,p):
    if rev.startswith('/'):
        try: return open(os.path.join(rev,p),encoding='utf-8').read()
        except (FileNotFoundError,UnicodeDecodeError,IsADirectoryError): return None
    b=git('show',f'{rev}:{p}')
    try: return b.decode('utf-8')
    except UnicodeDecodeError: return None
def counts(t): return collections.Counter(x.strip() for x in t.split('\n'))
added=collections.defaultdict(collections.Counter); cur=None
for line in git('diff','-U0','--no-color',BASE,FORK).decode('utf-8','replace').split('\n'):
    if line.startswith('+++ '): cur=line[6:] if line.startswith('+++ b/') else None; continue
    if line[:4] in ('--- ','@@ -','diff ','inde'): continue
    if cur and line.startswith('+'):
        s=line[1:].strip()
        if len(s)>=MINLEN and s not in GENERIC and not re.fullmatch(r'[\W_]+',s) and not s.startswith(('#','//')):
            added[cur][s]+=1
if TREE.startswith('/'):
    tree_paths=set(p for p in subprocess.run(['git','-C',TREE,'ls-files'],capture_output=True,text=True).stdout.split('\n') if p)
    tree_paths|=set(p for p in subprocess.run(['git','-C',TREE,'ls-files','--others','--exclude-standard'],capture_output=True,text=True).stdout.split('\n') if p)
else:
    tree_paths=set(p for p in git('ls-tree','-r','--name-only',TREE).decode().split('\n') if p)
all_tree=None
def all_tree_lines():
    global all_tree
    if all_tree is None:
        all_tree=set()
        for p in tree_paths:
            if re.search(r'\.(py|ts|tsx|js|mjs|json|toml|yml|yaml|md|sh|txt|cfg|ini|html|css)$',p):
                t=text(TREE,p)
                if t: all_tree|=set(x.strip() for x in t.split('\n'))
    return all_tree
report={'tree':TREE,'files':{},'totals':{}}; n_files=0; n_lines=0; n_drop=0; n_gone=0
for f in sorted(added):
    ft=text(FORK,f)
    if ft is None: continue
    fc=counts(ft)
    if f not in tree_paths:
        drops=[(l,fc[l],0) for l in added[f]]
        status='FILE_ABSENT_IN_TREE'
    else:
        tt=text(TREE,f)
        if tt is None: continue
        tc=counts(tt)
        drops=[(l,fc[l],tc[l]) for l in added[f] if tc[l]<fc[l]]
        status='CONFLICT_PENDING' if f in conflicts else 'CLEAN_MERGE'
    n_files+=1; n_lines+=sum(added[f].values())
    if drops:
        at=all_tree_lines()
        gone=[d for d in drops if d[0] not in at]
        n_drop+=len(drops); n_gone+=len(gone)
        report['files'][f]={'status':status,'fork_added_distinct':len(added[f]),'dropped':len(drops),'gone_everywhere':len(gone),
            'dropped_lines':[{'line':l[:160],'fork_count':a,'tree_count':b,'elsewhere':(l in at)} for l,a,b in drops[:80]]}
report['totals']={'files_with_fork_adds':n_files,'fork_added_lines':n_lines,'dropped_in_file':n_drop,'gone_everywhere':n_gone,'min_len':MINLEN}
json.dump(report,open(OUT,'w', encoding='utf-8'),indent=1)
print(json.dumps(report['totals']))
for st in ('CLEAN_MERGE','FILE_ABSENT_IN_TREE','CONFLICT_PENDING'):
    fs=[(f,v) for f,v in report['files'].items() if v['status']==st]
    print(f"\n== {st}: {len(fs)} files with dropped fork lines ==")
    for f,v in sorted(fs,key=lambda x:-x[1]['dropped'])[:50]:
        print(f"  dropped={v['dropped']:4d} gone={v['gone_everywhere']:4d}  {f}")
