#!/usr/bin/env bash
# promote.sh — point an environment at a built artifact.
#
# Usage:
#   scripts/promote.sh <env> <artifact-id>
#   scripts/promote.sh staging v0.8.5
#   scripts/promote.sh prod    v0.8.5
#
# Effect:
#   1. verifies $HOME/Library/prep/artifacts/<artifact-id>/ exists
#   2. atomically swaps $HOME/Library/prep/current/<env> symlink to it
#   3. launchctl kickstart com.zamua.prep-<env>  (if loaded)
#
# The launchd plist drops out of scope here — install once with
# scripts/install-launchd.sh, then promote.sh just kickstarts it on each
# call.

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <staging|prod> <artifact-id>" >&2
  exit 1
fi
DEPLOY_ENV="$1"
REF="$2"

case "$DEPLOY_ENV" in
  staging|prod) ;;
  *) echo "first arg must be staging or prod (got: $DEPLOY_ENV)" >&2; exit 1 ;;
esac

ROOT="$HOME/Library/prep"
ARTIFACT="$ROOT/artifacts/$REF"
CURRENT_DIR="$ROOT/current"
LINK="$CURRENT_DIR/$DEPLOY_ENV"
LABEL="com.zamua.prep-$DEPLOY_ENV"

if [[ ! -d "$ARTIFACT" ]]; then
  echo "==> no such artifact: $ARTIFACT" >&2
  echo "    build first: scripts/build.sh REF=$REF" >&2
  exit 1
fi

mkdir -p "$CURRENT_DIR"

echo "==> promoting $DEPLOY_ENV → $REF"

# Atomic symlink replace. `ln -sfn` is the canonical incantation:
#   -s symbolic, -f force (remove existing target), -n don't dereference
# the destination if it's a symlink-to-directory. Without -n, mv/ln
# follow the existing symlink into the artifact dir and write the new
# link INSIDE it instead of replacing it. (Hit on the first promote.)
ln -sfn "$ARTIFACT" "$LINK"

echo "    $LINK -> $(readlink "$LINK")"

# Kickstart the launchd plist. -k restarts if running, -p loads then runs.
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "==> launchctl kickstart -k gui/$(id -u)/$LABEL"
else
  echo "==> $LABEL not loaded — run scripts/install-launchd.sh $DEPLOY_ENV first."
fi

echo "==> done."
