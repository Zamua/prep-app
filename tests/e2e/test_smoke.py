"""End-to-end smoke against a deployed prep instance.

Each test asserts a behavior that broke in production at some point:

- Index loads + shows the deck list (covers "is the app even up").
- Static-asset cache-bust resolves (covers the v0.28-era importmap
  miss where iOS PWA served stale JS modules).
- Deck page renders + the type eyebrow shows (covers the demo-D
  layout regression).
- Deterministic answer-grading round-trip (covers the simple route +
  template + queue path).
- Pin toggle + Pinned section appears on the index (covers the
  v0.28.1 pin-doesn't-float-to-top bug).
- Trivia-page imports resolve in the browser (covers the
  ModuleNotFoundError path that crashed staging earlier today).

Run via `make e2e` against staging by default. Override target with
`E2E_BASE_URL=...`.
"""

from __future__ import annotations

import re

import httpx

# ---- HTTP-only smokes (don't need a browser) ---------------------------


def test_index_returns_200(http: httpx.Client):
    r = http.get("/")
    assert r.status_code == 200, r.status_code
    assert "decks" in r.text.lower()


def test_static_css_serves(http: httpx.Client):
    r = http.get("/static/css/index.css")
    assert r.status_code == 200, r.status_code
    assert "@layer" in r.text  # the new ITCSS entry, never the old monolithic one


def test_versioned_module_path_resolves(http: httpx.Client):
    """Importmap base is /static/js/v<build>/. Every deploy gets a
    fresh URL space; this catches the regression where the version
    failed to bump and iOS PWA served stale modules."""
    home = http.get("/").text
    m = re.search(r"/static/js/v(\d+)/", home)
    assert m, "no versioned import path found in homepage HTML"
    base = f"/static/js/v{m.group(1)}/modules/details-toggle.js"
    r = http.get(base)
    assert r.status_code == 200, f"{base} → {r.status_code}"
    assert r.headers.get("cache-control", "").startswith("public, max-age=315"), (
        f"versioned module should be long-cache + immutable, got "
        f"{r.headers.get('cache-control')!r}"
    )


def test_deck_page_renders_with_type_eyebrow(http: httpx.Client, test_deck: dict):
    """Deck-type eyebrow above the title (demo D layout). Hard-codes
    the class so a regression that drops the eyebrow back into the
    pill row is caught."""
    r = http.get(f"/deck/{test_deck['name']}")
    assert r.status_code == 200, r.status_code
    assert "deck-type-eyebrow" in r.text
    # All seeded questions appear on the page.
    for qid in test_deck["qids"]:
        assert f'data-qid="{qid}"' in r.text


def test_deterministic_grading_returns_correct(http: httpx.Client, test_deck: dict):
    """Submit the canonical answer to a seeded short-answer question;
    expect a "right" verdict via the deterministic path (no claude
    needed). Covers the answer route end-to-end without any AI flake."""
    qid = test_deck["qids"][0]
    r = http.post(f"/trivia/{qid}/answer", data={"answer": "Paris"})
    assert r.status_code == 200, r.status_code
    assert "trivia-result-right" in r.text, "expected right-verdict result block"


def test_claude_grading_round_trip(http: httpx.Client, test_deck: dict):
    """Submit a paraphrased-correct answer to the claude-routed question
    and assert the route returns within a reasonable budget with a
    verdict block. Catches:
    - the threadpool-exhaustion regression that took prod down (sync
      claude_grade in a sync route handler) — async path now yields
      the loop; if a regression makes it block again, this test will
      either fail outright or take 30s+
    - the agent-server "no model module" path, since we run a real
      claude call here
    - the per-call timeout (12s); if claude is slow or wedged this
      test surfaces it instead of looking like an outage
    """
    qid = test_deck["qids"][3]  # the claude-routed question
    paraphrase = (
        "It's a mutex that prevents multiple threads from executing " "Python bytecode in parallel."
    )
    # 30s ceiling on the request itself: 12s claude_grade timeout +
    # margin for HTTP + handler. If the route hangs longer than this,
    # we want a hard fail with a clear message, not a silent stall.
    r = http.post(
        f"/trivia/{qid}/answer",
        data={"answer": paraphrase},
        timeout=30.0,
    )
    assert r.status_code == 200, r.status_code
    # Either right or wrong — the regex_update prompt is non-deterministic
    # enough that we don't lock in a specific verdict, just that the
    # round-trip rendered SOMETHING. Right-feedback is more common for
    # this paraphrase; this test mainly proves the path didn't hang.
    assert (
        "trivia-result-right" in r.text or "trivia-result-wrong" in r.text
    ), "no result block — the claude path didn't return a verdict"


def test_claude_regrade_round_trip(http: httpx.Client, test_deck: dict):
    """Re-grade flow: post a wrong answer, then call /regrade with the
    same answer + a defensible reason. Asserts the regrade route
    returns 200 with a feedback block. Covers `trivia_regrade` async
    conversion + the claude_regrade alias — same async-grading regression
    surface as the answer route."""
    qid = test_deck["qids"][3]
    initial = "no idea"
    r = http.post(f"/trivia/{qid}/answer", data={"answer": initial}, timeout=30.0)
    assert r.status_code == 200, r.status_code
    # Now ask claude to re-grade the same answer. The form is the
    # same shape the UI uses (the `Re-grade` button on the result
    # panel POSTs the user's typed answer back).
    r = http.post(
        f"/trivia/{qid}/regrade",
        data={"answer": initial},
        timeout=30.0,
    )
    assert r.status_code == 200, r.status_code
    # Re-graded note is rendered when a regrade hits the route, even
    # if the verdict didn't flip.
    assert (
        "re-graded by claude" in r.text or "trivia-regrade-note" in r.text
    ), "regrade did not surface — route may have failed silently"


def test_pin_toggle_floats_deck_to_top(http: httpx.Client, test_deck: dict):
    """Toggle pin via POST + assert the index renders the deck under
    the "Pinned" section."""
    r = http.post(f"/deck/{test_deck['name']}/pin", data={"pinned": "on"}, follow_redirects=False)
    assert r.status_code in (200, 303), r.status_code
    idx = http.get("/").text
    # Pinned section appears at all (it's omitted when nothing's pinned).
    assert ">Pinned<" in idx, "Pinned section header not found"
    # And our test deck's name appears within or after it. Cheap proxy:
    # the section header position should be earlier than the deck name.
    pinned_at = idx.find(">Pinned<")
    deck_at = idx.find(test_deck["name"])
    assert pinned_at < deck_at, "test deck appears before the Pinned section header"
    # Cleanup: unpin so the next test (and the next run) doesn't see
    # this state. The deck is deleted in teardown anyway, but we keep
    # tests independent.
    http.post(f"/deck/{test_deck['name']}/pin", data={"pinned": "off"})
