#!/usr/bin/env bash
# build.sh — produce a deployable artifact from a git ref.
#
# Usage:
#   scripts/build.sh REF=v0.8.5
#   scripts/build.sh REF=main
#   scripts/build.sh                 # defaults to current HEAD
#
# Output: $HOME/Library/prep/artifacts/<artifact-id>/, an immutable dir
# containing the source at REF + a built .venv + a built worker binary
# + a built cm-bundle.js. No .git inside.
#
# <artifact-id> = the resolved tag if REF is a tag, otherwise
# main-<short-sha>. Re-running with an existing artifact-id refuses to
# clobber unless FORCE=1 is set.
#
# The pipeline is intentionally hermetic: it `git archive`s the ref
# into a clean temp dir and builds there, so uncommitted changes in the
# working tree never leak into an artifact.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

REF="${REF:-HEAD}"
FORCE="${FORCE:-0}"

# Resolve REF to a stable artifact-id.
SHA="$(git rev-parse --verify "$REF^{commit}")"
SHORT="$(git rev-parse --short=10 "$SHA")"
TAG="$(git describe --tags --exact-match "$SHA" 2>/dev/null || true)"
if [[ -n "$TAG" ]]; then
  ARTIFACT_ID="$TAG"
else
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached")"
  ARTIFACT_ID="${BRANCH}-${SHORT}"
fi

ARTIFACTS_ROOT="$HOME/Library/prep/artifacts"
DEST="$ARTIFACTS_ROOT/$ARTIFACT_ID"
mkdir -p "$ARTIFACTS_ROOT"

if [[ -e "$DEST" && "$FORCE" != "1" ]]; then
  echo "==> artifact already exists: $DEST" >&2
  echo "    pass FORCE=1 to rebuild." >&2
  exit 1
fi

# Build into a temp dir, then atomically move into place. Avoids leaving
# a half-built artifact behind on failure.
STAGE="$(mktemp -d "$ARTIFACTS_ROOT/.build-XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

echo "==> building $ARTIFACT_ID (sha=$SHORT, ref=$REF) → $DEST"

echo "==> [1/4] git archive $REF"
git archive --format=tar "$SHA" | tar -x -C "$STAGE"

echo "==> [2/4] uv sync (python deps + .venv)"
( cd "$STAGE" && mise exec -- uv sync --frozen --quiet )

echo "==> [3/4] go build (worker)"
( cd "$STAGE/worker-go" && mise exec -- go build -o bin/worker . )

echo "==> [4/4] bun build (cm-bundle.js)"
( cd "$STAGE/static/cm" && mise exec -- bun install --silent && mise exec -- bun run build )

# Manifest for traceability — promote.sh and humans alike.
cat > "$STAGE/MANIFEST.json" <<EOF
{
  "artifact_id": "$ARTIFACT_ID",
  "sha": "$SHA",
  "tag": "$TAG",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "built_on": "$(uname -n)"
}
EOF

# Atomically move the staged dir to its final name.
if [[ -e "$DEST" ]]; then rm -rf "$DEST"; fi
mv "$STAGE" "$DEST"
trap - EXIT

echo "==> done: $DEST"
echo "    promote with: scripts/promote.sh ENV=<staging|prod> REF=$ARTIFACT_ID"
