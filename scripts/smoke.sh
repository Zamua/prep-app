#!/bin/sh
set -eu

base=${1:?usage: scripts/smoke.sh https://host [expected-build-id]}
expected=${2:-}
base=${base%/}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

health=$(curl -fsS --max-time 15 "$base/healthz")
if [ "$health" != "ok" ]; then
  echo "healthz returned unexpected body" >&2
  exit 1
fi

landing_url="$base/"
if [ -n "$expected" ]; then
  landing_url="$base/?deploy-check=$expected"
fi
curl -fsS -H 'Cache-Control: no-cache' --max-time 15 "$landing_url" -o "$tmp/landing.html"
grep -F '<!doctype html>' "$tmp/landing.html" >/dev/null
grep -F '<title>' "$tmp/landing.html" >/dev/null
if [ -n "$expected" ]; then
  grep -F "$expected" "$tmp/landing.html" >/dev/null
fi

curl -fsS --max-time 15 "$base/manifest.json" -o "$tmp/manifest.json"
python3 - "$tmp/manifest.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as source:
    manifest = json.load(source)
if manifest.get("short_name") != "prep" or manifest.get("start_url") != "/":
    raise SystemExit("manifest contract mismatch")
PY

curl -fsS --max-time 15 "$base/openapi.json" -o "$tmp/openapi.json"
python3 - "$tmp/openapi.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as source:
    document = json.load(source)
if document.get("openapi") != "3.1.0" or document.get("info", {}).get("title") != "prep":
    raise SystemExit("OpenAPI contract mismatch")
if len(document.get("paths", {})) < 1:
    raise SystemExit("OpenAPI paths missing")
PY

printf 'smoke passed: %s\n' "$base"
