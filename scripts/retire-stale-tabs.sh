#!/usr/bin/env bash
#
# retire-stale-tabs.sh — cutover helper for the System Health consolidation.
#
# The dashboard's overlapping status/control tabs have been replaced by the
# single System Health tab. This script reversibly retires the three
# superseded dashboard plugins and deletes the stale ruflo-goap-control
# backup directories.
#
# Run this AFTER the System Health PR is merged and the dashboard is rebuilt.
# It is operator-run on purpose: it is NOT executed by the build or the app.
#
# Safety:
#   * It never restarts services and never touches the live dashboard process.
#   * Plugin dirs are MOVED (not deleted) into _retired-20260522/, so the
#     change is fully reversible.
#   * The hidden strike-freedom-cockpit plugin (a theme) is left untouched.
#
set -euo pipefail

PLUGINS_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins"
RETIRED_DIR="$PLUGINS_DIR/_retired-20260522"

# Superseded dashboard tabs — reversibly moved into _retired-20260522/.
RETIRE=(
  hermes-mission-control
  ruflo-goap-control
  understand-anything-dashboard
)

echo "System Health cutover — retiring stale dashboard tabs"
echo "Plugins dir: $PLUGINS_DIR"
echo

if [[ ! -d "$PLUGINS_DIR" ]]; then
  echo "ERROR: plugins directory not found: $PLUGINS_DIR" >&2
  exit 1
fi

mkdir -p "$RETIRED_DIR"

# --- Reversibly retire the superseded plugin tabs ---------------------------
for name in "${RETIRE[@]}"; do
  src="$PLUGINS_DIR/$name"
  dest="$RETIRED_DIR/$name"
  if [[ -d "$src" ]]; then
    if [[ -e "$dest" ]]; then
      echo "SKIP     $name — a copy already exists in _retired-20260522/"
    else
      mv "$src" "$dest"
      echo "RETIRED  $name -> _retired-20260522/$name"
    fi
  elif [[ -d "$dest" ]]; then
    echo "SKIP     $name — already retired"
  else
    echo "SKIP     $name — not installed"
  fi
done

# --- Delete the stale ruflo-goap-control backup dirs (pure cruft) -----------
shopt -s nullglob
baks=("$PLUGINS_DIR"/ruflo-goap-control.bak.*)
shopt -u nullglob

if [[ ${#baks[@]} -eq 0 ]]; then
  echo "SKIP     no ruflo-goap-control.bak.* directories found"
else
  for bak in "${baks[@]}"; do
    rm -rf "$bak"
    echo "DELETED  $(basename "$bak")"
  done
fi

echo
echo "Done. strike-freedom-cockpit (theme) and every other plugin were left untouched."
echo
echo "To reverse the retirement:"
echo "  mv \"$RETIRED_DIR\"/<plugin-name> \"$PLUGINS_DIR\"/"
echo
echo "Then restart the dashboard so the tab list refreshes:"
echo "  systemctl --user restart hermes-dashboard.service"
