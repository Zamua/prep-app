"""The landing page renders THIS DEVICE'S decks when it holds them.

A visitor the server cannot identify (signed out of the provider, or
the anonymous cookie is gone) whose IndexedDB still holds a snapshot
gets the dashboard, not the splash, and never sees the splash on the
way. IndexedDB answers too late for that call, so the choice is made
before paint from a localStorage flag (static/js/offline/store.js)
read by a classic script in the landing's head.

What each test pins:

- flag plus snapshot: the dashboard, its route into the study loop,
  and the splash never painted. The proof is a per-frame sample of
  the DOM taken from the first frame on, not the end state.
- empty storage: today's landing, with the topic box live and none of
  the host's chain fetched. The same sampler asserts the splash IS
  painted there, so the negative above cannot pass vacuously.
- a flag with nothing behind it: the splash, and the flag cleared.
- a refresh that stores nothing: no flag, because the flag states what
  the stores hold. The same call over a snapshot with cards in it does
  write it.
- a snapshot with no flag (a device from before the flag existed):
  the dashboard mounts and writes the flag, and the document is parsed
  with the splash still in hand.

The server runs in the PUBLIC deploy shape (clerk plus a free tier)
so the splash under test is the instant hero a real visitor gets. The
deploy's inference endpoint is a dead address and the one test that
submits the topic box mocks the generation route, so no test can
reach an upstream.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.e2e.conftest import LocalOfflineServer
from tests.e2e.test_instant_start_e2e import _new_anon_context

pytestmark = [pytest.mark.slow, pytest.mark.browser]

SNAPSHOT_FLAG = "prep:offline_snapshot"
FLAG_ATTR = "data-local-decks"

DECK_LABEL = "World Capitals"
_DECK_ID = 41

BUSY_BODY = {
    "kind": "busy",
    "message": "The free AI is busy right now. Try again in a few minutes.",
}


def _seed_records() -> dict:
    """One snapshot deck and its three due cards, in the shape
    GET /api/offline/snapshot serves (prep/offline/entities.py)."""
    due = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    cards = [
        {
            "question_id": 900 + i,
            "deck_id": _DECK_ID,
            "type": "short",
            "prompt": prompt,
            "choices": None,
            "answer": answer,
            "answer_regex": answer.lower(),
            "step": 0,
            "next_due": due,
            "local_step": None,
            "local_next_due": None,
        }
        for i, (prompt, answer) in enumerate(
            [
                ("Capital of Kenya?", "Nairobi"),
                ("Capital of Peru?", "Lima"),
                ("Capital of Japan?", "Tokyo"),
            ]
        )
    ]
    return {
        "decks": [
            {
                "id": _DECK_ID,
                "name": "world-capitals",
                "display_name": DECK_LABEL,
                "pinned_at": None,
                "total": len(cards),
            }
        ],
        "cards": cards,
        "owner": {
            "user_id": "landing-e2e-owner",
            "display_name": "Landing Tester",
            "snapshot_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "build": None,
        },
    }


# The app's own writer does the seeding: the object stores, the schema
# version and the flag key all stay the module's business, so a test
# cannot pass against a database shape the app does not write.
_SEED_JS = """
async ({prefix, decks, cards, owner, flag}) => {
  const store = await import(prefix + "offline/store.js");
  await store.bulkReplace("decks", decks);
  await store.bulkReplace("cards", cards);
  await store.metaPut("owner", owner);
  if (flag) store.markSnapshotHeld();
  else store.clearSnapshotFlag();
}
"""

_SET_FLAG_JS = """
async (prefix) => {
  const store = await import(prefix + "offline/store.js");
  store.markSnapshotHeld();
}
"""

# Stamp the flag, then run the app's own snapshot refresh over
# whatever the route below is serving, and report the flag either
# side of it.
_REFRESH_JS = """
async (prefix) => {
  const sync = await import(prefix + "offline/sync.js");
  const store = await import(prefix + "offline/store.js");
  store.markSnapshotHeld();
  const before = localStorage.getItem("prep:offline_snapshot");
  const result = await sync.refreshSnapshot({force: true});
  return {before, result, after: localStorage.getItem("prep:offline_snapshot")};
}
"""

# Sampled before EVERY paint, from the first frame on: whether the
# root was already stamped, and what the splash's computed display
# was. "absent" is the window before the body is parsed, which is not
# a painted splash. The end state cannot answer "was it ever shown".
#
# Both clocks, because headless chromium does not guarantee a
# compositor frame before the assertions run: rAF alone leaves the
# list empty on a fast page, and an empty list fails every assertion
# below rather than passing them vacuously.
_FRAME_SAMPLER = """
window.__frames = [];
const sample = () => {
  // The timer can fire before the document has a root element; that
  // window painted nothing, so there is nothing to record.
  const root = document.documentElement;
  if (!root) return;
  const splash = document.querySelector("[data-landing-splash]");
  window.__frames.push({
    stamped: root.hasAttribute("data-local-decks"),
    splash: splash ? getComputedStyle(splash).display : "absent",
  });
};
const onFrame = () => {
  sample();
  requestAnimationFrame(onFrame);
};
requestAnimationFrame(onFrame);
const onTimer = () => {
  sample();
  setTimeout(onTimer, 16);
};
setTimeout(onTimer, 0);
"""

# The state the document is parsed with, for the path decided after
# paint rather than before it. app.js runs as a deferred module, so
# DOMContentLoaded fires with its IndexedDB probe still outstanding:
# this is the moment the splash is the page in hand, and it does not
# depend on when the browser chose to paint.
_AT_PARSE_PROBE = """
document.addEventListener("DOMContentLoaded", () => {
  const splash = document.querySelector("[data-landing-splash]");
  window.__atParse = {
    stamped: document.documentElement.hasAttribute("data-local-decks"),
    splash: splash ? getComputedStyle(splash).display : "absent",
  };
}, {once: true});
"""


@pytest.fixture(scope="session")
def landing_server(tmp_path_factory):
    """A LOCAL prep in the public deploy shape: clerk auth (so an
    unidentified visitor gets the landing and the provider exposes a
    sign-in URL) plus a configured free tier (so the splash is the
    instant hero). The inference address is deliberately dead; no test
    here generates for real."""
    db_path = tmp_path_factory.mktemp("landing-e2e") / "data.sqlite"
    server = LocalOfflineServer(db_path)
    server.extra_env = {
        "PREP_AUTH_MODE": "clerk",
        "CLERK_SECRET_KEY": "sk_test_landing_e2e_dummy",
        "CLERK_AUTHORIZED_PARTIES": server.base_url,
        "CLERK_FRONTEND_API_URL": "https://accounts.example.test",
        "PREP_FREE_INFERENCE_BASE_URL": "http://127.0.0.1:1/v1",
        "PREP_FREE_INFERENCE_API_KEY": "landing-e2e-test-key",
        "PREP_FREE_INFERENCE_MODEL": "landing-e2e-test-model",
    }
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def anon_ctx(browser_session, landing_server):
    """A browser context with no identity of any kind and a no-op
    service worker, so every navigation is a real server render."""
    ctx = _new_anon_context(browser_session)
    try:
        yield ctx
    finally:
        ctx.close()


# ---- helpers -------------------------------------------------------------


def _module_prefix(page) -> str:
    """The versioned '@/' URL prefix from the page's importmap."""
    prefix = page.evaluate(
        "() => JSON.parse(document.querySelector('script[type=importmap]').textContent)"
        ".imports['@/']"
    )
    assert prefix
    return prefix


