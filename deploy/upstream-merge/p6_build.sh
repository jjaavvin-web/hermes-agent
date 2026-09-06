#!/usr/bin/env bash
# P6 — build the IMMUTABLE candidate deployment WITHOUT activation (Hermes amendment 3 / GATE-B owns
# the `current` symlink, both HERMES_TUI_DIR pins, unit pins and restarts), then build + seal the
# candidate-bound external TUI bundle, then record provenance + checksums + a secret scan.
# Usage: p6_build.sh <EXPECTED_HEAD>
set -uo pipefail
WT=/home/josep/.local/share/hermes-agent-worktrees/v021-fork-merge
AUD=/home/josep/.hermes/audits/20260903T202249Z-v021-candidate
RT=/home/josep/.local/share/hermes-agent-deployments/.python-runtimes/cpython-3.11.16-pbs20260814-x86_64-gnu/bin/python3.11
DEPLOY_ROOT=/home/josep/.local/share/hermes-agent-deployments
BUNDLES=/home/josep/.local/share/hermes-tui-bundles
HEAD_EXPECTED="${1:?EXPECTED_HEAD required}"
mkdir -p "$AUD/p6"; LOG="$AUD/p6/P6.log"; : > "$LOG"
say(){ echo "$*" | tee -a "$LOG"; }
cd "$WT"
export UV_PYTHON="$RT"; export PATH="/home/josep/.hermes/node/bin:$PATH"; export HERMES_NO_UPDATE_CHECK=1
HEAD=$(git rev-parse HEAD); [ "$HEAD" = "$HEAD_EXPECTED" ] || { say "ABORT: HEAD $HEAD != $HEAD_EXPECTED"; exit 2; }
[ -z "$(git status --porcelain)" ] || { say "ABORT: worktree not clean"; git status --porcelain | head; exit 2; }
BEFORE=$(readlink -f "$DEPLOY_ROOT/current"); say "current before: $BEFORE"
say "== dry run =="; bash scripts/build_deployment.sh "$HEAD" --dry-run 2>&1 | tee "$AUD/p6/build-dry-run.log" | tail -12
say "== build (NO --activate) =="; DEST=$(bash scripts/build_deployment.sh "$HEAD" 2>&1 | tee "$AUD/p6/build.log" | tail -1)
say "build tail: $DEST"; DEST=$(grep -oE "$DEPLOY_ROOT/v[^ ]+" "$AUD/p6/build.log" | tail -1); say "DEST=$DEST"
[ -d "$DEST" ] || { say "ABORT: DEST missing"; exit 3; }
AFTER=$(readlink -f "$DEPLOY_ROOT/current"); [ "$AFTER" = "$BEFORE" ] && say "OK: current symlink UNCHANGED ($AFTER)" || { say "ABORT: current symlink MOVED to $AFTER"; exit 4; }
say "== G5 provenance from inside the new deployment =="
"$DEST/.venv/bin/python" -c "import sys,sqlite3;print('python',sys.version.split()[0],'sqlite',sqlite3.sqlite_version)" | tee -a "$LOG"
grep -n '^home' "$DEST/.venv/pyvenv.cfg" | tee -a "$LOG"
(cd "$DEST" && HERMES_HOME=$(mktemp -d) .venv/bin/python -m hermes_cli.main --version 2>&1 | head -3) | tee -a "$LOG"
(cd "$DEST" && .venv/bin/python -c "import hermes_cli, agent, gateway; print([m.__file__ for m in (hermes_cli, agent, gateway)])") | tee -a "$LOG"
cat "$DEST/.deployed-commit" | tee -a "$LOG"
say "== TUI bundle: build twice in an isolated copy, compare, publish read-only =="
TB=$(mktemp -d); cp -r "$DEST/ui-tui" "$TB/ui-tui"; rm -rf "$TB/ui-tui/dist"
( cd "$TB/ui-tui" && npm ci --ignore-scripts --no-audit --no-fund >"$AUD/p6/tui-npm-ci.log" 2>&1; npm run build >"$AUD/p6/tui-build-1.log" 2>&1 ) || say "WARN: tui build 1 exit $?"
S1=$(sha256sum "$TB/ui-tui/dist/entry.js" 2>/dev/null | cut -d' ' -f1); rm -rf "$TB/ui-tui/dist"
( cd "$TB/ui-tui" && npm run build >"$AUD/p6/tui-build-2.log" 2>&1 ) || say "WARN: tui build 2 exit $?"
S2=$(sha256sum "$TB/ui-tui/dist/entry.js" 2>/dev/null | cut -d' ' -f1); say "tui sha1=$S1 sha2=$S2"
if [ -n "$S1" ] && [ "$S1" = "$S2" ]; then
  BD="$BUNDLES/$HEAD"; mkdir -p "$BD/dist"; cp "$TB/ui-tui/dist/entry.js" "$BD/dist/entry.js"; cp "$TB/ui-tui/package.json" "$BD/package.json"
  SZ=$(stat -c %s "$BD/dist/entry.js")
  cat > "$BD/manifest.json" <<EOF
{
  "schema": "hermes-external-tui-bundle-v1",
  "source_deployment": "$DEST",
  "source_commit": "$HEAD",
  "entry": "dist/entry.js",
  "sha256": "$S1",
  "size": $SZ,
  "build_proof": "Two consecutive npm run build executions in an isolated copy produced the same SHA-256 (P6, audit 20260903T202249Z-v021-candidate).",
  "audit_workspace": "$AUD/p6"
}
EOF
  chmod 0444 "$BD/dist/entry.js" "$BD/package.json" "$BD/manifest.json"; chmod 0555 "$BD/dist" "$BD"; say "TUI bundle published (NOT activated): $BD"
  node --check "$BD/dist/entry.js" && say "node --check OK"
else say "TUI bundle NOT reproducible or build failed — see p6/tui-build-*.log"; fi
rm -rf "$TB"
say "== mirror live custody: remove generated ui-tui/dist from the deployment (tripwire counts tracked files only) =="
rm -rf "$DEST/ui-tui/dist" && say "removed $DEST/ui-tui/dist"
say "== G9 checksums + secret scan =="
( cd "$DEST" && git -C "$WT" ls-tree -r --name-only "$HEAD" | while IFS= read -r f; do [ -f "$f" ] && sha256sum "$f"; done ) > "$AUD/p6/DEPLOYMENT-SHA256SUMS.txt"; say "tracked files hashed: $(wc -l < "$AUD/p6/DEPLOYMENT-SHA256SUMS.txt")"
grep -rEn '(sk-ant-|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xoxb-|-----BEGIN (RSA|OPENSSH|EC) PRIVATE KEY)' "$AUD" --include='*.md' --include='*.txt' --include='*.json' --include='*.log' 2>/dev/null | grep -v 'p6/DEPLOYMENT-SHA256SUMS' | head -5 > "$AUD/p6/secret-scan-hits.txt"; say "secret scan hits in audit dir: $(wc -l < "$AUD/p6/secret-scan-hits.txt") (must be 0)"
say "== negative activation proof =="
say "current -> $(readlink -f $DEPLOY_ROOT/current)"; grep -n 'HERMES_TUI_DIR' /home/josep/.local/bin/hermes /home/josep/.hermes/scripts/cockpit-dashboard.sh | tee -a "$LOG"
systemctl --user show hermes-gateway.service -p ExecStart | cut -c1-160 | tee -a "$LOG"
say "P6 done: DEST=$DEST (not activated)"
