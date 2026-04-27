#!/bin/bash
# deploy.sh — promote a git tag to the current checkout and restart services.
#
# Workflow we use for prep-app:
#   • Develop in ~/Dropbox/workspace/macmini/prep-app-staging/ on `main`.
#   • Test on https://example-host.ts.net/prep-staging/ (port 8082).
#   • When green: tag from staging, push the tag.
#   • In prod (~/Dropbox/workspace/macmini/prep-app/): run `./deploy.sh <tag>`.
#
# This script:
#   1. Fetches tags from origin
#   2. Checks out the requested tag (detached HEAD)
#   3. Reinstalls Python deps (if requirements.txt changed)
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

echo "==> Installing Python deps"
.venv/bin/pip install -q -r requirements.txt

echo "==> Building Go worker"
( cd worker-go && /opt/homebrew/bin/go build -o bin/worker . )

echo "==> Building CodeMirror bundle"
( cd static/cm && /opt/homebrew/bin/bun install --silent && /opt/homebrew/bin/bun run build )

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