def _open_landing(page, base: str):
    """First load of the origin: the splash, and the storage context
    the seeding below needs."""
    page.goto(base + "/")
    page.locator("[data-instant-start]").wait_for()
    return page


def _seed_snapshot(page, *, flag: bool) -> None:
    records = _seed_records()
    page.evaluate(_SEED_JS, {"prefix": _module_prefix(page), "flag": flag, **records})


def _flag(page):
    return page.evaluate(f"() => localStorage.getItem({SNAPSHOT_FLAG!r})")


def _frames(page) -> list[dict]:
    """The sampled frames in which the splash element existed."""
    frames = page.evaluate("() => window.__frames")
    assert frames, "the frame sampler never ran"
    present = [f for f in frames if f["splash"] != "absent"]
    assert present, "the splash element never entered the DOM"
    return present


def _splash_display(page) -> str:
    return page.evaluate(
        "() => getComputedStyle(document.querySelector('[data-landing-splash]')).display"
    )


def _page_errors(page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


# ---- the device holds cards ----------------------------------------------


def test_a_device_with_a_snapshot_lands_on_its_decks_with_no_splash(landing_server, anon_ctx):
    base = landing_server.base_url
    landing_server.start()  # idempotent; heals a prior test's failure state
    page = anon_ctx.new_page()
    errors = _page_errors(page)

    _open_landing(page, base)
    _seed_snapshot(page, flag=True)

    page.add_init_script(_FRAME_SAMPLER)
    page.goto(base + "/")

    # The decks, rendered by the shared dashboard components. Deck
    # names are CSS-lowercased on this row, so compare case-blind.
    row = page.locator(".deck-list .deck-card").first
    row.wait_for()
    assert DECK_LABEL.lower() in row.inner_text().lower()
    assert "3" in row.locator(".stat-due .stat-value").inner_text()
    assert "on this device" in page.locator(".dashboard-status").inner_text()

    # The route into the study loop, on every render of this surface:
    # the due-strip button and the empty-state CTA each render under a
    # condition, so neither can be the only way in.
    pill = page.locator(".deck-actions .deck-action-pill")
    assert pill.get_attribute("href").endswith("/offline")

    # The splash was never painted: not in the end state, and not in
    # any frame the browser drew.
    assert _splash_display(page) == "none"
    frames = _frames(page)
    assert [f for f in frames if f["splash"] != "none"] == []
    assert frames[0]["stamped"] is True

    # The one action this surface can offer is the study loop that
    # runs from the same snapshot.
    page.get_by_role("button", name="Study").click()
    page.wait_for_url("**/offline")
    page.wait_for_selector("[data-offline-root] .prelude")

    assert errors == [], errors


# ---- the device holds nothing --------------------------------------------


def test_a_first_time_visitor_gets_the_splash_and_a_live_topic_box(landing_server, anon_ctx):
    base = landing_server.base_url
    landing_server.start()  # idempotent; heals a prior test's failure state
    page = anon_ctx.new_page()
    errors = _page_errors(page)
    posted: list[str] = []

    def _serve_generate(route):
        posted.append(route.request.post_data or "")
        route.fulfill(
            status=429,
            content_type="application/json",
            body=json.dumps(BUSY_BODY),
        )

    page.route("**/api/instant/generate", _serve_generate)

    page.add_init_script(_FRAME_SAMPLER)
    page.goto(base + "/")
    hero = page.locator("[data-instant-start]")
    hero.wait_for()

    # Today's landing: the splash is what paints, nothing is stamped,
    # and the region for the device's decks stays out of the layout.
    assert "What do you want to learn today?" in hero.locator("h1").inner_text()
    assert _splash_display(page) == "block"
    assert page.locator(".landing-local").is_hidden()
    assert page.evaluate(f"() => document.documentElement.hasAttribute({FLAG_ATTR!r})") is False
    assert _flag(page) is None
    # The escape hatch this region replaced is gone.
    assert "Study offline" not in page.locator("body").inner_text()

    # None of it is on this visitor's critical path: the host's chain
    # is neither fetched nor preloaded, which is what the whole
    # attribute-keyed design buys.
    assert (
        page.evaluate(
            "() => performance.getEntriesByType('resource')"
            ".filter((e) => e.name.includes('/dashboard/')).map((e) => e.name)"
        )
        == []
    )
    assert page.locator("link[rel=modulepreload]").count() == 0

    # The sampler CAN see a painted splash, which is what makes the
    # "never painted" assertion above a real result.
    frames = _frames(page)
    assert [f for f in frames if f["splash"] == "block"] != []
    assert [f for f in frames if f["stamped"]] == []

    # The topic box works: enabled by its module, submits once, and
    # the server's answer lands back in the form.
    page.locator(".instant-form textarea").fill("world capitals")
    page.get_by_role("button", name="Generate my deck").click()
    error = page.locator("[data-instant-error]")
    error.wait_for(state="visible")
    assert error.inner_text() == BUSY_BODY["message"]
    assert len(posted) == 1
    assert json.loads(posted[0]) == {"topic": "world capitals"}
    assert page.locator(".instant-form textarea").input_value() == "world capitals"

    assert errors == [], errors


def test_a_flag_with_no_snapshot_behind_it_shows_the_splash_and_clears_itself(
    landing_server, anon_ctx
):
    base = landing_server.base_url
    landing_server.start()  # idempotent; heals a prior test's failure state
    page = anon_ctx.new_page()

    _open_landing(page, base)
    # Site data cleared unevenly, or a wipe that stopped midway: the
    # flag says yes and the stores hold nothing.
    page.evaluate(_SET_FLAG_JS, _module_prefix(page))
    assert _flag(page) == "1"

    page.goto(base + "/")

    # The splash renders (after the host finds nothing), and the flag
    # that lied is gone, so the next load never hides it at all.
    page.locator("[data-landing-splash]").wait_for(state="visible")
    page.wait_for_function(f"() => localStorage.getItem({SNAPSHOT_FLAG!r}) === null")
    assert page.evaluate(f"() => document.documentElement.hasAttribute({FLAG_ATTR!r})") is False
    assert page.locator(".landing-local").is_hidden()
    assert page.locator(".deck-list .deck-card").count() == 0


def test_a_refresh_that_stores_nothing_does_not_claim_this_device_holds_cards(
    landing_server, anon_ctx
):
    """The flag states what the stores hold, not that a sync ran. An
    account with no decks stamping it would hide the splash behind the
    fallback note on this account's own next signed-out visit. The
    non-empty half of the test is what makes the empty half a result:
    the same call, the same route, one different payload."""
    base = landing_server.base_url
    landing_server.start()  # idempotent; heals a prior test's failure state
    payload = {"decks": [], "cards": []}

    def _serve_snapshot(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "user": {"id": "landing-e2e-owner", "display_name": "Landing Tester"},
                    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    **payload,
                }
            ),
        )

    # One page per payload: the page's own app.js runs a refresh on
    # load, so a payload swapped underneath a live page would race it.
    empty_page = anon_ctx.new_page()
    empty_page.route("**/api/offline/snapshot", _serve_snapshot)
    _open_landing(empty_page, base)
    empty = empty_page.evaluate(_REFRESH_JS, _module_prefix(empty_page))
    empty_page.close()
    assert empty["result"]["ok"] is True
    assert empty["before"] == "1"
    assert empty["after"] is None

    records = _seed_records()
    payload = {"decks": records["decks"], "cards": records["cards"]}
    held_page = anon_ctx.new_page()
    held_page.route("**/api/offline/snapshot", _serve_snapshot)
    _open_landing(held_page, base)
    held = held_page.evaluate(_REFRESH_JS, _module_prefix(held_page))
    held_page.close()
    assert held["result"]["ok"] is True
    assert held["after"] == "1"


# ---- the device predates the flag ----------------------------------------


def test_a_snapshot_with_no_flag_mounts_the_dashboard_and_writes_the_flag(landing_server, anon_ctx):
    base = landing_server.base_url
    landing_server.start()  # idempotent; heals a prior test's failure state
    page = anon_ctx.new_page()

    _open_landing(page, base)
    _seed_snapshot(page, flag=False)

    page.add_init_script(_AT_PARSE_PROBE)
    page.goto(base + "/")

    row = page.locator(".deck-list .deck-card").first
    row.wait_for()
    assert DECK_LABEL.lower() in row.inner_text().lower()
    # Written on mount, so this device is decided before paint from
    # now on.
    assert _flag(page) == "1"
    assert page.evaluate(f"() => document.documentElement.hasAttribute({FLAG_ATTR!r})") is True

    # The cost of that one visit, and only for these devices: the
    # decision is made after the document is parsed, not before it, so
    # the splash is the page in hand until IndexedDB answers.
    assert page.evaluate("() => window.__atParse") == {"stamped": False, "splash": "block"}
