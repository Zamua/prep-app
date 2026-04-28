#!/usr/bin/env bash
# install-launchd.sh — one-time setup of the artifact-based deploy
# infrastructure for one environment.
#
# Usage:
#   scripts/install-launchd.sh staging
#   scripts/install-launchd.sh prod
#
# What it does (idempotent):
#   1. mkdirs $HOME/Library/prep/{artifacts,current,data/<env>,config,bin}
#      and $HOME/Library/Logs/prep-<env>/.
#   2. copies scripts/run-prep.sh → $HOME/Library/prep/bin/run-prep.sh.
#   3. seeds $HOME/Library/prep/config/<env>.env from
#      config/<env>.env.example if it doesn't exist (you'll need to
#      edit it before first launch).
#   4. expands launchd/com.zamua.prep.plist.template into
#      $HOME/Library/LaunchAgents/com.zamua.prep-<env>.plist.
#   5. bootstraps the plist into the user's launchd domain.
#
# Re-running upgrades the wrapper script + replaces the plist. Doesn't
# touch the env file or any data once they exist.

set -euo pipefail

ENV="${1:?usage: install-launchd.sh <staging|prod>}"
case "$ENV" in
  staging|prod) ;;
  *) echo "ENV must be staging or prod (got: $ENV)" >&2; exit 1 ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$HOME/Library/prep"
LABEL="com.zamua.prep-$ENV"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

echo "==> ensuring runtime tree under $ROOT"
mkdir -p "$ROOT/artifacts" "$ROOT/current" "$ROOT/bin" \
         "$ROOT/data/$ENV" "$ROOT/config" \
         "$HOME/Library/Logs/prep-$ENV"

echo "==> installing run wrapper → $ROOT/bin/run-prep.sh"
install -m 0755 "$REPO_ROOT/scripts/run-prep.sh" "$ROOT/bin/run-prep.sh"

ENVFILE="$ROOT/config/$ENV.env"
if [[ ! -f "$ENVFILE" ]]; then
  echo "==> seeding env file → $ENVFILE (edit before launching!)"
  cp "$REPO_ROOT/config/$ENV.env.example" "$ENVFILE"
else
  echo "==> env file already exists at $ENVFILE — leaving as is."
fi

echo "==> writing launchd plist → $PLIST"
sed -e "s|{{ENV}}|$ENV|g" \
    -e "s|{{HOME}}|$HOME|g" \
    "$REPO_ROOT/launchd/com.zamua.prep.plist.template" > "$PLIST"

# Replace any prior load before bootstrapping the new one.
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "==> bootout existing $LABEL"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
fi

echo "==> bootstrap $LABEL"
launchctl bootstrap "$DOMAIN" "$PLIST"

cat <<EOF

==> done. next steps:
    1. confirm env file:    \$EDITOR $ENVFILE
    2. build an artifact:   scripts/build.sh REF=<sha-or-tag>
    3. promote it:          scripts/promote.sh ENV=$ENV REF=<artifact-id>
    4. tail logs:           tail -F $HOME/Library/Logs/prep-$ENV/stderr.log

    teardown if needed:
       launchctl bootout $DOMAIN/$LABEL && rm "$PLIST"
EOF
