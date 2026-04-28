#!/usr/bin/env bash
# promote.sh — point an environment at a built artifact.
#
# Usage:
#   scripts/promote.sh ENV=staging REF=v0.8.5
#   scripts/promote.sh ENV=prod    REF=v0.8.5
#
# Effect:
#   1. verifies $HOME/Library/prep/artifacts/<REF>/ exists
#   2. atomically swaps $HOME/Library/prep/current/<ENV> symlink to it
#   3. launchctl kickstart com.zamua.prep-<ENV>  (if loaded)
#
# The launchd plist drops out of scope here — install once with
# scripts/install-launchd.sh, then promote.sh just kickstarts it on each
# call.

set -euo pipefail

ENV="${ENV:?usage: ENV=staging|prod REF=<artifact-id> $0}"
REF="${REF:?usage: ENV=staging|prod REF=<artifact-id> $0}"

case "$ENV" in
  staging|prod) ;;
  *) echo "ENV must be staging or prod (got: $ENV)" >&2; exit 1 ;;
esac

ROOT="$HOME/Library/prep"
ARTIFACT="$ROOT/artifacts/$REF"
CURRENT_DIR="$ROOT/current"
LINK="$CURRENT_DIR/$ENV"
LABEL="com.zamua.prep-$ENV"

if [[ ! -d "$ARTIFACT" ]]; then
  echo "==> no such artifact: $ARTIFACT" >&2
  echo "    build first: scripts/build.sh REF=$REF" >&2
  exit 1
fi

mkdir -p "$CURRENT_DIR"

echo "==> promoting $ENV → $REF"

# Atomic symlink swap: write into a tmp name then rename. Avoids a
# window where the symlink doesn't exist.
TMP="$(mktemp -du "$CURRENT_DIR/.$ENV.swap.XXXXXX")"
ln -s "$ARTIFACT" "$TMP"
mv -f "$TMP" "$LINK"

echo "    $LINK -> $(readlink "$LINK")"

# Kickstart the launchd plist. -k restarts if running, -p loads then runs.
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "==> launchctl kickstart -k gui/$(id -u)/$LABEL"
else
  echo "==> $LABEL not loaded — run scripts/install-launchd.sh $ENV first."
fi

echo "==> done."
