#!/usr/bin/env bash
# Build an immutable hermes-agent deployment directory from a verified
# commit of THIS repo. In-repo generalization of the one-off builder used
# for the v0.20.1 upstream-merge candidate
# (~/.hermes/audits/v0201-upstream-merge-20260813/build-v0201-deployment.sh):
# same identity gate, ledger, and preflight structure, but driven entirely
# by deploy/DEPLOYMENT-MANIFEST.toml instead of hardcoded values, so it can
# be reused for the next candidate without editing this script.
#
# Never touches any live deployment, systemd unit, or the `hermes` wrapper.
# Never restarts a service — that stays a separate, human-gated step.
#
# Usage:
#   scripts/build_deployment.sh EXPECTED_HEAD [--activate] [--dry-run]
#
#   EXPECTED_HEAD  Required. Must equal `git rev-parse HEAD` for this repo
#                  (identity gate) — the whole point is that a deployment
#                  is traceable to one exact, verified commit.
#   --activate     After a successful build, atomically repoint
#                  deployments_root/current (see [layout] in the manifest)
#                  at the new deployment, and print the previous target so
#                  it can be rolled back to. Does NOT restart any service.
#   --dry-run      Resolve and print the build plan (dest dir, extras,
#                  version gates, out-of-band packages, node install) and
#                  exit — no filesystem writes at all. Still enforces the
#                  HEAD-match half of the identity gate (a wrong
#                  EXPECTED_HEAD still aborts); the clean-tree half is
#                  skipped, since a preview does not need a reproducible
#                  checkout, only a plan.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: build_deployment.sh EXPECTED_HEAD [--activate] [--dry-run]

  EXPECTED_HEAD   Commit this repo's HEAD must match (required).
  --activate      Atomically point deployments_root/current at the new
                  build and print the previous target for rollback.
                  Never restarts any service.
  --dry-run       Print the resolved build plan; no filesystem writes.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
MANIFEST="$REPO_ROOT/deploy/DEPLOYMENT-MANIFEST.toml"

EXPECTED_HEAD=""
ACTIVATE=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --activate)
      ACTIVATE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "ABORT: unknown flag: $1" >&2
      usage
      exit 1
      ;;
    *)
      if [ -n "$EXPECTED_HEAD" ]; then
        echo "ABORT: unexpected extra argument: $1" >&2
        usage
        exit 1
      fi
      EXPECTED_HEAD="$1"
      shift
      ;;
  esac
done

if [ -z "$EXPECTED_HEAD" ]; then
  echo "ABORT: EXPECTED_HEAD is required" >&2
  usage
  exit 1
fi

step() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LEDGER"; }

# --- 0. identity gate -------------------------------------------------------
HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD)"
if [ "$HEAD" != "$EXPECTED_HEAD" ]; then
  echo "ABORT: head $HEAD != expected $EXPECTED_HEAD" >&2
  exit 1
fi

TREE_DIRTY=0
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  TREE_DIRTY=1
fi

if [ "$DRY_RUN" -eq 0 ] && [ "$TREE_DIRTY" -eq 1 ]; then
  echo "ABORT: candidate tree dirty" >&2
  exit 1
fi

[ -f "$MANIFEST" ] || { echo "ABORT: manifest not found: $MANIFEST" >&2; exit 1; }

# --- 1. read the manifest (+ project version) via python3 tomllib ---------
MANIFEST_VARS="$(python3 - "$MANIFEST" "$REPO_ROOT/pyproject.toml" <<'PY'
import shlex
import sys
import tomllib

manifest_path, pyproject_path = sys.argv[1], sys.argv[2]

with open(manifest_path, encoding="utf-8") as f:
    manifest = tomllib.loads(f.read())
with open(pyproject_path, encoding="utf-8") as f:
    pyproject = tomllib.loads(f.read())


def emit(name, value):
    if isinstance(value, bool):
        print(f"{name}={shlex.quote('true' if value else 'false')}")
    elif isinstance(value, list):
        print(f"{name}=({' '.join(shlex.quote(str(v)) for v in value)})")
    else:
        print(f"{name}={shlex.quote(str(value))}")


venv = manifest.get("venv", {})
emit("MANIFEST_EXTRAS", venv.get("extras", []))
emit("MANIFEST_LOCK_ENFORCED", bool(venv.get("lock_enforced", False)))

gates = venv.get("version_gates", {})
emit("MANIFEST_VERSION_GATES", [f"{k}={v}" for k, v in gates.items()])

oob = venv.get("out_of_band", {}).get("packages", [])
emit("MANIFEST_OOB_PACKAGES", oob)

node = manifest.get("node", {})
emit("MANIFEST_NODE_INSTALL", node.get("install", "npm ci --ignore-scripts"))

layout = manifest.get("layout", {})
emit(
    "MANIFEST_DEPLOYMENTS_ROOT",
    layout.get("deployments_root", "~/.local/share/hermes-agent-deployments"),
)
emit("MANIFEST_CURRENT_SYMLINK", layout.get("current_symlink", "current"))

emit("PROJECT_VERSION", pyproject["project"]["version"])
PY
)"
eval "$MANIFEST_VARS"

if [ "$MANIFEST_LOCK_ENFORCED" != "true" ]; then
  echo "ABORT: manifest venv.lock_enforced must be true — this script only ever runs 'uv sync --frozen'" >&2
  exit 1
