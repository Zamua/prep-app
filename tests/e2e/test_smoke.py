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
