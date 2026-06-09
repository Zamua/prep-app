"""End-to-end browser tests via Playwright.

Why this file exists separately from test_smoke.py / test_ai_flows.py:
those drive the app over httpx, so they can prove the server returns
the right HTML, but they can't prove the page actually works in a
browser. The canonical example of this gap: an importmap-ordering bug
where every server route returns 200 with the expected fragment, but
every page with an inline `<script type="module">` silently dies
because the importmap is reachable too late in the document. httpx
tests stay green; the UI is completely broken.

A real browser test catches that class of bug because it runs the JS,
follows imports, fires htmx polls, and observes DOM swaps. Everything
in this file is a concrete assertion that would have flipped red on a
class of bug we've actually shipped.

These tests are SLOW (~5-30s each) and are gated by both `slow` and
`browser` marks so a fast iteration loop can skip them with
`pytest -m "not browser" tests/e2e`. `make e2e` runs them by default
because `make promote` leans on this gate to keep prod safe.

Run subsets:
    .venv/bin/pytest -m browser tests/e2e        # only browser tests
    .venv/bin/pytest -m "not slow" tests/e2e     # skip browser + AI flows
"""

from __future__ import annotations

import re
import time

import httpx
import pytest

# Apply both marks file-wide. `slow` mirrors the AI-flow tests'
# signaling (they all spend real time), `browser` gates anything that
# needs a Chromium binary.
pytestmark = [pytest.mark.slow, pytest.mark.browser]


# Pages that render at least one `<script type="module">` block —
# either the always-present /static/js/app.js bootstrap (in <head>) or
# an inline-in-body block. Either kind is subject to the same
# importmap-ordering bug class: if the importmap is unreachable when
# the module's `import "@/..."` statements get parsed, the import
# silently fails and every behavior wired up in that block goes dead.
#
# Keep this list synced with the templates that render unique inline
# module logic; pages that ONLY have the bootstrap module are still
# worth testing because they exercise the importmap-in-head invariant
# (a regression that moves the importmap below <head> would break
# bootstrap on every page).
#
# Each entry is (page_id, path_factory(test_deck)→str). The factory
# pattern routes per-deck pages through the existing test_deck fixture
# without hard-coding a deck name.
_INLINE_MODULE_PAGES = [
    # bootstrap-only — still proves the importmap → app.js → modules
    # chain resolves on the most-trafficked surface.
    ("index", lambda d: "/"),
    # inline-in-body: deck.html imports 4 modules from `@/modules/...`
    # via an importmap-bare specifier; the canonical importmap-
    # ordering regression target.
    ("deck", lambda d: f"/deck/{d['name']}"),
    # inline-in-body — notify_settings.html imports notify-settings.js.
    ("notify-settings", lambda d: "/notify"),
    # study.html + session.html only render their inline CodeMirror
    # blocks when a `code`-type question is on screen; the seeded e2e
    # deck has none. Skip them rather than fork the seed fixture — the
    # importmap-in-head guarantee covers the whole document; if the
    # index, deck, and notify pages all parse cleanly, codemirror boot
    # blocks elsewhere are subject to the same global guarantee.
]


def _collect_browser_errors(page) -> tuple[list[str], list[str], list[str]]:
    """Wire console / pageerror listeners on a fresh page and return
    three accumulator lists. Caller navigates after wiring."""
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []

    def _on_console(msg):
        if msg.type == "error":
            console_errors.append(f"{msg.type}: {msg.text}")

    def _on_pageerror(err):
        # Stringify — playwright passes a `Error` proxy.
        page_errors.append(str(err))

    def _on_requestfailed(req):
        # Track failed module fetches specifically (importmap misses
        # come through as failed module requests). Filter out the long
        # tail of font / analytics / etc. failures by limiting to
        # /static/ which is our app's surface.
        if "/static/" in req.url:
            failure = req.failure or "<unknown>"
            failed_requests.append(f"{req.method} {req.url} → {failure}")

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    page.on("requestfailed", _on_requestfailed)
    return console_errors, page_errors, failed_requests


# ---- bug-class 1: inline `<script type="module">` parse-and-execute ----


