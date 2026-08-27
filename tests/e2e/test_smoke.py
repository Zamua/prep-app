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
    """Importmap base is /static/js/v<build>/, the build id being the
    commit sha. Every deploy gets a fresh URL space; this catches the
    regression where the version failed to bump and iOS PWA served stale
    modules."""
    home = http.get("/").text
    m = re.search(r"/static/js/v(\w+)/", home)
    assert m, "no versioned import path found in homepage HTML"
    base = f"/static/js/v{m.group(1)}/modules/details-toggle.js"
    r = http.get(base)
    assert r.status_code == 200, f"{base} → {r.status_code}"
    assert r.headers.get("cache-control", "").startswith("public, max-age=315"), (
        f"versioned module should be long-cache + immutable, got {r.headers.get('cache-control')!r}"
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
    expect a "right" verdict via the deterministic path (no AI
    needed). Covers the answer route end-to-end without any AI flake."""
    qid = test_deck["qids"][0]
    r = http.post(f"/trivia/{qid}/answer", data={"answer": "Paris"})
    assert r.status_code == 200, r.status_code
    assert "trivia-result-right" in r.text, "expected right-verdict result block"


def test_ai_grading_round_trip(http: httpx.Client, test_deck: dict):
    """Submit a paraphrased-correct answer to the AI-routed question
    and assert the route returns within a reasonable budget with a
    verdict block. Catches:
    - the threadpool-exhaustion regression that took prod down (sync
      grading in a sync route handler) - async path now yields
      the loop; if a regression makes it block again, this test will
      either fail outright or take 30s+
    - the agent-server "no model module" path, since we run a real
      agent call here
    - the per-call timeout (12s); if the agent is slow or wedged this
      test surfaces it instead of looking like an outage
    """
    qid = test_deck["qids"][3]  # the AI-routed question
    paraphrase = (
        "It's a mutex that prevents multiple threads from executing Python bytecode in parallel."
    )
    # 30s ceiling on the request itself: 12s ai_grade timeout +
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
    assert "trivia-result-right" in r.text or "trivia-result-wrong" in r.text, (
        "no result block - the AI path didn't return a verdict"
    )


def test_ai_regrade_round_trip(http: httpx.Client, test_deck: dict):
    """Re-grade flow: post a wrong answer, then call /regrade with the
    same answer + a defensible reason. Asserts the regrade route
    returns 200 with a feedback block. Covers `trivia_regrade` async
    conversion + the ai_regrade alias - same async-grading regression
    surface as the answer route."""
    qid = test_deck["qids"][3]
    initial = "no idea"
    r = http.post(f"/trivia/{qid}/answer", data={"answer": initial}, timeout=30.0)
    assert r.status_code == 200, r.status_code
    # Now ask the AI to re-grade the same answer. The form is the
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
    assert "re-graded by AI" in r.text or "trivia-regrade-note" in r.text, (
        "regrade did not surface — route may have failed silently"
    )


def test_details_toggle_skips_close_on_action_taps(http: httpx.Client):
    """Static-asset assertion: the details-toggle.js outside-pointerup
    handler must skip closing when the tap is on an actionable element
    (link/button/role=button/submit input). Without that exemption,
    iOS PWA cancels the click navigation when the close shifts layout
    mid-tap. Symptoms: 'tap Next while Explain open does nothing';
    'tap back link while interval popover open does nothing'."""
    home = http.get("/").text
    m = re.search(r"/static/js/v(\w+)/", home)
    assert m, "no versioned import path"
    js = http.get(f"/static/js/v{m.group(1)}/modules/details-toggle.js").text
    # Hard-coded substring assertion — the exemption must be present
    # in the deployed JS. Don't bother parsing; if the literal string
    # disappears, that's the regression.
    assert "input[type='submit']" in js, (
        "details-toggle.js no longer has the action-element exemption — "
        "tapping a link/button while an open <details> popover loses navigation on iOS"
    )


def test_metrics_exposes_the_histogram_families(http: httpx.Client):
    """Prometheus scrape target. Must serve plain-text exposition with
    the prep-specific metrics. Catches a regression that drops the
    /metrics route or breaks the registry serialization.

    The threadpool gauges are deliberately absent: an isolate has no
    threadpool to report on."""
    r = http.get("/metrics")
    assert r.status_code == 200, r.status_code
    assert r.headers.get("content-type", "").startswith("text/plain"), r.headers
    body = r.text
    # Histograms surface their _bucket / _count / _sum families. The
    # ai_grade histogram won't have observations from this test,
    # but the metric registration alone should produce a TYPE line.
    assert "prep_ai_grade_duration_seconds" in body
    assert "prep_http_request_duration_seconds" in body
    assert "prep_anyio_threadpool" not in body


def test_pin_toggle_floats_deck_to_top(http: httpx.Client, test_deck: dict):
    """Toggle pin via POST + assert the dashboard payload floats the
    deck to the top marked pinned. The rows are rendered client-side
    from this payload, so it is what an httpx client can assert on."""
    r = http.post(f"/deck/{test_deck['name']}/pin", data={"pinned": "on"}, follow_redirects=False)
    assert r.status_code in (200, 303), r.status_code
    decks = http.get("/api/dashboard/overview").json()["decks"]
    assert decks, "dashboard overview listed no decks"
    assert decks[0]["slug"] == test_deck["name"], "pinned deck is not first"
    assert decks[0]["pinned"] is True
    # Cleanup: unpin so the next test (and the next run) doesn't see
    # this state. The deck is deleted in teardown anyway, but we keep
    # tests independent.
    http.post(f"/deck/{test_deck['name']}/pin", data={"pinned": "off"})
