#!/usr/bin/env bash
# Phase 0b spike harness, the boardtogether scripts/e2e-worker.sh shape:
# deploy one spike project to the scratch bucket, (re)start a celld node
# on 127.0.0.1:8788 with its internal listener on loopback, wait for
# /healthz, then leave the node running. `run-node.sh stop` kills it.
#
#   run-node.sh <spike-dir> [extra celld node env as KEY=VALUE ...]
#   run-node.sh stop
#
# Env (defaults target the local scratch MinIO container):
#   CELLD_BIN              celld binary            (default: celld on PATH)
#   SPIKE_S3_ENDPOINT      S3-compatible endpoint  (default: http://127.0.0.1:9010)
#   SPIKE_S3_BUCKET        fleet bucket            (default: prep-spikes)
#   AWS_ACCESS_KEY_ID      bucket access key       (default: scratchadmin)
#   AWS_SECRET_ACCESS_KEY  bucket secret key       (default: scratchpass123)
#   AWS_REGION             bucket region           (default: us-east-1)
#   SPIKE_SKIP_DEPLOY=1    restart the node without redeploying
#   SPIKE_PORT             public listener port    (default: 8788)
#   SPIKE_S3_PREFIX        bucket prefix, one fleet per prefix (default: none)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CELLD_BIN="${CELLD_BIN:-celld}"
SPIKE_S3_ENDPOINT="${SPIKE_S3_ENDPOINT:-http://127.0.0.1:9010}"
SPIKE_S3_BUCKET="${SPIKE_S3_BUCKET:-prep-spikes}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-scratchadmin}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-scratchpass123}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

# SPIKE_PORT and SPIKE_S3_PREFIX let two spikes run side by side: a
# bucket prefix is its own fleet, and the internal listener is PORT+10.
PORT="${SPIKE_PORT:-8788}"
LISTEN="127.0.0.1:${PORT}"
INTERNAL_LISTEN="127.0.0.1:$((PORT + 10))"
BASE_URL="http://${LISTEN}"
BUCKET_REF="s3://${SPIKE_S3_BUCKET}${SPIKE_S3_PREFIX:+/$SPIKE_S3_PREFIX}"
STATE_DIR="${SPIKE_STATE_DIR:-/private/tmp/prep-spikes-state/$PORT}"
PID_FILE="$STATE_DIR/node.pid"
NODE_LOG="$STATE_DIR/node.log"
ARGV_FILE="$STATE_DIR/node.argv"
mkdir -p "$STATE_DIR"

stop_node() {
  if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
  leftover="$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN -t 2>/dev/null || true)"
  [ -n "$leftover" ] && kill $leftover 2>/dev/null || true
  sleep 1
}

if [ "${1:-}" = "stop" ]; then
  stop_node
  echo "==> node stopped"
  exit 0
fi

SPIKE="${1:?spike dir}"
shift || true
SPIKE="$(cd "$SPIKE" && pwd)"

if [ "${SPIKE_SKIP_DEPLOY:-0}" != "1" ]; then
  echo "==> deploying $SPIKE to ${BUCKET_REF} (${SPIKE_S3_ENDPOINT})"
  PATH="$HERE/node_modules/.bin:$PATH" "$CELLD_BIN" deploy "$SPIKE" \
    --bucket "$BUCKET_REF" \
    --endpoint "$SPIKE_S3_ENDPOINT"
fi

stop_node

WATCH_DIR="${SPIKE_WATCH_DIR:-$STATE_DIR/watch}"
mkdir -p "$WATCH_DIR"
echo "==> starting celld node on ${LISTEN} (log: ${NODE_LOG})"
# Extra KEY=VALUE args become node process env: that is spike 1's subject.
env "$@" CELLD_WATCH="$WATCH_DIR" "$CELLD_BIN" \
  --bucket "$BUCKET_REF" \
  --endpoint "$SPIKE_S3_ENDPOINT" \
  --listen "$LISTEN" \
  --internal-listen "$INTERNAL_LISTEN" \
  --advertise "$INTERNAL_LISTEN" \
  >>"$NODE_LOG" 2>&1 &
NODE_PID=$!
echo "$NODE_PID" > "$PID_FILE"
printf '%s\n' "$@" > "$ARGV_FILE"

echo "==> waiting for ${BASE_URL}/healthz"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/healthz" || true)"
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