fi

DEPLOYMENTS_ROOT="${MANIFEST_DEPLOYMENTS_ROOT/#\~/$HOME}"
CURRENT_LINK="$DEPLOYMENTS_ROOT/$MANIFEST_CURRENT_SYMLINK"
LEDGER_DIR="$DEPLOYMENTS_ROOT/.build-ledgers"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$DEPLOYMENTS_ROOT/v${PROJECT_VERSION}-${HEAD:0:12}-$TS"
LEDGER="$LEDGER_DIR/$(basename "$DEST").log"

EXTRA_ARGS=()
for e in "${MANIFEST_EXTRAS[@]}"; do
  EXTRA_ARGS+=(--extra "$e")
done

# --- 2. dry run: print the resolved plan, touch nothing --------------------
if [ "$DRY_RUN" -eq 1 ]; then
  echo "== build_deployment.sh dry run =="
  echo "repo root:          $REPO_ROOT"
  echo "manifest:           $MANIFEST"
  echo "head:               $HEAD"
  if [ "$TREE_DIRTY" -eq 1 ]; then
    echo "tree dirty:         yes (a real build would ABORT until clean)"
  else
    echo "tree dirty:         no"
  fi
  echo
  echo "dest dir:           $DEST"
  echo "ledger:             $LEDGER"
  echo "activate requested: $([ "$ACTIVATE" -eq 1 ] && echo yes || echo no)"
  echo "current symlink:    $CURRENT_LINK"
  echo
  echo "venv extras:        ${MANIFEST_EXTRAS[*]}"
  echo "lock enforced:      $MANIFEST_LOCK_ENFORCED (uv sync --frozen ${EXTRA_ARGS[*]})"
  echo "version gates:      ${MANIFEST_VERSION_GATES[*]:-<none>}"
  echo "out-of-band pkgs:   ${MANIFEST_OOB_PACKAGES[*]:-<none>}"
  echo "node install:       $MANIFEST_NODE_INSTALL"
  echo
  echo "NOTE: --dry-run performed no filesystem writes."
  exit 0
fi

# --- 3. export the committed tree (no .git, no venv, no node_modules) -----
mkdir -p "$DEST" "$LEDGER_DIR"
step "identity OK: $HEAD"

git -C "$REPO_ROOT" archive HEAD | tar -x -C "$DEST"
step "source exported to $DEST"
printf '%s\n' "$HEAD" > "$DEST/.deployed-commit"

# --- 4. python venv — LOCK-ENFORCED (see [venv] lock_enforced above) ------
cd "$DEST"
uv sync --frozen "${EXTRA_ARGS[@]}"
step "uv sync --frozen done (extras: ${MANIFEST_EXTRAS[*]})"

# --- 5. out-of-band installs (fork-operational, not a pyproject extra) ----
if [ "${#MANIFEST_OOB_PACKAGES[@]}" -gt 0 ]; then
  uv pip install -p .venv/bin/python "${MANIFEST_OOB_PACKAGES[@]}"
  step "out-of-band installs done (${MANIFEST_OOB_PACKAGES[*]})"
fi

uv pip check -p .venv/bin/python
step "uv pip check clean"

# --- 6. version-gate assertions --------------------------------------------
for pair in "${MANIFEST_VERSION_GATES[@]:-}"; do
  [ -n "$pair" ] || continue
  pkg="${pair%%=*}"
  want="${pair#*=}"
  got="$(uv pip list -p .venv/bin/python | awk -v p="$pkg" '$1==p{print $2}')"
  if [ "$got" != "$want" ]; then
    step "ABORT: $pkg $got != gate $want"
    exit 1
  fi
  step "version gate OK: $pkg == $got"
done

# --- 7. node runtime deps ---------------------------------------------------
eval "$MANIFEST_NODE_INSTALL" >>"$LEDGER" 2>&1
step "node install done ($MANIFEST_NODE_INSTALL)"

# --- 8. preflight probes (never against a live HERMES_HOME) ---------------
PHH="$(mktemp -d)"
HERMES_HOME="$PHH" "$DEST/.venv/bin/hermes" --version | tee -a "$LEDGER"
HERMES_HOME="$PHH" "$DEST/.venv/bin/hermes" pause --reason preflight >/dev/null
HERMES_HOME="$PHH" "$DEST/.venv/bin/hermes" unpause >/dev/null
rm -rf "$PHH"
step "preflight: version + pause/unpause OK (isolated home)"

step "BUILD COMPLETE: $DEST"
echo "$DEST"

# --- 9. optional activation (symlink swap only — never restarts anything) -
if [ "$ACTIVATE" -eq 1 ]; then
  PREV_TARGET=""
  if [ -L "$CURRENT_LINK" ]; then
    PREV_TARGET="$(readlink "$CURRENT_LINK")"
  fi
  mkdir -p "$DEPLOYMENTS_ROOT"
  ln -sfn "$DEST" "$CURRENT_LINK"
  step "activated: $CURRENT_LINK -> $DEST"
  if [ -n "$PREV_TARGET" ]; then
    echo "previous target (for rollback): $PREV_TARGET"
  else
    echo "previous target (for rollback): (none — no prior symlink)"
  fi
  echo "NOTE: services were NOT restarted — that remains a separate, human-gated step."
fi
