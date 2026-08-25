#!/usr/bin/env bash
# Local run: build, deploy to the scratch bucket, (re)start one celld node
# on 127.0.0.1:$PREP_DEV_PORT (internal listener on PORT+10), wait for /healthz.
#
#   scripts/run-node.sh          build, deploy, start
#   scripts/run-node.sh stop
#
# Env:
#   CELLD_BIN          celld binary                 (default: ~/.local/bin/celld)
#   PREP_RUN_CONFIG    wrangler config to deploy    (default: wrangler.dev.jsonc)
#   PREP_BUILD_ID      build token input            (default: git HEAD)
#   PREP_DEV_PORT      public listener port         (default: 8791)
#   SKIP_BUILD=1       deploy what build/ and dist/ already hold
#   SKIP_DEPLOY=1      restart the node without redeploying
#
# The scratch MinIO is the docker container `celld-scratch-minio` on
# 127.0.0.1:9010 (root user scratchadmin); it is started when exited and the
# bucket is created when missing. State lives under /private/tmp/prep-dev-state.
set -euo pipefail

WORKER="$(cd "$(dirname "$0")/.." && pwd)"
CELLD_BIN="${CELLD_BIN:-$HOME/.local/bin/celld}"
CONFIG="${PREP_RUN_CONFIG:-$WORKER/wrangler.dev.jsonc}"
ENDPOINT="${PREP_DEV_S3_ENDPOINT:-http://127.0.0.1:9010}"
BUCKET="${PREP_DEV_S3_BUCKET:-prep-dev}"
MINIO_CONTAINER="${PREP_DEV_MINIO_CONTAINER:-celld-scratch-minio}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-scratchadmin}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-scratchpass123}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export CELLD_ESBUILD="${CELLD_ESBUILD:-$WORKER/node_modules/.bin/esbuild}"

PORT="${PREP_DEV_PORT:-8791}"
LISTEN="127.0.0.1:${PORT}"
INTERNAL_LISTEN="127.0.0.1:$((PORT + 10))"
BASE_URL="http://${LISTEN}"
STATE_DIR="${PREP_DEV_STATE_DIR:-/private/tmp/prep-dev-state}"
PID_FILE="$STATE_DIR/node.pid"
NODE_LOG="$STATE_DIR/node.log"
mkdir -p "$STATE_DIR" "$STATE_DIR/watch"

# Only the node this script started is ever killed: the port is shared
# territory on a dev box, and a stranger listening there is reported, not
# shot.
stop_node() {
  if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
    sleep 1
  fi
}

require_free_port() {
  holder="$( (lsof -nP -iTCP:${PORT} -sTCP:LISTEN 2>/dev/null || true) | awk 'NR>1 {print $1 " (pid " $2 ")"}' | head -1)"
  if [ -n "$holder" ]; then
    echo "error: 127.0.0.1:${PORT} is held by $holder; set PREP_DEV_PORT to a free port" >&2
    exit 1
  fi
}

if [ "${1:-}" = "stop" ]; then
  stop_node
  echo "==> node stopped"
  exit 0
fi

ensure_minio() {
  if [ "$(docker inspect -f '{{.State.Running}}' "$MINIO_CONTAINER" 2>/dev/null)" != "true" ]; then
    echo "==> starting $MINIO_CONTAINER"
    docker start "$MINIO_CONTAINER" >/dev/null
  fi
  for _ in $(seq 1 30); do
    curl -sf -o /dev/null "$ENDPOINT/minio/health/live" && break
    sleep 1
  done
  docker exec "$MINIO_CONTAINER" sh -c \
    "mc alias set local http://127.0.0.1:9000 '$AWS_ACCESS_KEY_ID' '$AWS_SECRET_ACCESS_KEY' >/dev/null && mc mb --ignore-existing local/$BUCKET" >/dev/null
}

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  echo "==> building"
  (cd "$WORKER" && npm run build)
fi

ensure_minio

if [ "${SKIP_DEPLOY:-0}" != "1" ]; then
  echo "==> deploying $CONFIG to s3://$BUCKET ($ENDPOINT)"
  "$CELLD_BIN" deploy --config "$CONFIG" --bucket "s3://$BUCKET" --endpoint "$ENDPOINT"
fi

stop_node
require_free_port

echo "==> starting celld node on $LISTEN (log: $NODE_LOG)"
CELLD_WATCH="$STATE_DIR/watch" "$CELLD_BIN" \
  --bucket "s3://$BUCKET" \
  --endpoint "$ENDPOINT" \
  --listen "$LISTEN" \
  --internal-listen "$INTERNAL_LISTEN" \
  --advertise "$INTERNAL_LISTEN" \
  >>"$NODE_LOG" 2>&1 &
NODE_PID=$!
echo "$NODE_PID" > "$PID_FILE"

echo "==> waiting for $BASE_URL/healthz"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/healthz" || true)"
  if [ "$code" = "200" ]; then
    echo "==> ready (pid $NODE_PID)"
    exit 0
  fi
  if ! kill -0 "$NODE_PID" 2>/dev/null; then
    echo "error: celld node exited during startup; log tail:" >&2
    tail -n 20 "$NODE_LOG" >&2
    exit 1
  fi
  sleep 1
done
echo "error: timed out waiting for /healthz; log tail:" >&2
tail -n 20 "$NODE_LOG" >&2
exit 1