@pytest.mark.parametrize("page_id,path_fn", _INLINE_MODULE_PAGES)
def test_inline_module_scripts_execute_on_every_page(
    page, base_url: str, test_deck: dict, page_id: str, path_fn
):
    """For every page that has an inline `<script type="module">`, the
    page must load WITHOUT:
      - browser-side console errors
      - uncaught page errors (parse / unresolved-import / runtime)
      - failed /static/ network requests (any module the importmap
        was supposed to resolve)

    This is the test that catches the importmap-ordering regression
    class. The bug pattern: the importmap moves out of <head>, inline
    `import "@/foo.js"` blocks in the page body silently fail at parse
    time, every behavior wired up in those blocks (htmx polling,
    click handlers, etc.) goes dead, but every server route returns
    the same HTML it always had. httpx can't see it; chromium can.
    """
    path = path_fn(test_deck)
    url = f"{base_url}{path}"

    console_errors, page_errors, failed_requests = _collect_browser_errors(page)

    page.goto(url, wait_until="networkidle")
    # Tiny settle: any module that imports + then runs init() on
    # DOMContentLoaded has already executed by `networkidle`, but
    # leave a beat for late console errors (e.g. async htmx wiring).
    page.wait_for_timeout(500)

    # The page must have at least ONE module script — either the
    # bootstrap (in <head>, src=app.js) or an inline-in-body block.
    # Pages with zero module scripts can't trigger the importmap bug
    # class so a parametrize entry for them is wrong.
    module_count = page.evaluate("() => document.querySelectorAll('script[type=module]').length")
    assert module_count >= 1, (
        f"{page_id}: page renders no <script type=module> at all — "
        f"can't exercise importmap resolution. Update _INLINE_MODULE_PAGES."
    )

    # Hard assertions on browser-side health.
    assert not page_errors, (
        f"{page_id}: page raised {len(page_errors)} uncaught error(s):\n  - "
        + "\n  - ".join(page_errors)
    )
    # Console errors include "Failed to resolve module specifier" and
    # "Cannot find module" — the symptoms of a broken importmap.
    importmap_symptoms = [
        m
        for m in console_errors
        if "resolve module" in m.lower()
        or "cannot find module" in m.lower()
        or "failed to load" in m.lower()
        or "specifier" in m.lower()
    ]
    assert not importmap_symptoms, (
        f"{page_id}: importmap-resolution errors in console:\n  - "
        + "\n  - ".join(importmap_symptoms)
    )
    # Any other console errors are also a regression — surface them.
    assert not console_errors, (
        f"{page_id}: {len(console_errors)} console error(s):\n  - " + "\n  - ".join(console_errors)
    )
    # Failed module requests (importmap mis-resolution would show up
    # here as a 404 on the unresolved URL).
    assert not failed_requests, (
        f"{page_id}: {len(failed_requests)} failed /static/ request(s):\n  - "
        + "\n  - ".join(failed_requests)
    )


# ---- bug-class 2: htmx polling actually fires in the browser ----------


