"""Browser side of the offline grader/ladder parity pin.

tests/offline/test_parity_fixtures.py pins the PYTHON domain to the
shared fixture files in tests/offline/fixtures/; this module pins the
JS ports (static/js/offline/grader.js and scheduler.js) to the same
files by importing them into a real Chromium page and running every
case. Together the two suites make it impossible for the online
grader/ladder and their offline ports to drift apart silently: a
semantic change on either side breaks a fixture case loudly.

Cases are dispatched generically ({module, fn, args, expected}), so a
new fixture case needs no test-code change on either side. `expected`
is always the JS-side expectation; the deliberate Python/JS regex
divergences carry `expected_py`, which only the Python suite reads.

The page context comes from the LOCAL offline-suite server (the
/offline shell, whose importmap points at the versioned module URLs);
results come back to Python and are compared here, so a mismatch
reports the exact case id and both values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "offline" / "fixtures"


def _load(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


GRADER_FIXTURE = _load("grader_cases.json")
LADDER_FIXTURE = _load("ladder_cases.json")

# Runs every case against the real modules inside the page. undefined
# is sentinel-ized because evaluate() would serialize it the same as
# null, and the fixtures' null expectations are load-bearing (null is
# the grader's "fall through to self-verdict" signal).
_RUN_CASES_JS = """
async ({prefix, cases}) => {
  const mods = {
    grader: await import(prefix + "offline/grader.js"),
    scheduler: await import(prefix + "offline/scheduler.js"),
  };
  const out = [];
  for (const c of cases) {
    try {
      const value = mods[c.module][c.fn](...c.args);
      out.push({id: c.id, ok: true, value: value === undefined ? "__undefined__" : value});
    } catch (e) {
      out.push({id: c.id, ok: false, error: String(e)});
    }
  }
  return out;
}
"""


def _module_prefix(page) -> str:
    """The versioned "@/" URL prefix from the offline shell's importmap
    (e.g. /static/js/v<token>/). Importing by resolved absolute URL
    keeps the test independent of whether import maps apply inside
    evaluate()'d scripts."""
    prefix = page.evaluate(
        "() => JSON.parse(document.querySelector('script[type=importmap]').textContent)"
        ".imports['@/']"
    )
    assert prefix, "offline shell importmap missing the '@/' entry"
    return prefix


def _run_cases(page, base_url: str, cases: list[dict]) -> list[dict]:
    page.goto(base_url + "/offline")
    prefix = _module_prefix(page)
    results = page.evaluate(_RUN_CASES_JS, {"prefix": prefix, "cases": cases})
    assert len(results) == len(cases)
    return results


def _mismatches(cases: list[dict], results: list[dict]) -> list[str]:
    problems = []
    for case, result in zip(cases, results, strict=False):
        if not result.get("ok"):
            problems.append(f"{case['id']}: threw {result.get('error')}")
        elif result["value"] != case["expected"]:
            problems.append(f"{case['id']}: got {result['value']!r}, expected {case['expected']!r}")
    return problems


def test_grader_cases_in_browser(offline_server, offline_page):
    """Every grader fixture case, run against the real grader.js in
    Chromium. The count floor keeps the pin falsifiable (an empty or
    mis-loaded fixture must not pass vacuously), matching the Python
    suite's floor."""
    cases = GRADER_FIXTURE["cases"]
    assert len(cases) >= 40
    results = _run_cases(offline_page, offline_server.base_url, cases)
    problems = _mismatches(cases, results)
    assert not problems, "grader.js diverged from fixtures:\n" + "\n".join(problems)


def test_ladder_cases_in_browser(offline_server, offline_page):
    """Every ladder fixture case against the real scheduler.js, plus
    the exported table itself: LADDER_MINUTES and TERMINAL_STEP must
    equal the fixture header the Python suite pins to prep/domain/srs
    (the browser side of the same three-way pin)."""
    cases = LADDER_FIXTURE["cases"]
    assert len(cases) >= 25
    offline_page.goto(offline_server.base_url + "/offline")
    prefix = _module_prefix(offline_page)

    exported = offline_page.evaluate(
        """async (prefix) => {
          const m = await import(prefix + "offline/scheduler.js");
          return {ladder: m.LADDER_MINUTES, terminal: m.TERMINAL_STEP};
        }""",
        prefix,
    )
    assert exported["ladder"] == LADDER_FIXTURE["ladder_minutes"]
    assert exported["terminal"] == len(LADDER_FIXTURE["ladder_minutes"]) - 1

    results = offline_page.evaluate(_RUN_CASES_JS, {"prefix": prefix, "cases": cases})
    problems = _mismatches(cases, results)
    assert not problems, "scheduler.js diverged from fixtures:\n" + "\n".join(problems)
