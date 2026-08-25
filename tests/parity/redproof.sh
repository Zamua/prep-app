#!/usr/bin/env bash
# The red proof (docs/PARITY-GATE.md section E): each knob must turn
# exactly its own check red. Exits 0 only when every failing set matches.
#
#   PARITY_PERTURB_CSS=1   red: every pixel shot        green: oracles, domdiff
#   PARITY_PERTURB_FSRS=1  red: test_oracles[fsrs]      green: everything else
#   PARITY_PERTURB_DOM=1   red: test_oracles[html]      green: everything else
#
# Usage: tests/parity/redproof.sh   (from the repo root; PARITY_PHASE
# defaults to 1 for the pixel run).
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
OUT="${PARITY_REDPROOF_OUT:-artifacts/parity/redproof}"
mkdir -p "$OUT"
export PARITY_PHASE="${PARITY_PHASE:-1}"

run() {  # name knob -- pytest args...
  local name="$1" knob="$2"; shift 2
  env "$knob=1" "$PY" -m pytest "$@" -q -p no:cacheprovider \
    --junitxml="$OUT/$name.xml" >"$OUT/$name.log" 2>&1 || true
}

run css  PARITY_PERTURB_CSS  tests/parity/test_flows_*.py tests/parity/test_oracles.py tests/parity/test_dom_diff.py
run fsrs PARITY_PERTURB_FSRS tests/parity/test_oracles.py tests/parity/test_dom_diff.py tests/parity/harness
run dom  PARITY_PERTURB_DOM  tests/parity/test_oracles.py tests/parity/test_dom_diff.py tests/parity/harness

"$PY" - "$OUT" <<'EOF'
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

out = Path(sys.argv[1])


def outcomes(name):
    ran, failed = set(), set()
    for case in ET.parse(out / f"{name}.xml").iter("testcase"):
        nodeid = f"{case.get('classname')}::{case.get('name')}"
        if case.find("skipped") is not None:
            continue
        ran.add(nodeid)
        if case.find("failure") is not None or case.find("error") is not None:
            failed.add(nodeid)
    return ran, failed


ok = True
for name, want in (
    ("css", lambda n: "test_flows_" in n),
    ("fsrs", lambda n: n.endswith("test_oracles[fsrs]") or n.endswith("test_oracles::test_oracles[fsrs]")),
    ("dom", lambda n: n.endswith("test_oracles[html]") or n.endswith("test_oracles::test_oracles[html]")),
):
    ran, failed = outcomes(name)
    expected = {n for n in ran if want(n)}
    if not expected:
        print(f"{name}: nothing ran that the knob should redden")
        ok = False
    elif failed != expected:
        print(f"{name}: red set mismatch\n  unexpected: {sorted(failed - expected)}\n  missing:    {sorted(expected - failed)}")
        ok = False
    else:
        print(f"{name}: red exactly {len(expected)} of {len(ran)}")
sys.exit(0 if ok else 1)
EOF