def test_transform_polling_fires_in_browser(
    page, http: httpx.Client, base_url: str, test_deck: dict
):
    """Kick off a transform via httpx (faster + more reliable than
    driving the form in the browser), then open the resulting
    `/transform/{wid}` page. Assert:
      - the browser issues at least one GET to `.../fragment` within
        a few seconds (proves htmx polling is actually attached and
        firing)
      - the workflow eventually reaches awaiting_apply
      - the accept/reject buttons appear in the DOM WITHOUT a full
        page navigation (proves the htmx swap landed)

    This test catches the OTHER direction of the importmap-class
    outage: even if a future regression makes all inline modules
    parse OK but htmx itself fails to wire (e.g. wrong attr, racing
    boot order, htmx script 404), this assertion goes red.
    """
    name = test_deck["name"]
    # Same trick the AI-flow tests use — ask for a no-op rephrase so
    # the plan returns quickly.
    r = http.post(
        f"/deck/{name}/transform",
        data={
            "prompt": (
                "rephrase each prompt to be slightly more concise; " "keep the same answer for each"
            )
        },
    )
    assert r.status_code in (200, 303), f"transform start: {r.status_code}"
    loc = r.headers.get("location", "")
    wid = loc.rsplit("/", 1)[-1]
    assert wid.startswith("transform-deck-"), f"unexpected wid: {wid!r}"

    fragment_path_marker = f"/transform/{wid}/fragment"
    initial_url = f"{base_url}/transform/{wid}"

    # Track every GET the browser makes so we can prove the htmx poll
    # actually fired. We can't use page.on("requestfinished") alone
    # because htmx polls inject XHRs, not navigations — those come
    # through as `request` events.
    fragment_requests: list[float] = []
    t_open = time.monotonic()

    def _record(req):
        if fragment_path_marker in req.url and req.method == "GET":
            fragment_requests.append(time.monotonic() - t_open)

    page.on("request", _record)

    page.goto(initial_url, wait_until="domcontentloaded")
    # First poll happens after `every 2s` from when htmx attaches
    # (htmx attaches on DOMContentLoaded). Give it 6s to be safe.
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and not fragment_requests:
        page.wait_for_timeout(250)
    assert fragment_requests, (
        f"htmx polling never fired — no GET to {fragment_path_marker} "
        f"observed within 6s of opening {initial_url}. The page may "
        f"have rendered but htmx didn't attach to the trigger element."
    )

    # Wait for at least one MORE poll so we know the loop is actually a
    # loop (not a one-shot). Up to 5s after the first poll lands.
    second_deadline = time.monotonic() + 5.0
    while time.monotonic() < second_deadline and len(fragment_requests) < 2:
        page.wait_for_timeout(250)
    assert len(fragment_requests) >= 2, (
        f"htmx polled once but didn't continue — only {len(fragment_requests)} "
        f"poll(s) observed. The hx-trigger may be one-shot or the swap "
        f"may have lost the trigger attribute."
    )

    # Capture the URL before the workflow lands so we can prove no
    # navigation happened during the swap.
    pre_swap_url = page.url

    # Wait up to 4 minutes for the transform to reach awaiting_apply
    # and the action UI to swap in. The accept form's action attribute
    # carries the wid, which is the cheapest stable selector.
    apply_form_locator = page.locator(f'form[action*="/transform/{wid}/apply"]')
    apply_form_locator.first.wait_for(state="attached", timeout=240_000)

    # Critical: the URL must NOT have changed. If it did, htmx wasn't
    # swapping — the user got bounced via a server redirect or some
    # script triggered a navigation, both of which break the "live
    # update in place" UX contract.
    assert page.url == pre_swap_url, (
        f"URL changed during swap (pre={pre_swap_url!r}, post={page.url!r}) "
        f"— htmx in-place swap regressed to a navigation."
    )

    # And the reject form should be there too, in the same DOM.
    reject_form = page.locator(f'form[action*="/transform/{wid}/reject"]')
    assert reject_form.count() >= 1, "reject form missing from awaiting_apply DOM"

    # Cleanup: cancel the transform so the deck doesn't accumulate
    # in-flight workflows.
    http.post(f"/transform/{wid}/reject")


# ---- bug-class 3: hx-post in-place swap (no navigation on click) ------


def test_transform_reject_button_swaps_fragment_in_place(
    page, http: httpx.Client, base_url: str, test_deck: dict
):
    """Drive a transform to awaiting_apply, then click Reject in the
    browser. Assert:
      - clicking the button does NOT navigate (URL stable)
      - within 5s the status text transitions to a cancelled-shaped
        state (`rejecting` then `rejected` / `gone`)
      - the accept/reject buttons disappear

    This catches a swap-target regression: if `hx-target` or `hx-swap`
    on the reject form gets mangled, the response either replaces the
    wrong element (visible) or the form does a normal POST→303 (URL
    changes). Either failure mode lights this test up.
    """
    name = test_deck["name"]
    r = http.post(
        f"/deck/{name}/transform",
        data={
            "prompt": (
                "rephrase each prompt to be slightly more concise; " "keep the same answer for each"
            )
        },
    )
    assert r.status_code in (200, 303), f"transform start: {r.status_code}"
    loc = r.headers.get("location", "")
    wid = loc.rsplit("/", 1)[-1]

    page.goto(f"{base_url}/transform/{wid}", wait_until="domcontentloaded")
    # Wait for the awaiting_apply UI.
    apply_form = page.locator(f'form[action*="/transform/{wid}/apply"]').first
    apply_form.wait_for(state="attached", timeout=240_000)

    # Snapshot the URL right before click — must be stable through swap.
    url_before_click = page.url
    reject_button = page.locator(
        f'form[action*="/transform/{wid}/reject"] button[type="submit"]'
    ).first
    reject_button.click()

    # The transient `rejecting` (or `rejected` / `gone` if the
    # workflow already exited) state must show up within 5s. We
    # poll the data-status attribute the fragment renders.
    status_locator = page.locator(".transform-status[data-status]")
    deadline = time.monotonic() + 10.0
    last_status = ""
    while time.monotonic() < deadline:
        try:
            last_status = status_locator.first.get_attribute("data-status") or ""
        except Exception:
            last_status = ""
        if last_status in ("rejecting", "rejected", "gone", "done"):
            break
        page.wait_for_timeout(250)
    assert last_status in ("rejecting", "rejected", "gone", "done"), (
        f"status didn't transition after reject click within 10s — "
        f"last data-status={last_status!r}. The hx-post target may have "
        f"missed."
    )

    # URL must not have changed during the swap.
    assert page.url == url_before_click, (
        f"URL changed during reject swap (pre={url_before_click!r}, "
        f"post={page.url!r}) — reject form fell through to a real POST."
    )

    # Apply form gone (because the awaiting_apply fragment was
    # replaced).
    assert (
        page.locator(f'form[action*="/transform/{wid}/apply"]').count() == 0
    ), "apply form still in DOM after reject swap — wrong element was replaced"


