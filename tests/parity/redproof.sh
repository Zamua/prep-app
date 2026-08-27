#!/usr/bin/env bash
# The red proof: the knob must turn its own check red. Exits 0 only
# when the failing set matches.
#
#   PARITY_PERTURB_CSS=1   red: every pixel shot
#
# Usage: tests/parity/redproof.sh   (from the repo root; PARITY_PHASE
# defaults to 1 and PARITY_BASE_URL must name a running target). Pixel
# flow files run one per pytest invocation, like tests/e2e.
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
OUT="${PARITY_REDPROOF_OUT:-artifacts/parity/redproof}"
rm -rf "$OUT"
mkdir -p "$OUT"
export PARITY_PHASE="${PARITY_PHASE:-1}"

run() {  # name knob -- pytest args...
  local name="$1" knob="$2"; shift 2
  env "$knob=1" "$PY" -m pytest "$@" -q -p no:cacheprovider \
    --junitxml="$OUT/$name.xml" >"$OUT/$name.log" 2>&1 || true
}

for f in tests/parity/test_flows_*.py; do
  stem="$(basename "$f" .py)"
  run "css-${stem#test_flows_}" PARITY_PERTURB_CSS "$f"
done

"$PY" - "$OUT" <<'PYEOF'
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

out = Path(sys.argv[1])


def outcomes(prefix):
    ran, failed = set(), set()
    for report in sorted(out.glob(f"{prefix}*.xml")):
        for case in ET.parse(report).iter("testcase"):
            nodeid = f"{case.get('classname')}::{case.get('name')}"
            if case.find("skipped") is not None:
                continue
            ran.add(nodeid)
            if case.find("failure") is not None or case.find("error") is not None:
                failed.add(nodeid)
    return ran, failed


ran, failed = outcomes("css")
expected = {n for n in ran if "test_flows_" in n}
if not expected:
    print("css: nothing ran that the knob should redden")
    sys.exit(1)
if failed != expected:
    print(f"css: red set mismatch\n  unexpected: {sorted(failed - expected)}\n  missing:    {sorted(expected - failed)}")
    sys.exit(1)
print(f"css: red exactly {len(expected)} of {len(ran)}")
PYEOF
