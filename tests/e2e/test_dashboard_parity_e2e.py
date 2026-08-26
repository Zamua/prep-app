"""The signed-in dashboard and the offline shell are one screen.

Both surfaces render the deck list from static/js/dashboard/
components.js: the signed-in page over the overview its shell embeds,
the offline shell over the same rows in IndexedDB. This suite drives
both against ONE seeded account and asserts the rows come out
byte-identical once the two documented per-surface seams are taken
off (the deck URL and the server-composed row menu, neither of which
an offline device can answer).

A markup difference here is the dashboard splitting back into two
implementations, which is the thing the shared components exist to
prevent. Failing on the rendered HTML is deliberate: a class renamed,
a wrapper added, or a count computed differently on one side all
surface as a diff.
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.browser]

# The deck list as a surface can honestly be compared: the row menu is
# server markup the offline device has no route for, and the deck link
# points at a page it cannot open. Everything else must match.
_DECK_LIST_JS = """
() => {
  const list = document.querySelector(".deck-list");
  if (!list) return null;
  const clone = list.cloneNode(true);
  clone.querySelectorAll(".row-overflow-menu").forEach((n) => n.remove());
  clone.querySelectorAll("a.deck-link").forEach((a) => a.removeAttribute("href"));
  return clone.outerHTML;
}
"""

_PRELUDE_JS = """
() => {
  const root = document.querySelector(".prelude");
  const status = document.querySelector(".dashboard-status");
  return {
    eyebrow: root.querySelector(".eyebrow").textContent.trim(),
    headline: root.querySelector(".display").textContent.trim(),
    breaks: root.querySelectorAll(".display br").length,
    status: status ? status.textContent.trim() : null,
    wordmark: document.querySelector(".masthead-brand .brand-word").textContent.trim(),
  };
}
"""

# Both readouts abort the upgrade a versionless open triggers: creating the
# database empty at version 1 would make the app skip its own upgrade and
# throw NotFoundError on every later transaction.
_IDB_META_GET_JS = """
async (name) => new Promise((resolve) => {
  const req = indexedDB.open("prep-offline");
  req.onupgradeneeded = () => { req.transaction.abort(); };
  req.onsuccess = () => {
    const db = req.result;
    if (!db.objectStoreNames.contains("meta")) { db.close(); resolve(null); return; }
    const tx = db.transaction("meta", "readonly");
    const get = tx.objectStore("meta").get(name);
    get.onsuccess = () => { db.close(); resolve(get.result ?? null); };
    get.onerror = () => { db.close(); resolve(null); };
  };
  req.onerror = () => resolve(null);
})
"""

_IDB_ALL_JS = """
async (storeName) => new Promise((resolve) => {
  const req = indexedDB.open("prep-offline");
  req.onupgradeneeded = () => { req.transaction.abort(); };
  req.onsuccess = () => {
    const db = req.result;
    if (!db.objectStoreNames.contains(storeName)) { db.close(); resolve([]); return; }
    const tx = db.transaction(storeName, "readonly");
    const all = tx.objectStore(storeName).getAll();
    all.onsuccess = () => { db.close(); resolve(all.result); };
    all.onerror = () => { db.close(); resolve([]); };
  };
  req.onerror = () => resolve([]);
})
"""

# The pin route, driven from inside the page so the context's route
# handler injects the identity header (an APIRequestContext request
# bypasses it and would silently 401).
_PIN_JS = """
async ({base, value}) => {
  const res = await fetch(base + "/deck/offline-e2e/pin", {
    method: "POST",
    body: new URLSearchParams({pinned: value}),
    credentials: "same-origin",
  });
  return res.status;
}
"""


def _wait_for(predicate, timeout: float = 20.0, message: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    pytest.fail(f"timed out waiting for {message}")


def test_both_dashboards_render_the_same_deck_list(offline_server, offline_page):
    offline_server.start()
    page = offline_page
    base = offline_server.base_url

    # The deck is PINNED and holds a SUSPENDED question for this test.
    # Both are facts the shared row states and the offline device holds
    # no card for, so an unpinned row, a different order, or a smaller
    # "in deck" count all surface as a diff below. Without them the
    # comparison passes whether or not either side can answer.
    page.goto(base + "/", wait_until="domcontentloaded")
    assert page.evaluate(_PIN_JS, {"base": base, "value": "on"}) == 200
    try:
        # -- the signed-in dashboard ------------------------------------
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_selector(".decks-section-pinned .deck-card-pinned", timeout=15_000)
        online_list = page.evaluate(_DECK_LIST_JS)
        online_prelude = page.evaluate(_PRELUDE_JS)
        # The row menu is the seam, so it has to actually be there: an
        # online row that lost its menu would make the comparison below
        # pass for the wrong reason.
        assert page.locator(".deck-card .row-overflow-menu").count() == 1
        assert (
            page.locator(".deck-card .deck-link")
            .first.get_attribute("href")
            .endswith("/deck/offline-e2e")
        )
        # 3 studiable cards, 4 questions in the deck: the suspended one
        # counts in the label both surfaces render.
        assert page.locator(".deck-card .stat-due .stat-value").inner_text().strip() == "3"
        assert page.locator(".deck-card .stat-total .stat-value").inner_text().strip() == "4"

        # sync.js writes the snapshot on any authenticated page load;
        # the offline shell has nothing to render until it lands.
        _wait_for(
            lambda: page.evaluate(_IDB_META_GET_JS, "owner"),
            message="snapshot owner in IndexedDB",
        )
        _wait_for(
            lambda: len(page.evaluate(_IDB_ALL_JS, "cards")) == 3,
            message="3 snapshot cards in IndexedDB",
        )
        # The snapshot has to carry the pin, or the offline row below
        # would be rendering an unpinned deck for a reason this suite
        # could not name.
        _wait_for(
            lambda: all(d.get("pinned_at") for d in page.evaluate(_IDB_ALL_JS, "decks")),
            message="pinned_at on the snapshot deck",
        )

        # -- the offline shell ------------------------------------------
        page.goto(base + "/offline", wait_until="domcontentloaded")
        page.wait_for_selector("#offline-root .deck-list .deck-card", timeout=15_000)
        offline_list = page.evaluate(_DECK_LIST_JS)
        offline_prelude = page.evaluate(_PRELUDE_JS)

        assert online_list, "the signed-in dashboard rendered no deck list"
        assert offline_list == online_list
        assert page.locator("#offline-root .decks-section-pinned .deck-card-pinned").count() == 1

        # The masthead and the headline are the same on both; what
        # differs is one status line, which only the offline surface
        # can say, and which leads with the condition.
        assert offline_prelude["wordmark"] == online_prelude["wordmark"]
        assert (
            offline_prelude["headline"]
            == online_prelude["headline"]
            == "A standing libraryof questions."
        )
        assert offline_prelude["breaks"] == online_prelude["breaks"] == 1
        assert offline_prelude["eyebrow"] == online_prelude["eyebrow"] == "The decks"
        assert online_prelude["status"] is None
        assert offline_prelude["status"].startswith("Offline.")
        assert "Studying as Offline Tester" in offline_prelude["status"]

        # Offline has no deck page and no server routes for a row menu,
        # so it renders neither rather than a dead link.
        assert page.locator("#offline-root .row-overflow-menu").count() == 0
        assert page.locator("#offline-root .deck-link").first.get_attribute("href") is None
        # The all-decks study session is this surface's one action.
        assert page.locator("#offline-root .due-strip .btn-primary").inner_text().strip() == "Study"
    finally:
        page.goto(base + "/", wait_until="domcontentloaded")
        assert page.evaluate(_PIN_JS, {"base": base, "value": "off"}) == 200


def test_pin_from_the_row_menu_re_renders_the_list_in_place(offline_server, offline_page):
    """The one dashboard action the client owns end to end. The POST is
    the server's route; what the host owns is re-reading afterwards,
    both the counts and the menu whose Pin row has now flipped. A menu
    that came back stale would offer the action the user just took."""
    offline_server.start()
    page = offline_page
    base = offline_server.base_url
    page.goto(base + "/", wait_until="domcontentloaded")
    page.wait_for_selector(".deck-card .row-overflow-menu")
    try:
        assert page.locator(".decks-section-pinned").count() == 0
        page.locator(".deck-card .row-overflow-trigger").click()
        page.locator(".deck-pin-menu-form button[type=submit]").click()

        page.wait_for_selector(".decks-section-pinned .deck-card-pinned", timeout=10_000)
        # No navigation: the host re-rendered the region in place.
        assert page.url.rstrip("/") == base
        assert page.locator(".deck-card").count() == 1
        assert page.locator(".deck-card .deck-pin-mark").count() == 1
        # The re-read menu offers the opposite action now. text_content,
        # not inner_text: the menu is collapsed behind its <details>.
        form = page.locator(".deck-pin-menu-form")
        assert "Unpin" in form.text_content()
        assert form.locator("input[name=pinned]").get_attribute("value") == "off"
    finally:
        assert page.evaluate(_PIN_JS, {"base": base, "value": "off"}) == 200
