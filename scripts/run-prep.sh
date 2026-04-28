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

# mise activation: the artifact ships a .tool-versions file declaring
# pinned python+go+bun (matching the versions used to build it). We need
# those tools on PATH at runtime — the Procfile invokes `.venv/bin/uvicorn`
# which depends on the matching python being resolvable, and goreman
# itself was installed into mise's go bin. `mise exec --` puts everything
# in scope for the exec'd command.
MISE_BIN="$(command -v mise || echo /opt/homebrew/bin/mise)"
if [[ ! -x "$MISE_BIN" ]]; then
  echo "mise not found at $MISE_BIN" >&2
  exit 1
fi
# `-set-ports=false` keeps goreman from clobbering our $PORT (its
# default behavior is to auto-assign 5000+offset per child, which fights
# the env-file value). `-exit-on-error` makes the wrapper bubble up
# child failures so launchd's KeepAlive can restart cleanly.
exec "$MISE_BIN" exec -- goreman -f Procfile.deployed -set-ports=false -exit-on-error start
