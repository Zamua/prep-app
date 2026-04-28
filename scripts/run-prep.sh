#!/usr/bin/env bash
# run-prep.sh — entry point for the launchd plist. Sources the env file
# and exec's goreman against the active artifact's Procfile.deployed.
#
# Lives in the repo at scripts/run-prep.sh; install-launchd.sh copies it
# into $HOME/Library/prep/bin/run-prep.sh so the running service does
# not depend on the source repo path being on disk.
#
# Usage (called by launchd):
#   run-prep.sh staging
#   run-prep.sh prod

set -euo pipefail

ENV="${1:?usage: run-prep.sh <env>}"
ROOT="$HOME/Library/prep"
ENVFILE="$ROOT/config/$ENV.env"
ACTIVE="$ROOT/current/$ENV"

[[ -f "$ENVFILE" ]] || { echo "missing env file: $ENVFILE" >&2; exit 1; }
[[ -d "$ACTIVE" ]]  || { echo "no active artifact: $ACTIVE — run promote.sh" >&2; exit 1; }
[[ -f "$ACTIVE/Procfile.deployed" ]] || { echo "active artifact missing Procfile.deployed" >&2; exit 1; }

# `set -a` exports every var assigned by the sourced env file so the
# children goreman launches inherit them without per-line `export`.
set -a
# shellcheck disable=SC1090
source "$ENVFILE"
set +a

cd "$ACTIVE"

# goreman is installed by `make setup` into $GOPATH/bin (or $HOME/go/bin
# by default). Try both, plus mise, before giving up.
if command -v goreman >/dev/null 2>&1; then
  exec goreman -f Procfile.deployed start
fi
for c in "$HOME/go/bin/goreman" "/opt/homebrew/bin/goreman"; do
  if [[ -x "$c" ]]; then exec "$c" -f Procfile.deployed start; fi
done
echo "goreman not found in PATH or known fallback locations" >&2
exit 1
