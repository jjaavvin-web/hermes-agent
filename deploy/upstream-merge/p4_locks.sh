#!/usr/bin/env bash
# P4 — regenerate lock files in the candidate worktree under the PINNED runtime, then diff against BOTH parents.
# Never hand-edits a lock. Writes evidence to $AUD/p4/. Run only after pyproject.toml, uv.lock, web/package.json,
# package-lock.json are resolved (no markers) in the worktree.
set -uo pipefail
WT=/home/josep/.local/share/hermes-agent-worktrees/v021-fork-merge
AUD=/home/josep/.hermes/audits/20260903T202249Z-v021-candidate
RT=/home/josep/.local/share/hermes-agent-deployments/.python-runtimes/cpython-3.11.16-pbs20260814-x86_64-gnu/bin/python3.11
FORK=938b676d7ab8; UP=29112bef0992
mkdir -p "$AUD/p4"; cd "$WT"
export UV_PYTHON="$RT"; export PATH="/home/josep/.hermes/node/bin:$PATH"
echo "== runtime ==" | tee "$AUD/p4/P4.log"; "$RT" -c "import sys,sqlite3;print(sys.version.split()[0],'sqlite',sqlite3.sqlite_version)" | tee -a "$AUD/p4/P4.log"
uv --version | tee -a "$AUD/p4/P4.log"; node --version | tee -a "$AUD/p4/P4.log"; npm --version | tee -a "$AUD/p4/P4.log"
for f in pyproject.toml uv.lock web/package.json package-lock.json; do
  if grep -q '^<<<<<<< ' "$f"; then echo "ABORT: $f still has conflict markers" | tee -a "$AUD/p4/P4.log"; exit 2; fi
done
echo "== uv lock ==" | tee -a "$AUD/p4/P4.log"
uv lock 2>&1 | tee "$AUD/p4/uv-lock.log" | tail -5; echo "uv lock exit=${PIPESTATUS[0]}" | tee -a "$AUD/p4/P4.log"
uv lock --check 2>&1 | tee "$AUD/p4/uv-lock-check.log" | tail -3; echo "uv lock --check exit=${PIPESTATUS[0]}" | tee -a "$AUD/p4/P4.log"
git diff HEAD -- uv.lock > "$AUD/p4/uv.lock.vs-fork.diff"; git diff "$UP" -- uv.lock > "$AUD/p4/uv.lock.vs-upstream.diff"
python3 - "$AUD" "$FORK" "$UP" <<'EOF' | tee "$AUD/p4/uv-lock-package-delta.txt"
import subprocess,sys,tomllib
aud,fork,up=sys.argv[1:4]
def pkgs(text):
    d=tomllib.loads(text); out={}
    for p in d.get('package',[]):
        src=p.get('source',{}); out[p['name']]=(p.get('version'),src.get('registry') or src.get('git') or src.get('path') or src.get('url') or 'unknown')
    return out
new=pkgs(open('uv.lock',encoding='utf-8').read())
f=pkgs(subprocess.run(['git','show',f'{fork}:uv.lock'],capture_output=True,text=True).stdout)
u=pkgs(subprocess.run(['git','show',f'{up}:uv.lock'],capture_output=True,text=True).stdout)
print(f"packages: new={len(new)} fork={len(f)} upstream={len(u)}")
nonreg=[(n,v) for n,v in new.items() if 'pypi.org' not in str(v[1])]
print(f"NON-PyPI sources in new lock: {nonreg[:20]}")
print("\n== packages whose version differs from BOTH parents (needs a reason) ==")
for n,(v,s) in sorted(new.items()):
    fv=f.get(n,(None,))[0]; uv_=u.get(n,(None,))[0]
    if v!=fv and v!=uv_: print(f"  {n}: new={v} fork={fv} upstream={uv_}")
print("\n== packages present in fork lock but absent from new lock ==")
for n in sorted(set(f)-set(new)): print(f"  {n} (fork {f[n][0]})" + ("  [also absent upstream]" if n not in u else "  [present upstream!]"))
print("\n== packages new vs fork (from upstream) ==", len(set(new)-set(f)))
gate=new.get('fastapi',(None,))[0]; print(f"\nfastapi version gate: lock={gate} manifest=0.133.1 -> {'OK' if gate=='0.133.1' else 'MISMATCH — bump gate+lock together or STOP'}")
for n in ('mcp','httpx2','starlette','pytest-timeout','uvicorn','cryptography','h2','aiohttp'):
    print(f"  {n}: new={new.get(n,(None,))[0]} fork={f.get(n,(None,))[0]} upstream={u.get(n,(None,))[0]}")
EOF
echo "== npm lock (workspace root) ==" | tee -a "$AUD/p4/P4.log"
npm install --package-lock-only --ignore-scripts --no-audit --no-fund 2>&1 | tee "$AUD/p4/npm-lock.log" | tail -5; echo "npm exit=${PIPESTATUS[0]}" | tee -a "$AUD/p4/P4.log"
git diff HEAD -- package-lock.json > "$AUD/p4/package-lock.vs-fork.diff"; git diff "$UP" -- package-lock.json > "$AUD/p4/package-lock.vs-upstream.diff"
python3 - <<'EOF' | tee "$AUD/p4/npm-lock-checks.txt"
import json
d=json.load(open('package-lock.json')); pk=d.get('packages',{})
def ver(name):
    hits={k:v.get('version') for k,v in pk.items() if k.endswith('node_modules/'+name)}
    return hits
print('nanoid:',ver('nanoid'))
for n in ['@playwright/test','@testing-library/jest-dom','@testing-library/react','@testing-library/user-event','@babel/core','@rolldown/plugin-babel','@types/babel__core','babel-plugin-react-compiler','jsdom','vitest']:
    print(f"  {n}: {ver(n)}")
nonreg=[k for k,v in pk.items() if v.get('resolved') and 'registry.npmjs.org' not in v['resolved']]
print('non-registry resolved entries:',len(nonreg),nonreg[:10])
EOF
echo "== git status of lock files ==" | tee -a "$AUD/p4/P4.log"; git status --short -- pyproject.toml uv.lock web/package.json package-lock.json | tee -a "$AUD/p4/P4.log"
echo "P4 done — review $AUD/p4/ before staging"
