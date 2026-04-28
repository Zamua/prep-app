#!/bin/bash
# deploy.sh — promote a git tag to the current checkout and restart services.
#
# Workflow:
#   • Develop in <deploy-root>/prep-app-staging/ on `main`.
#   • Test on /prep-staging/.
#   • When green: tag from staging, push the tag.
#   • In <deploy-root>/prep-app/: run `./deploy.sh <tag>`.
#
# This script:
#   1. Fetches tags from origin
#   2. Checks out the requested tag (detached HEAD)
#   3. Syncs Python deps via uv (or falls back to pip if uv unavailable)
#   4. Rebuilds the Go worker
#   5. Rebuilds the CodeMirror bundle (if cm/ changed)
#   6. Restarts pm2 services
#   7. Prints the resolved tag for confirmation
#
# Usage:
#   ./deploy.sh v0.2.0          # deploy a specific tag
#   ./deploy.sh                 # bare run = check current state + restart only

set -euo pipefail

TAG="${1:-}"

if [[ -n "$TAG" ]]; then
  echo "==> Fetching tags from origin"
  git fetch --tags --quiet
  echo "==> Checking out $TAG"
  git checkout "$TAG"
fi

echo "==> Syncing Python deps"
if command -v uv >/dev/null 2>&1; then
  uv sync --quiet
elif [[ -f .venv/bin/pip && -f requirements.txt ]]; then
  # Fallback for hosts that haven't installed uv yet (or where the prior
  # deploy left a pip-built venv that we want to keep using).
  .venv/bin/pip install -q -r requirements.txt
else
  echo "ERROR: uv not on PATH and no .venv/bin/pip + requirements.txt to fall back to" >&2
  exit 1
fi

echo "==> Building Go worker"
( cd worker-go && /opt/homebrew/bin/go build -o bin/worker . )

echo "==> Building CodeMirror bundle (if static/cm/ changed)"
if [[ -f static/cm/package.json ]] && command -v /opt/homebrew/bin/bun >/dev/null 2>&1; then
  ( cd static/cm && /opt/homebrew/bin/bun install --silent && /opt/homebrew/bin/bun run build )
fi

# Decide which pm2 services to restart based on which checkout we're in.
# This file lives in prep-app/ for prod and prep-app-staging/ for staging.
DIR_NAME=$(basename "$(pwd)")
if [[ "$DIR_NAME" == "prep-app-staging" ]]; then
  SERVICES="prep-app-staging prep-worker-staging"
else
  SERVICES="prep-app prep-worker"
fi

echo "==> Restarting pm2 services: $SERVICES"
pm2 restart $SERVICES

echo "==> Deployed $(git describe --tags --always)"
