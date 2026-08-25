#!/usr/bin/env bash
# Kill the 8803 node while a job RPC is in flight, restart it, retry the
# same idempotency key, and report the user's row count.
#   kill-test.sh <key> <hangMs> <hangAfterMs> <killAfterSec>
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$1"; HANG="$2"; HANG_AFTER="$3"; KILL_AFTER="$4"
BASE=http://127.0.0.1:8803
PIDFILE=/private/tmp/prep-spikes-state/8803/node.pid

echo "--- in-flight run $KEY (hangMs=$HANG hangAfterMs=$HANG_AFTER)"
curl -s --max-time 30 "$BASE/job/run?user=u2&key=$KEY&hangMs=$HANG&hangAfterMs=$HANG_AFTER" > /tmp/prep-spikes-inflight-$KEY.out 2>&1 &
CURL=$!
sleep "$KILL_AFTER"
PID="$(cat "$PIDFILE")"
echo "--- kill -9 node $PID at t+${KILL_AFTER}s"
kill -9 "$PID"
wait $CURL; echo "in-flight request result: $(cat /tmp/prep-spikes-inflight-$KEY.out | head -c 300)"
echo "--- restart node"
SPIKE_PORT=8803 SPIKE_S3_PREFIX=idempotent SPIKE_SKIP_DEPLOY=1 "$HERE/run-node.sh" "$HERE/idempotent" 2>&1 | grep -v "WARN celld::memory" | tail -1
echo "--- retry $KEY until the cells are claimable"
for i in $(seq 1 30); do
  out="$(curl -s --max-time 30 "$BASE/job/run?user=u2&key=$KEY")"
  echo "$out" | grep -q '"rows"' && break
  echo "  attempt $i: $out"; sleep 2
done
echo "retry result: $out"
echo "user rows:    $(curl -s "$BASE/user/rows?user=u2")"
echo "job attempts: $(curl -s "$BASE/job/attempts?user=u2")"
