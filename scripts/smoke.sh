#!/bin/sh
# The deployed public contract. A failure names the check, the URL, the
# status and the start of the body, so a one-off can still be named after
# the fact.
set -eu

base=${1:?usage: scripts/smoke.sh https://host [expected-build-id]}
expected=${2:-}
base=${base%/}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

# fail CHECK URL STATUS BODY-FILE REASON
fail() {
  {
    printf 'smoke FAILED: %s\n' "$1"
    printf '  url:    %s\n' "$2"
    printf '  status: %s\n' "$3"
    printf '  reason: %s\n' "$5"
    if [ -s "$4" ]; then
      printf '  body, first 20 lines:\n'
      head -n 20 "$4" | awk '{ print "    " $0 }'
    else
      printf '  body: empty\n'
    fi
  } >&2
  exit 1
}

# fetch CHECK URL OUT [curl args]: the body lands in OUT; anything but a 200
# fails CHECK, a transport error included (status 000).
fetch() {
  check=$1 url=$2 out=$3
  shift 3
  : >"$out"
  if ! status=$(curl -sS --max-time 15 -o "$out" -w '%{http_code}' "$@" "$url" 2>"$tmp/curl.err"); then
    fail "$check" "$url" "${status:-000}" "$out" "$(tr '\n' ' ' <"$tmp/curl.err")"
  fi
  [ "$status" = 200 ] || fail "$check" "$url" "$status" "$out" "expected 200"
}

fetch healthz "$base/healthz" "$tmp/healthz"
[ "$(cat "$tmp/healthz")" = ok ] || fail healthz "$base/healthz" 200 "$tmp/healthz" "body is not ok"

landing_url="$base/"
if [ -n "$expected" ]; then
  landing_url="$base/?deploy-check=$expected"
fi
fetch landing "$landing_url" "$tmp/landing.html" -H 'Cache-Control: no-cache'
grep -qF '<!doctype html>' "$tmp/landing.html" || fail landing "$landing_url" 200 "$tmp/landing.html" "no <!doctype html>"
grep -qF '<title>' "$tmp/landing.html" || fail landing "$landing_url" 200 "$tmp/landing.html" "no <title>"
if [ -n "$expected" ] && ! grep -qF "$expected" "$tmp/landing.html"; then
  served=$(grep -oE '[0-9a-f]{40}' "$tmp/landing.html" | head -n 1 || true)
  fail landing-build "$landing_url" 200 "$tmp/landing.html" "expected build $expected, page carries ${served:-no build id}"
fi

fetch manifest "$base/manifest.json" "$tmp/manifest.json"
python3 - "$tmp/manifest.json" 2>"$tmp/py.err" <<'PY' || fail manifest "$base/manifest.json" 200 "$tmp/manifest.json" "$(tail -n 1 "$tmp/py.err")"
import json
import sys

with open(sys.argv[1]) as source:
    manifest = json.load(source)
if manifest.get("short_name") != "prep" or manifest.get("start_url") != "/":
    raise SystemExit("manifest contract mismatch")
PY

fetch openapi "$base/openapi.json" "$tmp/openapi.json"
python3 - "$tmp/openapi.json" 2>"$tmp/py.err" <<'PY' || fail openapi "$base/openapi.json" 200 "$tmp/openapi.json" "$(tail -n 1 "$tmp/py.err")"
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