# ---- bug-class 4: HX-Redirect on grading terminal -------------------


def test_grading_redirect_via_hx_redirect_works(
    page, http: httpx.Client, base_url: str, test_deck: dict
):
    """Drive a card answer through claude-grading; the polling endpoint
    sets `HX-Redirect` on terminal so htmx does a full navigation. The
    browser must follow it.

    If a future change breaks the HX-Redirect contract (wrong header
    name, htmx out of date, response shape mismatch), the polling page
    sits spinning forever instead of advancing to the result view.

    Path: POST /study/{deck} (single-card path, no session) on the
    claude-routed long-answer question kicks off a GradeAnswer
    workflow and returns a 303 to /grading/{wid}. We follow that in
    the browser, then watch for the page to advance off the
    .grading-panel polling element — which only happens via the
    HX-Redirect that the fragment endpoint sets on terminal status.
    """
    deck = test_deck["name"]
    qid = test_deck["qids"][3]  # the claude-routed long-answer question

    # Drive the async grading path. Single-card POST /study/{name}
    # kicks off a workflow when agent.is_available; otherwise the
    # route returns a self-grade form (200) and we skip cleanly.
    r = http.post(
        f"/study/{deck}",
        data={
            "question_id": str(qid),
            "type": "short",
            "answer": (
                "It's a mutex that prevents multiple threads from "
                "executing Python bytecode in parallel."
            ),
        },
        timeout=30.0,
        follow_redirects=False,
    )
    if r.status_code != 303:
        pytest.skip(
            f"async grading didn't kick off — /study/{deck} returned "
            f"{r.status_code} (agent likely unavailable on this env; "
            f"route fell through to self-grade). Skipping grading-"
            f"redirect test."
        )
    loc = r.headers.get("location", "")
    m = re.search(r"/grading/([^/?#]+)", loc)
    assert m, f"no /grading/<wid> in redirect: {loc!r}"
    wid = m.group(1)

    grading_url = f"{base_url}/grading/{wid}"
    page.goto(grading_url, wait_until="domcontentloaded")

    # The polling fragment carries .grading-panel — confirm we actually
    # landed on the polling page. (If the workflow already terminated
    # by the time we got here — possible if claude was very fast —
    # /grading/{wid} renders result.html instead, no .grading-panel,
    # which is a legitimate pass.)
    grading_panel = page.locator(".grading-panel")
    if grading_panel.count() == 0:
        # Already terminal on first hit — htmx never had to redirect.
        # The browser-side path we want to test wasn't exercised, so
        # skip rather than declare success.
        pytest.skip("grading workflow terminated before browser opened the polling page")

    # Wait up to 90s for the page to advance off the polling fragment.
    # The HX-Redirect either bounces to /grading/{wid} (rendered as
    # result.html — same URL, different content) or to /session/{sid}.
    # The cleanest signal is .grading-panel disappearing.
    deadline = time.monotonic() + 90.0
    redirected = False
    while time.monotonic() < deadline:
        if page.locator(".grading-panel").count() == 0:
            redirected = True
            break
        page.wait_for_timeout(500)

    assert redirected, (
        f"grading polling page never advanced — .grading-panel still "
        f"present after 90s on {page.url}. HX-Redirect may have failed "
        f"to fire or the browser didn't follow it."
    )
